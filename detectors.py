"""Defect detectors — the arena guarantees at least one systematic defect:
"a class of event that is internally well-formed and wrong", with nothing
else disclosed. Our book's invariants are how we find it.

THE ARMING POLICY, AND WHY IT IS DELIBERATELY TIMID
---------------------------------------------------
The payoff is asymmetric and it runs against us:

  * A defect event we wrongly POST loses that event's weight — and a
    correctly-rejected event is a no-leg event, scored at a QUARTER of a
    normal event.
  * A clean event we wrongly REJECT loses that event's FULL weight.

So a false positive costs roughly four times what the miss it prevents
would have saved. An unproven detector is therefore worth less than no
detector, and the default here is OBSERVE: log the finding, post the
event unchanged, decide later with evidence.

A detector is promoted to ARMED only when it is either (a) a pure lookup
or an economic impossibility that cannot false-positive by construction,
or (b) proven by the deployment rule — zero attributable disagreements
across >= 2 full practice feeds, A/B replayed with the detector on and
off (tools/detector_ab.py).

WHAT IS ARMED TODAY, AND WHERE IT LIVES
---------------------------------------
D1 and D3 are enforced INLINE in book.py, at their handler's validation
step, and are left there deliberately: they are proven, tested, and
moving working rejects into a new indirection buys nothing.

  D1  a fill naming a broker that does not trade that asset class
      (book.py, _fill) — a table lookup against the published tariff;
      the arena's own table says it cannot happen. Zero FP.
  D3  interest whose customer_share exceeds the gross
      (book.py, on_interest_credited) — the firm cannot pay out more
      interest than it received; the remainder leg would be negative
      income. Economic impossibility, not a rounding question.

  (The fx "customer rate better than market" reject is not a detector at
  all — the spec states it as a rule.)

D2 IS SHIPPED **OBSERVE**, WHICH DEVIATES FROM THE PHASE PLAN
--------------------------------------------------------------
The plan lists D2 (dividend `net != gross - withholding_tax`) as ARMED,
reasoning that an arithmetic identity cannot false-positive. That
reasoning does not survive contact with the spec's own rounding rule:
"every amount is rounded to the cent independently". If the feed rounds
gross, tax and net independently from unrounded values, the identity can
legitimately break by a cent on clean data — e.g. raw 10.005 / 1.004 /
9.001 rounds to 10.01 / 1.00 / 9.00, where gross - tax = 9.01 != 9.00.

We cannot know which convention the feed uses without looking at one,
and the cost of being wrong is asymmetric. So D2 observes, every
occurrence is logged, and the first practice feed answers it in seconds:
if the identity holds on every dividend except a distinct cluster, that
cluster IS the planted defect and D2 arms for submission and final. If
it breaks diffusely, D2 stays disarmed and we lost nothing.

Everything else (D4-D11) observes for the reasons in the table below.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

import tariff
from tariff import money

D = Decimal
CENT = D("0.01")

# Where each detector is enforced or observed, and why it is not armed.
#   ARMED   -> the event is rejected (legs [], book untouched)
#   OBSERVE -> the event posts exactly as it would without the detector;
#              the finding is only recorded
#   OFF     -> not evaluated
DETECTOR_MODE: dict[str, str] = {
    "D1": "ARMED",     # inline in book.py — tariff coverage lookup
    "D2": "OBSERVE",   # deviation from plan; see the module docstring
    "D3": "ARMED",     # inline in book.py — economic impossibility
    "D4": "OBSERVE",   # fill outside the order's limit price
    "D5": "OBSERVE",   # principal != qty x price (cent-convention risk)
    "D6": "OBSERVE",   # fx usd amounts vs rates (quote-orientation risk)
    "D7": "OBSERVE",   # reinvest net vs price x qty (same cent risk)
    # D8 — ARMED on evidence, not on theory. Practice run 1 identified it
    # as the arena's planted systematic defect: a second fill reusing an
    # earlier fill's trade_id, internally well-formed in every other way.
    # Over that feed the predicate fired on exactly the events the
    # reference rejected — zero false positives, zero misses — which is
    # the deployment rule satisfied on real data rather than on our own
    # generators. Enforced inline in book.py's _fill validation block.
    "D8": "ARMED",     # duplicate trade_id across distinct fills
    "D9": "OBSERVE",   # cumulative overfill vs placed quantity
    "D10": "OBSERVE",  # dividend on a zero position (ordering FP risk)
    "D11": "OBSERVE",  # overdraw withdrawal (highest FP risk — fees
                       # legitimately push wallets negative)
}

# A/B and post-practice arming without editing code:
#   DETECTOR_MODES="D2=ARMED,D5=OFF" python client.py ...
for _pair in os.environ.get("DETECTOR_MODES", "").split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        _k, _v = _k.strip().upper(), _v.strip().upper()
        if _k in DETECTOR_MODE and _v in ("ARMED", "OBSERVE", "OFF"):
            DETECTOR_MODE[_k] = _v


def mode(det_id: str) -> str:
    return DETECTOR_MODE.get(det_id, "OFF")


# ------------------------------------------------------------------ #
#  predicates — every one is READ-ONLY on the book                   #
# ------------------------------------------------------------------ #
# Each returns (observed, expected) on a finding, or None. They must
# never raise: a detector that throws would turn a postable event into a
# rejected one, which is the exact failure this module exists to avoid.
# The caller wraps them anyway; belt and braces.

def _dec(x):
    v = D(str(x))
    if not v.is_finite():
        raise ArithmeticError("non-finite")
    return v


def d4_limit_violation(p, book):
    """A buy filled above its limit, or a sell filled below it. Compared
    against the FIRST-DELIVERED placement only: a fill that arrived
    before its placement has no limit to check, so it is skipped — that
    is what makes this zero-false-positive under out-of-order delivery."""
    o = book.orders.get(p.get("order_id"))
    if not o or not o.get("placed") or o.get("limit") is None:
        return None
    price, limit = _dec(p["price"]), o["limit"]
    if p.get("side") == "buy" and price > limit:
        return (str(price), f"<= {limit}")
    if p.get("side") == "sell" and price < limit:
        return (str(price), f">= {limit}")
    return None


def d5_principal_mismatch(p, book):
    """principal != money(quantity x price). Observe-only: a feed may
    round principal by a different convention and be a cent out on
    perfectly clean fills."""
    got = money(p["principal"])
    want = money(_dec(p["quantity"]) * _dec(p["price"]))
    return (str(got), str(want)) if got != want else None


def d6_fx_mismatch(p, book):
    """usd_at_*_rate vs amount_foreign x the matching rate. Observe-only:
    quote orientation (USD-per-foreign vs foreign-per-USD) is ambiguous,
    so a 'mismatch' may just mean we multiplied where we should divide."""
    fx = _dec(p["amount_foreign"])
    for rate_f, usd_f in (("market_rate", "usd_at_market_rate"),
                          ("customer_rate", "usd_at_customer_rate")):
        got = money(p[usd_f])
        want = money(fx * _dec(p[rate_f]))
        if abs(got - want) > CENT:
            return (f"{usd_f}={got}", str(want))
    return None


def d9_overfill(p, book):
    """Cumulative filled quantity exceeding the placed quantity. Skipped
    entirely when the placement has not been delivered (out-of-order
    caveat) — zero FP by construction."""
    o = book.orders.get(p.get("order_id"))
    if not o or not o.get("placed") or o.get("qty_ordered") is None:
        return None
    total = o["filled_qty"] + _dec(p["quantity"])
    if total > o["qty_ordered"]:
        return (str(total), f"<= {o['qty_ordered']}")
    return None


def d10_phantom_dividend(p, book):
    """A dividend for a symbol the customer holds none of. HIGH false
    positive risk — the stream is deliberately not ordered, so a
    dividend can legitimately precede the buy that creates the position.
    Observe forever unless practice proves otherwise."""
    cid, symbol = p.get("customer_id"), p.get("symbol")
    if not symbol:
        return None       # no symbol to check a holding against
    index = book.lot_index.get((cid, symbol), [])
    held = sum((book.lots[i]["qty"] for i in index), D(0))
    return ("0", "> 0") if held == 0 else None


def d11_overdraw_withdrawal(p, book):
    """A withdrawal larger than the wallet. HIGHEST false positive risk
    of all: fees and charges legitimately drive wallets negative, so a
    'overdraw' is usually just a customer who owes the firm money.
    Observe only; arming this without practice evidence would be the
    single most expensive detector mistake available."""
    cid = p.get("customer_id")
    wallet = -book.balances.get((cid, "2010"), D("0.00"))
    amount = money(p["amount"])
    return (str(amount), f"<= {wallet}") if amount > wallet else None


# Detector id -> (event types it applies to, predicate). D1/D2/D3/D7/D8
# are enforced or observed inline in book.py at their handler sites and
# are deliberately not duplicated here — one implementation each.
DETECTORS: dict[str, tuple[tuple[str, ...], object]] = {
    "D4": (("order_partially_filled", "order_filled"), d4_limit_violation),
    "D5": (("order_partially_filled", "order_filled"), d5_principal_mismatch),
    "D6": (("fx_deposit",), d6_fx_mismatch),
    "D9": (("order_partially_filled", "order_filled"), d9_overfill),
    "D10": (("dividend_cash", "dividend_reinvested"), d10_phantom_dividend),
    "D11": (("withdrawal_requested",), d11_overdraw_withdrawal),
}

# ------------------------------------------------------------------ #
#  the quarantine file — a WRITE-ONLY side channel                   #
# ------------------------------------------------------------------ #
# Nothing in the book ever reads this back. If it did, a run with
# logging enabled would diverge from a replay of the same log, and
# as-of answers and crash recovery would quietly start lying.
QUARANTINE_PATH = os.environ.get("QUARANTINE_LOG", "")


def log_finding(event_id, ev_type, det_id, observed, expected,
                det_mode, action) -> None:
    if not QUARANTINE_PATH:
        return
    try:
        with open(QUARANTINE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": event_id, "type": ev_type,
                                 "detector": det_id, "observed": observed,
                                 "expected": expected, "mode": det_mode,
                                 "action": action}) + "\n")
    except Exception:
        pass          # a log that cannot be written must never stop a run


def run(ev_type: str, payload, book) -> list[tuple[str, str, str, str]]:
    """Every applicable finding for this event, as
    (detector_id, mode, observed, expected). Read-only; never raises."""
    out = []
    if not isinstance(payload, dict):
        return out
    for det_id, (types, predicate) in DETECTORS.items():
        if ev_type not in types:
            continue
        m = mode(det_id)
        if m == "OFF":
            continue
        try:
            found = predicate(payload, book)
        except Exception:
            # A malformed payload is the malformed-payload path's job,
            # not ours. Silence here can only cost an observation.
            continue
        if found:
            out.append((det_id, m, found[0], found[1]))
    return out
