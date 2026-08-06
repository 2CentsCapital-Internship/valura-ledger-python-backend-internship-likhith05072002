"""Phase 2 gate, part 1: fill_charges / partner_share — every box in the
TEST GATE of phases/PHASE-2-tariff.md.

The firm-accounts block is graded all-or-nothing, so this file carries the
densest verification in the repo: hand-worked fixtures asserted on every
field, the nine min-fee boundary cases, the loss-making guard at all four
partner rates, the half-cent partner table, a 10,000-principal exact
fractions.Fraction oracle (seeded), and purity canaries.

Fraction lives HERE only — tariff.py itself imports nothing but decimal.
"""
import os
import random
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tariff import TARIFF, fill_charges, partner_share, money  # noqa: E402

D = Decimal
CENT = D("0.01")
FIELDS = ("b", "c", "r", "bc", "cc", "ps")

ORACLE_SEED = 20260806  # fixed in-repo; logged on any failure


# ------------------------------------------------------------------ #
#  exact-arithmetic oracle helpers (integer cents, ties away from 0) #
# ------------------------------------------------------------------ #

def round_half_up_cents(frac: Fraction) -> int:
    """Round an exact Fraction amount to integer cents, ties away from zero.

    Pure integer arithmetic: n = frac*100; split into quotient/remainder of
    |n|; bump when 2*rem >= den; re-apply sign. Independent of decimal.
    """
    n = frac * 100
    sign = -1 if n < 0 else 1
    a = -n if n < 0 else n
    q, rem = divmod(a.numerator, a.denominator)
    if 2 * rem >= a.denominator:
        q += 1
    return sign * q


def cents(x: Decimal) -> int:
    """A 2-dp Decimal to integer cents, exactly (fails loudly otherwise)."""
    tup = x.as_tuple()
    assert tup.exponent == -2, f"not 2 dp: {x!r}"
    scaled = x.scaleb(2)
    return int(scaled)


# Oracle's own copy of the tariff constants, as exact rationals / ints,
# transcribed from the work order's Reference card (NOT from tariff.TARIFF)
# so a typo in either table is caught, not mirrored.
ORACLE_TAB = {
    #          brokerage        custody          broker_cost      custody_cost    min¢  ticket¢
    "BRK-A": (Fraction(20, 10**4), Fraction(4, 10**4), Fraction(9, 10**4), Fraction(2, 10**4), 100, 35),
    "BRK-B": (Fraction(15, 10**4), Fraction(5, 10**4), Fraction(8, 10**4), Fraction(3, 10**4), 250, 300),
    "BRK-C": (Fraction(25, 10**4), Fraction(3, 10**4), Fraction(12, 10**4), Fraction(1, 10**4), 50, 20),
}
REG = Fraction(8, 10**4)


def oracle(broker: str, p_cents: int, rate: Fraction) -> dict:
    """b/c/r/bc/cc/margin/ps in exact Fraction arithmetic, HALF_UP applied
    at exactly the module's independent rounding points. Integer cents out."""
    brk, cus, bcost, ccost, min_c, tick_c = ORACLE_TAB[broker]
    p = Fraction(p_cents, 100)
    b = max(round_half_up_cents(p * brk), min_c)   # floor AFTER rounding
    c = round_half_up_cents(p * cus)
    r = round_half_up_cents(p * REG)
    bc = round_half_up_cents(p * bcost) + tick_c   # ticket folds in (A1)
    cc = round_half_up_cents(p * ccost)
    margin = (b + c) - (bc + cc)                   # of FOLDED bc (A2)
    ps = round_half_up_cents(rate * Fraction(margin, 100)) if margin > 0 else 0
    return {"b": b, "c": c, "r": r, "bc": bc, "cc": cc,
            "margin": margin, "ps": ps}


def margin_of(d: dict) -> Decimal:
    return (d["b"] + d["c"]) - (d["bc"] + d["cc"])


# ------------------------------------------------------------------ #
#  hand-worked fixtures: P = 10000.00, partner_rate = 0.50           #
# ------------------------------------------------------------------ #

class TestHandWorkedFixtures(unittest.TestCase):
    """All six fields asserted on each of the three P=10000 fixtures."""

    def assert_fixture(self, broker, principal, rate, expect, exp_margin):
        d = fill_charges(broker, D(principal), D(rate))
        self.assertEqual(set(d), set(FIELDS), "exactly the six fee keys")
        for k in FIELDS:
            self.assertEqual(d[k], D(expect[k]),
                             f"{broker} P={principal} field {k}: "
                             f"got {d[k]}, want {expect[k]}")
        self.assertEqual(margin_of(d), D(exp_margin),
                         f"{broker} margin (b+c)-(bc+cc)")
        return d

    def test_brk_a_10000(self):
        # raw ps = 0.50 x 12.65 = 6.325 -> HALF_UP -> 6.33 (half-cent proof
        # inside a full fixture; binary float says 6.32)
        self.assert_fixture(
            "BRK-A", "10000.00", "0.50",
            {"b": "20.00", "c": "4.00", "r": "8.00",
             "bc": "9.35", "cc": "2.00", "ps": "6.33"},
            "12.65")

    def test_brk_b_10000(self):
        self.assert_fixture(
            "BRK-B", "10000.00", "0.50",
            {"b": "15.00", "c": "5.00", "r": "8.00",
             "bc": "11.00", "cc": "3.00", "ps": "3.00"},
            "6.00")

    def test_brk_c_10000(self):
        self.assert_fixture(
            "BRK-C", "10000.00", "0.50",
            {"b": "25.00", "c": "3.00", "r": "8.00",
             "bc": "12.20", "cc": "1.00", "ps": "7.40"},
            "14.80")

    def test_ticket_folds_into_bc_no_ticket_key(self):
        """A1: bc == money(P x broker_cost_bps) + ticket; no 'ticket' key."""
        P = D("10000.00")
        for broker in ("BRK-A", "BRK-B", "BRK-C"):
            t = TARIFF[broker]
            d = fill_charges(broker, P, D("0.50"))
            self.assertEqual(d["bc"],
                             money(P * t["broker_cost"]) + t["ticket"],
                             f"{broker}: bc must be rounded cost + ticket")
            self.assertNotIn("ticket", d,
                             f"{broker}: no separate ticket key (A1)")

    def test_brk_c_odd_margin_1030(self):
        # b: 2.575 -> 2.58 (HALF_UP), bc: 1.236 -> 1.24 + 0.20,
        # margin 1.35, ps: 0.675 -> 0.68
        self.assert_fixture(
            "BRK-C", "1030.00", "0.50",
            {"b": "2.58", "c": "0.31", "r": "0.82",
             "bc": "1.44", "cc": "0.10", "ps": "0.68"},
            "1.35")

    def test_unknown_broker_keyerror(self):
        with self.assertRaises(KeyError):
            fill_charges("BRK-Z", D("100.00"), D("0.50"))


# ------------------------------------------------------------------ #
#  min-fee boundary: +-1 cent around each crossover                  #
# ------------------------------------------------------------------ #

class TestMinFeeBoundary(unittest.TestCase):
    """max() applies AFTER rounding the bps amount — nine knife-edges."""

    def assert_b(self, broker, principal, raw_rounded, expect_b):
        t = TARIFF[broker]
        P = D(principal)
        # the rounded-first bps amount is what the floor compares against
        self.assertEqual(money(P * t["brokerage"]), D(raw_rounded),
                         f"{broker} P={principal}: rounded bps amount")
        d = fill_charges(broker, P, D("0.50"))
        self.assertEqual(d["b"], D(expect_b),
                         f"{broker} P={principal}: b")
        # and b really is max(rounded, min_fee) — round-then-floor order
        self.assertEqual(d["b"], max(money(P * t["brokerage"]), t["min_fee"]))

    def test_brk_a_boundaries(self):  # 20 bps, min 1.00
        self.assert_b("BRK-A", "497.49", "0.99", "1.00")   # floored
        self.assert_b("BRK-A", "497.50", "1.00", "1.00")   # 0.995 up to min
        self.assert_b("BRK-A", "502.50", "1.01", "1.01")   # 1.005 up, exceeds

    def test_brk_b_boundaries(self):  # 15 bps, min 2.50
        self.assert_b("BRK-B", "1663.33", "2.49", "2.50")
        self.assert_b("BRK-B", "1666.67", "2.50", "2.50")
        self.assert_b("BRK-B", "1670.00", "2.51", "2.51")  # 2.505 -> 2.51

    def test_brk_c_boundaries(self):  # 25 bps, min 0.50
        self.assert_b("BRK-C", "197.99", "0.49", "0.50")
        self.assert_b("BRK-C", "198.00", "0.50", "0.50")   # 0.495 -> 0.50
        self.assert_b("BRK-C", "202.00", "0.51", "0.51")   # 0.505 -> 0.51


# ------------------------------------------------------------------ #
#  loss-making fill: ps floored at zero, no clawback                 #
# ------------------------------------------------------------------ #

class TestLossMakingGuard(unittest.TestCase):
    RATES = ("0", "0.25", "0.5", "0.75")

    def test_brk_b_p100_all_rates(self):
        """BRK-B, P=100.00: margin = 2.55 - 3.11 = -0.56 -> ps 0.00 exactly
        at every partner rate; no negative component anywhere."""
        for rate in self.RATES:
            d = fill_charges("BRK-B", D("100.00"), D(rate))
            self.assertEqual(d["b"], D("2.50"), "floored at min")
            self.assertEqual(d["c"], D("0.05"))
            self.assertEqual(d["bc"], D("3.08"))
            self.assertEqual(d["cc"], D("0.03"))
            self.assertEqual(margin_of(d), D("-0.56"))
            self.assertEqual(d["ps"], D("0.00"),
                             f"rate {rate}: ps must be exactly 0.00")
            self.assertEqual(str(d["ps"]), "0.00",
                             f"rate {rate}: 0.00, not -0.00/0/0E-2")
            for k in FIELDS:
                self.assertGreaterEqual(
                    d[k], D("0.00"),
                    f"rate {rate}: negative component {k}={d[k]}")

    def test_a2_margin_includes_ticket(self):
        """A2 proof: same fixture WITHOUT the ticket fold would have margin
        (2.50+0.05) - (0.08+0.03) = +2.44 and ps > 0 at rate 0.50. With the
        fold (the default), ps == 0.00 — so ps==0 discriminates A2."""
        t = TARIFF["BRK-B"]
        P = D("100.00")
        margin_unfolded = (D("2.50") + D("0.05")) \
            - (money(P * t["broker_cost"]) + money(P * t["custody_cost"]))
        self.assertEqual(margin_unfolded, D("2.44"))
        self.assertGreater(partner_share(margin_unfolded, D("0.50")),
                           D("0.00"), "unfolded margin WOULD pay a share")
        self.assertEqual(fill_charges("BRK-B", P, D("0.50"))["ps"],
                         D("0.00"), "folded margin pays none (A2 holds)")


# ------------------------------------------------------------------ #
#  partner_share direct                                              #
# ------------------------------------------------------------------ #

class TestPartnerShareDirect(unittest.TestCase):
    def test_zero_margin_strict_gt_branch(self):
        for rate in ("0", "0.25", "0.5", "0.75", "1"):
            self.assertEqual(partner_share(D("0.00"), D(rate)), D("0.00"))

    def test_negative_margin(self):
        self.assertEqual(partner_share(D("-0.01"), D("0.50")), D("0.00"))
        self.assertEqual(partner_share(D("-123.45"), D("0.75")), D("0.00"))

    def test_zero_rate_profitable_fill(self):
        d = fill_charges("BRK-A", D("10000.00"), D("0"))
        self.assertGreater(margin_of(d), D("0.00"))
        self.assertEqual(d["ps"], D("0.00"))
        self.assertEqual(partner_share(D("12.65"), D("0")), D("0.00"))

    def test_half_cent_table(self):
        """Every row lands on an exact half-cent (or odd quarter) — HALF_UP
        away from zero decides; float or bankers' rounding fails rows."""
        table = [
            ("0.01", "0.50", "0.01"),    # 0.005  -> 0.01
            ("0.03", "0.50", "0.02"),    # 0.015  -> 0.02
            ("0.05", "0.50", "0.03"),    # 0.025  -> 0.03
            ("1.35", "0.50", "0.68"),    # 0.675  -> 0.68
            ("12.65", "0.50", "6.33"),   # 6.325  -> 6.33
            ("1.35", "0.25", "0.34"),    # 0.3375 -> 0.34
        ]
        for margin, rate, want in table:
            got = partner_share(D(margin), D(rate))
            self.assertEqual(got, D(want),
                             f"margin {margin} x {rate}: got {got}, "
                             f"want {want}")


# ------------------------------------------------------------------ #
#  property: exact Fraction oracle, 10,000 seeded principals         #
# ------------------------------------------------------------------ #

class TestFractionOracle(unittest.TestCase):
    def test_oracle_10k(self):
        """10,000 seeded random 2-dp principals in [0.01, 1,000,000.00]
        x all 3 brokers x rates {0, 0.25, 0.5, 0.75}: cent-for-cent
        equality on b/c/r/bc/cc/margin/ps against exact Fractions."""
        rng = random.Random(ORACLE_SEED)
        rates = [("0", Fraction(0)), ("0.25", Fraction(1, 4)),
                 ("0.5", Fraction(1, 2)), ("0.75", Fraction(3, 4))]
        rate_dec = {s: D(s) for s, _ in rates}
        brokers = ("BRK-A", "BRK-B", "BRK-C")
        for _ in range(10_000):
            p_cents = rng.randint(1, 100_000_000)  # 0.01 .. 1,000,000.00
            P = D(p_cents).scaleb(-2)
            for broker in brokers:
                for rate_s, rate_f in rates:
                    want = oracle(broker, p_cents, rate_f)
                    got = fill_charges(broker, P, rate_dec[rate_s])
                    got_margin = cents(margin_of(got))
                    for k in FIELDS:
                        self.assertEqual(
                            cents(got[k]), want[k],
                            f"ORACLE MISMATCH seed={ORACLE_SEED} "
                            f"(broker={broker}, P={P}, rate={rate_s}) "
                            f"field {k}: tariff={got[k]} "
                            f"oracle={want[k]}c")
                    self.assertEqual(
                        got_margin, want["margin"],
                        f"ORACLE MISMATCH seed={ORACLE_SEED} "
                        f"(broker={broker}, P={P}, rate={rate_s}) "
                        f"margin: tariff={got_margin}c "
                        f"oracle={want['margin']}c")


# ------------------------------------------------------------------ #
#  purity                                                            #
# ------------------------------------------------------------------ #

class TestPurity(unittest.TestCase):
    SAMPLES = [("BRK-A", "0.01", "0.50"), ("BRK-A", "1000000.00", "0.75"),
               ("BRK-B", "10000.00", "0.50"), ("BRK-B", "100.00", "0.25"),
               ("BRK-C", "1030.00", "0.50"), ("BRK-C", "999999.99", "0.25")]

    def test_same_args_twice_equal_dicts(self):
        for broker, p, r in self.SAMPLES:
            d1 = fill_charges(broker, D(p), D(r))
            d2 = fill_charges(broker, D(p), D(r))
            self.assertEqual(d1, d2, f"impure: {broker} {p} {r}")

    def test_no_book_import_in_source(self):
        """grep -c 'import book' tariff.py == 0 — the module stays pure so
        the oracles stay trustworthy (and book -> tariff stays acyclic)."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tariff.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("import book", src)

    def test_no_scientific_notation_and_exactly_2dp(self):
        for broker, p, r in self.SAMPLES:
            d = fill_charges(broker, D(p), D(r))
            for k, v in d.items():
                s = str(v)
                self.assertNotIn("E", s,
                                 f"{broker} {p} {r} {k}: sci notation {s}")
                self.assertEqual(v.as_tuple().exponent, -2,
                                 f"{broker} {p} {r} {k}: not 2 dp: {s}")


if __name__ == "__main__":
    unittest.main()
