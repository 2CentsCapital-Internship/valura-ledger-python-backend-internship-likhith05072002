"""Local chaos generator — the arena, before the arena.

Design decisions:

  * Everything is driven by random.Random(seed): same seed, same feed, same
    corruption, same delivery schedule. A failing run is a repro command,
    not an anecdote.
  * Three separate stages, composable in any order:
        generate  -> a clean, well-formed feed
        corrupt   -> the same feed with malformed events and conflicting
                     duplicates spliced in as the server would send them
        deliver   -> feeds a Book with redeliveries and rewind-replays,
                     returning the submissions a real client would send
    Each stage is pure: it never mutates its input list.
  * Malformed events reuse an IMPLEMENTED type (deposit) with a broken
    payload — a malformed unknown type would just land in `todo` and prove
    nothing. The Book must reject these without mutating and without raising
    out of apply().
  * Wire shape matches PROTOCOL.md exactly:
        {"offset": int, "event_id": "evt_...", "type": str, "payload": {...}}
    Redeliveries and rewinds re-send the stored event verbatim (original
    offset), which is what an SSE resume actually does; the Book keys on
    event_id and must not care.
"""
from __future__ import annotations

import random

# The known-customer universe. Small on purpose: collisions between events
# on the same customer are where ledger bugs live.
CUSTOMERS = [f"CUST-{n}" for n in range(1001, 1051)]

# Types the Book has never heard of. It must count them in `todo`, post
# nothing, and keep going — the arena promises unimplemented types exist.
UNKNOWN_TYPES = ["mystery_event", "audit_ping", "risk_recalc"]

# Roughly the arena's observed ratio of no-leg events in the mix.
UNKNOWN_RATE = 1 / 7


def _amount_2dp(rng: random.Random) -> str:
    """A clean money string, built from integer cents so no float is ever
    involved in producing 'well-formed' test data."""
    cents = rng.randrange(1, 5_000_00)   # $0.01 .. $4,999.99
    return f"{cents // 100}.{cents % 100:02d}"


def _payload_for(t: str, rng: random.Random) -> dict:
    """A plausible payload for type t. Phase 0 knows deposits; anything else
    gets a generic payload — the Book skips unknown types unread anyway."""
    if t == "deposit":
        return {"customer_id": rng.choice(CUSTOMERS),
                "amount": _amount_2dp(rng)}
    return {"customer_id": rng.choice(CUSTOMERS), "note": f"unhandled:{t}"}


def generate(seed: int, n: int, types: list[str] | None = None) -> list[dict]:
    """n well-formed events, deterministic in seed.

    With types=None (Phase 0 default): deposits, with unknown types
    sprinkled in at roughly the arena's ratio. With an explicit types list:
    uniform draw from it (unknown sprinkling off — the caller chose the mix).
    Offsets run 0..n-1; event ids are evt_<seed>_<i> so two feeds with
    different seeds can never collide on an id.
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        if types is None:
            t = (rng.choice(UNKNOWN_TYPES) if rng.random() < UNKNOWN_RATE
                 else "deposit")
        else:
            t = rng.choice(types)
        out.append({"offset": i,
                    "event_id": f"evt_{seed}_{i}",
                    "type": t,
                    "payload": _payload_for(t, rng)})
    return out


# ------------------------------------------------------------------ #
#  corruption                                                        #
# ------------------------------------------------------------------ #

MALFORMABLE = ("deposit", "fee_charged", "withdrawal_requested")


def _malformed(base: dict, kind: int, rng: random.Random, eid: str) -> dict:
    """One broken cash event. Five defect classes, chosen by `kind`:

      0  missing amount            -> Rejected (validation) / KeyError
      1  non-numeric amount        -> InvalidOperation (an ArithmeticError)
      2  wrong-type payload        -> TypeError on payload["..."]
      3  amount as a raw float with binary-junk decimals (0.1 + 0.2 =
         0.30000000000000004) — this one is LEGAL input: money() goes
         through str() and must book exactly 0.30, not reject it.
      4  negative amount           -> Rejected (S10: sign-flip is bad data)

    The broken event reuses the base event's type when that type takes an
    `amount` (so Phase 1 handlers face their own garbage), falling back to
    deposit otherwise.
    """
    t = base["type"] if base["type"] in MALFORMABLE else "deposit"
    p = base["payload"] if isinstance(base["payload"], dict) else {}
    cid = p.get("customer_id", rng.choice(CUSTOMERS))
    payload: object
    if kind == 0:
        payload = {"customer_id": cid}
    elif kind == 1:
        payload = {"customer_id": cid, "amount": "twelve dollars"}
    elif kind == 2:
        payload = "this is not a payload object"
    elif kind == 3:
        payload = {"customer_id": cid,
                   "amount": rng.randrange(1, 10_000) / 10 + 0.2}
    else:
        payload = {"customer_id": cid,
                   "amount": f"-{_amount_2dp(rng)}"}
    if t == "withdrawal_requested" and isinstance(payload, dict):
        payload["withdrawal_id"] = f"wd_bad_{eid}"
    return {"offset": -1, "event_id": eid, "type": t, "payload": payload}


def corrupt(events: list[dict], seed: int,
            malformed_rate: float = 0.03,
            conflict_dup_rate: float = 0.01) -> list[dict]:
    """The clean feed with garbage spliced in, offsets renumbered.

    Two injection kinds, both as NEW stream positions (the server never
    edits an event in place, it sends more events):

      * malformed events — fresh ids evt_<seed>_bad_<k>, cycling through
        the four defect classes above;
      * conflicting duplicates — the SAME event_id as an earlier deposit
        but a different amount. First delivery wins forever: the Book must
        return [] and keep the original booking untouched.
    """
    rng = random.Random(seed ^ 0xC0FFEE)   # decorrelate from generate(seed)
    out: list[dict] = []
    bad = 0
    for ev in events:
        out.append(dict(ev))               # never mutate the caller's list
        if rng.random() < malformed_rate:
            out.append(_malformed(ev, bad % 5, rng, f"evt_{seed}_bad_{bad}"))
            bad += 1
        if rng.random() < conflict_dup_rate:
            donors = [e for e in out
                      if isinstance(e["payload"], dict)
                      and "amount" in e["payload"]]
            if donors:
                d = rng.choice(donors)
                out.append({"offset": -1,
                            "event_id": d["event_id"],   # the conflict
                            "type": d["type"],
                            "payload": {**d["payload"],
                                        "amount": _amount_2dp(rng)}})
    for i, ev in enumerate(out):           # stream positions are sequential
        ev["offset"] = i
    return out


# ------------------------------------------------------------------ #
#  Phase 1: coherent cash chaos                                      #
# ------------------------------------------------------------------ #

def generate_cash(seed: int, n: int) -> list[dict]:
    """n cash events with real cross-event structure, deterministic in seed.

    Coherence is the point: refunds reference fees this feed actually
    charged, settlements reference withdrawals it actually requested — and
    every documented trap is woven in at a steady rate:

      * refund-before-fee (the fee arrives a few events later; the refund
        must stay rejected forever), unknown-source refunds, double refunds;
      * withdrawal races: settle/reject of unknown wids, double settles,
        settle-then-reject and reject-then-settle, duplicate wid requests;
      * interest with share == gross (zero 4200 leg), share == 0 (zero 2010
        leg), and share > gross (bad data, reject);
      * fx with real spread, exactly zero spread (posts), customer better
        by exactly one cent (rejects), and raw-rate fields whose ordering
        contradicts the USD amounts (the book must never read raw rates);
      * transfers including from == to, plus one recipient-only customer
        whose whole existence is a single incoming credit (R18);
      * unknown event types at roughly the arena's 1-in-7 ratio.
    """
    rng = random.Random(seed ^ 0xCA51)     # decorrelate from the other stages
    out: list[dict] = []
    fees_open: list[str] = []       # charged, not yet refunded
    fees_done: list[str] = []       # already refunded once
    wids_open: list[str] = []
    wids_closed: list[str] = []
    pending: list[tuple[int, dict]] = []   # (emit_at, event) for late fees
    k = 0

    def eid() -> str:
        nonlocal k
        k += 1
        return f"evt_{seed}_c{k}"

    def emit(t: str, payload: dict) -> None:
        out.append({"offset": len(out), "event_id": eid(),
                    "type": t, "payload": payload})

    recv_only_done = False
    while len(out) < n:
        for due, ev in [pe for pe in pending if pe[0] <= len(out)]:
            out.append(ev)
            pending.remove((due, ev))
        r = rng.random()
        cid = rng.choice(CUSTOMERS)
        if r < 1 / 7:
            emit(rng.choice(UNKNOWN_TYPES),
                 {"customer_id": cid, "note": "no-op"})
        elif r < 0.32:
            emit("deposit", {"customer_id": cid, "amount": _amount_2dp(rng)})
        elif r < 0.44:
            emit("fee_charged", {"customer_id": cid,
                                 "amount": _amount_2dp(rng)})
            fees_open.append(out[-1]["event_id"])
        elif r < 0.52:
            roll = rng.random()
            if roll < 0.60 and fees_open:            # valid refund
                src = fees_open.pop(rng.randrange(len(fees_open)))
                fees_done.append(src)
                emit("fee_refund", {"refunds_source_id": src,
                                    "customer_id": cid})
            elif roll < 0.72 and fees_done:          # double refund -> reject
                emit("fee_refund",
                     {"refunds_source_id": rng.choice(fees_done),
                      "customer_id": cid})
            elif roll < 0.86:                        # refund-before-fee (R11)
                fee_id = eid()
                emit("fee_refund", {"refunds_source_id": fee_id,
                                    "customer_id": cid})
                pending.append((len(out) + rng.randrange(2, 8),
                                {"offset": -1, "event_id": fee_id,
                                 "type": "fee_charged",
                                 "payload": {"customer_id": cid,
                                             "amount": _amount_2dp(rng)}}))
            else:                                    # unknown source -> reject
                emit("fee_refund",
                     {"refunds_source_id": f"evt_{seed}_never_{k}",
                      "customer_id": cid})
        elif r < 0.60:
            gross_c = rng.randrange(2, 100_00)
            roll = rng.random()
            if roll < 0.70:
                share_c = rng.randrange(0, gross_c)
            elif roll < 0.80:
                share_c = gross_c                    # R16: no 4200 leg
            elif roll < 0.90:
                share_c = 0                          # no 2010 leg
            else:
                share_c = gross_c + rng.randrange(1, 100)   # reject
            emit("interest_credited",
                 {"customer_id": cid,
                  "gross_amount": f"{gross_c // 100}.{gross_c % 100:02d}",
                  "customer_share": f"{share_c // 100}.{share_c % 100:02d}"})
        elif r < 0.70:
            if not recv_only_done:                   # R18, exactly once
                dst, recv_only_done = f"CUST-RECV-{seed}", True
            elif rng.random() < 0.05:
                dst = cid                            # R17: from == to
            else:
                dst = rng.choice(CUSTOMERS)
            emit("transfer_between_customers",
                 {"from_customer_id": cid, "to_customer_id": dst,
                  "amount": _amount_2dp(rng)})
        elif r < 0.80:
            m_c = rng.randrange(100, 500_000)
            roll = rng.random()
            if roll < 0.70:
                c_c = m_c - rng.randrange(1, min(m_c, 500))  # real spread
            elif roll < 0.85:
                c_c = m_c                            # R10: zero spread, posts
            else:
                c_c = m_c + 1                        # better by 1 cent: reject
            # Raw-rate fields deliberately contradict the USD ordering at
            # random: the book must compare USD amounts only.
            lie = rng.random() < 0.5
            emit("fx_deposit",
                 {"customer_id": cid, "amount_foreign": _amount_2dp(rng),
                  "currency": rng.choice(["EUR", "GBP", "JPY"]),
                  "market_rate": "1.10" if lie else "0.90",
                  "customer_rate": "1.20" if lie else "0.80",
                  "usd_at_market_rate": f"{m_c // 100}.{m_c % 100:02d}",
                  "usd_at_customer_rate": f"{c_c // 100}.{c_c % 100:02d}"})
        elif r < 0.90:
            if rng.random() < 0.06 and wids_open + wids_closed:  # dup wid
                wid = rng.choice(wids_open + wids_closed)
            else:
                wid = f"wd_{seed}_{k}"
            emit("withdrawal_requested",
                 {"withdrawal_id": wid, "customer_id": cid,
                  "amount": _amount_2dp(rng)})
            if wid not in wids_open and wid not in wids_closed:
                wids_open.append(wid)
        else:
            roll = rng.random()
            if roll < 0.70 and wids_open:            # valid close
                wid = wids_open.pop(rng.randrange(len(wids_open)))
                wids_closed.append(wid)
                emit(rng.choice(["withdrawal_settled",
                                 "withdrawal_rejected"]),
                     {"withdrawal_id": wid})
            elif roll < 0.85 and wids_closed:        # race: already closed
                emit(rng.choice(["withdrawal_settled",
                                 "withdrawal_rejected"]),
                     {"withdrawal_id": rng.choice(wids_closed)})
            else:                                    # unknown wid
                emit(rng.choice(["withdrawal_settled",
                                 "withdrawal_rejected"]),
                     {"withdrawal_id": f"wd_{seed}_never_{k}"})
    for i, ev in enumerate(out):
        ev["offset"] = i
    return out


# ------------------------------------------------------------------ #
#  Phase 3: coherent market chaos                                    #
# ------------------------------------------------------------------ #

# Twelve symbols across the three asset classes, the SAME map for every
# seed: routing and broker-coverage decisions must be stable across feeds
# or a replayed run would disagree with itself.
SYMBOL_CLASS: dict[str, str] = {
    "ACME": "equity", "BLUTH": "equity", "HOOLI": "equity", "INITECH": "equity",
    "GLOB-E": "etf", "ISHR-Q": "etf", "SPDR-X": "etf", "VANG-T": "etf",
    "CORP-AA": "bond", "GILT-10": "bond", "MUNI-CA": "bond", "T-2044": "bond",
}
SYMBOLS = sorted(SYMBOL_CLASS)

# Broker coverage per class — restated from the task sheet (mirrors
# tariff.COVERAGE) so this module keeps importing nothing but `random`.
CLASS_BROKERS = {"equity": ("BRK-A", "BRK-B"),
                 "etf": ("BRK-A", "BRK-C"),
                 "bond": ("BRK-B", "BRK-C")}

# Principal (in cents) below which each broker's min fee beats its bps
# line: min_fee / brokerage_bps. The sub-min-fee trap aims under these.
MIN_FEE_XOVER_CENTS = {"BRK-A": 50_000,    # 1.00 / 20 bps  = $500.00
                       "BRK-B": 166_600,   # 2.50 / 15 bps  = $1666.67
                       "BRK-C": 20_000}    # 0.50 / 25 bps  = $200.00

PARTNER_RATES = ("0", "0.25", "0.5", "0.75")

# Trap dial board, all ON by default. Per-lifecycle / per-fill dials are
# probabilities at that decision point; the last three are per main-loop
# draw (standalone events). Set a dial to 0 to switch that trap off.
MARKET_TRAPS = {
    "fill_before_placement": 0.03,   # per lifecycle: placement 2..8 later
    "cancel_before_placement": 0.02, # per lifecycle: closed tombstone (S5)
    "fill_after_cancel": 0.04,       # per cancelled/rejected lifecycle (S7)
    "fill_after_close": 0.04,        # per filled BUY lifecycle
    "overfill": 0.03,                # per filled BUY lifecycle w/ partials
    "settle_before_fill": 0.03,      # per fill: settle 1..5 earlier (S6)
    "double_settle": 0.02,           # per settled fill
    "sub_min_fee": 0.04,             # per fill: principal under crossover
    "dup_trade_id": 0.003,           # per BUY fill (~0.3%, D8)
    "oversell": 0.02,                # standalone: sell qty > position (L1)
    "unknown_oid_cancel": 0.02,      # standalone: cancel a ghost order id
    "malformed": 0.02,               # standalone: broken order payloads
}

# Observability side-channel: generate_market() rewrites this dict on every
# call with trap-fire counts and totals for the run it just produced. It is
# derived output, never an input — generation stays deterministic in
# (seed, n, settle_all). generate_market_stats() is the pure-looking wrap.
LAST_STATS: dict = {}


def _cents_str(c: int) -> str:
    return f"{c // 100}.{c % 100:02d}"


def _qty_str(micro: int) -> str:
    """Canonical share-quantity string from integer millionths: '8', not
    '8.000000'; '0.5', not '0.500000'."""
    whole, frac = divmod(micro, 1_000_000)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:06d}".rstrip("0")


def generate_market(seed: int, n: int, settle_all: bool = True) -> list[dict]:
    """~n coherent full-market events, deterministic in (seed, n, settle_all).

    The mix: ~15% deposits (front-loaded so wallets exist before orders),
    ~10% other Phase 1 cash, ~1/7 unknown types, the rest full order
    lifecycles — order_placed (12 symbols over 3 classes, fractional 6dp
    quantities occasionally) -> 0..4 order_partially_filled ->
    order_filled | order_cancelled | order_rejected, every fill priced
    ~limit ±2% with its OWN broker (valid for the class, varying across
    fills of one order) and partner_rate, its trade_settled trailing 3..40
    positions behind — plus every MARKET_TRAPS defect at a steady rate.

    The generator mirrors the Book's acceptance rules as it emits (position
    per (customer, symbol), posted trades, settled flags), so it always
    KNOWS which fills posted. Sells are only scheduled against position the
    feed has actually delivered — reserved at schedule time — so the only
    oversells are the deliberate ones. With settle_all=True a tail of
    trade_settled events drains every posted-and-unsettled trade to zero;
    the only 2350/1150 residue left is from duplicate-trade_id fills
    (quarantined by the Book, tallied here in dup_stuck_2350_cents).

    Offsets run 0..len-1; ids are evt_/ord_/trd_<seed>_m<k>, disjoint from
    every other generator in this module. Trap-fire counts land in
    LAST_STATS (see generate_market_stats).
    """
    rng = random.Random(seed ^ 0xA43CE7)   # decorrelate from other stages
    tr = MARKET_TRAPS
    out: list[dict] = []
    pending: list[tuple] = []              # (emit_at, event, meta)
    stats = {k: 0 for k in (
        "deposits", "cash_misc", "unknown", "placements", "fills_emitted",
        "fills_posted", "settles_emitted", "settles_posted",
        "settles_rejected", "settle_all_emitted", "fill_before_placement",
        "cancel_before_placement", "fill_after_cancel", "fill_after_close",
        "overfill", "settle_before_fill", "double_settle", "sub_min_fee",
        "dup_trade_id", "oversell", "oversell_accidental",
        "unknown_oid_cancel", "malformed", "dup_stuck_2350_cents")}
    # The generator's mirror of the Book: Σ lot qty per (cid, symbol) in
    # integer millionths, what is still reservable for future sells, and
    # every trade the Book stored -> whether a settle has posted for it.
    book_pos: dict[tuple[str, str], int] = {}
    avail: dict[tuple[str, str], int] = {}
    trades: dict[str, bool] = {}
    seqs = {"e": 0, "o": 0, "t": 0}
    bad_kind = 0

    def _id(kind: str, prefix: str) -> str:
        seqs[kind] += 1
        return f"{prefix}_{seed}_m{seqs[kind]}"

    def mkev(t: str, payload) -> dict:
        return {"offset": -1, "event_id": _id("e", "evt"),
                "type": t, "payload": payload}

    def track(meta) -> None:
        """Apply one emitted event's effect to the mirror — the same
        accept/reject decision the Book will make, made at emission time so
        scheduling races can never desynchronize the two."""
        if meta is None:
            return
        if meta[0] == "fill":
            _, side, cid, symbol, q, t_id, dup, expect_reject, p_c = meta
            key = (cid, symbol)
            stats["fills_emitted"] += 1
            if side == "sell" and q > book_pos.get(key, 0):
                # the Book rejects before any lot mutation (L1)
                stats["oversell" if expect_reject
                      else "oversell_accidental"] += 1
                return
            if side == "sell":
                book_pos[key] -= q
            else:
                book_pos[key] = book_pos.get(key, 0) + q
                avail[key] = avail.get(key, 0) + q
            stats["fills_posted"] += 1
            if dup:
                # posts legs, quarantined, but stores NO trade (D8): its
                # 2350 credit can never drain — tallied for the gate.
                stats["dup_trade_id"] += 1
                stats["dup_stuck_2350_cents"] += p_c
            else:
                trades[t_id] = False
        else:                              # ("settle", trade_id, kind)
            _, t_id, _kind = meta
            stats["settles_emitted"] += 1
            if trades.get(t_id) is False:  # known and not yet settled
                trades[t_id] = True
                stats["settles_posted"] += 1
            else:                          # early / double / never-posted
                stats["settles_rejected"] += 1

    def emit(ev: dict, meta=None) -> None:
        out.append(ev)
        track(meta)

    def _split(total: int, parts: int) -> list[int]:
        """total micro-shares into `parts` positive chunks (fewer when the
        total is too small to split)."""
        if parts <= 1 or total <= parts:
            return [total]
        cuts = sorted(rng.sample(range(1, total), parts - 1))
        return [b - a for a, b in zip([0] + cuts, cuts + [total])]

    def _placement(o_id, cid, side, symbol, q, limit_c) -> dict:
        p = {"order_id": o_id, "customer_id": cid, "side": side,
             "symbol": symbol, "quantity": _qty_str(q),
             "limit_price": _cents_str(limit_c),
             "asset_class": SYMBOL_CLASS[symbol]}
        # one in ten uses the stale kit's field name (A11 fallback coverage)
        field = "est_commission" if rng.random() < 0.10 else "est_charges"
        p[field] = _cents_str(rng.randrange(0, 2_001))
        return p

    def build_fill(o_id, cid, side, symbol, cls, limit_c, q, final):
        """One fill event + its mirror meta. Broker, price and partner_rate
        are the FILL's own (varying within the class across fills of one
        order); the sub-min-fee and duplicate-trade_id traps live here."""
        broker = rng.choice(CLASS_BROKERS[cls])
        price_c = max(1, limit_c * rng.randrange(9_800, 10_201) // 10_000)
        if rng.random() < tr["sub_min_fee"]:
            cap_q = max(1, rng.randrange(100, MIN_FEE_XOVER_CENTS[broker])
                        * 1_000_000 // price_c)
            if cap_q < q:                  # shrink under the min-fee floor
                q = cap_q
                stats["sub_min_fee"] += 1
        p_c = max(1, (q * price_c + 500_000) // 1_000_000)
        dup = (side == "buy" and bool(trades)
               and rng.random() < tr["dup_trade_id"])
        t_id = rng.choice(list(trades)) if dup else _id("t", "trd")
        payload = {"order_id": o_id, "trade_id": t_id, "customer_id": cid,
                   "side": side, "symbol": symbol, "quantity": _qty_str(q),
                   "price": _cents_str(price_c),
                   "principal": _cents_str(p_c), "broker": broker,
                   "asset_class": cls,
                   "partner_rate": rng.choice(PARTNER_RATES)}
        ev = mkev("order_filled" if final else "order_partially_filled",
                  payload)
        return ev, ("fill", side, cid, symbol, q, t_id, dup, False, p_c), \
            t_id, dup

    def lifecycle(now: int) -> None:
        """Emit the first event of one order lifecycle and schedule the
        rest — fills, settles, finals, and any armed traps — as pending."""
        if rng.random() < tr["cancel_before_placement"]:      # S5 shape
            stats["cancel_before_placement"] += 1
            stats["placements"] += 1
            o_id = _id("o", "ord")
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            emit(mkev("order_cancelled", {"order_id": o_id}))
            pending.append((now + rng.randrange(2, 9),
                            mkev("order_placed",
                                 _placement(o_id, cid, "buy", symbol,
                                            rng.randrange(1, 501) * 1_000_000,
                                            rng.randrange(200, 40_001))),
                            None))
            return

        sellable = [k for k, v in avail.items() if v > 0]
        side = "sell" if sellable and rng.random() < 0.40 else "buy"
        if side == "sell":
            cid, symbol = rng.choice(sellable)
            have = avail[(cid, symbol)]
            if rng.random() < 0.10:
                q_ord = have               # L2: sell exactly the position
            else:
                q_ord = rng.randrange(1, have + 1)
                if q_ord > 1_000_000 and rng.random() < 0.80:
                    q_ord -= q_ord % 1_000_000   # mostly whole shares
            avail[(cid, symbol)] = have - q_ord  # reserve at schedule time
        else:
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            q_ord = (rng.randrange(1_000_000, 500_000_001)  # 6dp fractional
                     if rng.random() < 0.15
                     else rng.randrange(1, 501) * 1_000_000)
        cls = SYMBOL_CLASS[symbol]
        limit_c = rng.randrange(200, 40_001)     # $2.00 .. $400.00
        o_id = _id("o", "ord")
        stats["placements"] += 1
        n_partials = rng.choice((0, 0, 1, 1, 1, 2, 2, 3, 4))
        f_roll = rng.random()
        final = ("filled" if f_roll < 0.72
                 else "cancelled" if f_roll < 0.90 else "rejected")

        if final == "filled":
            chunks = _split(q_ord, n_partials + 1)
        elif n_partials == 0:
            chunks = []
        else:                              # cancel path fills only part
            part = q_ord * rng.randrange(10, 81) // 100
            chunks = _split(part, n_partials) if part >= 1 else []

        overfill = (side == "buy" and final == "filled" and len(chunks) >= 2
                    and rng.random() < tr["overfill"])
        if overfill:                       # partials ALONE exceed the order
            stats["overfill"] += 1
            chunks[rng.randrange(len(chunks) - 1)] += q_ord + 1_000_000

        timeline: list[tuple] = []         # (rel_pos, event, meta) in order
        fill_first = bool(chunks) and rng.random() < tr["fill_before_placement"]
        placement = mkev("order_placed",
                         _placement(o_id, cid, side, symbol, q_ord, limit_c))
        if not fill_first:
            timeline.append((0, placement, None))
        rel = 0
        for i, q in enumerate(chunks):
            is_final = final == "filled" and i == len(chunks) - 1
            if timeline or i:
                rel += rng.randrange(1, 6)
            ev, meta, t_id, dup = build_fill(o_id, cid, side, symbol, cls,
                                             limit_c, q, is_final)
            if not dup and rel > 0 and rng.random() < tr["settle_before_fill"]:
                stats["settle_before_fill"] += 1     # S6: rejected, then
                timeline.append((max(0, rel - rng.randrange(1, 6)),
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 ("settle", t_id, "early")))
            timeline.append((rel, ev, meta))
            if not dup:                    # dup fills store no trade (D8)
                srel = rel + rng.randrange(3, 41)
                timeline.append((srel,
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 ("settle", t_id, "normal")))
                if rng.random() < tr["double_settle"]:
                    stats["double_settle"] += 1
                    timeline.append((srel + rng.randrange(1, 10),
                                     mkev("trade_settled",
                                          {"trade_id": t_id}),
                                     ("settle", t_id, "double")))
            if fill_first and i == 0:      # S4: placement follows 2..8 later
                stats["fill_before_placement"] += 1
                timeline.append((rel + rng.randrange(2, 9), placement, None))

        if final != "filled":
            rel += rng.randrange(1, 6)
            timeline.append((rel, mkev("order_cancelled" if final == "cancelled"
                                       else "order_rejected",
                                       {"order_id": o_id}), None))
            remainder = q_ord - sum(chunks)
            if remainder >= 1 and rng.random() < tr["fill_after_cancel"]:
                stats["fill_after_cancel"] += 1      # S7: posts, hold stays 0
                ev, meta, t_id, dup = build_fill(
                    o_id, cid, side, symbol, cls, limit_c,
                    rng.randrange(1, remainder + 1), False)
                frel = rel + rng.randrange(1, 5)
                timeline.append((frel, ev, meta))
                if not dup:
                    timeline.append((frel + rng.randrange(3, 41),
                                     mkev("trade_settled",
                                          {"trade_id": t_id}),
                                     ("settle", t_id, "normal")))
        elif side == "buy" and rng.random() < tr["fill_after_close"]:
            stats["fill_after_close"] += 1           # extra partial post-final
            rel += rng.randrange(1, 5)
            ev, meta, t_id, dup = build_fill(
                o_id, cid, "buy", symbol, cls, limit_c,
                rng.randrange(1, 51) * 1_000_000, False)
            timeline.append((rel, ev, meta))
            if not dup:
                timeline.append((rel + rng.randrange(3, 41),
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 ("settle", t_id, "normal")))

        emit(timeline[0][1], timeline[0][2])
        for rel_i, ev_i, meta_i in timeline[1:]:
            pending.append((now + max(1, rel_i), ev_i, meta_i))

    def cash_misc() -> None:
        """A light Phase 1 sprinkle — valid payloads only; the cash-trap
        deep end lives in generate_cash, and corrupt() adds the garbage."""
        cid = rng.choice(CUSTOMERS)
        roll = rng.random()
        if roll < 0.35:
            emit(mkev("fee_charged",
                      {"customer_id": cid, "amount": _amount_2dp(rng)}))
        elif roll < 0.60:
            emit(mkev("transfer_between_customers",
                      {"from_customer_id": cid,
                       "to_customer_id": rng.choice(CUSTOMERS),
                       "amount": _amount_2dp(rng)}))
        elif roll < 0.80:
            gross_c = rng.randrange(2, 10_000)
            emit(mkev("interest_credited",
                      {"customer_id": cid,
                       "gross_amount": _cents_str(gross_c),
                       "customer_share":
                           _cents_str(rng.randrange(0, gross_c + 1))}))
        else:
            m_c = rng.randrange(100, 500_000)
            emit(mkev("fx_deposit",
                      {"customer_id": cid,
                       "amount_foreign": _amount_2dp(rng),
                       "currency": rng.choice(["EUR", "GBP", "JPY"]),
                       "market_rate": "0.90", "customer_rate": "0.80",
                       "usd_at_market_rate": _cents_str(m_c),
                       "usd_at_customer_rate":
                           _cents_str(m_c - rng.randrange(0, min(m_c, 500)))}))

    def standalone_oversell() -> None:
        """A lone sell fill for MORE than the whole position — the mirror
        knows the position exactly, so rejection is guaranteed (L1)."""
        held = [k for k, v in book_pos.items() if v > 0]
        cid, symbol = (rng.choice(held) if held
                       else (rng.choice(CUSTOMERS), rng.choice(SYMBOLS)))
        q = book_pos.get((cid, symbol), 0) + rng.randrange(1, 501) * 1_000_000
        cls = SYMBOL_CLASS[symbol]
        price_c = rng.randrange(200, 40_001)
        p_c = max(1, (q * price_c + 500_000) // 1_000_000)
        t_id = _id("t", "trd")
        emit(mkev("order_filled",
                  {"order_id": _id("o", "ord"), "trade_id": t_id,
                   "customer_id": cid, "side": "sell", "symbol": symbol,
                   "quantity": _qty_str(q), "price": _cents_str(price_c),
                   "principal": _cents_str(p_c),
                   "broker": rng.choice(CLASS_BROKERS[cls]),
                   "asset_class": cls,
                   "partner_rate": rng.choice(PARTNER_RATES)}),
             ("fill", "sell", cid, symbol, q, t_id, False, True, p_c))

    def malformed_order() -> None:
        """Five broken-order defect classes, cycled, fresh event ids. All
        must be Rejected with zero mutation and zero trade stored."""
        nonlocal bad_kind
        cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        cls = SYMBOL_CLASS[symbol]
        base = {"order_id": _id("o", "ord"), "trade_id": _id("t", "trd"),
                "customer_id": cid, "side": rng.choice(("buy", "sell")),
                "symbol": symbol, "quantity": "5", "price": "10.00",
                "principal": "50.00",
                "broker": rng.choice(CLASS_BROKERS[cls]),
                "asset_class": cls, "partner_rate": "0.5"}
        k = bad_kind % 5
        bad_kind += 1
        if k == 0:
            del base["broker"]             # unknown broker -> Rejected
            ev = mkev("order_filled", base)
        elif k == 1:
            del base["trade_id"]           # missing id -> Rejected
            ev = mkev("order_partially_filled", base)
        elif k == 2:
            base["quantity"] = "-5"        # sign flip -> Rejected (S10)
            ev = mkev("order_filled", base)
        elif k == 3:
            base["principal"] = "0.00"     # zero principal -> Rejected
            ev = mkev("order_filled", base)
        else:                              # wrong-type payload -> Rejected
            ev = mkev("order_placed", "this is not a payload object")
        emit(ev)
        stats["malformed"] += 1

    # ---- the feed: fund wallets first, then the mixed main body --------
    for _ in range(min(60, max(1, n // 8))):
        emit(mkev("deposit",
                  {"customer_id": rng.choice(CUSTOMERS),
                   "amount": _cents_str(rng.randrange(100_000, 10_000_001))}))
        stats["deposits"] += 1

    while len(out) < n:
        for item in [x for x in pending if x[0] <= len(out)]:
            pending.remove(item)
            emit(item[1], item[2])
        r = rng.random()
        if r < 0.27:
            emit(mkev(rng.choice(UNKNOWN_TYPES),
                      {"customer_id": rng.choice(CUSTOMERS),
                       "note": "no-op"}))
            stats["unknown"] += 1
        elif r < 0.52:
            emit(mkev("deposit", {"customer_id": rng.choice(CUSTOMERS),
                                  "amount": _amount_2dp(rng)}))
            stats["deposits"] += 1
        elif r < 0.70:
            cash_misc()
            stats["cash_misc"] += 1
        elif r < 0.70 + tr["oversell"]:
            standalone_oversell()
        elif r < 0.70 + tr["oversell"] + tr["unknown_oid_cancel"]:
            emit(mkev("order_cancelled",
                      {"order_id": f"ord_{seed}_ghost_{seqs['e']}"}))
            stats["unknown_oid_cancel"] += 1
        elif r < (0.70 + tr["oversell"] + tr["unknown_oid_cancel"]
                  + tr["malformed"]):
            malformed_order()
        else:
            lifecycle(len(out))

    # ---- settle_all: drain every trade the Book is holding open --------
    # Exactly the trades the mirror knows posted and were never settled
    # mid-run (fresh event ids: an early settle-before-fill was rejected
    # forever, but the trade itself is still owed its T+2).
    if settle_all:
        for t_id, done in list(trades.items()):
            if not done:
                emit(mkev("trade_settled", {"trade_id": t_id}),
                     ("settle", t_id, "settle_all"))
                stats["settle_all_emitted"] += 1

    for i, ev in enumerate(out):           # stream positions are sequential
        ev["offset"] = i
    stats["events"] = len(out)
    stats["pending_dropped"] = len(pending)
    stats["trades_posted"] = len(trades)
    LAST_STATS.clear()
    LAST_STATS.update(stats)
    return out


def generate_market_stats(seed: int, n: int, settle_all: bool = True) -> dict:
    """The companion probe: run generate_market and return a copy of its
    LAST_STATS — trap-fire counts, totals, and the duplicate-trade_id
    stuck-2350 tally — without touching the feed itself."""
    generate_market(seed, n, settle_all)
    return dict(LAST_STATS)


# ------------------------------------------------------------------ #
#  Phase 4: corporate-action chaos                                   #
# ------------------------------------------------------------------ #

# (ratio_from, ratio_to) pairs: 2:1 and 3:1 forward splits, 1:2 and 1:3
# reverse splits, plus 3->2 and 2->3. Every pair with ratio_from == 3
# manufactures repeating decimals that must quantize 6 dp PER LOT.
SPLIT_RATIOS = ((1, 2), (1, 3), (2, 1), (3, 1), (3, 2), (2, 3))

# Corporate trap dial board, all ON by default — same convention as
# MARKET_TRAPS: per-decision-point probabilities, 0 switches a trap off.
CORP_TRAPS = {
    "phantom_dividend": 0.08,     # dividend_cash on an UNHELD symbol (posts)
    "d2_mismatch": 0.05,          # net != gross - tax  -> posts + quarantine
    "d7_mismatch": 0.05,          # net != price x qty  -> posts + quarantine
    "split_zero_position": 0.08,  # split of a no-lot (cid, symbol): no-op
    "rename_zero_position": 0.06, # rename of a no-lot (cid, symbol): no-op
    "split_then_sell": 0.60,      # forced sell lifecycle right after a split
    "rename_chain": 0.15,         # A -> B -> C, second hop scheduled later
    "rename_then_trade": 0.50,    # buy under the new name after a rename
    "corp_malformed": 0.01,       # standalone broken corporate payloads
}

# Same observability side-channel pattern as LAST_STATS, separate dict so
# the two generators never clobber each other's numbers.
CORP_LAST_STATS: dict = {}


def generate_corporate(seed: int, n: int,
                       settle_all: bool = True) -> list[dict]:
    """~n coherent events with the four corporate-action types woven in at
    roughly a fifth of the mix, deterministic in (seed, n, settle_all).
    Same machinery as generate_market: a mirror of the Book's acceptance
    rules updated at EMISSION time, pending-scheduled lifecycles, and a
    settle_all tail that drains every posted-and-unsettled trade.

    Corporate coherence woven in:
      * cash dividends on symbols the customer actually holds, plus
        phantom dividends on unheld symbols (legal, observe-only), plus
        D2 net != gross - tax at a low rate (they POST net + quarantine);
      * reinvestments creating real lots that later sell lifecycles
        consume (their quantity joins the reservable position), plus D7
        net != price x qty mismatches (post + quarantine);
      * splits — 2:1, 3:1, reverses, repeating-decimal 3->x — usually
        followed by a forced sell of the same (cid, symbol) (the
        split->sell poison), hitting partially-consumed lots whenever the
        key has history, scoped per customer while other holders of the
        symbol stand untouched;
      * renames into existing holdings (merge collisions), A->B->C chains
        into fresh synthetic symbols, and buys under the new name after
        the rename; zero-position splits/renames as valid no-ops;
      * malformed corporate payloads on fresh event ids (missing
        net_amount, negative net, negative reinvest_quantity, zero and
        non-numeric ratios, empty new_symbol) — all must Reject clean.

    The mirror is LOT-exact this time (integer micro-shares per lot, FIFO
    by arrival): splits round per lot, so a totals-only mirror would
    drift off the Book by a micro-share and desynchronize accept/reject
    on later sells. (2*q*to + from) // (2*from) is HALF_UP — digit-
    identical to the Book's qnum(qty * to / from) for these ratios.
    Reservations survive corporate actions approximately (avail is scaled
    on splits, moved on renames); any scheduled sell the actions have
    outrun is rejected identically by mirror and Book and tallied as
    oversell_accidental. Ids are evt_/ord_/trd_<seed>_q<k> — disjoint
    from every other generator here. Stats land in CORP_LAST_STATS.
    """
    rng = random.Random(seed ^ 0x0C0A57)   # decorrelate from other stages
    tr = MARKET_TRAPS
    ct = CORP_TRAPS
    out: list[dict] = []
    pending: list[tuple] = []              # (emit_at, event, meta)
    stats = {k: 0 for k in (
        "deposits", "cash_misc", "unknown", "placements", "fills_emitted",
        "fills_posted", "settles_emitted", "settles_posted",
        "settles_rejected", "settle_all_emitted", "settle_before_fill",
        "double_settle", "dup_trade_id", "oversell", "oversell_accidental",
        "unknown_oid_cancel", "malformed", "dup_stuck_2350_cents",
        "dividend_cash", "phantom_dividend", "d2_mismatch",
        "dividend_reinvested", "d7_mismatch",
        "stock_split", "split_reverse", "split_repeating",
        "split_zero_position", "split_partially_consumed",
        "split_scope_pair", "split_then_sell",
        "symbol_change", "rename_merge_collision", "rename_chain",
        "rename_then_trade", "rename_zero_position", "corp_malformed")}
    sym_class = dict(SYMBOL_CLASS)         # synthetic rename targets join in
    # The lot-exact mirror: (cid, sym) -> [[arrival, qty_micro], ...] kept
    # in arrival order (the Book's global-seq FIFO, derived independently).
    lots: dict = {}
    avail: dict = {}                       # reservable for scheduled sells
    partial_keys: set = set()              # keys with a partially-eaten lot
    trades: dict = {}                      # trade_id -> settled?
    seqs = {"e": 0, "o": 0, "t": 0, "s": 0}
    arr = 0                                # mirror lot arrival counter
    bad_kind = 0
    corp_bad = 0

    def _id(kind: str, prefix: str) -> str:
        seqs[kind] += 1
        return f"{prefix}_{seed}_q{seqs[kind]}"

    def mkev(t: str, payload) -> dict:
        return {"offset": -1, "event_id": _id("e", "evt"),
                "type": t, "payload": payload}

    def pos(key) -> int:
        return sum(q for _a, q in lots.get(key, ()))

    def held_keys() -> list:
        return [k for k in lots if pos(k) > 0]

    def _consume(key, q: int) -> None:
        """Mirror FIFO relief in integer micro-shares, arrival order."""
        lst = lots.get(key, [])
        rem = q
        i = 0
        while rem > 0 and i < len(lst):
            have = lst[i][1]
            if have <= 0:
                lst.pop(i)     # mirror carries no cost: husks can just go
                continue
            if have <= rem:
                lst.pop(i)
                rem -= have
            else:
                partial_keys.add(key)
                lst[i][1] = have - rem
                rem = 0

    def track(meta) -> None:
        """Apply one emitted event's effect to the mirror — the same
        accept/reject decision the Book will make, made at emission time
        (= first-delivery order) so nothing can desynchronize the two."""
        nonlocal arr
        if meta is None:
            return
        kind = meta[0]
        if kind == "fill":
            _, side, cid, symbol, q, t_id, dup, expect_reject, p_c = meta
            key = (cid, symbol)
            stats["fills_emitted"] += 1
            if side == "sell" and q > pos(key):
                # the Book rejects before any lot mutation (L1) — including
                # sells that a reverse split / rename outran mid-schedule
                stats["oversell" if expect_reject
                      else "oversell_accidental"] += 1
                return
            if side == "sell":
                _consume(key, q)
            else:
                arr += 1
                lots.setdefault(key, []).append([arr, q])
                avail[key] = avail.get(key, 0) + q
            stats["fills_posted"] += 1
            if dup:                        # posts legs, stores NO trade (D8)
                stats["dup_trade_id"] += 1
                stats["dup_stuck_2350_cents"] += p_c
            else:
                trades[t_id] = False
        elif kind == "settle":
            _, t_id, _skind = meta
            stats["settles_emitted"] += 1
            if trades.get(t_id) is False:  # known and not yet settled
                trades[t_id] = True
                stats["settles_posted"] += 1
            else:                          # early / double / never-posted
                stats["settles_rejected"] += 1
        elif kind == "reinvest":           # a new lot, back of the queue (L8)
            _, cid, symbol, q = meta
            arr += 1
            key = (cid, symbol)
            lots.setdefault(key, []).append([arr, q])
            avail[key] = avail.get(key, 0) + q
        elif kind == "split":              # per-lot HALF_UP, cost-free mirror
            _, cid, symbol, r_from, r_to = meta
            key = (cid, symbol)
            if key in lots:
                scaled = [[a, (2 * q * r_to + r_from) // (2 * r_from)]
                          for a, q in lots[key]]
                lots[key] = [aq for aq in scaled if aq[1] > 0]
            if avail.get(key, 0) > 0:
                avail[key] = avail[key] * r_to // r_from
        else:                              # ("rekey", cid, old, new)
            _, cid, old, new = meta
            ko, kn = (cid, old), (cid, new)
            moved = lots.pop(ko, None)
            if moved is not None:
                lots[kn] = sorted(lots.get(kn, []) + moved)
            freed = avail.pop(ko, 0)
            if freed:
                avail[kn] = avail.get(kn, 0) + freed
            if ko in partial_keys:
                partial_keys.discard(ko)
                partial_keys.add(kn)

    def emit(ev: dict, meta=None) -> None:
        out.append(ev)
        track(meta)

    def _chunks(total: int, parts: int) -> list[int]:
        if parts <= 1 or total <= parts:
            return [total]
        cuts = sorted(rng.sample(range(1, total), parts - 1))
        return [b - a for a, b in zip([0] + cuts, cuts + [total])]

    def _placement(o_id, cid, side, symbol, q, limit_c) -> dict:
        p = {"order_id": o_id, "customer_id": cid, "side": side,
             "symbol": symbol, "quantity": _qty_str(q),
             "limit_price": _cents_str(limit_c),
             "asset_class": sym_class[symbol]}
        field = "est_commission" if rng.random() < 0.10 else "est_charges"
        p[field] = _cents_str(rng.randrange(0, 2_001))
        return p

    def build_fill(o_id, cid, side, symbol, limit_c, q, final):
        cls = sym_class[symbol]
        broker = rng.choice(CLASS_BROKERS[cls])
        price_c = max(1, limit_c * rng.randrange(9_800, 10_201) // 10_000)
        p_c = max(1, (q * price_c + 500_000) // 1_000_000)
        dup = (side == "buy" and bool(trades)
               and rng.random() < tr["dup_trade_id"])
        t_id = rng.choice(list(trades)) if dup else _id("t", "trd")
        payload = {"order_id": o_id, "trade_id": t_id, "customer_id": cid,
                   "side": side, "symbol": symbol, "quantity": _qty_str(q),
                   "price": _cents_str(price_c),
                   "principal": _cents_str(p_c), "broker": broker,
                   "asset_class": cls,
                   "partner_rate": rng.choice(PARTNER_RATES)}
        ev = mkev("order_filled" if final else "order_partially_filled",
                  payload)
        return ev, ("fill", side, cid, symbol, q, t_id, dup, False, p_c), \
            t_id, dup

    def lifecycle(now: int, force_key=None) -> None:
        """One order lifecycle: placement now, fills/settles/final as
        pending. force_key drives the split->sell poison: a sell of the
        just-split (cid, symbol) from its post-split reservable amount."""
        if force_key is not None:
            if avail.get(force_key, 0) <= 0:
                return
            side, (cid, symbol) = "sell", force_key
        else:
            sellable = [k for k, v in avail.items() if v > 0]
            side = "sell" if sellable and rng.random() < 0.42 else "buy"
            if side == "sell":
                cid, symbol = rng.choice(sellable)
            else:
                cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        if side == "sell":
            have = avail[(cid, symbol)]
            if rng.random() < 0.10:
                q_ord = have               # L2: sell exactly the position
            else:
                q_ord = rng.randrange(1, have + 1)
                if q_ord > 1_000_000 and rng.random() < 0.60:
                    q_ord -= q_ord % 1_000_000   # mostly whole shares
            avail[(cid, symbol)] = have - q_ord  # reserve at schedule time
        else:
            q_ord = (rng.randrange(1_000_000, 300_000_001)  # 6dp fractional
                     if rng.random() < 0.15
                     else rng.randrange(1, 301) * 1_000_000)
        limit_c = rng.randrange(200, 40_001)     # $2.00 .. $400.00
        o_id = _id("o", "ord")
        stats["placements"] += 1
        n_partials = rng.choice((0, 0, 1, 1, 2, 2, 3))
        f_roll = rng.random()
        final = ("filled" if f_roll < 0.78
                 else "cancelled" if f_roll < 0.92 else "rejected")
        if final == "filled":
            chunks = _chunks(q_ord, n_partials + 1)
        elif n_partials == 0:
            chunks = []
        else:                              # cancel path fills only part
            part = q_ord * rng.randrange(10, 81) // 100
            chunks = _chunks(part, n_partials) if part >= 1 else []

        timeline = [(0, mkev("order_placed",
                             _placement(o_id, cid, side, symbol, q_ord,
                                        limit_c)), None)]
        rel = 0
        for i, q in enumerate(chunks):
            is_final = final == "filled" and i == len(chunks) - 1
            rel += rng.randrange(1, 6)
            ev, meta, t_id, dup = build_fill(o_id, cid, side, symbol,
                                             limit_c, q, is_final)
            if not dup and rng.random() < tr["settle_before_fill"]:
                stats["settle_before_fill"] += 1     # S6: rejected forever
                timeline.append((max(1, rel - rng.randrange(1, 6)),
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 ("settle", t_id, "early")))
            timeline.append((rel, ev, meta))
            if not dup:                    # dup fills store no trade (D8)
                srel = rel + rng.randrange(3, 41)
                timeline.append((srel,
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 ("settle", t_id, "normal")))
                if rng.random() < tr["double_settle"]:
                    stats["double_settle"] += 1
                    timeline.append((srel + rng.randrange(1, 10),
                                     mkev("trade_settled",
                                          {"trade_id": t_id}),
                                     ("settle", t_id, "double")))
        if final != "filled":
            rel += rng.randrange(1, 6)
            timeline.append((rel,
                             mkev("order_cancelled" if final == "cancelled"
                                  else "order_rejected",
                                  {"order_id": o_id}), None))
        emit(timeline[0][1], timeline[0][2])
        for rel_i, ev_i, meta_i in timeline[1:]:
            pending.append((now + max(1, rel_i), ev_i, meta_i))

    def _sched_buy(symbol: str, cid: str, at: int) -> None:
        """A scheduled placement + final buy fill + settle — the trade
        under a freshly renamed symbol."""
        o_id = _id("o", "ord")
        q = rng.randrange(1, 51) * 1_000_000
        limit_c = rng.randrange(200, 40_001)
        stats["placements"] += 1
        pending.append((at, mkev("order_placed",
                                 _placement(o_id, cid, "buy", symbol, q,
                                            limit_c)), None))
        ev, meta, t_id, dup = build_fill(o_id, cid, "buy", symbol,
                                         limit_c, q, True)
        frel = at + rng.randrange(1, 4)
        pending.append((frel, ev, meta))
        if not dup:
            pending.append((frel + rng.randrange(3, 41),
                            mkev("trade_settled", {"trade_id": t_id}),
                            ("settle", t_id, "normal")))

    # ---- the four corporate emitters -----------------------------------
    def dividend_cash() -> None:
        held = held_keys()
        if rng.random() < ct["phantom_dividend"] or not held:
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            for _ in range(8):             # find a genuinely unheld pair
                if pos((cid, symbol)) == 0:
                    break
                cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            stats["phantom_dividend"] += 1
        else:
            cid, symbol = rng.choice(held)
        gross_c = rng.randrange(100, 50_001)
        tax_c = gross_c * rng.randrange(0, 31) // 100
        net_c = gross_c - tax_c
        if rng.random() < ct["d2_mismatch"]:
            net_c = max(1, net_c + rng.choice((-1, 1))
                        * rng.randrange(1, 200))
            if net_c == gross_c - tax_c:
                net_c += 1                 # the mismatch must be real
            stats["d2_mismatch"] += 1
        emit(mkev("dividend_cash",
                  {"customer_id": cid, "symbol": symbol,
                   "gross_amount": _cents_str(gross_c),
                   "withholding_tax": _cents_str(tax_c),
                   "net_amount": _cents_str(net_c)}))
        stats["dividend_cash"] += 1

    def dividend_reinvested() -> None:
        held = held_keys()
        if held and rng.random() < 0.85:
            cid, symbol = rng.choice(held)
        else:                              # legal: reinvest seeds a holding
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        q = rng.randrange(50_000, 5_000_001)     # 0.05 .. 5 shares, 6 dp
        if rng.random() < 0.30:
            q = rng.randrange(1, 6) * 1_000_000  # sometimes whole shares
        price_c = rng.randrange(200, 40_001)
        exact_c = (q * price_c + 500_000) // 1_000_000
        net_c = exact_c
        if rng.random() < ct["d7_mismatch"]:
            net_c = max(1, net_c + rng.choice((-1, 1))
                        * rng.randrange(2, 300))
            if abs(net_c - exact_c) <= 1:
                net_c = exact_c + 2        # D7 fires only beyond a cent
            stats["d7_mismatch"] += 1
        tax_c = rng.randrange(0, net_c // 4 + 1)
        emit(mkev("dividend_reinvested",
                  {"customer_id": cid, "symbol": symbol,
                   "gross_amount": _cents_str(net_c + tax_c),
                   "withholding_tax": _cents_str(tax_c),
                   "net_amount": _cents_str(net_c),
                   "reinvest_price": _cents_str(price_c),
                   "reinvest_quantity": _qty_str(q)}),
             ("reinvest", cid, symbol, q))
        stats["dividend_reinvested"] += 1

    def stock_split() -> None:
        held = held_keys()
        r_from, r_to = SPLIT_RATIOS[rng.randrange(len(SPLIT_RATIOS))]
        if rng.random() < ct["split_zero_position"] or not held:
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            for _ in range(8):
                if pos((cid, symbol)) == 0:
                    break
                cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            stats["split_zero_position"] += 1
        else:
            cid, symbol = rng.choice(held)
            if any(s == symbol and c != cid and pos((c, s)) > 0
                   for (c, s) in lots):
                stats["split_scope_pair"] += 1   # L12 pair under fire
            if (cid, symbol) in partial_keys:
                stats["split_partially_consumed"] += 1   # L4
        if r_to < r_from:
            stats["split_reverse"] += 1
        if r_from == 3:
            stats["split_repeating"] += 1        # L3
        emit(mkev("stock_split",
                  {"customer_id": cid, "symbol": symbol,
                   "ratio_from": str(r_from), "ratio_to": str(r_to)}),
             ("split", cid, symbol, r_from, r_to))
        stats["stock_split"] += 1
        if (avail.get((cid, symbol), 0) > 0
                and rng.random() < ct["split_then_sell"]):
            stats["split_then_sell"] += 1        # the split->sell poison
            lifecycle(len(out), force_key=(cid, symbol))

    def symbol_change() -> None:
        held = held_keys()
        roll = rng.random()
        if roll < ct["rename_zero_position"] or not held:
            cid, old = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            for _ in range(8):
                if pos((cid, old)) == 0:
                    break
                cid, old = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            new = rng.choice([s for s in SYMBOLS if s != old])
            stats["rename_zero_position"] += 1
            emit(mkev("symbol_change",
                      {"customer_id": cid, "old_symbol": old,
                       "new_symbol": new}),
                 ("rekey", cid, old, new))
            stats["symbol_change"] += 1
            return
        cid, old = rng.choice(held)
        if roll < ct["rename_zero_position"] + ct["rename_chain"]:
            # A -> B -> C: hop 1 now, hop 2 scheduled, then a buy under C.
            seqs["s"] += 1
            mid = f"NEWCO-{seed}-{seqs['s']}A"
            fin = f"NEWCO-{seed}-{seqs['s']}B"
            sym_class[mid] = sym_class[fin] = sym_class[old]
            emit(mkev("symbol_change",
                      {"customer_id": cid, "old_symbol": old,
                       "new_symbol": mid}),
                 ("rekey", cid, old, mid))
            stats["symbol_change"] += 2
            stats["rename_chain"] += 1
            hop2 = len(out) + rng.randrange(2, 7)
            pending.append((hop2,
                            mkev("symbol_change",
                                 {"customer_id": cid, "old_symbol": mid,
                                  "new_symbol": fin}),
                            ("rekey", cid, mid, fin)))
            if rng.random() < ct["rename_then_trade"]:
                stats["rename_then_trade"] += 1
                _sched_buy(fin, cid, hop2 + rng.randrange(1, 5))
            return
        # plain rename, often INTO an existing holding (merge collision)
        mine = [s for (c, s) in lots
                if c == cid and s != old and pos((c, s)) > 0]
        if mine and rng.random() < 0.5:
            new = rng.choice(mine)
        else:
            new = rng.choice([s for s in SYMBOLS if s != old])
        if pos((cid, new)) > 0:
            stats["rename_merge_collision"] += 1     # L9 / A6 under fire
        emit(mkev("symbol_change",
                  {"customer_id": cid, "old_symbol": old,
                   "new_symbol": new}),
             ("rekey", cid, old, new))
        stats["symbol_change"] += 1
        if rng.random() < ct["rename_then_trade"]:
            stats["rename_then_trade"] += 1
            _sched_buy(new, cid, len(out) + rng.randrange(1, 5))

    def corp_malformed() -> None:
        """Eight broken corporate payloads, cycled, fresh event ids — every
        one must be Rejected with zero mutation."""
        nonlocal corp_bad
        cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        k = corp_bad % 8
        corp_bad += 1
        if k == 0:      # missing net_amount
            ev = mkev("dividend_cash",
                      {"customer_id": cid, "symbol": symbol,
                       "gross_amount": "10.00", "withholding_tax": "1.50"})
        elif k == 1:    # negative net
            ev = mkev("dividend_cash",
                      {"customer_id": cid, "symbol": symbol,
                       "gross_amount": "10.00", "withholding_tax": "1.00",
                       "net_amount": "-9.00"})
        elif k == 2:    # negative reinvest_quantity
            ev = mkev("dividend_reinvested",
                      {"customer_id": cid, "symbol": symbol,
                       "net_amount": "20.00", "reinvest_price": "10.00",
                       "reinvest_quantity": "-2"})
        elif k == 3:    # missing net_amount on a reinvest
            ev = mkev("dividend_reinvested",
                      {"customer_id": cid, "symbol": symbol,
                       "reinvest_price": "10.00", "reinvest_quantity": "2"})
        elif k == 4:    # zero ratio
            ev = mkev("stock_split",
                      {"customer_id": cid, "symbol": symbol,
                       "ratio_from": "0", "ratio_to": "2"})
        elif k == 5:    # missing ratio_to
            ev = mkev("stock_split",
                      {"customer_id": cid, "symbol": symbol,
                       "ratio_from": "1"})
        elif k == 6:    # non-numeric ratio
            ev = mkev("stock_split",
                      {"customer_id": cid, "symbol": symbol,
                       "ratio_from": "1", "ratio_to": "three"})
        else:           # empty new_symbol
            ev = mkev("symbol_change",
                      {"customer_id": cid, "old_symbol": symbol,
                       "new_symbol": ""})
        emit(ev)
        stats["corp_malformed"] += 1

    def cash_misc() -> None:
        cid = rng.choice(CUSTOMERS)
        roll = rng.random()
        if roll < 0.40:
            emit(mkev("fee_charged",
                      {"customer_id": cid, "amount": _amount_2dp(rng)}))
        elif roll < 0.70:
            emit(mkev("transfer_between_customers",
                      {"from_customer_id": cid,
                       "to_customer_id": rng.choice(CUSTOMERS),
                       "amount": _amount_2dp(rng)}))
        else:
            gross_c = rng.randrange(2, 10_000)
            emit(mkev("interest_credited",
                      {"customer_id": cid,
                       "gross_amount": _cents_str(gross_c),
                       "customer_share":
                           _cents_str(rng.randrange(0, gross_c + 1))}))

    def standalone_oversell() -> None:
        held = held_keys()
        cid, symbol = (rng.choice(held) if held
                       else (rng.choice(CUSTOMERS), rng.choice(SYMBOLS)))
        q = pos((cid, symbol)) + rng.randrange(1, 501) * 1_000_000
        cls = sym_class[symbol]
        price_c = rng.randrange(200, 40_001)
        p_c = max(1, (q * price_c + 500_000) // 1_000_000)
        t_id = _id("t", "trd")
        emit(mkev("order_filled",
                  {"order_id": _id("o", "ord"), "trade_id": t_id,
                   "customer_id": cid, "side": "sell", "symbol": symbol,
                   "quantity": _qty_str(q), "price": _cents_str(price_c),
                   "principal": _cents_str(p_c),
                   "broker": rng.choice(CLASS_BROKERS[cls]),
                   "asset_class": cls,
                   "partner_rate": rng.choice(PARTNER_RATES)}),
             ("fill", "sell", cid, symbol, q, t_id, False, True, p_c))

    def malformed_order() -> None:
        nonlocal bad_kind
        cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        cls = sym_class[symbol]
        base = {"order_id": _id("o", "ord"), "trade_id": _id("t", "trd"),
                "customer_id": cid, "side": rng.choice(("buy", "sell")),
                "symbol": symbol, "quantity": "5", "price": "10.00",
                "principal": "50.00",
                "broker": rng.choice(CLASS_BROKERS[cls]),
                "asset_class": cls, "partner_rate": "0.5"}
        k = bad_kind % 5
        bad_kind += 1
        if k == 0:
            del base["broker"]             # unknown broker -> Rejected
            ev = mkev("order_filled", base)
        elif k == 1:
            del base["trade_id"]           # missing id -> Rejected
            ev = mkev("order_partially_filled", base)
        elif k == 2:
            base["quantity"] = "-5"        # sign flip -> Rejected (S10)
            ev = mkev("order_filled", base)
        elif k == 3:
            base["principal"] = "0.00"     # zero principal -> Rejected
            ev = mkev("order_filled", base)
        else:                              # wrong-type payload -> Rejected
            ev = mkev("order_placed", "this is not a payload object")
        emit(ev)
        stats["malformed"] += 1

    # ---- the feed: fund wallets first, then the woven main body --------
    for _ in range(min(60, max(1, n // 8))):
        emit(mkev("deposit",
                  {"customer_id": rng.choice(CUSTOMERS),
                   "amount": _cents_str(rng.randrange(100_000, 10_000_001))}))
        stats["deposits"] += 1

    corp_hi = 0.78                         # 0.36..0.78: the corporate band
    while len(out) < n:
        for item in [x for x in pending if x[0] <= len(out)]:
            pending.remove(item)
            emit(item[1], item[2])
        r = rng.random()
        if r < 0.12:
            emit(mkev(rng.choice(UNKNOWN_TYPES),
                      {"customer_id": rng.choice(CUSTOMERS),
                       "note": "no-op"}))
            stats["unknown"] += 1
        elif r < 0.28:
            emit(mkev("deposit", {"customer_id": rng.choice(CUSTOMERS),
                                  "amount": _amount_2dp(rng)}))
            stats["deposits"] += 1
        elif r < 0.36:
            cash_misc()
            stats["cash_misc"] += 1
        elif r < corp_hi:
            c = rng.random()
            if c < 0.30:
                dividend_cash()
            elif c < 0.55:
                dividend_reinvested()
            elif c < 0.80:
                stock_split()
            else:
                symbol_change()
        elif r < corp_hi + tr["oversell"]:
            standalone_oversell()
        elif r < corp_hi + tr["oversell"] + 0.01:
            emit(mkev("order_cancelled",
                      {"order_id": f"ord_{seed}_ghost_{seqs['e']}"}))
            stats["unknown_oid_cancel"] += 1
        elif r < corp_hi + tr["oversell"] + 0.01 + tr["malformed"]:
            malformed_order()
        elif r < (corp_hi + tr["oversell"] + 0.01 + tr["malformed"]
                  + ct["corp_malformed"]):
            corp_malformed()
        else:
            lifecycle(len(out))

    # ---- settle_all: drain every trade the Book is holding open --------
    if settle_all:
        for t_id, done in list(trades.items()):
            if not done:
                emit(mkev("trade_settled", {"trade_id": t_id}),
                     ("settle", t_id, "settle_all"))
                stats["settle_all_emitted"] += 1

    for i, ev in enumerate(out):           # stream positions are sequential
        ev["offset"] = i
    stats["events"] = len(out)
    stats["pending_dropped"] = len(pending)
    stats["trades_posted"] = len(trades)
    corp_total = (stats["dividend_cash"] + stats["dividend_reinvested"]
                  + stats["stock_split"] + stats["symbol_change"]
                  + stats["corp_malformed"])
    stats["corporate_events"] = corp_total
    stats["corporate_share_pct"] = round(100.0 * corp_total / len(out), 1)
    CORP_LAST_STATS.clear()
    CORP_LAST_STATS.update(stats)
    return out


def generate_corporate_stats(seed: int, n: int,
                             settle_all: bool = True) -> dict:
    """Companion probe: run generate_corporate and return a copy of its
    CORP_LAST_STATS without touching the feed itself."""
    generate_corporate(seed, n, settle_all)
    return dict(CORP_LAST_STATS)


# ------------------------------------------------------------------ #
#  Phase 5: the finale — full mix + dense reversals + settlements    #
# ------------------------------------------------------------------ #
# The finale mirror must undo its own lot effects EXACTLY the way the
# Book undoes them (restore-across-a-split scales by an exact Fraction
# ratio and quantizes 6 dp), so this generator — unlike the earlier ones —
# uses Decimal/Fraction for its lot mirror. Still stdlib, still fully
# deterministic in (seed, n, settle_all).
from decimal import Decimal as _D, ROUND_HALF_UP as _RHU  # noqa: E402
from fractions import Fraction as _Fr                     # noqa: E402

_QSTEP = _D("0.000001")
_ZQ = _D("0.000000")


def _qdec(x) -> _D:
    """The Book's qnum, restated: 6 dp, half away from zero."""
    return _D(str(x)).quantize(_QSTEP, rounding=_RHU)


# Fee lines in integer bps/cents, restated from the task sheet so the
# payable mirror can accrue the SAME cent-rounded per-fill components the
# Book does (M4) without importing tariff.
FEE_CENTS = {
    "BRK-A": {"brok": 20, "cust": 4, "bcost": 9, "ccost": 2,
              "min_c": 100, "ticket_c": 35},
    "BRK-B": {"brok": 15, "cust": 5, "bcost": 8, "ccost": 3,
              "min_c": 250, "ticket_c": 300},
    "BRK-C": {"brok": 25, "cust": 3, "bcost": 12, "ccost": 1,
              "min_c": 50, "ticket_c": 20},
}
RATE_QUARTERS = {"0": 0, "0.25": 1, "0.5": 2, "0.75": 3}
PAYABLE_OF = {"BRK-A": "2411", "BRK-B": "2412", "BRK-C": "2413"}
PAYABLE_ACCTS = ("2400", "2411", "2412", "2413", "2420", "2430")
# acct -> (event type, broker payload value or None)
SETTLE_EVENT_OF = {"2411": ("broker_fees_settled", "BRK-A"),
                   "2412": ("broker_fees_settled", "BRK-B"),
                   "2413": ("broker_fees_settled", "BRK-C"),
                   "2420": ("custodian_fees_settled", None),
                   "2400": ("reg_fees_remitted", None),
                   "2430": ("partner_payout", None)}


def _fee_components_cents(broker: str, p_c: int, rate_key: str):
    """(r, bc, cc, ps) in integer cents — digit-identical to
    tariff.fill_charges on a 2dp principal string (HALF_UP per line)."""
    t = FEE_CENTS[broker]
    b = max((p_c * t["brok"] + 5000) // 10000, t["min_c"])
    c = (p_c * t["cust"] + 5000) // 10000
    r = (p_c * 8 + 5000) // 10000
    bc = (p_c * t["bcost"] + 5000) // 10000 + t["ticket_c"]
    cc = (p_c * t["ccost"] + 5000) // 10000
    margin = (b + c) - (bc + cc)
    k = RATE_QUARTERS[rate_key]
    ps = (margin * k + 2) // 4 if (margin > 0 and k) else 0
    return r, bc, cc, ps


# Finale trap dial board. The rev_* dials are slices of each reversal
# slot's roll (the remainder is a VALID reversal of a posted event); the
# settle_* dials slice each settlement slot's roll; resettle is the chance
# a reversed settlement gets a scheduled re-settle. 0 switches a trap off.
FINALE_TRAPS = {
    "rev_unknown_ref": 0.06,      # reversal naming an id never sent (R1)
    "rev_of_rejected": 0.07,      # reversal of an event the Book rejected (R2)
    "rev_double": 0.07,           # second reversal of the same original (R3)
    "rev_of_reversal": 0.06,      # reversal of a posted reversal (R4/A5)
    "rev_before_original": 0.05,  # reversal first, original later: stays rejected
    "settle_unknown_broker": 0.05,   # broker_fees_settled for BRK-Z
    "settle_zero_payable": 0.10,     # settlement with nothing outstanding (R9)
    "resettle": 0.60,             # re-settle after a reversed settlement (R8)
}

# Same observability side-channel pattern as LAST_STATS / CORP_LAST_STATS.
FINALE_LAST_STATS: dict = {}


def generate_finale(seed: int, n: int, settle_all: bool = True) -> list[dict]:
    """~n events: everything generate_corporate produces PLUS dense
    reversals (~8% of events) and fee settlements (~5%), deterministic in
    (seed, n, settle_all).

    Reversal coverage: posted cash events (deposits, fees, transfers,
    interest, cash dividends), buy fills (untouched, partially consumed,
    settled, unsettled), sell fills (including across later splits and
    renames), reinvest lots, no-leg corporate events (splits, renames —
    [] legs, lots undone), and settlement events (payable re-raised, then
    often re-settled). Every FINALE_TRAPS defect is woven in at a steady
    rate, and settlements cycle settle -> accrue -> settle per
    (customer, account).

    The mirror tracks reversals EXACTLY: it undoes its own lot, payable
    and trade mirrors with the Book's own arithmetic (Fraction multiplier
    scaling, 6 dp quantize), so scheduled sells stay valid — any sell a
    reversal outruns is rejected identically by mirror and Book — and
    settle_all still drains exactly the posted-and-unsettled, unreversed
    trades. Ids are evt_/ord_/trd_<seed>_f<k>, disjoint from every other
    generator here. Stats land in FINALE_LAST_STATS.
    """
    rng = random.Random(seed ^ 0xF1A7E5)   # decorrelate from other stages
    tr = MARKET_TRAPS
    ct = CORP_TRAPS
    ft = FINALE_TRAPS
    out: list[dict] = []
    pending: list[tuple] = []              # (emit_at, event, meta)
    stats = {k: 0 for k in (
        "deposits", "cash_misc", "unknown", "placements", "fills_emitted",
        "fills_posted", "settles_emitted", "settles_posted",
        "settles_rejected", "settle_all_emitted", "settle_before_fill",
        "double_settle", "dup_trade_id", "oversell", "oversell_accidental",
        "unknown_oid_cancel", "malformed", "dup_stuck_2350_cents",
        "dividend_cash", "phantom_dividend", "d2_mismatch",
        "dividend_reinvested", "d7_mismatch", "stock_split",
        "split_zero_position", "split_then_sell", "symbol_change",
        "rename_merge_collision", "rename_chain", "rename_then_trade",
        "rename_zero_position", "corp_malformed",
        "rev_slots", "rev_emitted", "rev_valid", "rev_unknown_ref",
        "rev_of_rejected", "rev_double", "rev_of_reversal",
        "rev_before_original", "rev_cash", "rev_fill_buy", "rev_fill_sell",
        "rev_reinvest", "rev_split", "rev_rekey", "rev_settlement",
        "rev_buy_partial", "rev_settled_trade_kept",
        "rev_unsettled_trade_dropped", "rev_sell_across_split",
        "resettle_after_reversal",
        "stl_slots", "stl_emitted", "stl_posted", "stl_rejected_zero",
        "stl_zero_payable_trap", "stl_unknown_broker",
        "stl_cents_settled")}
    sym_class = dict(SYMBOL_CLASS)
    # -- the mirror -----------------------------------------------------
    holdings: dict = {}    # (cid, sym) -> [lot, ...]; lot is a dict
    avail: dict = {}       # (cid, sym) -> Decimal reservable for new sells
    pay: dict = {}         # (cid, acct) -> signed integer cents outstanding
    trades: dict = {}      # trade_id -> settled? (absent = never/deleted)
    registry: dict = {}    # posted eid -> how to undo it
    revable: list = []     # eids eligible to be validly reversed, in order
    reversed_ids: list = []            # originals already reversed (R3 pool)
    reversed_set: set = set()
    posted_reversals: list = []        # posted reversal eids (R4 pool)
    rejected_ids: list = []            # eids we KNOW the Book rejected (R2)
    seqs = {"e": 0, "o": 0, "t": 0, "s": 0}
    arr = [0]                          # mirror lot arrival counter
    bad_kind = 0
    corp_bad = 0

    def _id(kind: str, prefix: str) -> str:
        seqs[kind] += 1
        return f"{prefix}_{seed}_f{seqs[kind]}"

    def mkev(t: str, payload) -> dict:
        return {"offset": -1, "event_id": _id("e", "evt"),
                "type": t, "payload": payload}

    def pos(key) -> _D:
        return sum((l["qty"] for l in holdings.get(key, ())), _ZQ)

    def held_keys() -> list:
        return [k for k in holdings if pos(k) > 0]

    def _clamp_avail(key) -> None:
        avail[key] = min(avail.get(key, _ZQ), pos(key))
        if avail[key] < 0:
            avail[key] = _ZQ

    def _pay_add(cid, broker, p_c, rate_key, sign) -> None:
        r_c, bc_c, cc_c, ps_c = _fee_components_cents(broker, p_c, rate_key)
        for acct, amt in (("2400", r_c), (PAYABLE_OF[broker], bc_c),
                          ("2420", cc_c), ("2430", ps_c)):
            pay[(cid, acct)] = pay.get((cid, acct), 0) + sign * amt

    # ---- mirror application, at EMISSION time (= first-delivery order) --
    def track(ev: dict, meta) -> None:
        if meta is None:
            return
        eid = ev["event_id"]
        k = meta["kind"]
        if k == "reject":                  # a known, deliberate Book reject
            rejected_ids.append(eid)
            return
        if k == "cash":                    # posts, no lot/payable effect
            registry[eid] = {"kind": "cash"}
            revable.append(eid)
            return
        if k == "fill":
            side, cid, sym = meta["side"], meta["cid"], meta["sym"]
            key = (cid, sym)
            qd = _D(_qty_str(meta["q"]))
            stats["fills_emitted"] += 1
            if side == "sell" and qd > pos(key):
                # the Book rejects before any mutation (L1) — including
                # sells a reversal / reverse split outran mid-schedule
                stats["oversell" if meta["expect_reject"]
                      else "oversell_accidental"] += 1
                rejected_ids.append(eid)
                return
            _pay_add(cid, meta["broker"], meta["p_c"], meta["rate"], +1)
            if side == "buy":
                arr[0] += 1
                lot = {"arr": arr[0], "qty": qd, "mult": _Fr(1),
                       "cid": cid, "sym": sym}
                holdings.setdefault(key, []).append(lot)
                avail[key] = avail.get(key, _ZQ) + qd
                reg = {"kind": "fill_buy", "lot": lot, "q0": qd}
            else:
                consumes = []
                remaining = qd
                for lot in sorted(holdings.get(key, []),
                                  key=lambda l: l["arr"]):
                    if remaining <= 0:
                        break
                    if lot["qty"] <= 0:
                        continue           # zombie keeps its slot
                    take = min(lot["qty"], remaining)
                    consumes.append((lot, take, lot["mult"]))
                    lot["qty"] = _qdec(lot["qty"] - take)
                    remaining -= take
                reg = {"kind": "fill_sell", "consumes": consumes}
            reg.update({"cid": cid, "tid": meta["tid"], "dup": meta["dup"],
                        "broker": meta["broker"], "p_c": meta["p_c"],
                        "rate": meta["rate"]})
            registry[eid] = reg
            stats["fills_posted"] += 1
            if meta["dup"]:                # posts legs, stores NO trade (D8)
                stats["dup_trade_id"] += 1
                stats["dup_stuck_2350_cents"] += meta["p_c"]
            else:
                trades[meta["tid"]] = False
                revable.append(eid)        # dup fills are never reversed
            return
        if k == "settle":                  # trade_settled
            stats["settles_emitted"] += 1
            if trades.get(meta["tid"]) is False:
                trades[meta["tid"]] = True
                stats["settles_posted"] += 1
            else:                          # early / double / gone / never
                stats["settles_rejected"] += 1
                rejected_ids.append(eid)
            return
        if k == "reinvest":
            cid, sym = meta["cid"], meta["sym"]
            key = (cid, sym)
            qd = _D(_qty_str(meta["q"]))
            arr[0] += 1
            lot = {"arr": arr[0], "qty": qd, "mult": _Fr(1),
                   "cid": cid, "sym": sym}
            holdings.setdefault(key, []).append(lot)
            avail[key] = avail.get(key, _ZQ) + qd
            registry[eid] = {"kind": "reinvest", "lot": lot, "q0": qd,
                             "cid": cid}
            revable.append(eid)
            return
        if k == "split":
            cid, sym = meta["cid"], meta["sym"]
            key = (cid, sym)
            fr, to = meta["r_from"], meta["r_to"]
            to_d, fr_d = _D(str(to)), _D(str(fr))
            ratio = _Fr(to, fr)
            touched = [(lot, lot["qty"]) for lot in holdings.get(key, [])]
            for lot, _prior in touched:
                lot["qty"] = _qdec(lot["qty"] * to_d / fr_d)
                lot["mult"] = lot["mult"] * ratio
            if avail.get(key):
                avail[key] = _qdec(avail[key] * to_d / fr_d)
                _clamp_avail(key)
            registry[eid] = {"kind": "split", "touched": touched,
                             "ratio": ratio, "key": key}
            revable.append(eid)
            return
        if k == "rekey":
            cid, old, new = meta["cid"], meta["old"], meta["new"]
            ko, kn = (cid, old), (cid, new)
            moved = holdings.pop(ko, [])
            for lot in moved:
                lot["sym"] = new
            if moved:
                holdings.setdefault(kn, []).extend(moved)
            freed = avail.pop(ko, None)
            if freed:
                avail[kn] = avail.get(kn, _ZQ) + freed
            registry[eid] = {"kind": "rekey", "cid": cid, "old": old,
                             "new": new, "moved": moved}
            revable.append(eid)
            return
        if k == "settlement":              # one of the four fee settlements
            cid, acct = meta["cid"], meta["acct"]
            stats["stl_emitted"] += 1
            amt = pay.get((cid, acct), 0)
            if amt > 0:
                pay[(cid, acct)] = 0
                registry[eid] = {"kind": "settlement", "cid": cid,
                                 "acct": acct, "amt": amt}
                revable.append(eid)
                stats["stl_posted"] += 1
                stats["stl_cents_settled"] += amt
            else:                          # nothing outstanding (R9)
                stats["stl_rejected_zero"] += 1
                rejected_ids.append(eid)
            return

    def emit(ev: dict, meta=None) -> None:
        out.append(ev)
        track(ev, meta)

    # ---- the reversal engine -------------------------------------------
    def apply_reversal(src_eid: str, rev_eid: str) -> dict | None:
        """Mirror the Book's undo of one posted, unreversed original."""
        info = registry[src_eid]
        reversed_set.add(src_eid)
        reversed_ids.append(src_eid)
        posted_reversals.append(rev_eid)
        stats["rev_valid"] += 1
        k = info["kind"]
        if k == "cash":
            stats["rev_cash"] += 1
            return None
        if k in ("fill_buy", "reinvest"):
            lot = info["lot"]
            rem = lot["qty"]
            lot["qty"] = _qdec(0)          # remainder removed, zombie stays
            lkey = (lot["cid"], lot["sym"])
            if rem > 0:
                avail[lkey] = avail.get(lkey, _ZQ) - rem
                _clamp_avail(lkey)
            if k == "fill_buy":
                _pay_add(info["cid"], info["broker"], info["p_c"],
                         info["rate"], -1)
                _undo_trade(info)
                stats["rev_fill_buy"] += 1
                if rem < info["q0"]:
                    stats["rev_buy_partial"] += 1
            else:
                stats["rev_reinvest"] += 1
            return None
        if k == "fill_sell":
            across = False
            for lot, take, m0 in reversed(info["consumes"]):
                ratio = lot["mult"] / m0
                if ratio != 1:
                    across = True
                before = lot["qty"]
                lot["qty"] = _qdec(lot["qty"] + take
                                   * _D(ratio.numerator)
                                   / _D(ratio.denominator))
                lkey = (lot["cid"], lot["sym"])
                avail[lkey] = avail.get(lkey, _ZQ) + (lot["qty"] - before)
                _clamp_avail(lkey)
            _pay_add(info["cid"], info["broker"], info["p_c"],
                     info["rate"], -1)
            _undo_trade(info)
            stats["rev_fill_sell"] += 1
            if across:
                stats["rev_sell_across_split"] += 1
            return None
        if k == "split":
            for lot, prior in info["touched"]:
                lot["qty"] = prior
                lot["mult"] = lot["mult"] / info["ratio"]
            for lkey in {(lot["cid"], lot["sym"])
                         for lot, _p in info["touched"]}:
                _clamp_avail(lkey)
            stats["rev_split"] += 1
            return None
        if k == "rekey":
            cid, old = info["cid"], info["old"]
            back = _ZQ
            for lot in info["moved"]:
                ck = (cid, lot["sym"])
                lst = holdings.get(ck, [])
                if lot in lst:
                    lst.remove(lot)
                    if not lst:
                        holdings.pop(ck, None)
                _clamp_avail(ck)
                lot["sym"] = old
                holdings.setdefault((cid, old), []).append(lot)
                back += lot["qty"]
            ko = (cid, old)
            avail[ko] = avail.get(ko, _ZQ) + back
            _clamp_avail(ko)
            stats["rev_rekey"] += 1
            return None
        if k == "settlement":              # re-raise the payable (R8)
            pay[(info["cid"], info["acct"])] = \
                pay.get((info["cid"], info["acct"]), 0) + info["amt"]
            stats["rev_settlement"] += 1
            return info
        return None

    def _undo_trade(info) -> None:
        st = trades.get(info["tid"])
        if st is False:                    # unsettled: trade deleted (R6)
            trades.pop(info["tid"])
            stats["rev_unsettled_trade_dropped"] += 1
        elif st is True:                   # settled: stays settled (R7)
            stats["rev_settled_trade_kept"] += 1

    def reversal_slot() -> None:
        stats["rev_slots"] += 1
        roll = rng.random()
        cid = rng.choice(CUSTOMERS)

        def rev_ev(target: str) -> dict:
            return mkev("reversal", {"reverses_event_id": target,
                                     "customer_id": cid})

        edge = ft["rev_unknown_ref"]
        if roll < edge:
            emit(rev_ev(f"evt_{seed}_never_{seqs['e']}"), {"kind": "reject"})
            stats["rev_unknown_ref"] += 1
            stats["rev_emitted"] += 1
            return
        edge += ft["rev_of_rejected"]
        if roll < edge and rejected_ids:
            emit(rev_ev(rng.choice(rejected_ids)), {"kind": "reject"})
            stats["rev_of_rejected"] += 1
            stats["rev_emitted"] += 1
            return
        edge += ft["rev_double"]
        if roll < edge and reversed_ids:
            emit(rev_ev(rng.choice(reversed_ids)), {"kind": "reject"})
            stats["rev_double"] += 1
            stats["rev_emitted"] += 1
            return
        edge += ft["rev_of_reversal"]
        if roll < edge and posted_reversals:
            emit(rev_ev(rng.choice(posted_reversals)), {"kind": "reject"})
            stats["rev_of_reversal"] += 1
            stats["rev_emitted"] += 1
            return
        edge += ft["rev_before_original"]
        if roll < edge:
            # reversal first — rejected forever — original a few later
            orig_eid = _id("e", "evt")
            orig = {"offset": -1, "event_id": orig_eid, "type": "deposit",
                    "payload": {"customer_id": cid,
                                "amount": _amount_2dp(rng)}}
            emit(rev_ev(orig_eid), {"kind": "reject"})
            pending.append((len(out) + rng.randrange(2, 9), orig,
                            {"kind": "cash"}))
            stats["rev_before_original"] += 1
            stats["rev_emitted"] += 1
            return
        if not revable:
            return
        # 35% of valid picks aim at the RECENT tail so freshly-posted fills
        # get reversed BEFORE their T+2 settle lands (the unsettled-fill /
        # trade-deleted path, R6); the rest range over the whole history
        # (partially-consumed lots, across-split sells, old settlements).
        if rng.random() < 0.35 and len(revable) > 40:
            idx = rng.randrange(len(revable) - 40, len(revable))
        else:
            idx = rng.randrange(len(revable))
        src = revable.pop(idx)
        rev = rev_ev(src)
        emit(rev, None)
        stats["rev_emitted"] += 1
        info = apply_reversal(src, rev["event_id"])
        if info is not None and rng.random() < ft["resettle"]:
            # R8 full cycle: settle -> reverse -> re-settle (fresh eid)
            t, broker = SETTLE_EVENT_OF[info["acct"]]
            p = {"customer_id": info["cid"]}
            if broker:
                p["broker"] = broker
            pending.append((len(out) + rng.randrange(2, 8), mkev(t, p),
                            {"kind": "settlement", "cid": info["cid"],
                             "acct": info["acct"]}))
            stats["resettle_after_reversal"] += 1

    def settlement_slot() -> None:
        stats["stl_slots"] += 1
        roll = rng.random()
        if roll < ft["settle_unknown_broker"]:
            emit(mkev("broker_fees_settled",
                      {"customer_id": rng.choice(CUSTOMERS),
                       "broker": "BRK-Z"}), {"kind": "reject"})
            stats["stl_unknown_broker"] += 1
            return
        positives = sorted(k for k, v in pay.items() if v > 0)
        if (roll < ft["settle_unknown_broker"] + ft["settle_zero_payable"]
                or not positives):
            cid, acct = rng.choice(CUSTOMERS), rng.choice(PAYABLE_ACCTS)
            for _ in range(12):            # find a genuinely empty payable
                if pay.get((cid, acct), 0) <= 0:
                    break
                cid, acct = rng.choice(CUSTOMERS), rng.choice(PAYABLE_ACCTS)
            stats["stl_zero_payable_trap"] += 1
        else:
            cid, acct = rng.choice(positives)
        t, broker = SETTLE_EVENT_OF[acct]
        p = {"customer_id": cid}
        if broker:
            p["broker"] = broker
        emit(mkev(t, p), {"kind": "settlement", "cid": cid, "acct": acct})

    # ---- order machinery (generate_corporate's, on the Decimal mirror) --
    def _placement(o_id, cid, side, symbol, q_micro, limit_c) -> dict:
        p = {"order_id": o_id, "customer_id": cid, "side": side,
             "symbol": symbol, "quantity": _qty_str(q_micro),
             "limit_price": _cents_str(limit_c),
             "asset_class": sym_class[symbol]}
        field = "est_commission" if rng.random() < 0.10 else "est_charges"
        p[field] = _cents_str(rng.randrange(0, 2_001))
        return p

    def build_fill(o_id, cid, side, symbol, limit_c, q_micro, final):
        cls = sym_class[symbol]
        broker = rng.choice(CLASS_BROKERS[cls])
        price_c = max(1, limit_c * rng.randrange(9_800, 10_201) // 10_000)
        p_c = max(1, (q_micro * price_c + 500_000) // 1_000_000)
        dup = (side == "buy" and bool(trades)
               and rng.random() < tr["dup_trade_id"])
        t_id = rng.choice(sorted(trades)) if dup else _id("t", "trd")
        rate = rng.choice(PARTNER_RATES)
        payload = {"order_id": o_id, "trade_id": t_id, "customer_id": cid,
                   "side": side, "symbol": symbol,
                   "quantity": _qty_str(q_micro),
                   "price": _cents_str(price_c),
                   "principal": _cents_str(p_c), "broker": broker,
                   "asset_class": cls, "partner_rate": rate}
        ev = mkev("order_filled" if final else "order_partially_filled",
                  payload)
        meta = {"kind": "fill", "side": side, "cid": cid, "sym": symbol,
                "q": q_micro, "tid": t_id, "dup": dup,
                "expect_reject": False, "p_c": p_c, "broker": broker,
                "rate": rate}
        return ev, meta, t_id, dup

    def _chunks(total: int, parts: int) -> list[int]:
        if parts <= 1 or total <= parts:
            return [total]
        cuts = sorted(rng.sample(range(1, total), parts - 1))
        return [b - a for a, b in zip([0] + cuts, cuts + [total])]

    def lifecycle(now: int, force_key=None) -> None:
        if force_key is not None:
            if avail.get(force_key, _ZQ) <= 0:
                return
            side, (cid, symbol) = "sell", force_key
        else:
            sellable = sorted(k for k, v in avail.items() if v > 0)
            side = "sell" if sellable and rng.random() < 0.42 else "buy"
            if side == "sell":
                cid, symbol = rng.choice(sellable)
            else:
                cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        if side == "sell":
            have = avail[(cid, symbol)]
            have_micro = int(have.scaleb(6))
            if have_micro < 1:
                return
            if rng.random() < 0.10:
                q_ord = have_micro         # L2: sell exactly the position
            else:
                q_ord = rng.randrange(1, have_micro + 1)
                if q_ord > 1_000_000 and rng.random() < 0.60:
                    q_ord -= q_ord % 1_000_000   # mostly whole shares
            avail[(cid, symbol)] = have - _D(_qty_str(q_ord))
        else:
            q_ord = (rng.randrange(1_000_000, 300_000_001)  # 6dp fractional
                     if rng.random() < 0.15
                     else rng.randrange(1, 301) * 1_000_000)
        limit_c = rng.randrange(200, 40_001)     # $2.00 .. $400.00
        o_id = _id("o", "ord")
        stats["placements"] += 1
        n_partials = rng.choice((0, 0, 1, 1, 2, 2, 3))
        f_roll = rng.random()
        final = ("filled" if f_roll < 0.78
                 else "cancelled" if f_roll < 0.92 else "rejected")
        if final == "filled":
            chunks = _chunks(q_ord, n_partials + 1)
        elif n_partials == 0:
            chunks = []
        else:                              # cancel path fills only part
            part = q_ord * rng.randrange(10, 81) // 100
            chunks = _chunks(part, n_partials) if part >= 1 else []

        timeline = [(0, mkev("order_placed",
                             _placement(o_id, cid, side, symbol, q_ord,
                                        limit_c)), None)]
        rel = 0
        for i, q in enumerate(chunks):
            is_final = final == "filled" and i == len(chunks) - 1
            rel += rng.randrange(1, 6)
            ev, meta, t_id, dup = build_fill(o_id, cid, side, symbol,
                                             limit_c, q, is_final)
            if not dup and rng.random() < tr["settle_before_fill"]:
                stats["settle_before_fill"] += 1     # S6: rejected forever
                timeline.append((max(1, rel - rng.randrange(1, 6)),
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 {"kind": "settle", "tid": t_id}))
            timeline.append((rel, ev, meta))
            if not dup:                    # dup fills store no trade (D8)
                srel = rel + rng.randrange(3, 41)
                timeline.append((srel,
                                 mkev("trade_settled", {"trade_id": t_id}),
                                 {"kind": "settle", "tid": t_id}))
                if rng.random() < tr["double_settle"]:
                    stats["double_settle"] += 1
                    timeline.append((srel + rng.randrange(1, 10),
                                     mkev("trade_settled",
                                          {"trade_id": t_id}),
                                     {"kind": "settle", "tid": t_id}))
        if final != "filled":
            rel += rng.randrange(1, 6)
            timeline.append((rel,
                             mkev("order_cancelled" if final == "cancelled"
                                  else "order_rejected",
                                  {"order_id": o_id}), None))
        emit(timeline[0][1], timeline[0][2])
        for rel_i, ev_i, meta_i in timeline[1:]:
            pending.append((now + max(1, rel_i), ev_i, meta_i))

    def _sched_buy(symbol: str, cid: str, at: int) -> None:
        o_id = _id("o", "ord")
        q = rng.randrange(1, 51) * 1_000_000
        limit_c = rng.randrange(200, 40_001)
        stats["placements"] += 1
        pending.append((at, mkev("order_placed",
                                 _placement(o_id, cid, "buy", symbol, q,
                                            limit_c)), None))
        ev, meta, t_id, dup = build_fill(o_id, cid, "buy", symbol,
                                         limit_c, q, True)
        frel = at + rng.randrange(1, 4)
        pending.append((frel, ev, meta))
        if not dup:
            pending.append((frel + rng.randrange(3, 41),
                            mkev("trade_settled", {"trade_id": t_id}),
                            {"kind": "settle", "tid": t_id}))

    # ---- corporate + cash emitters (corporate's, meta-fied) ------------
    def dividend_cash() -> None:
        held = held_keys()
        if rng.random() < ct["phantom_dividend"] or not held:
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            for _ in range(8):
                if pos((cid, symbol)) == 0:
                    break
                cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            stats["phantom_dividend"] += 1
        else:
            cid, symbol = rng.choice(sorted(held))
        gross_c = rng.randrange(100, 50_001)
        tax_c = gross_c * rng.randrange(0, 31) // 100
        net_c = gross_c - tax_c
        if rng.random() < ct["d2_mismatch"]:
            net_c = max(1, net_c + rng.choice((-1, 1))
                        * rng.randrange(1, 200))
            if net_c == gross_c - tax_c:
                net_c += 1
            stats["d2_mismatch"] += 1
        emit(mkev("dividend_cash",
                  {"customer_id": cid, "symbol": symbol,
                   "gross_amount": _cents_str(gross_c),
                   "withholding_tax": _cents_str(tax_c),
                   "net_amount": _cents_str(net_c)}), {"kind": "cash"})
        stats["dividend_cash"] += 1

    def dividend_reinvested() -> None:
        held = held_keys()
        if held and rng.random() < 0.85:
            cid, symbol = rng.choice(sorted(held))
        else:
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        q = rng.randrange(50_000, 5_000_001)     # 0.05 .. 5 shares, 6 dp
        if rng.random() < 0.30:
            q = rng.randrange(1, 6) * 1_000_000
        price_c = rng.randrange(200, 40_001)
        exact_c = (q * price_c + 500_000) // 1_000_000
        net_c = exact_c
        if rng.random() < ct["d7_mismatch"]:
            net_c = max(1, net_c + rng.choice((-1, 1))
                        * rng.randrange(2, 300))
            if abs(net_c - exact_c) <= 1:
                net_c = exact_c + 2
            stats["d7_mismatch"] += 1
        tax_c = rng.randrange(0, net_c // 4 + 1)
        emit(mkev("dividend_reinvested",
                  {"customer_id": cid, "symbol": symbol,
                   "gross_amount": _cents_str(net_c + tax_c),
                   "withholding_tax": _cents_str(tax_c),
                   "net_amount": _cents_str(net_c),
                   "reinvest_price": _cents_str(price_c),
                   "reinvest_quantity": _qty_str(q)}),
             {"kind": "reinvest", "cid": cid, "sym": symbol, "q": q})
        stats["dividend_reinvested"] += 1

    def stock_split() -> None:
        held = held_keys()
        r_from, r_to = SPLIT_RATIOS[rng.randrange(len(SPLIT_RATIOS))]
        if rng.random() < ct["split_zero_position"] or not held:
            cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            for _ in range(8):
                if pos((cid, symbol)) == 0:
                    break
                cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            stats["split_zero_position"] += 1
        else:
            cid, symbol = rng.choice(sorted(held))
        emit(mkev("stock_split",
                  {"customer_id": cid, "symbol": symbol,
                   "ratio_from": str(r_from), "ratio_to": str(r_to)}),
             {"kind": "split", "cid": cid, "sym": symbol,
              "r_from": r_from, "r_to": r_to})
        stats["stock_split"] += 1
        if (avail.get((cid, symbol), _ZQ) > 0
                and rng.random() < ct["split_then_sell"]):
            stats["split_then_sell"] += 1
            lifecycle(len(out), force_key=(cid, symbol))

    def symbol_change() -> None:
        held = held_keys()
        roll = rng.random()
        if roll < ct["rename_zero_position"] or not held:
            cid, old = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            for _ in range(8):
                if pos((cid, old)) == 0:
                    break
                cid, old = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
            new = rng.choice([s for s in SYMBOLS if s != old])
            stats["rename_zero_position"] += 1
            emit(mkev("symbol_change",
                      {"customer_id": cid, "old_symbol": old,
                       "new_symbol": new}),
                 {"kind": "rekey", "cid": cid, "old": old, "new": new})
            stats["symbol_change"] += 1
            return
        cid, old = rng.choice(sorted(held))
        if roll < ct["rename_zero_position"] + ct["rename_chain"]:
            seqs["s"] += 1
            mid = f"FINCO-{seed}-{seqs['s']}A"
            fin = f"FINCO-{seed}-{seqs['s']}B"
            sym_class[mid] = sym_class[fin] = sym_class[old]
            emit(mkev("symbol_change",
                      {"customer_id": cid, "old_symbol": old,
                       "new_symbol": mid}),
                 {"kind": "rekey", "cid": cid, "old": old, "new": mid})
            stats["symbol_change"] += 2
            stats["rename_chain"] += 1
            hop2 = len(out) + rng.randrange(2, 7)
            pending.append((hop2,
                            mkev("symbol_change",
                                 {"customer_id": cid, "old_symbol": mid,
                                  "new_symbol": fin}),
                            {"kind": "rekey", "cid": cid, "old": mid,
                             "new": fin}))
            if rng.random() < ct["rename_then_trade"]:
                stats["rename_then_trade"] += 1
                _sched_buy(fin, cid, hop2 + rng.randrange(1, 5))
            return
        mine = sorted(s for (c, s) in holdings
                      if c == cid and s != old and pos((c, s)) > 0)
        if mine and rng.random() < 0.5:
            new = rng.choice(mine)
        else:
            new = rng.choice([s for s in SYMBOLS if s != old])
        if pos((cid, new)) > 0:
            stats["rename_merge_collision"] += 1
        emit(mkev("symbol_change",
                  {"customer_id": cid, "old_symbol": old,
                   "new_symbol": new}),
             {"kind": "rekey", "cid": cid, "old": old, "new": new})
        stats["symbol_change"] += 1
        if rng.random() < ct["rename_then_trade"]:
            stats["rename_then_trade"] += 1
            _sched_buy(new, cid, len(out) + rng.randrange(1, 5))

    def cash_misc() -> None:
        cid = rng.choice(CUSTOMERS)
        roll = rng.random()
        if roll < 0.40:
            emit(mkev("fee_charged",
                      {"customer_id": cid, "amount": _amount_2dp(rng)}),
                 {"kind": "cash"})
        elif roll < 0.70:
            emit(mkev("transfer_between_customers",
                      {"from_customer_id": cid,
                       "to_customer_id": rng.choice(CUSTOMERS),
                       "amount": _amount_2dp(rng)}), {"kind": "cash"})
        else:
            gross_c = rng.randrange(2, 10_000)
            emit(mkev("interest_credited",
                      {"customer_id": cid,
                       "gross_amount": _cents_str(gross_c),
                       "customer_share":
                           _cents_str(rng.randrange(0, gross_c + 1))}),
                 {"kind": "cash"})

    def standalone_oversell() -> None:
        held = held_keys()
        cid, symbol = (rng.choice(sorted(held)) if held
                       else (rng.choice(CUSTOMERS), rng.choice(SYMBOLS)))
        q = int(pos((cid, symbol)).scaleb(6)) \
            + rng.randrange(1, 501) * 1_000_000
        cls = sym_class[symbol]
        price_c = rng.randrange(200, 40_001)
        p_c = max(1, (q * price_c + 500_000) // 1_000_000)
        t_id = _id("t", "trd")
        broker = rng.choice(CLASS_BROKERS[cls])
        rate = rng.choice(PARTNER_RATES)
        emit(mkev("order_filled",
                  {"order_id": _id("o", "ord"), "trade_id": t_id,
                   "customer_id": cid, "side": "sell", "symbol": symbol,
                   "quantity": _qty_str(q), "price": _cents_str(price_c),
                   "principal": _cents_str(p_c), "broker": broker,
                   "asset_class": cls, "partner_rate": rate}),
             {"kind": "fill", "side": "sell", "cid": cid, "sym": symbol,
              "q": q, "tid": t_id, "dup": False, "expect_reject": True,
              "p_c": p_c, "broker": broker, "rate": rate})

    def malformed_order() -> None:
        nonlocal bad_kind
        cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        cls = sym_class[symbol]
        base = {"order_id": _id("o", "ord"), "trade_id": _id("t", "trd"),
                "customer_id": cid, "side": rng.choice(("buy", "sell")),
                "symbol": symbol, "quantity": "5", "price": "10.00",
                "principal": "50.00",
                "broker": rng.choice(CLASS_BROKERS[cls]),
                "asset_class": cls, "partner_rate": "0.5"}
        k = bad_kind % 5
        bad_kind += 1
        if k == 0:
            del base["broker"]
            ev = mkev("order_filled", base)
        elif k == 1:
            del base["trade_id"]
            ev = mkev("order_partially_filled", base)
        elif k == 2:
            base["quantity"] = "-5"
            ev = mkev("order_filled", base)
        elif k == 3:
            base["principal"] = "0.00"
            ev = mkev("order_filled", base)
        else:
            ev = mkev("order_placed", "this is not a payload object")
        emit(ev, {"kind": "reject"} if k != 4 else {"kind": "reject"})
        stats["malformed"] += 1

    def corp_malformed() -> None:
        nonlocal corp_bad
        cid, symbol = rng.choice(CUSTOMERS), rng.choice(SYMBOLS)
        k = corp_bad % 8
        corp_bad += 1
        if k == 0:
            ev = mkev("dividend_cash",
                      {"customer_id": cid, "symbol": symbol,
                       "gross_amount": "10.00", "withholding_tax": "1.50"})
        elif k == 1:
            ev = mkev("dividend_cash",
                      {"customer_id": cid, "symbol": symbol,
                       "gross_amount": "10.00", "withholding_tax": "1.00",
                       "net_amount": "-9.00"})
        elif k == 2:
            ev = mkev("dividend_reinvested",
                      {"customer_id": cid, "symbol": symbol,
                       "net_amount": "20.00", "reinvest_price": "10.00",
                       "reinvest_quantity": "-2"})
        elif k == 3:
            ev = mkev("dividend_reinvested",
                      {"customer_id": cid, "symbol": symbol,
                       "reinvest_price": "10.00", "reinvest_quantity": "2"})
        elif k == 4:
            ev = mkev("stock_split",
                      {"customer_id": cid, "symbol": symbol,
                       "ratio_from": "0", "ratio_to": "2"})
        elif k == 5:
            ev = mkev("stock_split",
                      {"customer_id": cid, "symbol": symbol,
                       "ratio_from": "1"})
        elif k == 6:
            ev = mkev("stock_split",
                      {"customer_id": cid, "symbol": symbol,
                       "ratio_from": "1", "ratio_to": "three"})
        else:
            ev = mkev("symbol_change",
                      {"customer_id": cid, "old_symbol": symbol,
                       "new_symbol": ""})
        emit(ev, {"kind": "reject"})
        stats["corp_malformed"] += 1

    # ---- the feed: fund wallets first, then the woven main body --------
    for _ in range(min(60, max(1, n // 8))):
        emit(mkev("deposit",
                  {"customer_id": rng.choice(CUSTOMERS),
                   "amount": _cents_str(rng.randrange(100_000, 10_000_001))}),
             {"kind": "cash"})
        stats["deposits"] += 1

    while len(out) < n:
        for item in [x for x in pending if x[0] <= len(out)]:
            pending.remove(item)
            emit(item[1], item[2])
        r = rng.random()
        if r < 0.10:
            emit(mkev(rng.choice(UNKNOWN_TYPES),
                      {"customer_id": rng.choice(CUSTOMERS),
                       "note": "no-op"}))
            stats["unknown"] += 1
        elif r < 0.22:
            emit(mkev("deposit", {"customer_id": rng.choice(CUSTOMERS),
                                  "amount": _amount_2dp(rng)}),
                 {"kind": "cash"})
            stats["deposits"] += 1
        elif r < 0.29:
            cash_misc()
            stats["cash_misc"] += 1
        elif r < 0.53:
            c = rng.random()
            if c < 0.30:
                dividend_cash()
            elif c < 0.55:
                dividend_reinvested()
            elif c < 0.80:
                stock_split()
            else:
                symbol_change()
        elif r < 0.69:
            reversal_slot()
        elif r < 0.79:
            settlement_slot()
        elif r < 0.79 + tr["oversell"]:
            standalone_oversell()
        elif r < 0.79 + tr["oversell"] + 0.01:
            emit(mkev("order_cancelled",
                      {"order_id": f"ord_{seed}_ghost_{seqs['e']}"}))
            stats["unknown_oid_cancel"] += 1
        elif r < 0.79 + tr["oversell"] + 0.01 + tr["malformed"]:
            malformed_order()
        elif r < (0.79 + tr["oversell"] + 0.01 + tr["malformed"]
                  + ct["corp_malformed"]):
            corp_malformed()
        else:
            lifecycle(len(out))

    # ---- settle_all: drain exactly the posted-and-unsettled, unreversed
    # trades the mirror knows the Book is still holding open --------------
    if settle_all:
        for t_id, done in list(trades.items()):
            if not done:
                emit(mkev("trade_settled", {"trade_id": t_id}),
                     {"kind": "settle", "tid": t_id})
                stats["settle_all_emitted"] += 1

    for i, ev in enumerate(out):           # stream positions are sequential
        ev["offset"] = i
    stats["events"] = len(out)
    stats["pending_dropped"] = len(pending)
    stats["trades_posted"] = len(trades)
    stats["reversal_share_pct"] = round(
        100.0 * stats["rev_emitted"] / len(out), 1)
    stats["settlement_share_pct"] = round(
        100.0 * (stats["stl_emitted"] + stats["stl_unknown_broker"])
        / len(out), 1)
    FINALE_LAST_STATS.clear()
    FINALE_LAST_STATS.update(stats)
    return out


def generate_finale_stats(seed: int, n: int,
                          settle_all: bool = True) -> dict:
    """Companion probe: run generate_finale and return a copy of its
    FINALE_LAST_STATS without touching the feed itself."""
    generate_finale(seed, n, settle_all)
    return dict(FINALE_LAST_STATS)


# ------------------------------------------------------------------ #
#  Phase 6: checkpoint requests + as-of targets                      #
# ------------------------------------------------------------------ #
# checkpoint_request is NOT a ledger event. The Book has no on_ handler
# for it, so book.apply would file it under `todo` and log it as a real
# stream position. The live client never does that — client.py routes on
# `type == "checkpoint_request"` BEFORE handle(), and its --resume path
# skips them too — so deliver() mirrors the client exactly: checkpoint
# requests are never applied, they are recorded into the caller's
# `checkpoints` list instead (see deliver()). Nothing about them reaches
# book.apply, book.todo or book.event_log, so every referee sees the same
# book it would have seen without them and as-of positions are unshifted.
#
# This is a thin WRAPPER over generate_finale rather than a change to it:
# generate_finale is the Phase 5 gate's feed and its FINALE_LAST_STATS are
# read against fixed seeds, so splicing checkpoints in from the outside
# keeps that feed byte-identical while this layer owns all Phase 6 needs.

CP_EVERY = 300            # one checkpoint_request per ~300 stream events
CP_ASOF_RATE = 0.5        # half of them carry an as_of_event_id
CP_DUP_EVERY = 350        # one verbatim redelivery spliced in per ~350
CP_BACKDATE_EVERY = 80    # one backdated_days carrier per ~80 CASH events
CP_RESPOND_SECONDS = 60   # the arena's checkpoint grace period

# The four as-of target classes, cycled so every checkpoint feed covers
# all of them: a normal posted event, an event the Book rejected, an event
# id delivered more than once (C2: resolves to its FIRST delivery), and an
# event sitting next to one carrying backdated_days (C4).
CP_TARGET_KINDS = ("normal", "rejected", "duplicated", "backdated_adjacent")

# Same observability side-channel pattern as the other generators.
CP_LAST_STATS: dict = {}

# Types generate_finale only ever emits well-formed: safe "normal posted
# event" as-of targets, and the carrier for the backdated_days field.
CP_POSTING_TYPES = ("deposit", "fee_charged")


def _cp_is_malformed(ev: dict) -> bool:
    """True for the events generate_finale emits deliberately broken (the
    malformed_order / corp_malformed cycles and unknown-ref reversals) —
    the Book rejects every one of them, and an as-of naming one must still
    answer (C2: processed through it, state unchanged by it)."""
    p = ev["payload"]
    if not isinstance(p, dict):
        return True                        # wrong-type payload
    t = ev["type"]
    if t in ("order_filled", "order_partially_filled"):
        return ("broker" not in p or "trade_id" not in p
                or str(p.get("quantity", "")).startswith("-")
                or p.get("principal") == "0.00")
    if t in ("dividend_cash", "dividend_reinvested"):
        return ("net_amount" not in p
                or str(p["net_amount"]).startswith("-")
                or str(p.get("reinvest_quantity", "1")).startswith("-"))
    if t == "stock_split":
        return not (str(p.get("ratio_from", "")).isdigit()
                    and str(p.get("ratio_to", "")).isdigit()
                    and int(p["ratio_from"]) > 0 and int(p["ratio_to"]) > 0)
    if t == "symbol_change":
        return not p.get("new_symbol")
    if t == "reversal":
        return "_never_" in str(p.get("reverses_event_id", ""))
    return False


def generate_finale_cp(seed: int, n: int,
                       settle_all: bool = True) -> list[dict]:
    """generate_finale's feed with the Phase 6 checkpoint layer spliced in,
    deterministic in (seed, n, settle_all).

    Three splices, all as NEW stream positions (never an edit in place,
    except the backdated_days field described below):

      * `checkpoint_request` every ~CP_EVERY events, payload
        {checkpoint_id, respond_within_seconds: 60} and, for ~half of them,
        an `as_of_event_id` naming an ALREADY-EMITTED event, cycling
        through CP_TARGET_KINDS so normal / rejected / duplicated /
        backdated-adjacent targets all appear in every feed;
      * verbatim duplicates of earlier events every ~CP_DUP_EVERY (the
        duplicated-id target pool — the Book must resolve an as-of on one
        to its FIRST delivery position, C2);
      * `backdated_days` on every ~CP_BACKDATE_EVERY-th cash event (a legal
        payload field the Book ignores — it posts in delivery order, C4);
        the very next event emitted after one is the "adjacent" target.

    Ids: checkpoints are evt_<seed>_cp<k> (disjoint from generate_finale's
    evt_<seed>_f<k>). Offsets are renumbered 0..len-1. Counts land in
    CP_LAST_STATS (see generate_finale_cp_stats).
    """
    base = generate_finale(seed, n, settle_all)
    rng = random.Random(seed ^ 0xCB0177)   # decorrelate from every stage
    out: list[dict] = []
    normal: list[str] = []                 # well-formed, posted event ids
    rejected: list[str] = []               # deliberately-broken event ids
    duplicated: list[str] = []             # ids delivered more than once
    adjacent: list[str] = []               # ids next to a backdated event
    stats = {k: 0 for k in (
        "checkpoints", "checkpoints_with_asof", "backdated", "duplicates",
        "target_normal", "target_rejected", "target_duplicated",
        "target_backdated_adjacent", "target_missing")}
    cp_k = 0
    kind_k = 0
    await_adjacent = False

    for i, ev in enumerate(base):
        # -- backdated_days on a deposit (copy: never mutate base's dict) --
        if (ev["type"] in CP_POSTING_TYPES and isinstance(ev["payload"], dict)
                and rng.random() < 1.0 / CP_BACKDATE_EVERY):
            ev = {**ev, "payload": {**ev["payload"],
                                    "backdated_days": rng.randrange(1, 30)}}
            stats["backdated"] += 1
            await_adjacent = True
        elif await_adjacent:
            adjacent.append(ev["event_id"])
            await_adjacent = False
        out.append(ev)
        if ev["type"] in CP_POSTING_TYPES and isinstance(ev["payload"], dict):
            normal.append(ev["event_id"])
        elif _cp_is_malformed(ev):
            rejected.append(ev["event_id"])

        # -- a verbatim redelivery: the duplicated-id target pool ----------
        if out and rng.random() < 1.0 / CP_DUP_EVERY:
            dup = rng.choice(out)
            if dup["type"] != "checkpoint_request":
                out.append(dict(dup))      # same event_id, same payload
                duplicated.append(dup["event_id"])
                stats["duplicates"] += 1

        # -- the checkpoint itself -----------------------------------------
        if rng.random() < 1.0 / CP_EVERY:
            cp_k += 1
            payload = {"checkpoint_id": f"cp_{seed}_{cp_k}",
                       "respond_within_seconds": CP_RESPOND_SECONDS}
            if rng.random() < CP_ASOF_RATE:
                kind = CP_TARGET_KINDS[kind_k % len(CP_TARGET_KINDS)]
                kind_k += 1
                pool = {"normal": normal, "rejected": rejected,
                        "duplicated": duplicated,
                        "backdated_adjacent": adjacent}[kind]
                if not pool:               # nothing of that class yet
                    pool, kind = normal, "normal"
                if pool:
                    payload["as_of_event_id"] = rng.choice(pool)
                    stats[f"target_{kind}"] += 1
                    stats["checkpoints_with_asof"] += 1
                else:
                    stats["target_missing"] += 1
            out.append({"offset": -1, "event_id": f"evt_{seed}_cp{cp_k}",
                        "type": "checkpoint_request", "payload": payload})
            stats["checkpoints"] += 1

    for i, ev in enumerate(out):           # stream positions are sequential
        ev["offset"] = i
    stats["events"] = len(out)
    stats["base_events"] = len(base)
    CP_LAST_STATS.clear()
    CP_LAST_STATS.update(stats)
    return out


def generate_finale_cp_stats(seed: int, n: int,
                             settle_all: bool = True) -> dict:
    """Companion probe: run generate_finale_cp and return a copy of its
    CP_LAST_STATS without touching the feed itself."""
    generate_finale_cp(seed, n, settle_all)
    return dict(CP_LAST_STATS)


def checkpoint_targets(events: list[dict]) -> list[str]:
    """Every as_of_event_id named by a checkpoint_request in `events`, in
    stream order — the ids an oracle must be able to answer."""
    return [ev["payload"]["as_of_event_id"] for ev in events
            if ev["type"] == "checkpoint_request"
            and isinstance(ev["payload"], dict)
            and "as_of_event_id" in ev["payload"]]


# ------------------------------------------------------------------ #
#  delivery                                                          #
# ------------------------------------------------------------------ #

REWIND_SPAN = 300   # how far back a rewind-replay reaches


def deliver(book, events: list[dict], seed: int,
            dup_rate: float = 0.05,
            rewind_at: list[int] | None = None,
            checkpoints: list | None = None,
            observer=None) -> list[dict]:
    """Feed `events` to book.apply the way the arena actually would.

    Chaos on top of the feed itself:
      * at dup_rate, a random already-sent event is re-sent before the next
        new one (point redelivery);
      * at each index in rewind_at, a contiguous slice of up to REWIND_SPAN
        already-sent events is replayed in order (an SSE reconnect from an
        earlier offset).

    `checkpoint_request` events are NEVER applied — the Book has no handler
    and the live client routes them to checkpoint() instead of handle().
    They are appended to `checkpoints` (when given) as
    {'stream_index', 'log_len', 'event'}, where log_len is the number of
    first deliveries already in book.event_log — i.e. the log position the
    checkpoint's answer describes. They are also excluded from the
    redelivery pool, so no referee ever sees one.

    `observer`, when given, is called as observer(book, log_idx, ev)
    immediately after every FIRST delivery — log_idx being that event's
    index in book.event_log. That is the only moment at which the live
    state for as-of index log_idx exists, so this is where an oracle store
    records it.

    Returns the submissions list exactly as a client would send it — one
    {'event_id', 'legs'} per delivery, in delivery order, including the
    legs:[] entries for duplicates, rejects, and unknown types.
    """
    rng = random.Random(seed ^ 0xD15EA5E)  # decorrelate from the other stages
    rewinds = set(rewind_at or ())
    sent: list[dict] = []
    subs: list[dict] = []

    def send(ev: dict) -> None:
        before = len(book.event_log)
        legs = book.apply(ev)
        subs.append({"event_id": ev["event_id"], "legs": legs})
        if observer is not None and len(book.event_log) > before:
            observer(book, before, ev)

    for i, ev in enumerate(events):
        if ev["type"] == "checkpoint_request":
            if checkpoints is not None:
                checkpoints.append({"stream_index": i,
                                    "log_len": len(book.event_log),
                                    "event": ev})
            continue                       # never a ledger event
        if i in rewinds and sent:
            start = max(0, len(sent) - REWIND_SPAN)
            for old in sent[start:]:
                send(old)
        if sent and rng.random() < dup_rate:
            send(rng.choice(sent))
        send(ev)
        sent.append(ev)
    return subs


# ------------------------------------------------------------------ #
#  Phase 7: planted defects (D1-D11) and the clean-mode feed          #
# ------------------------------------------------------------------ #
# Both of these are WRAPPERS over generate_finale, never edits to it:
# generate_finale is the Phase 5 gate's feed, read against fixed seeds,
# and it must stay byte-identical. Everything below post-processes a copy
# (copy-on-write per event: `{**ev, "payload": {...}}`, exactly the
# pattern generate_finale_cp already uses).
#
#   generate_defective  — plants known D1-D11 defects at a configurable
#                         rate, and reports exactly which event ids carry
#                         which defect. The detector suites measure their
#                         catch rate against that ground truth.
#   generate_clean      — every STREAM trap on (duplicates, malformed,
#                         oversells, fill-before-placement, backdating,
#                         out-of-order settles, reversal traps) and ZERO
#                         defects: the false-positive gate. A detector
#                         that fires here is firing on clean data.
#
# Cents and millionths are integers throughout — no float, no Decimal —
# so a "repaired" amount is bit-exact against the Book's own rounding.

DEFECT_CLASSES = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8",
                  "D9", "D10", "D11")

# Per-class rng salts: switching one class off must not reshuffle another
# class's draws, so every class owns its own generator.
_DEFECT_SALT = {d: (0xDEFEC7 ^ ((i + 1) * 0x9E3779B1)) & 0x7FFFFFFF
                for i, d in enumerate(DEFECT_CLASSES)}

_FILL_TYPES = ("order_filled", "order_partially_filled")

# The one broker each asset class may NOT trade (coverage is equity: A,B ·
# etf: A,C · bond: B,C). A table lookup, which is exactly why D1 arms.
_NON_COVERING = {"equity": "BRK-C", "etf": "BRK-B", "bond": "BRK-A"}

_FILL_FIELDS = ("order_id", "trade_id", "customer_id", "side", "symbol",
                "quantity", "price", "principal", "broker", "asset_class",
                "partner_rate")


def _parse_cents(s) -> int:
    """'1234.56' -> 123456. The generators only ever emit canonical 2 dp
    money strings, so this is exact and float-free."""
    txt = str(s)
    neg = txt.startswith("-")
    if neg:
        txt = txt[1:]
    whole, _dot, frac = txt.partition(".")
    frac = (frac + "00")[:2]
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"not a money string: {s!r}")
    c = int(whole) * 100 + int(frac)
    return -c if neg else c


def _parse_micro(s) -> int:
    """'8.5' -> 8500000. The inverse of _qty_str."""
    txt = str(s)
    neg = txt.startswith("-")
    if neg:
        txt = txt[1:]
    whole, _dot, frac = txt.partition(".")
    frac = (frac + "000000")[:6]
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"not a quantity string: {s!r}")
    q = int(whole) * 1_000_000 + int(frac)
    return -q if neg else q


def _exact_principal_c(q_micro: int, price_c: int) -> int:
    """money(quantity x price) in cents, half away from zero — the same
    arithmetic tariff.money does, done in integers."""
    return (q_micro * price_c + 500_000) // 1_000_000


def _fill_ok(p) -> bool:
    """A well-formed fill payload: every field present, positive amounts.
    Deliberately-malformed fills are never used as defect carriers — a
    defect must be the ONLY thing wrong with the event it rides on."""
    if not isinstance(p, dict) or any(k not in p for k in _FILL_FIELDS):
        return False
    if p["side"] not in ("buy", "sell"):
        return False
    try:
        return (_parse_micro(p["quantity"]) > 0
                and _parse_cents(p["price"]) > 0
                and _parse_cents(p["principal"]) > 0)
    except ValueError:
        return False


def _div_cash_ok(p) -> bool:
    if not isinstance(p, dict):
        return False
    if any(k not in p for k in ("gross_amount", "withholding_tax",
                                "net_amount")):
        return False
    try:
        g, t, net = (_parse_cents(p["gross_amount"]),
                     _parse_cents(p["withholding_tax"]),
                     _parse_cents(p["net_amount"]))
    except ValueError:
        return False
    return g > 0 and 0 <= t <= g and net > 0


def _reinvest_ok(p) -> bool:
    if not isinstance(p, dict):
        return False
    if any(k not in p for k in ("net_amount", "reinvest_price",
                                "reinvest_quantity")):
        return False
    try:
        return (_parse_cents(p["net_amount"]) > 0
                and _parse_cents(p["reinvest_price"]) > 0
                and _parse_micro(p["reinvest_quantity"]) > 0)
    except ValueError:
        return False


def _interest_ok(p) -> bool:
    if not isinstance(p, dict):
        return False
    if "gross_amount" not in p or "customer_share" not in p:
        return False
    try:
        return _parse_cents(p["gross_amount"]) > 0
    except ValueError:
        return False


def _placements(events: list[dict]) -> dict:
    """order_id -> the FIRST-delivered placement's limit / quantity / side,
    which is the only placement D4 and D9 are ever allowed to compare
    against (the Book keeps the first one too)."""
    out: dict = {}
    for ev in events:
        p = ev["payload"]
        if ev["type"] != "order_placed" or not isinstance(p, dict):
            continue
        oid = p.get("order_id")
        if not isinstance(oid, str) or oid in out:
            continue
        try:
            out[oid] = {"limit_c": _parse_cents(p["limit_price"]),
                        "qty": _parse_micro(p["quantity"]),
                        "side": p.get("side"), "symbol": p.get("symbol"),
                        "cid": p.get("customer_id"),
                        "cls": p.get("asset_class")}
        except (KeyError, ValueError):
            continue
    return out


# Order matters only when two classes want the same carrier event: the
# first one in this list wins it and the others record a collision. Armed
# classes go first so an armed/observe mixed run never starves them.
_INPLACE_ORDER = ("D1", "D3", "D2", "D7", "D10", "D4", "D5")

DEFECT_LAST_STATS: dict = {}


def generate_defective(seed: int, n: int, classes=None, rate: float = 0.015,
                       settle_all: bool = True) -> list[dict]:
    """generate_finale's feed with known D1-D11 defects planted in it,
    deterministic in (seed, n, classes, rate, settle_all).

    `classes` selects which defect classes are armed for injection (None =
    all eleven); `rate` is the per-carrier probability, default 1.5 %.
    Every planted event id is recorded in DEFECT_LAST_STATS["ids"][det],
    which is the ground truth the detector suites score against.

    Two injection shapes, both leaving the rest of the feed alone:

      * IN PLACE (D1-D5, D7, D10) — one field of a well-formed carrier is
        rewritten so exactly one predicate fires. Deliberately-malformed
        events are never carriers: a planted defect must be the only thing
        wrong with the event it rides on, or the measurement is worthless.
      * SPLICED (D6, D8, D9, D11) — the finale feed contains no fx_deposit
        and no withdrawal_requested at all, and D8/D9 need a second event
        to be a duplicate or an overfill OF something. These plant a new
        stream position immediately after their anchor, with a fresh
        evt_<seed>_dx<k> id, so attribution stays one-to-one.

    Every planted defect is constructed to fire its predicate with
    certainty at the moment of delivery — a spliced duplicate-trade_id
    twin sits directly behind the fill that stored the trade, the overfill
    twin directly behind its placement — so a catch rate below 100 % is a
    detector bug, never a feed accident.

    Not for invariant work: D9's twin leaves a trade nothing settles and
    D5's rewrite breaks principal == qty x price by design, so the
    settle-drain and payable-audit referees do not apply to this feed.
    """
    base = generate_finale(seed, n, settle_all)
    on = (set(DEFECT_CLASSES) if classes is None
          else {str(c).upper() for c in classes})
    unknown = sorted(on - set(DEFECT_CLASSES))
    if unknown:
        raise ValueError(f"unknown defect classes: {unknown}")
    rngs = {d: random.Random(seed ^ _DEFECT_SALT[d]) for d in DEFECT_CLASSES}
    placement = _placements(base)
    stats = {d: 0 for d in DEFECT_CLASSES}
    cand = {d: 0 for d in DEFECT_CLASSES}
    ids: dict = {d: [] for d in DEFECT_CLASSES}
    collisions = 0
    seen_tids: set = set()
    out: list[dict] = []
    k = [0]

    def _eid() -> str:
        k[0] += 1
        return f"evt_{seed}_dx{k[0]}"

    def _is_carrier(det: str, t: str, p) -> bool:
        if det == "D1":
            return (t in _FILL_TYPES and _fill_ok(p)
                    and p.get("asset_class") in _NON_COVERING
                    and p.get("broker") != _NON_COVERING[p["asset_class"]])
        if det == "D2":
            return t == "dividend_cash" and _div_cash_ok(p)
        if det == "D3":
            return t == "interest_credited" and _interest_ok(p)
        if det == "D4":
            return (t in _FILL_TYPES and _fill_ok(p)
                    and placement.get(p["order_id"], {}).get("limit_c", 0) > 1)
        if det == "D5":
            return t in _FILL_TYPES and _fill_ok(p)
        if det == "D7":
            return t == "dividend_reinvested" and _reinvest_ok(p)
        if det == "D10":
            return ((t == "dividend_cash" and _div_cash_ok(p))
                    or (t == "dividend_reinvested" and _reinvest_ok(p)))
        return False

    def _inject(det: str, p: dict, rng) -> dict | None:
        q = dict(p)
        if det == "D1":
            q["broker"] = _NON_COVERING[p["asset_class"]]
            return q
        if det == "D2":
            g, t_c, net = (_parse_cents(p["gross_amount"]),
                           _parse_cents(p["withholding_tax"]),
                           _parse_cents(p["net_amount"]))
            skew = rng.randrange(1, 200) * rng.choice((-1, 1))
            new = max(1, net + skew)
            if new == g - t_c:                 # never land back on identity
                new = g - t_c + 1
            q["net_amount"] = _cents_str(new)
            return q
        if det == "D3":
            g = _parse_cents(p["gross_amount"])
            q["customer_share"] = _cents_str(g + rng.randrange(1, 500))
            return q
        if det == "D4":
            limit_c = placement[p["order_id"]]["limit_c"]
            step = max(1, limit_c // 50)
            if p["side"] == "buy":
                price_c = limit_c + step       # filled ABOVE the buy limit
            else:
                price_c = max(1, limit_c - step)
                if price_c >= limit_c:
                    return None
            qm = _parse_micro(p["quantity"])
            exact = _exact_principal_c(qm, price_c)
            if exact <= 0:
                return None
            q["price"] = _cents_str(price_c)
            q["principal"] = _cents_str(exact)  # keep D5 silent: one defect
            return q
        if det == "D5":
            qm, price_c = _parse_micro(p["quantity"]), _parse_cents(p["price"])
            exact = _exact_principal_c(qm, price_c)
            skew = rng.randrange(1, 100) * rng.choice((-1, 1))
            new = max(1, exact + skew)
            if new == exact:
                new = exact + 1
            q["principal"] = _cents_str(new)
            return q
        if det == "D7":
            qm = _parse_micro(p["reinvest_quantity"])
            price_c = _parse_cents(p["reinvest_price"])
            exact = _exact_principal_c(qm, price_c)
            skew = rng.randrange(2, 300) * rng.choice((-1, 1))
            new = max(2, exact + skew)
            if abs(new - exact) <= 1:          # D7 tolerates one cent
                new = exact + 2
            q["net_amount"] = _cents_str(new)
            return q
        if det == "D10":
            # A symbol nobody can possibly hold: not "a customer who might
            # own none of it", a symbol that has never traded.
            q["symbol"] = f"PHANTOM-{seed}-{k[0] + 1}"
            k[0] += 1
            return q
        return None

    for ev in base:
        t, p = ev["type"], ev["payload"]
        # Every active class draws for every carrier of its own type, hit
        # or miss, so one class's stream of random numbers never depends on
        # which other classes are switched on.
        hits = []
        for det in _INPLACE_ORDER:
            if det not in on or not _is_carrier(det, t, p):
                continue
            cand[det] += 1
            if rngs[det].random() < rate:
                hits.append(det)
        cur = ev
        for det in hits:
            new_p = _inject(det, p, rngs[det])
            if new_p is not None:
                cur = {**ev, "payload": new_p}
                stats[det] += 1
                ids[det].append(ev["event_id"])
                break
        collisions += max(0, len(hits) - (1 if cur is not ev else 0))
        out.append(cur)

        # -- spliced carriers, immediately behind their anchor -------------
        if t == "deposit" and isinstance(p, dict) and p.get("customer_id"):
            if "D6" in on:
                cand["D6"] += 1
                if rngs["D6"].random() < rate:
                    fx = rngs["D6"].randrange(10_000, 1_000_001)   # cents
                    rate_m = rngs["D6"].randrange(8_000, 15_001)   # 1e-4
                    rate_c = rate_m * 98 // 100        # the worse customer rate
                    usd = (fx * rate_m + 5_000) // 10_000
                    off = rngs["D6"].randrange(5, 500)   # > 1 cent: a real
                    eid = _eid()                          # rate mismatch
                    out.append({"offset": -1, "event_id": eid,
                                "type": "fx_deposit",
                                "payload": {
                                    "customer_id": p["customer_id"],
                                    "currency": "EUR",
                                    "amount_foreign": _cents_str(fx),
                                    "market_rate":
                                        f"{rate_m // 10_000}."
                                        f"{rate_m % 10_000:04d}",
                                    "customer_rate":
                                        f"{rate_c // 10_000}."
                                        f"{rate_c % 10_000:04d}",
                                    "usd_at_market_rate":
                                        _cents_str(usd + off),
                                    "usd_at_customer_rate":
                                        _cents_str((fx * rate_c + 5_000)
                                                   // 10_000)}})
                    stats["D6"] += 1
                    ids["D6"].append(eid)
            if "D11" in on:
                cand["D11"] += 1
                if rngs["D11"].random() < rate:
                    eid = _eid()
                    out.append({"offset": -1, "event_id": eid,
                                "type": "withdrawal_requested",
                                "payload": {
                                    "customer_id": p["customer_id"],
                                    "withdrawal_id": f"wd_{seed}_dx{k[0]}",
                                    "amount": "99999999.99"}})
                    stats["D11"] += 1
                    ids["D11"].append(eid)

        if t in _FILL_TYPES and _fill_ok(p):
            tid = p["trade_id"]
            if ("D8" in on and cur is ev and p["side"] == "buy"
                    and tid not in seen_tids):
                cand["D8"] += 1
                if rngs["D8"].random() < rate:
                    # A twin of the fill that just stored this trade: the
                    # trade exists, nothing can have reversed it yet, so
                    # the duplicate-trade_id observation is certain.
                    eid = _eid()
                    twin = dict(p)
                    twin["order_id"] = f"ord_{seed}_dx{k[0]}"
                    out.append({"offset": -1, "event_id": eid,
                                "type": "order_partially_filled",
                                "payload": twin})
                    stats["D8"] += 1
                    ids["D8"].append(eid)
            seen_tids.add(tid)

        if (t == "order_placed" and "D9" in on and isinstance(p, dict)
                and placement.get(p.get("order_id"), {}).get("side") == "buy"
                and placement[p["order_id"]]["cls"] in CLASS_BROKERS):
            cand["D9"] += 1
            if rngs["D9"].random() < rate:
                pl = placement[p["order_id"]]
                qty = pl["qty"] + 1_000_000        # one share past the order
                price_c = max(1, pl["limit_c"])    # inside the limit: not D4
                eid = _eid()
                out.append({"offset": -1, "event_id": eid,
                            "type": "order_partially_filled",
                            "payload": {
                                "order_id": p["order_id"],
                                "trade_id": f"trd_{seed}_dx{k[0]}",
                                "customer_id": pl["cid"], "side": "buy",
                                "symbol": pl["symbol"],
                                "quantity": _qty_str(qty),
                                "price": _cents_str(price_c),
                                "principal": _cents_str(
                                    _exact_principal_c(qty, price_c)),
                                "broker": CLASS_BROKERS[pl["cls"]][0],
                                "asset_class": pl["cls"],
                                "partner_rate": "0.5"}})
                stats["D9"] += 1
                ids["D9"].append(eid)

    for i, ev in enumerate(out):               # positions stay sequential
        ev["offset"] = i
    stats.update({"events": len(out), "base_events": len(base),
                  "rate": rate, "classes": sorted(on),
                  "candidates": cand, "ids": ids, "collisions": collisions,
                  "injected": sum(stats[d] for d in DEFECT_CLASSES)})
    DEFECT_LAST_STATS.clear()
    DEFECT_LAST_STATS.update(stats)
    return out


def generate_defective_stats(seed: int, n: int, classes=None,
                             rate: float = 0.015,
                             settle_all: bool = True) -> dict:
    """Companion probe: run generate_defective and return a copy of its
    DEFECT_LAST_STATS without touching the feed itself."""
    generate_defective(seed, n, classes, rate, settle_all)
    return dict(DEFECT_LAST_STATS)


CLEAN_DUP_EVERY = 350          # one verbatim redelivery spliced in per ~350
CLEAN_BACKDATE_EVERY = 80      # one backdated_days carrier per ~80 cash events
CLEAN_FILL_BEFORE_PLACEMENT = 0.03   # per order: its placement lands later
CLEAN_LAST_STATS: dict = {}


def generate_clean(seed: int, n: int, settle_all: bool = True) -> list[dict]:
    """The false-positive feed: every STREAM trap on, ZERO defects.

    generate_finale is already full of traps a detector must not mistake
    for a defect — oversells, malformed payloads, settle-before-fill,
    double settles, zero-payable settlements, unknown ids, dense reversal
    edge cases, dividends on positions the customer does not hold. This
    wrapper keeps all of them, adds three more (verbatim redeliveries,
    backdated_days carriers, and placements delivered AFTER their first
    fill), and repairs the four defect classes generate_finale plants on
    purpose:

      * D2 — dividend net is set back to gross - withholding_tax;
      * D7 — reinvest net is set back to money(price x quantity);
      * D8 — duplicated trade_ids are re-issued unique;
      * D4/D5 — every fill price is moved INSIDE its order's limit (buy
        <= limit, sell >= limit) and its principal recomputed as exactly
        money(quantity x price).

    That last one is what makes the measurement honest: an armed-in-test
    D4 or D5 firing here is firing on data that satisfies the identity by
    construction, so the hit is a false positive and nothing else.

    Deliberately NOT repaired: dividends on a zero position (D10) and
    everything a stream can legitimately do out of order. D10's whole
    false-positive case is that a dividend may precede the buy — the
    clean feed is where we measure how often that happens.

    Composes with the other stages as usual: corrupt() adds conflicting
    duplicates and malformed cash, deliver(rewind_at=...) adds point
    redeliveries and rewinds. Deterministic in (seed, n, settle_all).
    Counts land in CLEAN_LAST_STATS.
    """
    base = generate_finale(seed, n, settle_all)
    rng = random.Random(seed ^ 0xC1EA00)       # decorrelate from every stage
    placement = _placements(base)
    stats = {k: 0 for k in ("fixed_price_buy", "fixed_price_sell",
                            "fixed_principal", "fixed_d2", "fixed_d7",
                            "fixed_dup_trade_id", "fills_too_small",
                            "dup_verbatim", "backdated",
                            "fill_before_placement", "fills", "orders")}
    seen_tids: set = set()
    tid_fix = [0]
    repaired: list[dict] = []

    for ev in base:
        t, p = ev["type"], ev["payload"]
        new_p = None
        if t in _FILL_TYPES and _fill_ok(p):
            stats["fills"] += 1
            q = dict(p)
            qm, price_c = _parse_micro(p["quantity"]), _parse_cents(p["price"])
            limit_c = placement.get(p["order_id"], {}).get("limit_c", 0)
            if limit_c > 0:
                if p["side"] == "buy" and price_c > limit_c:
                    price_c = limit_c
                    stats["fixed_price_buy"] += 1
                elif p["side"] == "sell" and price_c < limit_c:
                    price_c = limit_c
                    stats["fixed_price_sell"] += 1
            exact = _exact_principal_c(qm, price_c)
            if exact == 0:
                # Too few shares to round to a cent. A sell may always be
                # priced up; a buy is capped by its limit. If neither works
                # the fill is left with a principal of 0.00, which the Book
                # rejects on validation — never a detector's business.
                need = -(-500_000 // qm)
                if p["side"] == "sell" or need <= (limit_c or need):
                    price_c = max(price_c, need)
                    exact = _exact_principal_c(qm, price_c)
                if exact == 0:
                    stats["fills_too_small"] += 1
            if price_c != _parse_cents(p["price"]):
                q["price"] = _cents_str(price_c)
            if exact != _parse_cents(p["principal"]):
                q["principal"] = _cents_str(exact)
                stats["fixed_principal"] += 1
            if p["trade_id"] in seen_tids:     # D8: re-issue it unique
                tid_fix[0] += 1
                q["trade_id"] = f"trd_{seed}_c{tid_fix[0]}"
                stats["fixed_dup_trade_id"] += 1
            seen_tids.add(q["trade_id"])
            new_p = q
        elif t == "dividend_cash" and _div_cash_ok(p):
            g = _parse_cents(p["gross_amount"])
            tax = _parse_cents(p["withholding_tax"])
            if _parse_cents(p["net_amount"]) != g - tax:
                q = dict(p)
                if g - tax <= 0:               # keep the net strictly positive
                    tax = 0
                    q["withholding_tax"] = _cents_str(0)
                q["net_amount"] = _cents_str(g - tax)
                stats["fixed_d2"] += 1
                new_p = q
        elif t == "dividend_reinvested" and _reinvest_ok(p):
            exact = _exact_principal_c(_parse_micro(p["reinvest_quantity"]),
                                       _parse_cents(p["reinvest_price"]))
            if exact > 0 and _parse_cents(p["net_amount"]) != exact:
                q = dict(p)
                q["net_amount"] = _cents_str(exact)
                stats["fixed_d7"] += 1
                new_p = q
        repaired.append({**ev, "payload": new_p} if new_p is not None else ev)

    # -- fill-before-placement: the out-of-order trap D4/D9 must survive ---
    # Sorting by a fractional key moves the placement to just behind its
    # order's first fill without disturbing anything else's order.
    first_fill: dict = {}
    place_at: dict = {}
    for i, ev in enumerate(repaired):
        p = ev["payload"]
        if not isinstance(p, dict) or not isinstance(p.get("order_id"), str):
            continue
        if ev["type"] == "order_placed":
            place_at.setdefault(p["order_id"], i)
        elif ev["type"] in _FILL_TYPES:
            first_fill.setdefault(p["order_id"], i)
    moved: dict = {}
    for oid in sorted(place_at):
        stats["orders"] += 1
        ff = first_fill.get(oid)
        if ff is None or ff <= place_at[oid]:
            continue
        if rng.random() < CLEAN_FILL_BEFORE_PLACEMENT:
            moved[place_at[oid]] = ff + 0.5
            stats["fill_before_placement"] += 1
    ordered = [ev for _k, ev in
               sorted(((moved.get(i, float(i)), ev)
                       for i, ev in enumerate(repaired)),
                      key=lambda kv: kv[0])]

    # -- verbatim redeliveries + backdated_days carriers -------------------
    out: list[dict] = []
    for ev in ordered:
        if (ev["type"] in CP_POSTING_TYPES and isinstance(ev["payload"], dict)
                and rng.random() < 1.0 / CLEAN_BACKDATE_EVERY):
            ev = {**ev, "payload": {**ev["payload"],
                                    "backdated_days": rng.randrange(1, 30)}}
            stats["backdated"] += 1
        out.append(ev)
        if out and rng.random() < 1.0 / CLEAN_DUP_EVERY:
            out.append(dict(rng.choice(out)))   # same id, same payload
            stats["dup_verbatim"] += 1

    for i, ev in enumerate(out):
        ev["offset"] = i
    stats.update({"events": len(out), "base_events": len(base)})
    CLEAN_LAST_STATS.clear()
    CLEAN_LAST_STATS.update(stats)
    return out


def generate_clean_stats(seed: int, n: int, settle_all: bool = True) -> dict:
    """Companion probe: run generate_clean and return a copy of its
    CLEAN_LAST_STATS without touching the feed itself."""
    generate_clean(seed, n, settle_all)
    return dict(CLEAN_LAST_STATS)
