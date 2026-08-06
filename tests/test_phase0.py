"""Phase 0 gate — the foundations must be provably right before any
accounting logic is trusted on top of them.

What this file proves, in order:

  * Rounding canon: money() is 2dp half-away-from-zero on BOTH signs, and
    floats convert by their printed value (the str() path).
  * Format canon: fmt_money/fmt_qty produce the graded string forms and can
    never emit scientific notation for any input.
  * Deposit end-to-end: the worked example posts exactly, the trial balance
    balances, the liability sign convention is right, and duplicates —
    identical or conflicting — are perfect no-ops.
  * Rejection discipline: malformed or unknown events return [] and leave
    the book byte-identical, yet still enter the log (an as-of checkpoint
    may name them).
  * Replay identity at scale: 1k and 10k corrupted streams through the sim,
    with the incremental book, a cold replay, and the snapshot-ring path all
    byte-identical, including as-of answers.

The sim-backed classes import sim/ lazily inside setUpClass on purpose:
if the harness is missing the gate fails RED with an ImportError — it must
never silently pass by skipping.
"""
import json
import os
import sys
import time
import unittest
from decimal import Decimal

# Make `book` and `sim` importable from the repo root regardless of where
# the runner was started.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, money, qnum, fmt_money, fmt_qty  # noqa: E402

D = Decimal


def snap_bytes(book: Book, **kw) -> str:
    """Canonical byte form of a snapshot. Dict equality could hide type
    drift (Decimal vs str); JSON with sorted keys cannot."""
    return json.dumps(book.snapshot(**kw), sort_keys=True)


def deposit(eid: str, cid: str = "cust_1", amount="1000", offset: int = 0) -> dict:
    """A wire-shaped deposit event."""
    return {"offset": offset, "event_id": eid, "type": "deposit",
            "payload": {"customer_id": cid, "amount": amount}}


# --------------------------------------------------------------------- #
#  rounding canon                                                       #
# --------------------------------------------------------------------- #
class RoundingCanon(unittest.TestCase):
    def test_half_up_positive(self):
        # 2.675 is exactly a half-cent: half away from zero rounds it UP.
        self.assertEqual(money("2.675"), D("2.68"))

    def test_half_up_second_table_row(self):
        self.assertEqual(money("2.665"), D("2.67"))

    def test_half_away_from_zero_negative(self):
        # ROUND_HALF_UP in decimal is "half away from zero": -2.675 -> -2.68,
        # not -2.67 as banker's or truncation would give.
        self.assertEqual(money("-2.675"), D("-2.68"))

    def test_float_goes_through_str(self):
        # str(2.675) == '2.675'; Decimal(2.675) directly would see the binary
        # expansion 2.67499999... and round DOWN. The str() path is the whole
        # reason money() is safe against json.loads floats.
        self.assertEqual(money(2.675), D("2.68"))

    def test_smallest_half_cent(self):
        self.assertEqual(money("0.005"), D("0.01"))

    def test_returns_decimal(self):
        self.assertIsInstance(money("1"), Decimal)


# --------------------------------------------------------------------- #
#  format canon                                                         #
# --------------------------------------------------------------------- #
class FormatCanon(unittest.TestCase):
    # Inputs chosen to bait every Decimal string failure mode: exponent
    # forms, negative zero, values that quantize to zero, huge magnitudes.
    SPREAD = ["0", "-0", "0.00", "-0.00", "0.005", "-0.005", "0.000001",
              "-0.000001", "1E+1", "1E-7", "0E-8", "9.9E+12", "-9.9E+12",
              "2.675", "-2.675", "8.000000", "0.500000", "12345678.90",
              "-12345678.90", "1e2", "10", "-10"]

    def test_qty_minimal_integer(self):
        self.assertEqual(fmt_qty(D("8.000000")), "8")

    def test_qty_minimal_fraction(self):
        self.assertEqual(fmt_qty(D("0.500000")), "0.5")

    def test_qty_exponent_form_normalized(self):
        # Decimal('1E+1') formats as '1E+1' under str(); the canon must
        # render it as plain '10'.
        self.assertEqual(fmt_qty(D("1E+1")), "10")

    def test_money_negative_zero(self):
        self.assertEqual(fmt_money(D("-0.00")), "0.00")

    def test_no_scientific_notation_ever(self):
        for s in self.SPREAD:
            for fmt in (fmt_money, fmt_qty):
                out = fmt(D(s))
                self.assertNotIn("E", out, f"{fmt.__name__}({s!r}) = {out!r}")
                self.assertNotIn("e", out, f"{fmt.__name__}({s!r}) = {out!r}")

    def test_money_always_two_dp(self):
        for s in self.SPREAD:
            out = fmt_money(D(s))
            self.assertRegex(out, r"^-?\d+\.\d\d$",
                             f"fmt_money({s!r}) = {out!r}")


# --------------------------------------------------------------------- #
#  deposit end-to-end                                                   #
# --------------------------------------------------------------------- #
class DepositE2E(unittest.TestCase):
    def setUp(self):
        self.book = Book()
        self.legs = self.book.apply(deposit("evt_dep_1"))

    def test_exact_two_legs(self):
        # The worked example, character for character: Dr 1100 / Cr 2010,
        # both sides as 2dp strings, the unused side as '0.00'.
        self.assertEqual(self.legs, [
            {"account": "1100", "customer_id": "cust_1",
             "debit": "1000.00", "credit": "0.00"},
            {"account": "2010", "customer_id": "cust_1",
             "debit": "0.00", "credit": "1000.00"},
        ])

    def test_trial_balance_sums_to_zero(self):
        tb = self.book.snapshot()["trial_balance"]
        self.assertEqual(sum(D(v) for v in tb.values()), D("0"))

    def test_liability_sign_convention(self):
        snap = self.book.snapshot()
        # Trial balance is debit-positive: the customer-payable 2010 shows
        # as a negative number...
        self.assertEqual(snap["trial_balance"]["2010"], "-1000.00")
        self.assertEqual(snap["trial_balance"]["1100"], "1000.00")
        # ...but the customer's wallet is the NEGATED liability balance:
        # money the firm owes them, reported positive.
        self.assertEqual(snap["customers"]["cust_1"]["wallet_cash"], "1000.00")

    def test_duplicate_delivery_is_noop(self):
        before = snap_bytes(self.book)
        self.assertEqual(self.book.apply(deposit("evt_dep_1")), [])
        self.assertEqual(snap_bytes(self.book), before)

    def test_conflicting_duplicate_keeps_first_amount(self):
        # Same id, different amount: first delivery wins forever — the
        # redelivery is ignored entirely, not treated as a correction.
        self.assertEqual(
            self.book.apply(deposit("evt_dep_1", amount="999")), [])
        self.assertEqual(
            self.book.snapshot()["customers"]["cust_1"]["wallet_cash"],
            "1000.00")


# --------------------------------------------------------------------- #
#  malformed + unknown events                                           #
# --------------------------------------------------------------------- #
class MalformedRejects(unittest.TestCase):
    """Each malformed shape must return [] and leave the book
    byte-identical — while still being logged, because an as-of checkpoint
    may name a rejected event id."""

    def setUp(self):
        self.book = Book()
        self.book.apply(deposit("evt_good"))
        self.before = snap_bytes(self.book)

    def _assert_rejected(self, ev):
        self.assertEqual(self.book.apply(ev), [])
        self.assertEqual(snap_bytes(self.book), self.before)
        # Rejected, but still first-delivered: it must be in the log.
        self.assertIn(ev["event_id"], self.book.eid_pos)
        self.assertIn(ev["event_id"], self.book.seen)

    def test_missing_amount(self):
        self._assert_rejected({"offset": 1, "event_id": "evt_bad_1",
                               "type": "deposit",
                               "payload": {"customer_id": "cust_1"}})

    def test_non_numeric_amount(self):
        self._assert_rejected(deposit("evt_bad_2", amount="abc"))

    def test_payload_not_a_dict(self):
        self._assert_rejected({"offset": 3, "event_id": "evt_bad_3",
                               "type": "deposit", "payload": None})


class UnknownType(unittest.TestCase):
    def test_unknown_type_counted_once(self):
        book = Book()
        ev = {"offset": 0, "event_id": "evt_alien",
              "type": "alien_event", "payload": {}}
        self.assertEqual(book.apply(ev), [])
        self.assertEqual(book.todo.get("alien_event"), 1)
        # Redelivery hits the seen-check before the todo counter: still 1.
        self.assertEqual(book.apply(ev), [])
        self.assertEqual(book.todo.get("alien_event"), 1)
        # And an unknown event changes no balances.
        self.assertEqual(book.balances, {})


# --------------------------------------------------------------------- #
#  replay identity over a corrupted 1k stream (sim-backed)              #
# --------------------------------------------------------------------- #
class SimReplayIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Deliberately NOT a skip: the gate fails red until sim/ exists.
        from sim import arena_sim, invariants
        cls.sim, cls.inv = arena_sim, invariants
        cls.book = Book()
        events = arena_sim.corrupt(arena_sim.generate(7, 1000), seed=7)
        arena_sim.deliver(cls.book, events, seed=7)

    def _cold_replay(self, upto: int) -> Book:
        """Independent oracle: a fresh Book fed the first-delivery log
        prefix through the public apply() path."""
        b = Book()
        for ev in self.book.event_log[:upto]:
            b.apply(ev)
        return b

    def test_replay_identical(self):
        ok, why = self.inv.replay_identical(self.book)
        self.assertTrue(ok, why)

    def test_ring_identical(self):
        ok, why = self.inv.ring_identical(self.book)
        self.assertTrue(ok, why)

    def test_as_of_mid_stream_equals_cold_replay(self):
        pos = len(self.book.event_log) // 2
        mid = self.book.event_log[pos]["event_id"]
        cold = self._cold_replay(pos + 1)
        self.assertEqual(snap_bytes(self.book, as_of_event_id=mid),
                         snap_bytes(cold))

    def test_as_of_a_rejected_event(self):
        # Find a logged-but-rejected deposit (malformed): it is in the log
        # yet never posted, so 'as of it' must equal 'as of the event
        # before it' — processed through it, unchanged by it.
        rejected = next(
            (i, ev) for i, ev in enumerate(self.book.event_log)
            if i > 0 and ev.get("type") == "deposit"
            and ev["event_id"] not in self.book.events)
        pos, ev = rejected
        prev = self.book.event_log[pos - 1]["event_id"]
        self.assertEqual(
            snap_bytes(self.book, as_of_event_id=ev["event_id"]),
            snap_bytes(self.book, as_of_event_id=prev))

    def test_as_of_final_event_equals_live(self):
        last = self.book.event_log[-1]["event_id"]
        self.assertEqual(snap_bytes(self.book, as_of_event_id=last),
                         snap_bytes(self.book))


# --------------------------------------------------------------------- #
#  10k chaos (sim-backed)                                               #
# --------------------------------------------------------------------- #
class ChaosTenK(unittest.TestCase):
    def test_chaos_invariants_green(self):
        from sim import arena_sim, invariants
        events = arena_sim.corrupt(arena_sim.generate(42, 10_000), seed=42)
        book = Book()
        t0 = time.perf_counter()
        # Two mid-stream rewind-replays: redelivery storms exactly where
        # the snapshot ring has state to protect.
        arena_sim.deliver(book, events, seed=42,
                          rewind_at=[len(events) // 3,
                                     (2 * len(events)) // 3])
        violations = invariants.run_invariants(book)
        wall = time.perf_counter() - t0
        print(f"\n[chaos-10k] {len(events)} stream events (+chaos "
              f"redeliveries) + invariants in {wall:.2f}s")
        self.assertFalse(violations, f"invariant violations: {violations}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
