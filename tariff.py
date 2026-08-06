"""The tariff: every fee on every fill, and the routing rule — pure math.

This module is deliberately stateless and imports nothing but `decimal`:
purity is what makes the Fraction oracle and the routing brute force in the
test suite trustworthy, and `book.py` imports `money` FROM here (never the
reverse), so one rounding convention exists in the whole repo.

Ambiguity defaults owned by this module (see NOTES.md, confirmed against
practice diagnostics as they arrive):

  A1  — the flat ticket fee folds into the broker-cost line `bc`; there is
        no separate ticket leg (the graded 13-leg buy example shows none).
  A2  — the partner-share margin is computed from the FOLDED bc, so the
        ticket makes ~a quarter of fills loss-making, exactly as the spec
        says ("loss-making because of the ticket").
  A13 — a buy placement's hold is money(qty × limit + est_charges): one
        quantize of the sum (consumed by Phase 3).
  A14 — routing notional uses the ORIGINAL placement quantity, never the
        remaining-after-partials quantity.
  A15 — hold release accumulates per-fill rounded releases, remainder at
        close (formula "b"; FIFO-relief precedent), behind HOLD_FORMULA.

Every derived amount is rounded to the cent INDEPENDENTLY before use —
the spec's convention, graded exactly, and the reason the four settlement
events (Phase 5) can audit accumulated per-fill cents and match.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")
CENT = D("0.01")

# Regulatory fee: 8 bps of principal, charged to the customer, collected on
# the venue's behalf and owed onward (2400). Never the firm's income.
REG_RATE = D("0.0008")


def money(x) -> Decimal:
    """THE rounding canon: 2 decimal places, half away from zero.
    Via str() so a float that slipped out of json.loads converts by its
    printed value, not its binary expansion."""
    return D(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ #
#  the tariff table — verbatim from the task sheet                   #
# ------------------------------------------------------------------ #
# All rates are per unit of principal (bps as Decimal rates). Brokerage and
# custody are what the CUSTOMER is charged (firm revenue); broker cost and
# custody cost are what the FIRM is charged for the same trade (expenses).
TARIFF: dict[str, dict] = {
    "BRK-A": {"classes": ("equity", "etf"),
              "brokerage": D("0.0020"),      # 20 bps
              "custody": D("0.0004"),        # 4 bps
              "broker_cost": D("0.0009"),    # 9 bps
              "custody_cost": D("0.0002"),   # 2 bps
              "min_fee": D("1.00"),
              "ticket": D("0.35")},
    "BRK-B": {"classes": ("equity", "bond"),
              "brokerage": D("0.0015"),      # 15 bps
              "custody": D("0.0005"),        # 5 bps
              "broker_cost": D("0.0008"),    # 8 bps
              "custody_cost": D("0.0003"),   # 3 bps
              "min_fee": D("2.50"),
              "ticket": D("3.00")},
    "BRK-C": {"classes": ("etf", "bond"),
              "brokerage": D("0.0025"),      # 25 bps
              "custody": D("0.0003"),        # 3 bps
              "broker_cost": D("0.0012"),    # 12 bps
              "custody_cost": D("0.0001"),   # 1 bps
              "min_fee": D("0.50"),
              "ticket": D("0.20")},
}

# Every symbol belongs to one asset class for the whole run; no broker
# covers all three. A fill naming a broker outside its class is bad data
# (defect detector D1, Phase 7).
COVERAGE: dict[str, tuple[str, str]] = {
    "equity": ("BRK-A", "BRK-B"),
    "etf": ("BRK-A", "BRK-C"),
    "bond": ("BRK-B", "BRK-C"),
}


def covers(broker: str, asset_class: str) -> bool:
    return broker in COVERAGE.get(asset_class, ())


# ------------------------------------------------------------------ #
#  fees                                                              #
# ------------------------------------------------------------------ #

def partner_share(margin: Decimal, partner_rate) -> Decimal:
    """partner_rate × (revenue − cost), floored at zero — no clawback.

    Factored out because the two graded knife-edges live here: the strict
    `> 0` branch (a loss-making fill posts NO negative share), and the
    half-cent ties (rate 0.50 × odd-cent margin → exactly .005 → HALF_UP,
    which binary floating point would decide the other way).
    """
    if margin <= 0:
        return ZERO
    return money(D(str(partner_rate)) * margin)


def fill_charges(broker: str, principal, partner_rate) -> dict:
    """Every fee component of one fill, each independently at 2 dp.

        b   brokerage charged to the customer  -> Cr 4000, floored at min_fee
        c   custody charged to the customer    -> Cr 4010
        r   regulatory, customer-charged       -> Cr 2400 (payable, not income)
        bc  broker cost incl. the flat ticket  -> Dr 5000 / Cr 241x   (A1)
        cc  custody cost                       -> Dr 5010 / Cr 2420
        ps  partner share of (b+c) − (bc+cc)   -> Dr 5100 / Cr 2430, ≥ 0

    The min-fee floor applies AFTER rounding the bps amount (the ±1-cent
    boundary tests pin this), per FILL not per order. Unknown broker raises
    KeyError — the Book's broad except turns that into a rejection.
    """
    t = TARIFF[broker]
    P = money(principal)
    b = max(money(P * t["brokerage"]), t["min_fee"])
    c = money(P * t["custody"])
    r = money(P * REG_RATE)
    bc = money(P * t["broker_cost"]) + t["ticket"]
    cc = money(P * t["custody_cost"])
    ps = partner_share((b + c) - (bc + cc), partner_rate)
    return {"b": b, "c": c, "r": r, "bc": bc, "cc": cc, "ps": ps}


# ------------------------------------------------------------------ #
#  routing                                                           #
# ------------------------------------------------------------------ #

def route(asset_class: str, quantity, limit_price) -> str:
    """The broker with the lowest total CUSTOMER charge (brokerage with its
    min-fee floor, plus custody) on notional = quantity × limit_price,
    among the two brokers covering the class. Ties break on broker id
    ascending, so there is always exactly one right answer.

    Only b and c enter the comparison — bc, cc, r, and the ticket are the
    firm's problem, not the customer's, and the rule says "customer
    charge". Notional uses the ORIGINAL placement quantity (A14).
    Unknown asset class raises KeyError → the caller rejects.
    """
    n = D(str(quantity)) * D(str(limit_price))
    best: tuple[Decimal, str] | None = None
    for broker in COVERAGE[asset_class]:      # tuple order = id ascending
        t = TARIFF[broker]
        charge = max(money(n * t["brokerage"]), t["min_fee"]) \
            + money(n * t["custody"])
        if best is None or charge < best[0]:  # strict <: ties keep the first
            best = (charge, broker)
    return best[1]


# ------------------------------------------------------------------ #
#  hold release (A15 flag; Phase 3 is the caller)                    #
# ------------------------------------------------------------------ #

# "a": recompute the remaining hold from the remaining quantity each fill.
# "b": accumulate per-fill rounded releases; the remainder stays until the
#      final fill or a cancellation zeroes it — the DEFAULT, because the
#      FIFO relief rule ("remainder stays with the lot") sets the precedent.
# One practice checkpoint with a partially-filled open order picks between
# them; flipping is this one constant.
HOLD_FORMULA = "b"


def hold_release(hold_init: Decimal, fill_qty, qty_ordered) -> Decimal:
    """Formula (b): the rounded share of the hold this one fill releases.
    The caller clamps the running remainder at zero (cumulative rounding
    can overshoot by a cent) and zeroes it on close."""
    return money(hold_init * D(str(fill_qty)) / D(str(qty_ordered)))


def hold_remaining_recompute(hold_init: Decimal, remaining_qty,
                             qty_ordered) -> Decimal:
    """Formula (a): the remaining hold recomputed from the remaining
    quantity. Kept behind the flag so a practice diff flips one constant,
    not an implementation."""
    return money(hold_init * D(str(remaining_qty)) / D(str(qty_ordered)))
