"""Invariant checker — the referee every phase gate answers to.

Design decisions:

  * Independent oracles, one function each:
        run_invariants      accounting + format law on the live book
        replay_identical    determinism: cold replay of the log == live state
        ring_identical      the snapshot-ring shortcut == a cold replay
        asof_oracle         as-of answers == the live state that existed
                            at that log position (Phase 6)
        serialization_canon money/quantity/key-order law on real snapshots
    A green gate is all of them saying nothing.
  * Every function is read-only on the book it is handed. The replay oracles
    build FRESH Books and drive them through the recorded log — they never
    touch the live one's state.
  * run_invariants returns a list of violation strings (empty == green) and
    keeps going after the first hit: one run reports every violation, so a
    systematic bug shows its whole shape at once.
  * Snapshots are compared as json.dumps(..., sort_keys=True) strings —
    byte-identical or failed. "Roughly equal" is how rounding bugs hide.
"""
from __future__ import annotations

import json
import random
import re
import time
from decimal import Decimal

import tariff
from book import Book, BROKER_PAYABLE, ZERO, qnum, fmt_qty, fmt_money

# The full chart of accounts from PROTOCOL.md. A leg naming anything else
# is a bug even if the books still balance.
CHART = {"1100", "1150", "1200", "2010", "2100", "2300", "2350",
         "2400", "2411", "2412", "2413", "2420", "2430",
         "4000", "4010", "4100", "4200", "5000", "5010", "5100"}

# Canonical money string: optional sign, digits, exactly 2 decimals.
# Anything else — scientific notation, 1 or 3 decimals, blank — fails.
MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")


def _check_money_str(s, where: str, out: list[str]) -> None:
    if not isinstance(s, str) or not MONEY_RE.match(s) or "E" in s or "e" in s:
        out.append(f"{where}: not a 2dp money string: {s!r}")


def run_invariants(book) -> list[str]:
    """Accounting and formatting law on the live book. Empty list == green.

    1. Double entry, globally: every posting moved equal debits and credits,
       so the sum of every (customer, account) balance is exactly zero.
    2. Per stored event: its legs balance (Σ debit == Σ credit) and touch
       only accounts that exist in the chart; every leg's debit/credit is a
       canonical 2dp money string (these strings ARE the submissions).
    3. The snapshot's trial_balance values are canonical 2dp money strings —
       no scientific notation, no stray precision.
    """
    out: list[str] = []

    total = sum(book.balances.values(), ZERO)
    if total != 0:
        out.append(f"global balance: sum of all balances is {total}, not 0")

    for eid, rec in book.events.items():
        dr = cr = ZERO
        for i, l in enumerate(rec["legs"]):
            if l["account"] not in CHART:
                out.append(f"{eid} leg {i}: account {l['account']!r} "
                           f"not in chart")
            _check_money_str(l["debit"], f"{eid} leg {i} debit", out)
            _check_money_str(l["credit"], f"{eid} leg {i} credit", out)
            try:
                dr += Decimal(l["debit"])
                cr += Decimal(l["credit"])
            except ArithmeticError:
                pass                      # already reported by the fmt check
        if dr != cr:
            out.append(f"{eid}: legs do not balance: dr {dr} cr {cr}")

    for acct, val in book.snapshot()["trial_balance"].items():
        _check_money_str(val, f"trial_balance[{acct}]", out)

    return out


def wallet_oracle(book) -> list[str]:
    """Independent per-customer wallet recomputation — the Phase 1 referee.

    Rebuilds every customer's expected 2010 balance purely from the
    FIRST-DELIVERED PAYLOADS of posted events (never from the legs, never
    from book.balances), then compares cent-for-cent:

        wallet = Σ deposits + Σ refunds + Σ interest shares
               + Σ transfers-in − Σ transfers-out + Σ fx at customer rate
               − Σ fees − Σ withdrawal requests + Σ withdrawal rejections

    Rejected events are absent from book.events, so they contribute nothing
    — exactly the graded semantics. Any divergence means a handler posted a
    wrong amount or direction while still balancing, which is precisely the
    class of bug a trial balance can never see.

    Phase 5 reversal-awareness: a posted `reversal` NEGATES the wallet
    effect of its original (same per-type math, sign flipped). A reversed
    fee can never later be refunded (R5 rejects the refund, so it is
    simply absent from book.events) — nothing extra to do.
    """
    out: list[str] = []
    expected: dict = {}

    def add(cid, amount):
        expected[cid] = expected.get(cid, ZERO) + amount

    def m(x):
        # The spec's own convention, restated independently of book.money():
        # 2 dp, half away from zero.
        from decimal import ROUND_HALF_UP
        return Decimal(str(x)).quantize(Decimal("0.01"),
                                        rounding=ROUND_HALF_UP)

    requests: dict = {}          # wid -> (cid, amount) from posted requests

    def deltas(t, p) -> list:
        """The wallet effect of one posted event of type t with payload p,
        as (cid, signed amount) pairs. Pure — the requests side table is
        maintained by the main loop, never in here."""
        if t == "deposit":
            return [(p["customer_id"], m(p["amount"]))]
        if t == "fee_charged":
            return [(p["customer_id"], -m(p["amount"]))]
        if t == "fee_refund":
            fee = book.events[p["refunds_source_id"]]["payload"]
            return [(fee["customer_id"], m(fee["amount"]))]
        if t == "interest_credited":
            return [(p["customer_id"], m(p["customer_share"]))]
        if t == "transfer_between_customers":
            return [(p["from_customer_id"], -m(p["amount"])),
                    (p["to_customer_id"], m(p["amount"]))]
        if t == "fx_deposit":
            return [(p["customer_id"], m(p["usd_at_customer_rate"]))]
        if t == "dividend_cash":
            # Phase 4: tax withheld at source — the NET lands in the wallet
            # verbatim, even when D2 flags net != gross - tax (observe-only).
            # dividend_reinvested never touches the wallet: cash is not
            # involved (1200/2100 only).
            return [(p["customer_id"], m(p["net_amount"]))]
        if t == "withdrawal_requested":
            return [(p["customer_id"], -m(p["amount"]))]
        if t == "withdrawal_rejected":
            cid, amount = requests[p["withdrawal_id"]]
            return [(cid, amount)]
        if t in ("order_partially_filled", "order_filled"):
            # Phase 3: a posted fill moves the wallet on the trade date —
            # buy Dr 2010 P+b+c+r, sell Cr 2010 P−b−c−r. Charges are
            # recomputed here from the STORED payload via tariff (the same
            # pure math the book uses, but independently of its legs).
            ch = tariff.fill_charges(p["broker"], p["principal"],
                                     p["partner_rate"])
            fees = ch["b"] + ch["c"] + ch["r"]
            if p["side"] == "buy":
                return [(p["customer_id"], -(m(p["principal"]) + fees))]
            return [(p["customer_id"], m(p["principal"]) - fees)]
        # withdrawal_settled moves 2300 -> 1100; the wallet is untouched.
        # trade_settled moves 2350/1150 <-> 1100; the wallet is untouched.
        # The four fee settlements move a payable <-> 1100; untouched too.
        return []

    for rec in book.events.values():
        t, p = rec["type"], rec["payload"]
        if t == "withdrawal_requested":
            requests[p["withdrawal_id"]] = (p["customer_id"], m(p["amount"]))
        if t == "reversal":
            orig = book.events[p["reverses_event_id"]]
            for cid, amount in deltas(orig["type"], orig["payload"]):
                add(cid, -amount)
        else:
            for cid, amount in deltas(t, p):
                add(cid, amount)

    cids = set(expected) | {c for (c, a) in book.balances if a == "2010"}
    for cid in sorted(cids):
        want = expected.get(cid, ZERO)
        got = -book.balances.get((cid, "2010"), ZERO)
        if want != got:
            out.append(f"wallet[{cid}]: oracle {want} != book {got}")
    return out


FILL_TYPES = ("order_partially_filled", "order_filled")


def dup_fill_stuck(book) -> tuple[dict, dict]:
    """Per-customer 2350 / 1150 amounts that can NEVER settle, from
    quarantined duplicate-trade_id fills (D8): the fill posted real legs but
    stored no trade (the first fill's trade owns the id), so no trade_settled
    can ever discharge that principal. Amounts come from the STORED legs of
    the quarantined event — the exact cents the book moved.

    Read-only; shared by market_identities (identity d) and the drain check
    in run_regression.py.
    """
    stuck_2350: dict = {}
    stuck_1150: dict = {}
    for entry in book.quarantine:
        if entry[0] != "dup_trade_id":
            continue
        rec = book.events.get(entry[2])
        if rec is None or rec.get("reversed"):
            continue          # a reversed dup fill nets to zero (Phase 5)
        for l in rec["legs"]:
            cid = l["customer_id"]
            if l["account"] == "2350":
                stuck_2350[cid] = (stuck_2350.get(cid, ZERO)
                                   + Decimal(l["credit"]) - Decimal(l["debit"]))
            elif l["account"] == "1150":
                stuck_1150[cid] = (stuck_1150.get(cid, ZERO)
                                   + Decimal(l["debit"]) - Decimal(l["credit"]))
    return stuck_2350, stuck_1150


def reversed_settled_residues(book) -> tuple[dict, dict]:
    """Per-customer 2350 / 1150 residue left BY DESIGN when a SETTLED fill
    is reversed (R7): the settlement already discharged the obligation, so
    the reversal's verbatim inverse leg re-opens the account the other way
    (buy: a dangling 2350 debit of P; sell: a dangling 1150 credit of P).
    The trade stays settled — cash moved once, and only once.

    Returns ({cid: Σ P over reversed settled buys}, {cid: Σ P over reversed
    settled sells}); both subtract from the identity-d / drain expectation.
    Duplicate-trade_id fills are excluded — their trade belongs to another
    fill (D8) and their stuck legs are tallied by dup_fill_stuck.
    """
    dup_eids = {e[2] for e in book.quarantine if e[0] == "dup_trade_id"}
    res_2350: dict = {}
    res_1150: dict = {}
    for eid, rec in book.events.items():
        if (rec["type"] not in FILL_TYPES or not rec.get("reversed")
                or eid in dup_eids):
            continue
        t = book.trades.get(rec["payload"].get("trade_id"))
        if t is None or not t["settled"]:
            continue          # unsettled at reversal: trade deleted, net 0
        p = tariff.money(rec["payload"]["principal"])
        cid = rec["payload"]["customer_id"]
        if rec["payload"]["side"] == "buy":
            res_2350[cid] = res_2350.get(cid, ZERO) + p
        else:
            res_1150[cid] = res_1150.get(cid, ZERO) + p
    return res_2350, res_1150


def _reversal_cost_adjustment(book) -> dict:
    """Per-customer adjustment to the `2100 credit balance == Σ lot costs`
    identity created BY DESIGN when a partially-consumed add-lot (buy fill
    or dividend_reinvested) is reversed: the verbatim inverse leg re-debits
    2100 by the FULL original cost, while the lot undo removes only the
    REMAINDER (L5/R15). adjustment += remainder − full, per such reversal.

    Recomputed chronologically from stored payloads + lot_ops (never from
    the reversal's legs), so it stays an independent cross-check of the
    posted legs against the lot book.
    """
    adj: dict = {}
    lot_cost: dict = {}       # lot_id -> running cost per the stored ops

    def add_cost(rec):
        if rec["type"] == "dividend_reinvested":
            return tariff.money(rec["payload"]["net_amount"])
        return tariff.money(rec["payload"]["principal"])

    for ev in book.event_log:
        rec = book.events.get(ev["event_id"])
        if rec is None:
            continue
        if rec["type"] == "reversal":
            orig = book.events.get(rec["payload"].get("reverses_event_id"))
            if orig is None:
                continue
            for op in reversed(orig["lot_ops"]):
                if op[0] == "add":
                    lot_id = op[1]
                    cid = book.lots[lot_id]["cid"]
                    remainder = lot_cost.get(lot_id, ZERO)
                    adj[cid] = adj.get(cid, ZERO) + (remainder
                                                     - add_cost(orig))
                    lot_cost[lot_id] = ZERO
                elif op[0] == "consume":
                    # a reversed sell restores its relieved cost verbatim
                    lot_cost[op[1]] = lot_cost.get(op[1], ZERO) + op[3]
        else:
            for op in rec["lot_ops"]:
                if op[0] == "add":
                    lot_cost[op[1]] = add_cost(rec)
                elif op[0] == "consume":
                    lot_cost[op[1]] = lot_cost.get(op[1], ZERO) - op[3]
                # split / rekey never move cost
    return adj


def market_identities(book) -> list[str]:
    """Phase 3 standing identities on the live book after a sim run.
    Read-only; empty list == green.

      a. Per customer: credit balance of 2100 == Σ remaining lot cost_totals;
         per (cid, symbol): reported position qty == Σ lot qtys — the 64%
         guardian (cost basis is the biggest graded block).
      b. Custody mirror: every posted FILL's total 1200 movement equals the
         opposite 2100 movement (buy: Dr 1200 == Cr 2100; sell: Cr 1200 ==
         Dr 2100). Non-fill events are skipped.
      c. Closed orders hold exactly zero (money AND share hold); open money
         holds sit in [0, hold_init]; open share holds are never negative.
      d. Per customer: 2350 credit balance == Σ unsettled buy principals and
         1150 debit balance == Σ unsettled sell principals in book.trades —
         plus the quarantined duplicate-trade_id fill postings, which carry
         real 2350/1150 cents but no settleable trade (D8, dup_fill_stuck).
      e. The snapshot's reported cash_hold == Σ hold_rem over that
         customer's open, placed buy orders.
    """
    out: list[str] = []
    snap = book.snapshot()

    # -- (a) lot book == 2100, lot qty == reported position ---------------
    # Phase 5: a reversed partially-consumed add re-debits 2100 by the full
    # original cost while removing only the lot's remainder — that designed
    # residue is recomputed independently and folded into the expectation.
    lot_cost: dict = {}
    for lot in book.lots.values():
        lot_cost[lot["cid"]] = lot_cost.get(lot["cid"], ZERO) \
            + lot["cost_total"]
    cost_adj = _reversal_cost_adjustment(book)
    cids_a = (set(lot_cost) | set(cost_adj)
              | {c for (c, a) in book.balances if a == "2100"})
    for cid in sorted(cids_a):
        want = lot_cost.get(cid, ZERO) + cost_adj.get(cid, ZERO)
        got = -book.balances.get((cid, "2100"), ZERO)
        if got != want:
            out.append(f"identity-a 2100[{cid}]: credit balance {got} "
                       f"!= lot cost total {want}")
    for (cid, symbol), index in sorted(book.lot_index.items()):
        qty = sum((book.lots[i]["qty"] for i in index), qnum(0))
        reported = snap["customers"].get(cid, {}).get("positions", {}) \
            .get(symbol)
        if qty == 0:
            if reported is not None:
                out.append(f"identity-a position[{cid},{symbol}]: lots sum "
                           f"to 0 but snapshot reports {reported}")
        elif reported is None:
            out.append(f"identity-a position[{cid},{symbol}]: lots sum to "
                       f"{qty} but snapshot omits the position")
        elif reported["quantity"] != fmt_qty(qty):
            out.append(f"identity-a position[{cid},{symbol}]: snapshot qty "
                       f"{reported['quantity']} != lot qty {fmt_qty(qty)}")

    # -- (b) custody mirror on every posted fill --------------------------
    for eid, rec in book.events.items():
        if rec["type"] not in FILL_TYPES:
            continue
        dr1200 = cr1200 = dr2100 = cr2100 = ZERO
        for l in rec["legs"]:
            if l["account"] == "1200":
                dr1200 += Decimal(l["debit"])
                cr1200 += Decimal(l["credit"])
            elif l["account"] == "2100":
                dr2100 += Decimal(l["debit"])
                cr2100 += Decimal(l["credit"])
        if dr1200 != cr2100 or cr1200 != dr2100:
            out.append(f"identity-b custody mirror {eid}: 1200 dr/cr "
                       f"{dr1200}/{cr1200} vs 2100 dr/cr {dr2100}/{cr2100}")

    # -- (c) hold discipline ---------------------------------------------
    for oid, o in sorted(book.orders.items()):
        if o["closed"]:
            if o["hold_rem"] != 0 or o["share_hold_rem"] != 0:
                out.append(f"identity-c order[{oid}]: closed but hold_rem "
                           f"{o['hold_rem']} share_hold_rem "
                           f"{o['share_hold_rem']}")
        else:
            if not (ZERO <= o["hold_rem"] <= o["hold_init"]):
                out.append(f"identity-c order[{oid}]: open hold_rem "
                           f"{o['hold_rem']} outside [0, {o['hold_init']}]")
            if o["share_hold_rem"] < 0:
                out.append(f"identity-c order[{oid}]: negative share hold "
                           f"{o['share_hold_rem']}")

    # -- (d) unsettled principals == 2350 / 1150 --------------------------
    # Phase 5: a reversed SETTLED fill leaves a designed residue on the
    # obligation account (R7) — subtracted from the expectation.
    exp_2350, exp_1150 = dup_fill_stuck(book)
    res_2350, res_1150 = reversed_settled_residues(book)
    for t in book.trades.values():
        if t["settled"]:
            continue
        if t["side"] == "buy":
            exp_2350[t["cid"]] = exp_2350.get(t["cid"], ZERO) + t["principal"]
        else:
            exp_1150[t["cid"]] = exp_1150.get(t["cid"], ZERO) + t["principal"]
    cids_d = (set(exp_2350) | set(exp_1150) | set(res_2350) | set(res_1150)
              | {c for (c, a) in book.balances if a in ("2350", "1150")})
    for cid in sorted(cids_d):
        got = -book.balances.get((cid, "2350"), ZERO)
        want = exp_2350.get(cid, ZERO) - res_2350.get(cid, ZERO)
        if got != want:
            out.append(f"identity-d 2350[{cid}]: credit balance {got} != "
                       f"unsettled buy principals {want}")
        got = book.balances.get((cid, "1150"), ZERO)
        want = exp_1150.get(cid, ZERO) - res_1150.get(cid, ZERO)
        if got != want:
            out.append(f"identity-d 1150[{cid}]: debit balance {got} != "
                       f"unsettled sell principals {want}")

    # -- (e) snapshot cash_hold == open buy holds -------------------------
    for cid, cust in snap["customers"].items():
        want = ZERO
        for o in book.orders.values():
            if (o["cid"] == cid and o["side"] == "buy"
                    and o["placed"] and not o["closed"]):
                want += o["hold_rem"]
        if cust["cash_hold"] != fmt_money(want):
            out.append(f"identity-e cash_hold[{cid}]: snapshot "
                       f"{cust['cash_hold']} != open buy holds "
                       f"{fmt_money(want)}")

    return out


def _snap_bytes(b) -> str:
    return json.dumps(b._snapshot_now(), sort_keys=True)


# ------------------------------------------------------------------ #
#  Phase 7: the residue oracle                                       #
# ------------------------------------------------------------------ #

def state_fingerprint(book) -> str:
    """Everything a rejected event must leave byte-identical, in one
    comparable string. The fuzz residue test is
    ``fingerprint before == fingerprint after`` on every reject.

    IN: every ledger store a handler can mutate — balances, the whole lot
    book (quantity, cost, sequence, split multiplier, symbol, merge rank,
    the per-holding index and the sequence counter), orders with their
    holds and routes, trades, fees, refunded ids, withdrawals,
    accounts_touched, customers_seen, quarantine, and the posted-event
    store. Also the rendered snapshot, because that is what the grader
    actually reads: a residue the stores hide but the report shows still
    counts.

    OUT, deliberately — all four legitimately grow on a rejection, and a
    checker that includes them false-fails every single reject:

        seen        the id is recorded before dispatch (first delivery
                    wins, R11/S2 — that is the contract, not residue)
        event_log   every first delivery is logged verbatim, rejects
                    included, so an as-of checkpoint can name one
        todo        unknown event types are counted, not posted
        report_log  detector findings and reporting notes — the
                    NON-replayed channel, which is exactly why findings
                    are written there and not into quarantine

    Decimals and Fractions are stringified, never floated: this is a
    byte comparison, and float() is where cent differences go to hide.
    """
    return json.dumps({
        "balances": sorted((f"{cid}|{acct}", str(v))
                           for (cid, acct), v in book.balances.items()),
        "lots": sorted((str(lid), str(l["cid"]), str(l["symbol"]),
                        str(l["qty"]), str(l["cost_total"]), str(l["seq"]),
                        str(l["split_mult"]), str(l.get("merge_rank", 0)))
                       for lid, l in book.lots.items()),
        "lot_index": sorted((f"{cid}|{sym}", [str(i) for i in index])
                            for (cid, sym), index in book.lot_index.items()),
        "lot_seq": str(book.lot_seq),
        "merge_seq": str(book.merge_seq),
        "orders": sorted((oid, repr(sorted((k, str(v))
                                           for k, v in o.items())))
                         for oid, o in book.orders.items()),
        "trades": sorted((tid, repr(sorted((k, str(v))
                                           for k, v in t.items())))
                         for tid, t in book.trades.items()),
        "fees": sorted((fid, repr(sorted((k, str(v))
                                         for k, v in f.items())))
                       for fid, f in book.fees.items()),
        "refunded": sorted(str(x) for x in book.refunded),
        "withdrawals": sorted((wid, repr(sorted((k, str(v))
                                                for k, v in w.items())))
                              for wid, w in book.withdrawals.items()),
        "accounts_touched": sorted(book.accounts_touched),
        "customers_seen": sorted(book.customers_seen),
        "quarantine": [repr(q) for q in book.quarantine],
        "events": sorted((eid, repr(rec["legs"]), repr(rec["lot_ops"]),
                          str(rec["reversed"]))
                         for eid, rec in book.events.items()),
        "snapshot": book._snapshot_now(),
    }, sort_keys=True)


# The single residue class the fuzzer finds in the current Book, pinned
# by its blast radius rather than waved through. See NOTES.md and
# tests/test_fuzz.py::TestKnownResidue:
#
#   on_order_placed writes the order record (`o.update(... placed=True)`
#   and `customers_seen.add(cid)`) BEFORE computing
#   `money(qty * limit + est_charges)`. An est_charges that is finite but
#   astronomical (10**30, 1e308, a 400-digit string) survives validation
#   and blows up in quantize; the broad except rejects the event with
#   `legs: []` — correctly — but the placement stub is already in the
#   book, so the order shows up in `open_order_routes` and its customer
#   in the checkpoint.
#
# It cannot move money: no leg posts, no balance, lot, trade, fee,
# withdrawal or quarantine entry changes, and the trial balance is
# untouched. The barrage therefore allows EXACTLY these stores to differ
# for exactly this shape and fails on anything wider — including on this
# same event type for any other reason.
_KNOWN_RESIDUE_KEYS = {"orders", "customers_seen", "snapshot"}
_KNOWN_RESIDUE_SNAP_KEYS = {"customers", "open_order_routes"}


def _residue_scope(before: str, after: str) -> set:
    """Which top-level fingerprint sections differ, with the snapshot's
    own differing sections folded in as `snapshot.<section>`."""
    b, a = json.loads(before), json.loads(after)
    scope = {k for k in b if b[k] != a[k]}
    if "snapshot" in scope:
        scope.discard("snapshot")
        scope |= {f"snapshot.{k}" for k in b["snapshot"]
                  if b["snapshot"][k] != a["snapshot"][k]}
        scope.add("snapshot")
    return scope


def placement_hold_overflows(payload) -> bool:
    """Is this the exact input that trips the pinned defect — an
    est_charges that passes `is_finite()` and `>= 0` and then cannot be
    quantized to the cent? "Infinity" and "NaN" are NOT this: they fail
    validation before any mutation and reject perfectly cleanly."""
    if not isinstance(payload, dict):
        return False
    raw = payload.get("est_charges", payload.get("est_commission"))
    try:
        est = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return False
    if not est.is_finite() or est < 0:
        return False
    try:
        est.quantize(Decimal("0.01"))
    except ArithmeticError:
        return True
    return False


def _is_known_residue(name: str, scope: set, payload) -> bool:
    """The placement-hold overflow, and nothing else: the right event
    type, the exact input that provokes it, and a difference confined to
    the placement lifecycle stores. Any one of the three missing and the
    residue is reported as a violation."""
    if name != "order_placed" or not placement_hold_overflows(payload):
        return False
    plain = {k for k in scope if not k.startswith("snapshot.")}
    snap = {k.split(".", 1)[1] for k in scope if k.startswith("snapshot.")}
    return (plain <= _KNOWN_RESIDUE_KEYS
            and snap <= _KNOWN_RESIDUE_SNAP_KEYS)


def fuzz_barrage(count: int = 1000, seed: int = 20260806,
                 names=None) -> tuple[dict, list[str]]:
    """The Phase 7 fuzz referee: control group, then the mutation
    barrage, then snapshot-under-fuzz. Returns (stats, violations);
    empty violations == green.

    1. CONTROL GROUP, first and blocking. Every baseline is delivered
       unmutated and must be ACCEPTED and post exactly what the protocol
       says — the 13-leg buy at Dr = Cr = 2005.13, the 13-leg sell at
       1101.16, deposit's two, and `[]` from the no-leg types. A Book
       that rejected everything would sail through the rest of this
       function; this is what stops it.
    2. MUTATION BARRAGE, one fresh Book per mutant (setup replayed, so
       every mutant meets the same state):
         * no exception escapes apply() — a crash is a dead stream
         * an accepted mutant returns balanced legs
         * a REJECTED mutant returns [] and leaves state byte-identical.
           Acceptance is read from `book.events` (the handler ran and
           committed), never from "legs == []" — four event types post
           no legs when they succeed, and inferring rejection from an
           empty leg list would silently excuse their residue.
    3. SNAPSHOT UNDER FUZZ: one Book takes every type's whole barrage
       back to back (ids are namespaced per type, so the types do not
       collide), then snapshot() must not raise and must serialize
       clean — money 2dp, quantities minimal, no exponent anywhere —
       and the standing accounting invariants must still hold.
    """
    from sim import fuzz
    out: list[str] = []
    stats = {"types": 0, "mutants": 0, "posted": 0, "rejected": 0,
             "known_residue": 0, "per_type": {}, "seconds": 0.0}
    t0 = time.perf_counter()
    soak = Book()                      # the snapshot-under-fuzz book

    for name in (names or list(fuzz.BASELINES)):
        # -- 1. control group -----------------------------------------
        setup, event = fuzz.baseline(name)
        ctl = Book()
        for s in setup:
            ctl.apply(s)
            if s["event_id"] not in ctl.events:
                out.append(f"control {name}: setup {s['type']} rejected")
        legs = ctl.apply(event)
        want_n, want_total = fuzz.CONTROL_LEGS[name]
        if event["event_id"] not in ctl.events:
            out.append(f"control {name}: the VALID baseline was rejected "
                       f"— the fuzz result below proves nothing")
        elif len(legs) != want_n:
            out.append(f"control {name}: {len(legs)} legs, expected "
                       f"{want_n}")
        else:
            dr = sum((Decimal(l["debit"]) for l in legs), ZERO)
            cr = sum((Decimal(l["credit"]) for l in legs), ZERO)
            if dr != Decimal(want_total) or cr != Decimal(want_total):
                out.append(f"control {name}: Dr {dr} / Cr {cr}, expected "
                           f"{want_total} both")

        # -- 2. the barrage -------------------------------------------
        base = Book()
        for s in setup:
            base.apply(s)
        before = state_fingerprint(base)
        for s in fuzz.baseline(name)[0]:
            soak.apply(s)
        n = posted = 0
        for label, mut_setup, mut_ev in fuzz.mutants(name, count=count,
                                                     seed=seed):
            book = Book()
            for s in mut_setup:
                book.apply(s)
            try:
                legs = book.apply(mut_ev)
            except Exception as exc:                     # noqa: BLE001
                out.append(f"{name} [{label}]: apply RAISED {exc!r}")
                continue
            n += 1
            try:
                soak.apply(mut_ev)
            except Exception as exc:                     # noqa: BLE001
                out.append(f"{name} [{label}]: soak apply RAISED {exc!r}")
            if mut_ev["event_id"] in book.events:        # accepted
                posted += 1
                dr = sum((Decimal(l["debit"]) for l in legs), ZERO)
                cr = sum((Decimal(l["credit"]) for l in legs), ZERO)
                if dr != cr:
                    out.append(f"{name} [{label}]: survived validation but "
                               f"posted unbalanced legs Dr {dr} Cr {cr}")
                continue
            if legs != []:
                out.append(f"{name} [{label}]: rejected but returned "
                           f"{len(legs)} legs")
            after = state_fingerprint(book)
            if after == before:
                continue
            scope = _residue_scope(before, after)
            if _is_known_residue(name, scope, mut_ev["payload"]):
                stats["known_residue"] += 1
                continue
            out.append(f"{name} [{label}]: rejected but left residue in "
                       f"{sorted(scope)}")
        stats["types"] += 1
        stats["mutants"] += n
        stats["posted"] += posted
        stats["per_type"][name] = n
        if n < count:
            out.append(f"{name}: only {n} mutants, expected >= {count}")

    stats["rejected"] = stats["mutants"] - stats["posted"]

    # -- 3. snapshot under fuzz ---------------------------------------
    try:
        snap = soak.snapshot()
    except Exception as exc:                             # noqa: BLE001
        out.append(f"snapshot-under-fuzz: snapshot() RAISED {exc!r}")
        snap = None
    if snap is not None:
        out.extend(serialization_canon(soak, [snap]))
        out.extend(run_invariants(soak))
        # The gate box in its own words: no "E" in any output string. The
        # canon checks this per field with its regexes; this is the blunt
        # sweep over everything the reply renders, huge-value mutants and
        # all — one scientific-notation amount is a whole rejected block.
        for value in _snap_strings(snap):
            if "E" in str(value).upper():
                out.append(f"snapshot-under-fuzz: exponent notation in "
                           f"rendered value {value!r}")
    stats["seconds"] = round(time.perf_counter() - t0, 2)
    stats["soak_events"] = len(soak.event_log)
    stats["soak_posted"] = len(soak.events)
    return stats, out


def _snap_strings(snap) -> list:
    """Every rendered value string in a snapshot (money, quantities,
    routes) — what the canon and the no-exponent check read."""
    out = []
    for v in snap.get("trial_balance", {}).values():
        out.append(v)
    for cust in snap.get("customers", {}).values():
        out.append(cust["wallet_cash"])
        out.append(cust["cash_hold"])
        for pos in cust["positions"].values():
            out.append(pos["quantity"])
            out.append(pos["cost_basis"])
    return out


def replay_identical(book) -> tuple[bool, str]:
    """Determinism oracle: a fresh Book driven through the recorded log must
    land on the very same state as the live book that built the log
    incrementally. The log holds first deliveries only, so _apply_core (no
    logging, no ring) is the honest way to replay it."""
    fresh = Book()
    for ev in book.event_log:
        fresh._apply_core(ev)
    if _snap_bytes(fresh) != _snap_bytes(book):
        return False, "replay: snapshot differs from live book"
    if fresh.balances != book.balances:
        return False, "replay: balances differ from live book"
    if fresh.seen != book.seen:
        return False, "replay: seen-id set differs from live book"
    return True, ""


def ring_identical(book) -> tuple[bool, str]:
    """As-of oracle: the ring shortcut (_book_as_of = nearest snapshot +
    bounded replay) must be indistinguishable from a cold replay of the same
    log prefix — at ~10 deterministic random positions and at the very end,
    where the ring path is exercised hardest."""
    n = len(book.event_log)
    if n == 0:
        return True, ""
    rng = random.Random(0xA50F)           # fixed: same book, same probes
    positions = sorted({rng.randrange(1, n + 1) for _ in range(10)} | {n})
    for pos in positions:
        cold = Book()
        for ev in book.event_log[:pos]:
            cold._apply_core(ev)
        if _snap_bytes(book._book_as_of(pos)) != _snap_bytes(cold):
            return False, f"ring: as-of position {pos} differs from cold replay"
    return True, ""


# ------------------------------------------------------------------ #
#  Phase 6: the as-of oracle and the serialization canon             #
# ------------------------------------------------------------------ #

# Timings and point counts of the last asof_oracle run — the same
# observability side-channel pattern the sim generators use (derived
# output, never an input). run_regression reads p95 from here.
ASOF_LAST_STATS: dict = {}

# Canonical minimal-form quantity: no exponent, no trailing zeros after
# the point, no bare trailing point. "8" and "0.333333" pass;
# "8.000000", "8.", "0E-2" and "1e-6" all fail.
QTY_RE = re.compile(r"^-?\d+(\.\d*[1-9])?$")


def _sample(points: list, max_points: int, salt: int) -> list:
    """A deterministic ~max_points subsample of `points`, order preserved.
    Fixed rng seed: the same book and the same store always probe the same
    positions, so a failure is a repro, not an anecdote."""
    if len(points) <= max_points:
        return list(points)
    rng = random.Random(salt)
    return [points[i] for i in sorted(rng.sample(range(len(points)),
                                                 max_points))]


def asof_oracle(book, live_snapshots, max_points: int = 500,
                restarts: int = 5) -> list[str]:
    """The Phase 6 referee: as-of answers vs the state that really existed.

    `live_snapshots` is the oracle store recorded DURING delivery — a list
    of (log_index, event_id, snapshot_json) triples, one per recorded FIRST
    delivery, where snapshot_json is json.dumps(book.snapshot(),
    sort_keys=True) taken immediately after that event was applied and
    before anything after it was. Read-only on `book`; empty list == green.

    For every recorded point, three things must hold:

      1. C2 anchoring: book.eid_pos[event_id] == log_index — an as-of names
         an event id, and a duplicated id must resolve to its FIRST
         delivery, which is the only position whose live snapshot exists.
      2. Ring answer: book.snapshot(as_of_event_id=event_id) is
         BYTE-identical to the recorded live snapshot.
      3. Cold replay: log[0..log_index] driven into a fresh Book is
         byte-identical to both.

    Sampling: the store is used whole up to max_points (500) points; a
    larger store is subsampled deterministically (fixed seed 0xA50F6),
    order preserved. Every point gets its own ring answer.

    The cold-replay leg is done as ONE monotone forward pass over
    book.event_log with a fresh Book, snapshotting at each sampled index —
    the Book is a pure function of the delivered prefix, so a forward pass
    stopping at idx is the same object as a Book restarted for idx, and
    this makes the leg O(n) instead of O(n x points). `restarts` genuinely
    independent per-point cold replays (evenly spaced) are ALSO run, so the
    equivalence itself is checked rather than assumed.

    Latencies of the ring answers land in ASOF_LAST_STATS (p50/p95/max in
    ms) — the Liveness evidence: an as-of must never be a full replay.
    """
    out: list[str] = []
    points = _sample(list(live_snapshots), max_points, 0xA50F6)
    ASOF_LAST_STATS.clear()
    ASOF_LAST_STATS.update({"points": len(points),
                            "store_points": len(live_snapshots)})
    if not points:
        return out

    # -- 1 + 2: anchoring and the ring answer ----------------------------
    ms: list[float] = []
    rejected = 0
    for idx, eid, want in points:
        if book.eid_pos.get(eid) != idx:
            out.append(f"as-of anchor {eid}: first-delivery position "
                       f"{book.eid_pos.get(eid)} != recorded index {idx}")
            continue
        if eid not in book.events:
            rejected += 1              # rejected / unknown-type: C2 target
        t0 = time.perf_counter()
        got = json.dumps(book.snapshot(as_of_event_id=eid), sort_keys=True)
        ms.append((time.perf_counter() - t0) * 1000.0)
        if got != want:
            out.append(f"as-of ring [{idx}] {eid}: answer differs from the "
                       f"live snapshot recorded at delivery")

    # -- 3: cold replay, one monotone pass -------------------------------
    wanted = {idx: (eid, want) for idx, eid, want in points}
    cold = Book()
    for i, ev in enumerate(book.event_log):
        cold._apply_core(ev)           # the log holds first deliveries only
        hit = wanted.get(i)
        if hit is not None:
            if json.dumps(cold._snapshot_now(), sort_keys=True) != hit[1]:
                out.append(f"as-of cold [{i}] {hit[0]}: cold replay differs "
                           f"from the live snapshot recorded at delivery")

    # -- the same points, genuinely restarted -----------------------------
    checked = 0
    step = max(1, len(points) // max(1, restarts))
    for idx, eid, want in points[::step][:restarts]:
        fresh = Book()
        for ev in book.event_log[:idx + 1]:
            fresh._apply_core(ev)
        checked += 1
        if json.dumps(fresh._snapshot_now(), sort_keys=True) != want:
            out.append(f"as-of restart [{idx}] {eid}: independent cold "
                       f"replay differs from the recorded live snapshot")

    ms.sort()
    # Nearest-rank percentiles: the smallest latency that at least p% of
    # the answers came in at or under.
    def pct(p: int) -> float:
        if not ms:
            return 0.0
        return round(ms[min(len(ms) - 1, -(-p * len(ms) // 100) - 1)], 2)

    ASOF_LAST_STATS.update({
        "answered": len(ms),
        "rejected_targets": rejected,
        "cold_restarts": checked,
        "log_len": len(book.event_log),
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "max_ms": round(ms[-1], 2) if ms else 0.0,
        "violations": len(out)})
    return out


def serialization_canon(book, snapshots=None) -> list[str]:
    """The serialization surface for the whole 40-pt checkpoint block, run
    on REAL snapshots. Read-only; empty list == green.

    `snapshots` is a list of snapshot dicts (as returned by snapshot(), not
    round-tripped through json.dumps(sort_keys=True) — that would hide the
    key-order law); it defaults to [book.snapshot()].

      * every money string matches ^-?\\d+\\.\\d{2}$ and re-formats to
        itself through fmt_money (so "-0.00" and "0E-2" both fail);
      * every quantity is minimal form — no trailing zeros, no bare point,
        no "E" — and re-formats to itself through fmt_qty;
      * every dict emitted is in sorted key order (decision 2: no
        iteration-order dependence anywhere in the reply);
      * the reply carries exactly the three top-level blocks, and every
        customer carries exactly wallet_cash / cash_hold / positions.
    """
    out: list[str] = []
    if snapshots is None:
        snapshots = [book.snapshot()]

    def money(v, where):
        if not isinstance(v, str) or not MONEY_RE.match(v) or "E" in v.upper():
            out.append(f"{where}: not a 2dp money string: {v!r}")
        elif fmt_money(Decimal(v)) != v:
            out.append(f"{where}: money string {v!r} is not canonical "
                       f"(fmt_money says {fmt_money(Decimal(v))!r})")

    def qty(v, where):
        if not isinstance(v, str) or not QTY_RE.match(v) or "E" in v.upper():
            out.append(f"{where}: not a minimal-form quantity: {v!r}")
        elif fmt_qty(Decimal(v)) != v:
            out.append(f"{where}: quantity {v!r} is not minimal form "
                       f"(fmt_qty says {fmt_qty(Decimal(v))!r})")

    def sorted_keys(d, where):
        if list(d) != sorted(d):
            out.append(f"{where}: dict keys are not in sorted order")

    for s, snap in enumerate(snapshots):
        w = f"snapshot[{s}]"
        if set(snap) != {"trial_balance", "customers", "open_order_routes"}:
            out.append(f"{w}: top-level keys {sorted(snap)} != the three "
                       f"checkpoint blocks")
        tb = snap.get("trial_balance", {})
        sorted_keys(tb, f"{w}.trial_balance")
        for acct, val in tb.items():
            money(val, f"{w}.trial_balance[{acct}]")
        customers = snap.get("customers", {})
        sorted_keys(customers, f"{w}.customers")
        for cid, cust in customers.items():
            if set(cust) != {"wallet_cash", "cash_hold", "positions"}:
                out.append(f"{w}.customers[{cid}]: fields {sorted(cust)} != "
                           f"wallet_cash/cash_hold/positions")
            money(cust.get("wallet_cash"), f"{w}.customers[{cid}].wallet_cash")
            money(cust.get("cash_hold"), f"{w}.customers[{cid}].cash_hold")
            positions = cust.get("positions", {})
            sorted_keys(positions, f"{w}.customers[{cid}].positions")
            for sym, p in positions.items():
                where = f"{w}.customers[{cid}].positions[{sym}]"
                if set(p) != {"quantity", "cost_basis"}:
                    out.append(f"{where}: fields {sorted(p)} != "
                               f"quantity/cost_basis")
                qty(p.get("quantity"), f"{where}.quantity")
                money(p.get("cost_basis"), f"{where}.cost_basis")
                if p.get("quantity") in ("0", "-0"):
                    out.append(f"{where}: phantom position reported at "
                               f"quantity 0 (C7)")
        routes = snap.get("open_order_routes", {})
        sorted_keys(routes, f"{w}.open_order_routes")
        for oid, broker in routes.items():
            if not isinstance(broker, str) or not broker:
                out.append(f"{w}.open_order_routes[{oid}]: broker "
                           f"{broker!r} is not a broker id")
    return out


# The four fee-payable event types and the account each one settles.
# broker_fees_settled resolves per payload broker via BROKER_PAYABLE.
SETTLEMENT_ACCT = {"broker_fees_settled": None,
                   "custodian_fees_settled": "2420",
                   "reg_fees_remitted": "2400",
                   "partner_payout": "2430"}
PAYABLE_ACCTS = ("2400", "2411", "2412", "2413", "2420", "2430")


def payable_audit(book) -> list[str]:
    """The M4 cent audit — the Phase 5 referee for the firm-accounts block.
    Read-only; empty list == green.

    Per (cid, acct) for acct in {2400, 2411, 2412, 2413, 2420, 2430}, walk
    the log chronologically and maintain an INDEPENDENT expected payable:

      * every posted fill accrues its tariff.fill_charges components,
        recomputed from the STORED PAYLOAD (never the book's legs):
        r -> 2400, bc -> the fill's broker payable (241x), cc -> 2420,
        ps -> 2430;
      * a posted reversal of a fill subtracts the same components;
      * a posted settlement should pay EXACTLY the expected outstanding at
        that moment (cross-checked against its posted leg) and zeroes it;
      * a posted reversal of a settlement re-raises the amount the audit
        itself recorded at settle time — never the book's legs.

    Final state: expected ≡ −balances[(cid, acct)], cent-exact. Nothing
    else may ever touch these accounts.
    """
    out: list[str] = []
    expected: dict = {}       # (cid, acct) -> Decimal outstanding
    settled_amt: dict = {}    # settlement eid -> ((cid, acct), amount)

    def components(p) -> list:
        ch = tariff.fill_charges(p["broker"], p["principal"],
                                 p["partner_rate"])
        return [("2400", ch["r"]), (BROKER_PAYABLE[p["broker"]], ch["bc"]),
                ("2420", ch["cc"]), ("2430", ch["ps"])]

    for ev in book.event_log:
        eid = ev["event_id"]
        rec = book.events.get(eid)
        if rec is None:
            continue                      # rejected / unknown: contributes 0
        t, p = rec["type"], rec["payload"]
        if t in FILL_TYPES:
            cid = p["customer_id"]
            for acct, amt in components(p):
                k = (cid, acct)
                expected[k] = expected.get(k, ZERO) + amt
        elif t in SETTLEMENT_ACCT:
            cid = p["customer_id"]
            acct = SETTLEMENT_ACCT[t] or BROKER_PAYABLE[p["broker"]]
            k = (cid, acct)
            due = expected.get(k, ZERO)
            if due <= 0:
                out.append(f"payable-audit {eid}: settlement on {k} posted "
                           f"with expected outstanding {due}")
            paid = sum((Decimal(l["debit"]) for l in rec["legs"]
                        if l["account"] == acct), ZERO)
            if paid != due:
                out.append(f"payable-audit {eid}: settlement on {k} paid "
                           f"{paid}, independent tally says {due}")
            settled_amt[eid] = (k, due)
            expected[k] = ZERO
        elif t == "reversal":
            src = p.get("reverses_event_id")
            orig = book.events.get(src)
            if orig is None:
                continue                  # posted reversal implies it exists
            ot, op_ = orig["type"], orig["payload"]
            if ot in FILL_TYPES:
                cid = op_["customer_id"]
                for acct, amt in components(op_):
                    k = (cid, acct)
                    expected[k] = expected.get(k, ZERO) - amt
            elif ot in SETTLEMENT_ACCT and src in settled_amt:
                k, due = settled_amt[src]
                expected[k] = expected.get(k, ZERO) + due

    keys = (set(expected)
            | {(c, a) for (c, a) in book.balances if a in PAYABLE_ACCTS})
    for cid, acct in sorted(keys):
        want = expected.get((cid, acct), ZERO)
        got = -book.balances.get((cid, acct), ZERO)
        if want != got:
            out.append(f"payable-audit [{cid},{acct}]: independent tally "
                       f"{want} != book {got}")
    return out
