"""The fuzz rig: one VALID baseline per event type, and four mutators.

Two halves, and the first one is the one that matters:

  * BASELINES — a *valid* payload factory for every event type the
    protocol defines, each returning ``(setup_events, event)`` so a
    stateful type gets a working prefix (a fee before its refund, a
    placement before its fill, a lot before its sell). These are the
    control group. A fuzzer without one proves nothing: a Book that
    rejected every event on sight would score a perfect "zero crashes,
    zero residue". The baselines are lifted from the graded worked
    examples — the BRK-A buy fill posts the 13 legs at Dr = Cr =
    2005.13, the BRK-C sell posts its 13 at Dr = Cr = 1101.16 — so the
    control group is checked against the spec, not against ourselves.
  * The four mutators from the phase plan, each seedable and each
    applied at every applicable position:

        drop_field   remove one field (nested paths included)
        mutate_type  number -> non-numeric string, string -> list,
                     scalar -> dict / None / bool
        flip_sign    negate one numeric field (drives the S10 rejects)
        huge_value   10**30, "9"*400, 1e308, "Infinity", "NaN"

    ``mutants()`` emits every single-position mutant of all four
    operators exhaustively first — that is the part that is a proof
    rather than a sample — then fills the run out to >= 1000 per event
    type with seeded 1-3 operator combinations. Combinations are what
    reach the nested paths: once mutate_type has turned a scalar into a
    dict, the path enumerator sees inside it.

Deliberately dependency-free: this module builds dicts and nothing
else. It never imports book.py, so a bug in the Book cannot quietly
become a bug in the fuzzer that hides it.

Baselines are also chosen to leave every OBSERVE detector silent
(principal == qty x price, dividends on real positions, withdrawals
inside the wallet), so a control-group failure means a handler broke,
never that a detector spoke up.
"""
from __future__ import annotations

import copy
import random
import zlib

# One customer for almost everything (transfers need two), fixed ids for
# the stateful references. Every mutant runs against a FRESH book, so
# constant ids never collide.
CID = "FZ-C1"
CID2 = "FZ-C2"

HUGE_VALUES = (10 ** 30, "9" * 400, 1e308, "Infinity", "NaN")
TYPE_VARIANTS = ("string", "list", "dict", "none", "bool")
OPERATORS = ("drop_field", "mutate_type", "flip_sign", "huge_value")


def _ev(eid: str, etype: str, payload: dict, offset: int) -> dict:
    return {"offset": offset, "event_id": eid, "type": etype,
            "payload": payload}


# ------------------------------------------------------------------ #
#  payload fixtures                                                  #
# ------------------------------------------------------------------ #
# The graded worked example: BRK-A / equity, P = 1000.00, rate 0.50.
#   b 2.00  c 0.40  r 0.80  bc 1.25  cc 0.20  ps 0.48  -> Dr = Cr = 2005.13
def _buy_fill(oid="FZ-O1", tid="FZ-T1", cid=CID, symbol="AAPL",
              qty="10", price="100", principal="1000.00", broker="BRK-A",
              asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": "buy", "symbol": symbol, "quantity": qty,
            "price": price, "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


def _placement(oid="FZ-O1", cid=CID, side="buy", symbol="AAPL", qty="10",
               limit="100", est="5.00", asset_class="equity") -> dict:
    return {"order_id": oid, "customer_id": cid, "side": side,
            "symbol": symbol, "quantity": qty, "limit_price": limit,
            "est_charges": est, "asset_class": asset_class}


def _deposit(cid=CID, amount="500.00") -> dict:
    return {"customer_id": cid, "amount": amount}


# Every baseline: name -> () -> (setup_events, event). Names are event
# types except the two fill variants, which are graded separately.
def _b_deposit():
    return [], _ev("FZ-E", "deposit", _deposit(), 100)


def _b_fee_charged():
    setup = [_ev("FZ-S1", "deposit", _deposit(), 1)]
    return setup, _ev("FZ-E", "fee_charged",
                      {"customer_id": CID, "amount": "12.50"}, 100)


def _b_fee_refund():
    setup = [_ev("FZ-S1", "deposit", _deposit(), 1),
             _ev("FZ-FEE", "fee_charged",
                 {"customer_id": CID, "amount": "12.50"}, 2)]
    return setup, _ev("FZ-E", "fee_refund",
                      {"refunds_source_id": "FZ-FEE",
                       "customer_id": CID}, 100)


def _b_withdrawal_requested():
    # amount well inside the wallet: D11 (overdraw) must stay silent.
    setup = [_ev("FZ-S1", "deposit", _deposit(), 1)]
    return setup, _ev("FZ-E", "withdrawal_requested",
                      {"customer_id": CID, "withdrawal_id": "FZ-W1",
                       "amount": "100.00"}, 100)


def _wd_setup():
    return [_ev("FZ-S1", "deposit", _deposit(), 1),
            _ev("FZ-S2", "withdrawal_requested",
                {"customer_id": CID, "withdrawal_id": "FZ-W1",
                 "amount": "100.00"}, 2)]


def _b_withdrawal_settled():
    return _wd_setup(), _ev("FZ-E", "withdrawal_settled",
                            {"withdrawal_id": "FZ-W1"}, 100)


def _b_withdrawal_rejected():
    return _wd_setup(), _ev("FZ-E", "withdrawal_rejected",
                            {"withdrawal_id": "FZ-W1"}, 100)


def _b_interest_credited():
    return [], _ev("FZ-E", "interest_credited",
                   {"customer_id": CID, "gross_amount": "10.00",
                    "customer_share": "6.00"}, 100)


def _b_transfer_between_customers():
    setup = [_ev("FZ-S1", "deposit", _deposit(), 1)]
    return setup, _ev("FZ-E", "transfer_between_customers",
                      {"from_customer_id": CID, "to_customer_id": CID2,
                       "amount": "25.00"}, 100)


def _b_fx_deposit():
    # usd amounts agree with amount_foreign x rate to the cent (D6 quiet)
    # and the customer rate is worse than market (a legal spread).
    return [], _ev("FZ-E", "fx_deposit",
                   {"customer_id": CID, "amount_foreign": "100.00",
                    "currency": "EUR", "market_rate": "1.10",
                    "customer_rate": "1.05",
                    "usd_at_market_rate": "110.00",
                    "usd_at_customer_rate": "105.00"}, 100)


def _b_order_placed():
    return [], _ev("FZ-E", "order_placed", _placement(), 100)


def _b_order_partially_filled():
    # placed for 20, filled for 10: D9 (overfill) stays silent, the hold
    # keeps a remainder, and the order stays open.
    setup = [_ev("FZ-S1", "order_placed", _placement(qty="20"), 1)]
    return setup, _ev("FZ-E", "order_partially_filled", _buy_fill(), 100)


def _b_order_filled():
    setup = [_ev("FZ-S1", "order_placed", _placement(), 1)]
    return setup, _ev("FZ-E", "order_filled", _buy_fill(), 100)


def _b_order_filled_sell():
    # The graded sell example: one lot of 10 SPY at 1000.00, sell 5 at
    # P = 600.00 through BRK-C / etf, rate 0.25 -> Dr = Cr = 1101.16.
    setup = [_ev("FZ-S1", "order_filled",
                 _buy_fill(oid="FZ-OB", tid="FZ-TB", symbol="SPY",
                           asset_class="etf"), 1),
             _ev("FZ-S2", "order_placed",
                 _placement(oid="FZ-OS", side="sell", symbol="SPY",
                            qty="10", limit="100", est="0.00",
                            asset_class="etf"), 2)]
    return setup, _ev("FZ-E", "order_filled",
                      {"order_id": "FZ-OS", "trade_id": "FZ-TS",
                       "customer_id": CID, "side": "sell", "symbol": "SPY",
                       "quantity": "5", "price": "120",
                       "principal": "600.00", "broker": "BRK-C",
                       "asset_class": "etf", "partner_rate": "0.25"}, 100)


def _filled_setup():
    return [_ev("FZ-S1", "order_placed", _placement(), 1),
            _ev("FZ-S2", "order_filled", _buy_fill(), 2)]


def _b_trade_settled():
    return _filled_setup(), _ev("FZ-E", "trade_settled",
                                {"trade_id": "FZ-T1"}, 100)


def _b_order_cancelled():
    setup = [_ev("FZ-S1", "order_placed", _placement(), 1)]
    return setup, _ev("FZ-E", "order_cancelled",
                      {"order_id": "FZ-O1"}, 100)


def _b_order_rejected():
    setup = [_ev("FZ-S1", "order_placed", _placement(), 1)]
    return setup, _ev("FZ-E", "order_rejected",
                      {"order_id": "FZ-O1"}, 100)


def _b_broker_fees_settled():
    # the buy fill accrued 2411 = bc = 1.25
    return _filled_setup(), _ev("FZ-E", "broker_fees_settled",
                                {"customer_id": CID,
                                 "broker": "BRK-A"}, 100)


def _b_custodian_fees_settled():
    return _filled_setup(), _ev("FZ-E", "custodian_fees_settled",
                                {"customer_id": CID}, 100)


def _b_reg_fees_remitted():
    return _filled_setup(), _ev("FZ-E", "reg_fees_remitted",
                                {"customer_id": CID}, 100)


def _b_partner_payout():
    return _filled_setup(), _ev("FZ-E", "partner_payout",
                                {"customer_id": CID}, 100)


def _held_setup():
    """One AAPL lot: 10 shares at 1000.00, so the dividend and corporate
    baselines act on a real position (D10 stays silent)."""
    return [_ev("FZ-S1", "order_filled", _buy_fill(), 1)]


def _b_dividend_cash():
    return _held_setup(), _ev("FZ-E", "dividend_cash",
                              {"customer_id": CID, "symbol": "AAPL",
                               "gross_amount": "10.00",
                               "withholding_tax": "1.50",
                               "net_amount": "8.50"}, 100)


def _b_dividend_reinvested():
    return _held_setup(), _ev("FZ-E", "dividend_reinvested",
                              {"customer_id": CID, "symbol": "AAPL",
                               "gross_amount": "12.00",
                               "withholding_tax": "2.00",
                               "net_amount": "10.00",
                               "reinvest_price": "5.00",
                               "reinvest_quantity": "2"}, 100)


def _b_stock_split():
    return _held_setup(), _ev("FZ-E", "stock_split",
                              {"customer_id": CID, "symbol": "AAPL",
                               "ratio_from": "1", "ratio_to": "2"}, 100)


def _b_symbol_change():
    return _held_setup(), _ev("FZ-E", "symbol_change",
                              {"customer_id": CID, "old_symbol": "AAPL",
                               "new_symbol": "AAPL2"}, 100)


def _b_reversal():
    setup = [_ev("FZ-DEP", "deposit", _deposit(), 1)]
    return setup, _ev("FZ-E", "reversal",
                      {"reverses_event_id": "FZ-DEP"}, 100)


BASELINES = {
    "deposit": _b_deposit,
    "fee_charged": _b_fee_charged,
    "fee_refund": _b_fee_refund,
    "withdrawal_requested": _b_withdrawal_requested,
    "withdrawal_settled": _b_withdrawal_settled,
    "withdrawal_rejected": _b_withdrawal_rejected,
    "interest_credited": _b_interest_credited,
    "transfer_between_customers": _b_transfer_between_customers,
    "fx_deposit": _b_fx_deposit,
    "order_placed": _b_order_placed,
    "order_partially_filled": _b_order_partially_filled,
    "order_filled": _b_order_filled,
    "order_filled_sell": _b_order_filled_sell,
    "trade_settled": _b_trade_settled,
    "order_cancelled": _b_order_cancelled,
    "order_rejected": _b_order_rejected,
    "broker_fees_settled": _b_broker_fees_settled,
    "custodian_fees_settled": _b_custodian_fees_settled,
    "reg_fees_remitted": _b_reg_fees_remitted,
    "partner_payout": _b_partner_payout,
    "dividend_cash": _b_dividend_cash,
    "dividend_reinvested": _b_dividend_reinvested,
    "stock_split": _b_stock_split,
    "symbol_change": _b_symbol_change,
    "reversal": _b_reversal,
}

# The 24 protocol event types, once each (the two fill variants share
# order_filled). Kept as data so the test can assert coverage.
EVENT_TYPES = sorted({"order_filled" if n == "order_filled_sell" else n
                      for n in BASELINES})

# What the UNMUTATED baseline must do: (leg count, Sum debit == Sum credit).
# This is the control group, and it is the only thing standing between
# this suite and a Book that scores a perfect zero-crash / zero-residue
# run by rejecting every event it is handed. The two fill rows are the
# graded worked examples verbatim.
CONTROL_LEGS: dict[str, tuple[int, str]] = {
    "deposit": (2, "500.00"),
    "fee_charged": (2, "12.50"),
    "fee_refund": (2, "12.50"),
    "withdrawal_requested": (2, "100.00"),
    "withdrawal_settled": (2, "100.00"),
    "withdrawal_rejected": (2, "100.00"),
    "interest_credited": (3, "10.00"),
    "transfer_between_customers": (2, "25.00"),
    "fx_deposit": (3, "110.00"),
    "order_placed": (0, "0.00"),
    "order_partially_filled": (13, "2005.13"),
    "order_filled": (13, "2005.13"),
    "order_filled_sell": (13, "1101.16"),
    "trade_settled": (2, "1000.00"),
    "order_cancelled": (0, "0.00"),
    "order_rejected": (0, "0.00"),
    "broker_fees_settled": (2, "1.25"),      # bc = 0.90 + 0.35 ticket
    "custodian_fees_settled": (2, "0.20"),   # cc
    "reg_fees_remitted": (2, "0.80"),        # r = 8 bps
    "partner_payout": (2, "0.48"),           # ps, the half-cent HALF_UP
    "dividend_cash": (2, "8.50"),
    "dividend_reinvested": (2, "10.00"),
    "stock_split": (0, "0.00"),
    "symbol_change": (0, "0.00"),
    "reversal": (2, "500.00"),
}

def _namespace(name: str, setup: list, event: dict) -> tuple[list, dict]:
    """Prefix every id this baseline owns with its own name.

    Per-mutant runs use a fresh Book and would not care, but the
    snapshot-under-fuzz run drives ONE book through every type's whole
    barrage: without this, `order_filled`'s setup would re-place
    `order_placed`'s order id, be rejected as a duplicate placement, and
    quietly disarm that type. Event ids, order/trade/withdrawal ids and
    customer ids are all namespaced (everything the fixtures spell
    "FZ-..."); brokers and symbols are shared on purpose. The one prefix
    rule covers cross-references too — `refunds_source_id` and
    `reverses_event_id` name "FZ-" ids, so they move with them.
    """
    for e in setup + [event]:
        e["event_id"] = f"{name}:{e['event_id']}"
        for k, v in e["payload"].items():
            if isinstance(v, str) and v.startswith("FZ-"):
                e["payload"][k] = f"{name}:{v}"
    return setup, event


def baseline(name: str) -> tuple[list, dict]:
    """(setup_events, event) — fresh dicts every call, so a mutation can
    never leak into the next mutant."""
    setup, event = BASELINES[name]()
    return _namespace(name, setup, event)


# ------------------------------------------------------------------ #
#  paths                                                             #
# ------------------------------------------------------------------ #

def paths(value, prefix: tuple = ()) -> list[tuple]:
    """Every addressable field path, parents before children. Recursive,
    so a dict that a previous mutation grew is fuzzed inside as well."""
    out: list[tuple] = []
    if isinstance(value, dict):
        for k in value:
            out.append(prefix + (k,))
            out.extend(paths(value[k], prefix + (k,)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.append(prefix + (i,))
            out.extend(paths(v, prefix + (i,)))
    return out


def _get(obj, path):
    for key in path:
        obj = obj[key]
    return obj


def _set(obj, path, value) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def _del(obj, path) -> None:
    for key in path[:-1]:
        obj = obj[key]
    del obj[path[-1]]


def _numeric(value) -> bool:
    """Does this field carry a number we can negate? Strings that happen
    to be numeric count — every money field on the wire is one."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


# ------------------------------------------------------------------ #
#  the four operators                                                #
# ------------------------------------------------------------------ #
# Each takes a payload and a path, returns a NEW payload, or None when
# the operator does not apply at that position.

def drop_field(payload, path, _variant=None):
    """Remove one field, one at a time — nested paths included."""
    out = copy.deepcopy(payload)
    try:
        _del(out, path)
    except (KeyError, IndexError, TypeError):
        return None
    return out


def mutate_type(payload, path, variant):
    """Swap a field's TYPE while keeping it present: a number becomes a
    non-numeric string, a string becomes a list, any scalar becomes a
    dict, None, or a bool."""
    out = copy.deepcopy(payload)
    try:
        old = _get(out, path)
    except (KeyError, IndexError, TypeError):
        return None
    new = {"string": "not-a-number", "list": [old], "dict": {"value": old},
           "none": None, "bool": True}[variant]
    try:
        _set(out, path, new)
    except (KeyError, IndexError, TypeError):
        return None
    return out


def flip_sign(payload, path, _variant=None):
    """Negate one numeric field — the S10 driver (negative or zero
    amounts, quantities and prices must all reject)."""
    out = copy.deepcopy(payload)
    try:
        old = _get(out, path)
    except (KeyError, IndexError, TypeError):
        return None
    if not _numeric(old):
        return None
    if isinstance(old, str):
        new = old[1:] if old.startswith("-") else "-" + old
    else:
        new = -old
    _set(out, path, new)
    return out


def huge_value(payload, path, variant):
    """10**30, a 400-digit string, 1e308, "Infinity", "NaN" — the values
    that make Decimal.quantize raise InvalidOperation, and the ones a
    float() or a pre-quantize comparison would leak an OverflowError on."""
    out = copy.deepcopy(payload)
    try:
        _get(out, path)
    except (KeyError, IndexError, TypeError):
        return None
    _set(out, path, variant)
    return out


_OPS = {"drop_field": (drop_field, (None,)),
        "mutate_type": (mutate_type, TYPE_VARIANTS),
        "flip_sign": (flip_sign, (None,)),
        "huge_value": (huge_value, HUGE_VALUES)}


def apply_operator(payload, op: str, path: tuple, variant=None):
    """One operator at one position. None when it does not apply."""
    return _OPS[op][0](payload, path, variant)


# ------------------------------------------------------------------ #
#  the generator                                                     #
# ------------------------------------------------------------------ #

def mutants(name: str, count: int = 1000, seed: int = 20260806):
    """Yield ``(label, setup_events, event)`` for one event type.

    Phase 1 is exhaustive and seed-independent: every operator at every
    applicable position, plus the payload-is-not-a-dict cases. That is
    the part that is a proof. Phase 2 is seeded and tops the run up to
    ``count`` with 1-3 stacked operator applications, re-enumerating
    paths between steps so nested structures created by mutate_type get
    fuzzed in turn.
    """
    _setup, event = baseline(name)
    base = event["payload"]
    etype = event["type"]
    emitted = 0

    def emit(label, payload):
        nonlocal emitted
        s, e = baseline(name)          # fresh setup + envelope every time
        e["event_id"] = f"fz-{name}-{emitted}"
        e["payload"] = payload
        emitted += 1
        return (label, s, e)

    field_paths = paths(base)

    # -- phase 1: every operator at every position ---------------------
    for op in OPERATORS:
        fn, variants = _OPS[op]
        for path in field_paths:
            for variant in variants:
                out = fn(copy.deepcopy(base), path, variant)
                if out is None:
                    continue
                dotted = ".".join(str(k) for k in path)
                tag = f"{op}:{dotted}" if variant is None \
                    else f"{op}:{dotted}={variant!r}"
                yield emit(tag, out)

    # A payload that is not a dict at all — the S9 shape every handler's
    # isinstance guard exists for.
    for junk in (None, [], "junk", 0, ["a", "b"], {"nested": {"x": 1}}):
        yield emit(f"payload={junk!r}", copy.deepcopy(junk))

    # -- phase 2: seeded combinations, up to count ---------------------
    rng = random.Random(seed ^ zlib.crc32(name.encode()) ^ len(etype))
    while emitted < count:
        payload = copy.deepcopy(base)
        labels = []
        for _ in range(rng.randint(1, 3)):
            here = paths(payload)
            if not here:
                break
            path = here[rng.randrange(len(here))]
            op = OPERATORS[rng.randrange(len(OPERATORS))]
            fn, variants = _OPS[op]
            variant = variants[rng.randrange(len(variants))]
            out = fn(payload, path, variant)
            if out is None:
                continue
            payload = out
            labels.append(f"{op}:{'.'.join(str(k) for k in path)}")
        if not labels:
            continue
        yield emit("combo[" + "|".join(labels) + "]", payload)
