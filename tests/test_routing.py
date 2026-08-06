"""Phase 2 gate, part 2: routing + hold-release formulas.

The brute-force argmin below is coded INDEPENDENTLY from the work order's
routing rule text — its own tariff constants, its own rounding, its own
tie-break — and never calls tariff.route's internals. tariff.py agreeing
with it across a 200,000-point cent sweep plus 10k random notionals is the
evidence the router is right, not just self-consistent.

Pure Decimal throughout — no Book, no state.
"""
import os
import random
import sys
import unittest
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tariff  # noqa: E402
from tariff import (route, hold_release, hold_remaining_recompute,  # noqa: E402
                    HOLD_FORMULA, money)

D = Decimal
CENT = D("0.01")
RANDOM_SEED = 20260806  # fixed in-repo; logged on failure

# ------------------------------------------------------------------ #
#  independent brute force, transcribed from the work order TEXT     #
#  ("charge(broker) = max(money(N x brokerage_bps), min_fee)         #
#    + money(N x custody_bps); min charge wins; tie -> lowest id")   #
# ------------------------------------------------------------------ #

#            brokerage      custody        min_fee
BF_TAB = {
    "BRK-A": (D("0.0020"), D("0.0004"), D("1.00")),   # 20 bps / 4 bps
    "BRK-B": (D("0.0015"), D("0.0005"), D("2.50")),   # 15 bps / 5 bps
    "BRK-C": (D("0.0025"), D("0.0003"), D("0.50")),   # 25 bps / 3 bps
}
BF_COVER = {"equity": ("BRK-A", "BRK-B"),
            "etf": ("BRK-A", "BRK-C"),
            "bond": ("BRK-B", "BRK-C")}


def bf_money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def bf_charge(broker: str, n: Decimal) -> Decimal:
    brk, cus, min_fee = BF_TAB[broker]
    b = bf_money(n * brk)
    if b < min_fee:
        b = min_fee
    return b + bf_money(n * cus)


def bf_route(asset_class: str, n: Decimal) -> str:
    """Argmin over the covering brokers' customer charges; tie -> lowest
    broker id by explicit string comparison (not iteration order)."""
    best_broker = None
    best_charge = None
    for broker in BF_COVER[asset_class]:
        ch = bf_charge(broker, n)
        if (best_charge is None or ch < best_charge
                or (ch == best_charge and broker < best_broker)):
            best_charge, best_broker = ch, broker
    return best_broker


# ------------------------------------------------------------------ #
#  brute-force cross-checks                                          #
# ------------------------------------------------------------------ #

class TestBruteForceSweep(unittest.TestCase):
    def test_cent_sweep_001_to_2000(self):
        """N = 0.01 -> 2000.00 in cent steps, all 3 classes: route() must
        equal the independent argmin at every single point."""
        classes = ("equity", "etf", "bond")
        one = D(1)
        for n_cents in range(1, 200_001):
            n = D(n_cents).scaleb(-2)
            for ac in classes:
                want = bf_route(ac, n)
                got = route(ac, one, n)
                if got != want:  # assertEqual per-point is too slow at 600k
                    self.fail(f"sweep mismatch: class={ac} N={n} "
                              f"route()={got} brute={want} "
                              f"(charges: "
                              f"{[(b, bf_charge(b, n)) for b in BF_COVER[ac]]})")

    def test_random_10k_qty_times_price(self):
        """10k seeded random (quantity up to 6 dp) x (2-dp price):
        route(class, qty, price) == brute force on N = qty x price."""
        rng = random.Random(RANDOM_SEED)
        classes = ("equity", "etf", "bond")
        for _ in range(10_000):
            qty = D(rng.randint(1, 10_000_000_000)).scaleb(-6)  # <= 10000, 6dp
            price = D(rng.randint(1, 500_000)).scaleb(-2)       # <= 5000, 2dp
            ac = classes[rng.randrange(3)]
            n = qty * price
            want = bf_route(ac, n)
            got = route(ac, qty, price)
            if got != want:
                self.fail(f"random mismatch seed={RANDOM_SEED}: class={ac} "
                          f"qty={qty} price={price} N={n} "
                          f"route()={got} brute={want}")


# ------------------------------------------------------------------ #
#  exact crossover ties and their neighbors                          #
# ------------------------------------------------------------------ #

class TestCrossoverFixtures(unittest.TestCase):
    def test_equity_tie_1315_79(self):
        # charge_A = 2.63 + 0.53 = 3.16; charge_B = 2.50(min) + 0.66 = 3.16
        self.assertEqual(bf_charge("BRK-A", D("1315.79")), D("3.16"))
        self.assertEqual(bf_charge("BRK-B", D("1315.79")), D("3.16"))
        self.assertEqual(route("equity", 1, D("1315.79")), "BRK-A",
                         "tie must break to lowest id")
        self.assertEqual(bf_route("equity", D("1315.79")), "BRK-A")

    def test_equity_neighbors(self):
        # N=1300: A 2.60+0.52=3.12 < B 2.50+0.65=3.15 — A outright
        self.assertLess(bf_charge("BRK-A", D("1300.00")),
                        bf_charge("BRK-B", D("1300.00")))
        self.assertEqual(route("equity", 1, D("1300.00")), "BRK-A")
        # N=1350: A 2.70+0.54=3.24 > B 2.50+0.68=3.18 — B outright
        self.assertGreater(bf_charge("BRK-A", D("1350.00")),
                           bf_charge("BRK-B", D("1350.00")))
        self.assertEqual(route("equity", 1, D("1350.00")), "BRK-B")

    def test_etf_tie_416_67(self):
        # charge_A = 1.00(min) + 0.17 = 1.17; charge_C = 1.04 + 0.13 = 1.17
        self.assertEqual(bf_charge("BRK-A", D("416.67")), D("1.17"))
        self.assertEqual(bf_charge("BRK-C", D("416.67")), D("1.17"))
        self.assertEqual(route("etf", 1, D("416.67")), "BRK-A",
                         "tie must break to lowest id")
        self.assertEqual(bf_route("etf", D("416.67")), "BRK-A")

    def test_etf_neighbors(self):
        # N=400: C wins outright (1.12 < 1.16); verify against brute force
        self.assertEqual(bf_route("etf", D("400.00")), "BRK-C")
        self.assertEqual(route("etf", 1, D("400.00")), "BRK-C")
        # N=450: A wins outright (1.18 < 1.27)
        self.assertEqual(bf_route("etf", D("450.00")), "BRK-A")
        self.assertEqual(route("etf", 1, D("450.00")), "BRK-A")

    def test_bond_tie_1085_00(self):
        # charge_B = 2.50(min) + 0.54 = 3.04; charge_C = 2.71 + 0.33 = 3.04
        self.assertEqual(bf_charge("BRK-B", D("1085.00")), D("3.04"))
        self.assertEqual(bf_charge("BRK-C", D("1085.00")), D("3.04"))
        self.assertEqual(route("bond", 1, D("1085.00")), "BRK-B",
                         "tie must break to lowest id (B < C)")
        self.assertEqual(bf_route("bond", D("1085.00")), "BRK-B")

    def test_bond_neighbors(self):
        # N=1080: C outright — 3.02 < 3.04
        self.assertEqual(bf_charge("BRK-C", D("1080.00")), D("3.02"))
        self.assertEqual(bf_charge("BRK-B", D("1080.00")), D("3.04"))
        self.assertEqual(route("bond", 1, D("1080.00")), "BRK-C")
        # N=1086: B outright — 3.04 < 3.05
        self.assertEqual(bf_charge("BRK-B", D("1086.00")), D("3.04"))
        self.assertEqual(bf_charge("BRK-C", D("1086.00")), D("3.05"))
        self.assertEqual(route("bond", 1, D("1086.00")), "BRK-B")


class TestZeroAndUnknown(unittest.TestCase):
    def test_zero_notional_min_fee_decides(self):
        # N=0 -> charge = min fee, deterministic, no exception
        self.assertEqual(route("equity", 0, D("0.00")), "BRK-A")  # 1.00<2.50
        self.assertEqual(route("etf", 0, D("0.00")), "BRK-C")     # 0.50<1.00
        self.assertEqual(route("bond", 0, D("0.00")), "BRK-C")    # 0.50<2.50
        # zero via qty=0 at a nonzero price too
        self.assertEqual(route("equity", 0, D("123.45")), "BRK-A")
        self.assertEqual(route("etf", 0, D("123.45")), "BRK-C")
        self.assertEqual(route("bond", 0, D("123.45")), "BRK-C")

    def test_unknown_asset_class_keyerror(self):
        with self.assertRaises(KeyError):
            route("crypto", 1, D("100.00"))
        with self.assertRaises(KeyError):
            route("", 1, D("100.00"))


# ------------------------------------------------------------------ #
#  hold-release formulas (A15)                                       #
# ------------------------------------------------------------------ #

class TestHoldFormulas(unittest.TestCase):
    INIT = D("1000.01")

    def test_default_flag_is_b(self):
        self.assertEqual(HOLD_FORMULA, "b")
        self.assertEqual(tariff.HOLD_FORMULA, "b")

    def test_discriminating_fixture_1000_01_over_3(self):
        """hold_init=1000.01, qty_ordered=3, three fills of 1. After two
        partials the formulas DISAGREE by a cent — the fixture that lets a
        practice run pick between them:
          (a) remaining = money(1000.01 x 1/3)            = 333.34
          (b) remaining = 1000.01 - 333.34 - 333.34       = 333.33
        """
        # formula (a): recompute from remaining qty (1 of 3 left)
        rem_a = hold_remaining_recompute(self.INIT, 1, 3)
        self.assertEqual(rem_a, D("333.34"))
        # formula (b): accumulate per-fill rounded releases
        rel1 = hold_release(self.INIT, 1, 3)
        rel2 = hold_release(self.INIT, 1, 3)
        self.assertEqual(rel1, D("333.34"), "each 1/3 release: 333.336.. up")
        rem_b = self.INIT - rel1 - rel2
        self.assertEqual(rem_b, D("333.33"))
        self.assertNotEqual(rem_a, rem_b, "fixture must discriminate a vs b")

    def test_overfill_sum_exceeds_init_caller_must_clamp(self):
        """Cumulative rounded releases can exceed hold_init — three 1/3
        releases of 1000.01 sum to 1000.02. tariff.hold_release itself does
        NOT clamp (documented caller-side, Phase 3): the naive running
        remainder goes NEGATIVE, so the caller MUST clamp at zero."""
        releases = [hold_release(self.INIT, 1, 3) for _ in range(3)]
        total = sum(releases, D("0"))
        self.assertEqual(total, D("1000.02"))
        self.assertGreater(total, self.INIT,
                           "sum of releases exceeds the initial hold")
        self.assertLess(self.INIT - total, D("0.00"),
                        "unclamped remainder is negative -> caller clamps")
        # the caller-side clamp pattern keeps the hold at exactly zero
        remaining = self.INIT
        for r in releases:
            remaining = max(remaining - r, D("0.00"))
        self.assertEqual(remaining, D("0.00"))

    def test_final_release_zeroes_formula_a(self):
        # remaining_qty = 0 -> remaining hold recomputes to exactly 0.00
        self.assertEqual(hold_remaining_recompute(self.INIT, 0, 3),
                         D("0.00"))
        self.assertEqual(hold_remaining_recompute(D("777.77"), 0, 7),
                         D("0.00"))

    def test_final_release_zeroes_formula_b(self):
        """Under (b) the close/final fill releases the REMAINDER (clamped),
        so a closed order's hold is identically 0.00."""
        remaining = self.INIT
        for _ in range(2):
            remaining -= hold_release(self.INIT, 1, 3)
        self.assertEqual(remaining, D("333.33"))
        # final fill: computed release 333.34 > remainder 333.33 -> the
        # caller releases min(computed, remaining), then zeroes on close
        computed = hold_release(self.INIT, 1, 3)
        released = min(computed, remaining)
        self.assertEqual(released, D("333.33"))
        remaining -= released
        self.assertEqual(remaining, D("0.00"))
        self.assertEqual(str(remaining), "0.00")


if __name__ == "__main__":
    unittest.main()
