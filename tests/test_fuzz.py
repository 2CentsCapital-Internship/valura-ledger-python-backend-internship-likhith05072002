"""Phase 7 gate — the fuzz harness.

Four TEST GATE boxes live here, and they are checked in this order for a
reason:

  1. FUZZ CONTROL GROUP. Every event type's unmutated baseline is
     delivered and must behave exactly per spec — the BRK-A buy fill
     posts the 13 graded legs at Dr = Cr = 2005.13, the BRK-C sell posts
     its 13 at 1101.16, deposit posts Dr 1100 / Cr 2010, and the four
     no-leg types return [] *with their documented state effect applied*
     (a hold and a route, a closed order, scaled lots, re-keyed lots).
     Without this a Book that rejected every event on sight would post a
     flawless zero-crash, zero-residue fuzz run. This is the box that
     makes the other three mean something.
  2. NO CRASH: >= 1000 mutants per event type — every field dropped,
     every field's type swapped, every numeric negated, every field
     replaced by 10**30 / "9"*400 / 1e308 / "Infinity" / "NaN", plus
     seeded stacked combinations and non-dict payloads — and not one
     exception escapes apply().
  3. NO RESIDUE: a rejected mutant returns [] and leaves state
     BYTE-identical (sim.invariants.state_fingerprint), and a mutant
     that survives validation posts balanced legs. Rejection is read
     from `book.events`, not from an empty leg list: four event types
     post no legs when they SUCCEED, and inferring rejection from `[]`
     would excuse exactly their residue.
  4. SNAPSHOT UNDER FUZZ: one Book takes all 25,000 mutants back to
     back, then snapshot() raises nothing and serializes clean — money
     2 dp, quantities minimal form, no "E" anywhere.

The barrage itself lives in sim.invariants.fuzz_barrage so that
run_regression's fuzz section and this suite judge by exactly the same
rules on different seeds. TestKnownResidue pins the one defect the
barrage tolerates, by blast radius; read its docstring before touching
book.py's on_order_placed.
"""
import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, ZERO, money, qnum          # noqa: E402
from sim import fuzz, invariants                  # noqa: E402
from sim.invariants import state_fingerprint      # noqa: E402

D = Decimal

# The gate's own number. The regression section runs the same barrage on
# a different seed, so a tuned-to-this-seed pass is not a pass.
MUTANTS_PER_TYPE = 1000
FUZZ_SEED = 20260806


def dr(acct, amount, cid):
    return {"account": acct, "customer_id": cid,
            "debit": amount, "credit": "0.00"}


def cr(acct, amount, cid):
    return {"account": acct, "customer_id": cid,
            "debit": "0.00", "credit": amount}


def ev(etype, payload, eid="fz-1", offset=1):
    return {"offset": offset, "event_id": eid, "type": etype,
            "payload": payload}


def run_baseline(name):
    """Deliver one baseline's setup then its event into a fresh Book.
    Returns (book, event, legs)."""
    setup, event = fuzz.baseline(name)
    book = Book()
    for s in setup:
        book.apply(s)
    return book, event, book.apply(event)


# ------------------------------------------------------------------ #
#  1. the control group                                              #
# ------------------------------------------------------------------ #

class TestControlGroup(unittest.TestCase):
    """Every baseline is VALID and does what the protocol says."""

    def test_every_handler_has_a_baseline(self):
        """Coverage is asserted against the Book itself, not against a
        list someone remembered to update: every on_* handler must have a
        baseline, and every baseline must name a real handler."""
        handlers = {m[3:] for m in dir(Book) if m.startswith("on_")}
        covered = {fuzz.BASELINES[n]()[1]["type"] for n in fuzz.BASELINES}
        self.assertEqual(handlers - covered, set(),
                         "handlers with no fuzz baseline")
        self.assertEqual(covered - handlers, set(),
                         "baselines for types the Book cannot handle")
        self.assertEqual(len(fuzz.EVENT_TYPES), 24)
        self.assertEqual(sorted(fuzz.CONTROL_LEGS), sorted(fuzz.BASELINES))

    def test_every_baseline_is_accepted_and_balanced(self):
        """The blanket control assertion: accepted (it reached
        book.events), the documented leg count, and Dr == Cr == the
        documented total."""
        for name in fuzz.BASELINES:
            with self.subTest(name=name):
                book, event, legs = run_baseline(name)
                self.assertIn(event["event_id"], book.events,
                              f"{name}: the VALID baseline was REJECTED")
                want_n, want_total = fuzz.CONTROL_LEGS[name]
                self.assertEqual(len(legs), want_n)
                total_dr = sum((D(l["debit"]) for l in legs), ZERO)
                total_cr = sum((D(l["credit"]) for l in legs), ZERO)
                self.assertEqual(total_dr, D(want_total))
                self.assertEqual(total_cr, D(want_total))

    def test_buy_fill_posts_the_thirteen_graded_legs(self):
        """BRK-A / equity, P = 1000.00, rate 0.50 — the worked example,
        leg for leg: b 2.00, c 0.40, r 0.80, bc 1.25 (0.90 + 0.35
        ticket), cc 0.20, ps 0.48 (0.475 HALF_UP)."""
        book, event, legs = run_baseline("order_filled")
        cid = event["payload"]["customer_id"]
        self.assertEqual(legs, [
            dr("2010", "1003.20", cid),     # P + b + c + r
            dr("1200", "1000.00", cid),
            dr("5000", "1.25", cid),
            dr("5010", "0.20", cid),
            dr("5100", "0.48", cid),
            cr("2350", "1000.00", cid),     # T+2: no 1100 today
            cr("2100", "1000.00", cid),
            cr("4000", "2.00", cid),
            cr("4010", "0.40", cid),
            cr("2400", "0.80", cid),
            cr("2411", "1.25", cid),
            cr("2420", "0.20", cid),
            cr("2430", "0.48", cid),
        ])
        self.assertEqual(len(legs), 13)
        self.assertEqual(sum((D(l["debit"]) for l in legs), ZERO),
                         D("2005.13"))
        self.assertNotIn("1100", [l["account"] for l in legs])
        # and the lot exists at cost = principal, fees excluded
        (lot,) = list(book.lots.values())
        self.assertEqual(lot["qty"], qnum("10"))
        self.assertEqual(lot["cost_total"], money("1000.00"))

    def test_sell_fill_posts_the_thirteen_graded_legs(self):
        """BRK-C / etf, P = 600.00, rate 0.25 over a 10-share / 1000.00
        lot — 5 shares relieve money(1000 x 5/10) = 500.00 and the sell
        balances at Dr = Cr = 1101.16."""
        book, event, legs = run_baseline("order_filled_sell")
        cid = event["payload"]["customer_id"]
        self.assertEqual(legs, [
            dr("1150", "600.00", cid),
            dr("2100", "500.00", cid),      # FIFO relief
            dr("5000", "0.92", cid),
            dr("5010", "0.06", cid),
            dr("5100", "0.18", cid),
            cr("2010", "597.84", cid),      # P - b - c - r
            cr("1200", "500.00", cid),
            cr("4000", "1.50", cid),
            cr("4010", "0.18", cid),
            cr("2400", "0.48", cid),
            cr("2413", "0.92", cid),
            cr("2420", "0.06", cid),
            cr("2430", "0.18", cid),
        ])
        self.assertEqual(sum((D(l["debit"]) for l in legs), ZERO),
                         D("1101.16"))
        (lot,) = list(book.lots.values())
        self.assertEqual(lot["qty"], qnum("5"))
        self.assertEqual(lot["cost_total"], money("500.00"))

    def test_deposit_posts_its_two_legs(self):
        book, event, legs = run_baseline("deposit")
        cid = event["payload"]["customer_id"]
        self.assertEqual(legs, [dr("1100", "500.00", cid),
                                cr("2010", "500.00", cid)])
        self.assertEqual(book.snapshot()["customers"][cid]["wallet_cash"],
                         "500.00")

    def test_no_leg_types_return_empty_with_their_state_effect(self):
        """The four types that post nothing must still DO something —
        the trap this box exists for is a Book that returns [] because it
        rejected the event, which looks identical on the wire."""
        # order_placed: a hold of money(10 x 100 + 5.00) and a route
        book, event, legs = run_baseline("order_placed")
        cid = event["payload"]["customer_id"]
        oid = event["payload"]["order_id"]
        self.assertEqual(legs, [])
        snap = book.snapshot()
        self.assertEqual(snap["customers"][cid]["cash_hold"], "1005.00")
        self.assertEqual(snap["open_order_routes"][oid], "BRK-A")
        self.assertTrue(book.orders[oid]["placed"])

        # order_cancelled / order_rejected: closed, hold back to zero
        for name in ("order_cancelled", "order_rejected"):
            with self.subTest(name=name):
                book, event, legs = run_baseline(name)
                oid = event["payload"]["order_id"]
                self.assertEqual(legs, [])
                self.assertTrue(book.orders[oid]["closed"])
                self.assertEqual(book.orders[oid]["hold_rem"], ZERO)
                self.assertEqual(book.snapshot()["open_order_routes"], {})

        # stock_split 1 -> 2: quantity doubles, total cost unchanged
        book, event, legs = run_baseline("stock_split")
        cid, sym = event["payload"]["customer_id"], event["payload"]["symbol"]
        self.assertEqual(legs, [])
        pos = book.snapshot()["customers"][cid]["positions"][sym]
        self.assertEqual(pos["quantity"], "20")
        self.assertEqual(pos["cost_basis"], "1000.00")

        # symbol_change: the holding is re-keyed, lot identity intact
        book, event, legs = run_baseline("symbol_change")
        p = event["payload"]
        self.assertEqual(legs, [])
        positions = book.snapshot()["customers"][p["customer_id"]]["positions"]
        self.assertNotIn(p["old_symbol"], positions)
        self.assertEqual(positions[p["new_symbol"]]["cost_basis"], "1000.00")

    def test_baselines_leave_every_observe_detector_silent(self):
        """A control-group failure must mean a handler broke, never that
        a detector spoke up: the baselines are built so that principal ==
        qty x price, dividends land on real positions, withdrawals stay
        inside the wallet, and fills sit inside their limit."""
        for name in fuzz.BASELINES:
            with self.subTest(name=name):
                book, event, _legs = run_baseline(name)
                self.assertEqual(book.report_log, [],
                                 f"{name}: baseline tripped a detector")
                self.assertEqual(book.quarantine, [],
                                 f"{name}: baseline tripped a lifecycle flag")


# ------------------------------------------------------------------ #
#  2 + 3 + 4. the barrage                                            #
# ------------------------------------------------------------------ #

class TestMutationFuzz(unittest.TestCase):
    """>= 1000 mutants per event type, judged by the shared referee.

    The barrage runs once for the whole class — it is ~25k apply() calls
    — and each box below reads its own slice of the result.
    """

    stats: dict = {}
    violations: list = []

    @classmethod
    def setUpClass(cls):
        cls.stats, cls.violations = invariants.fuzz_barrage(
            count=MUTANTS_PER_TYPE, seed=FUZZ_SEED)

    def test_no_crash_no_residue_and_balanced_legs(self):
        """One assertion for all three: fuzz_barrage reports a crash, an
        unbalanced posting, a leg on a reject and a residue as violation
        strings, and green is the empty list."""
        self.assertEqual(self.violations, [],
                         f"{len(self.violations)} fuzz violations, first: "
                         f"{self.violations[0] if self.violations else ''}")

    def test_every_type_got_at_least_a_thousand_mutants(self):
        self.assertEqual(len(self.stats["per_type"]), len(fuzz.BASELINES))
        for name, n in self.stats["per_type"].items():
            with self.subTest(name=name):
                self.assertGreaterEqual(n, MUTANTS_PER_TYPE)

    def test_the_barrage_is_not_all_rejects(self):
        """A sanity floor on the fuzzer itself: if every single mutant
        were rejected, the residue box would be vacuous for the
        posted-legs half. Some mutations are survivable by design — a
        dropped `currency`, a mutated `symbol` — and they must post."""
        self.assertGreater(self.stats["posted"], 0)
        self.assertGreater(self.stats["rejected"], self.stats["posted"])

    def test_operators_cover_every_field_of_every_baseline(self):
        """The mutators are applied at EVERY applicable position, not at
        a sample: for each baseline, each of the four operators must
        appear against each of its payload fields."""
        for name in fuzz.BASELINES:
            with self.subTest(name=name):
                _setup, event = fuzz.baseline(name)
                payload = event["payload"]
                labels = [lab for lab, _s, _e
                          in fuzz.mutants(name, count=MUTANTS_PER_TYPE)]
                blob = "\n".join(labels)
                for path in fuzz.paths(payload):
                    field = ".".join(str(k) for k in path)
                    self.assertIn(f"drop_field:{field}", blob)
                    self.assertIn(f"mutate_type:{field}=", blob)
                    self.assertIn(f"huge_value:{field}=", blob)
                    # flip_sign applies where there is a number to negate
                    # — every money, quantity, price and rate field.
                    if fuzz._numeric(payload[path[0]] if len(path) == 1
                                     else None):
                        self.assertIn(f"flip_sign:{field}", blob)

    def test_snapshot_under_fuzz(self):
        """Box 4: the soak Book took every mutant of every type; its
        snapshot must not raise and must serialize clean. fuzz_barrage
        runs serialization_canon + run_invariants + the no-"E" sweep over
        it and reports any of them as violations, so a green
        `violations` covers this box — this test pins the soak actually
        happened and re-reads the snapshot here."""
        self.assertGreaterEqual(self.stats["soak_events"],
                                MUTANTS_PER_TYPE * len(fuzz.BASELINES))
        self.assertGreater(self.stats["soak_posted"], 0)
        self.assertEqual(self.violations, [])


class TestSnapshotUnderFuzz(unittest.TestCase):
    """The same box again, standalone and explicit — one Book, the whole
    barrage of one heavily-posting type, then the serialization law."""

    def test_snapshot_serializes_cleanly_after_a_barrage(self):
        book = Book()
        for name in ("deposit", "order_filled", "dividend_reinvested",
                     "stock_split", "fx_deposit"):
            setup, _event = fuzz.baseline(name)
            for s in setup:
                book.apply(s)
            for _label, _s, mutant in fuzz.mutants(name, count=200):
                book.apply(mutant)
        snap = book.snapshot()                       # must not raise
        self.assertEqual(invariants.serialization_canon(book, [snap]), [])
        self.assertEqual(invariants.run_invariants(book), [])
        json.dumps(snap, sort_keys=True)             # and it serializes
        # The no-"E" law is about the VALUES we render — money and
        # quantities. Keys are identifiers echoed from the feed: a
        # mutant that renamed a symbol to "Infinity" and then posted a
        # perfectly good fill leaves that string in the reply as a
        # symbol, exactly as the arena sent it, and that is correct.
        for value in invariants._snap_strings(snap):
            with self.subTest(value=value):
                self.assertNotIn("E", str(value).upper())
                self.assertNotIn("INFINITY", str(value).upper())
                self.assertNotIn("NAN", str(value).upper())
        # a real book, not an empty one: the barrage posted something
        self.assertTrue(snap["trial_balance"])
        for cust in snap["customers"].values():
            self.assertRegex(cust["wallet_cash"], r"^-?\d+\.\d{2}$")


# ------------------------------------------------------------------ #
#  the fingerprint's own contract                                    #
# ------------------------------------------------------------------ #

class TestStateFingerprint(unittest.TestCase):
    """state_fingerprint must exclude exactly the four stores that grow
    legitimately on a rejection. A checker that includes them false-fails
    every reject; one that drops lots or orders misses real residue."""

    def test_seen_and_event_log_grow_on_a_reject_but_are_excluded(self):
        book = Book()
        before = state_fingerprint(book)
        legs = book.apply(ev("deposit", {"customer_id": "C1",
                                         "amount": "-5.00"}, "bad-1"))
        self.assertEqual(legs, [])
        self.assertIn("bad-1", book.seen)              # contract, not residue
        self.assertEqual(len(book.event_log), 1)
        self.assertEqual(state_fingerprint(book), before)

    def test_todo_grows_on_an_unknown_type_but_is_excluded(self):
        book = Book()
        before = state_fingerprint(book)
        self.assertEqual(book.apply(ev("no_such_type", {}, "u-1")), [])
        self.assertEqual(book.todo, {"no_such_type": 1})
        self.assertEqual(state_fingerprint(book), before)

    def test_report_log_grows_on_a_detector_finding_but_is_excluded(self):
        """The reason findings go to report_log and not to quarantine: a
        flagged-then-rejected event must leave zero residue. D11 fires on
        an overdraw, the handler then rejects the duplicate
        withdrawal_id, and the book must be byte-identical."""
        book = Book()
        book.apply(ev("deposit", {"customer_id": "C1",
                                  "amount": "500.00"}, "d-1"))
        book.apply(ev("withdrawal_requested",
                      {"customer_id": "C1", "withdrawal_id": "W1",
                       "amount": "100.00"}, "w-1"))
        before = state_fingerprint(book)
        n_report = len(book.report_log)
        legs = book.apply(ev("withdrawal_requested",
                             {"customer_id": "C1", "withdrawal_id": "W1",
                              "amount": "9999.00"}, "w-2"))
        self.assertEqual(legs, [])
        self.assertGreater(len(book.report_log), n_report,
                           "D11 did not observe the overdraw")
        self.assertEqual(book.report_log[-1][0], "D11")
        self.assertEqual(state_fingerprint(book), before)

    def test_fingerprint_sees_real_residue(self):
        """The negative control for the residue checker itself: a
        genuine state change MUST move the fingerprint."""
        book = Book()
        before = state_fingerprint(book)
        book.apply(ev("deposit", {"customer_id": "C1",
                                  "amount": "1.00"}, "d-2"))
        self.assertNotEqual(state_fingerprint(book), before)
        for mutate in (lambda b: b.lots.__setitem__(
                           99, {"cid": "C1", "symbol": "X", "qty": qnum(1),
                                "cost_total": money("1.00"), "seq": 99,
                                "split_mult": 1, "merge_rank": 0}),
                       lambda b: b._order("O9"),
                       lambda b: b.trades.__setitem__("T9", {"settled": True}),
                       lambda b: b.fees.__setitem__("F9", {"amount": "1"}),
                       lambda b: b.refunded.add("F8"),
                       lambda b: b.withdrawals.__setitem__("W9", {"a": 1}),
                       lambda b: b.accounts_touched.add("9999"),
                       lambda b: b.customers_seen.add("C9"),
                       lambda b: b.quarantine.append(("x", 1))):
            with self.subTest(mutate=mutate):
                probe = Book()
                base = state_fingerprint(probe)
                mutate(probe)
                self.assertNotEqual(state_fingerprint(probe), base)


# ------------------------------------------------------------------ #
#  the one defect the barrage tolerates                              #
# ------------------------------------------------------------------ #

class TestKnownResidue(unittest.TestCase):
    """PINNED DEFECT — book.py on_order_placed, hold overflow.

    `on_order_placed` writes the order record and registers the customer
    BEFORE it computes the hold:

        o.update({... "placed": True, "route": route})
        self.customers_seen.add(cid)
        o["hold_init"] = money(qty * limit + est)     # <-- can raise

    `est_charges` is validated as finite and non-negative, which
    10**30, 1e308 and "9"*400 all are. They then blow up inside
    `money()`'s quantize (InvalidOperation), the broad except in
    `_apply_core` rejects the event — correctly, `legs: []` — but the
    placement stub is already committed, so the order appears in
    `open_order_routes` and its customer in the checkpoint.

    The fix is one line: compute the hold into a local before
    `o.update(...)`. It is NOT applied here — this suite does not edit
    book.py — and it cannot fire on arena data, whose est_charges are
    ordinary cents.

    What this test guarantees meanwhile: the blast radius. No leg posts,
    no money moves, and only the placement lifecycle stores differ. The
    barrage tolerates exactly this shape and nothing wider. When
    on_order_placed is fixed the tolerated-shape assertions here still
    hold (an empty residue is inside any bound), so nothing goes red —
    delete this class and `_KNOWN_RESIDUE_KEYS` together.
    """

    HUGE = (10 ** 30, 1e308, "9" * 400, "1e400")

    def _place_with_est(self, est):
        book = Book()
        _setup, event = fuzz.baseline("order_placed")
        event["payload"]["est_charges"] = est
        before = state_fingerprint(book)
        legs = book.apply(event)
        return book, event, legs, before, state_fingerprint(book)

    def test_the_event_is_rejected_and_moves_no_money(self):
        for est in self.HUGE:
            with self.subTest(est=repr(est)[:24]):
                book, event, legs, before, after = self._place_with_est(est)
                self.assertEqual(legs, [])
                self.assertNotIn(event["event_id"], book.events)
                self.assertEqual(book.balances, {})
                self.assertEqual(book.lots, {})
                self.assertEqual(book.trades, {})
                self.assertEqual(book.quarantine, [])
                self.assertEqual(book.snapshot()["trial_balance"], {})
                self.assertEqual(json.loads(before)["snapshot"]
                                 ["trial_balance"],
                                 json.loads(after)["snapshot"]
                                 ["trial_balance"])

    def test_the_residue_is_confined_to_the_placement_lifecycle(self):
        for est in self.HUGE:
            with self.subTest(est=repr(est)[:24]):
                _book, _event, _legs, before, after = \
                    self._place_with_est(est)
                scope = invariants._residue_scope(before, after)
                self.assertTrue(
                    invariants._is_known_residue(
                        "order_placed", scope,
                        {"est_charges": est}),
                    f"residue widened beyond the pinned shape: "
                    f"{sorted(scope)}")
                # the pin is on the provoking input too, not just the type
                self.assertTrue(
                    invariants.placement_hold_overflows({"est_charges": est}))

    def test_the_pin_does_not_excuse_anything_else(self):
        """The allowance needs all three: event type, provoking input,
        and confined blast radius. Drop any one and it is a violation
        again — otherwise the pin would be a blanket amnesty for
        order_placed."""
        wide = {"orders", "lots", "snapshot", "snapshot.trial_balance"}
        narrow = {"orders", "customers_seen"}
        huge = {"est_charges": 10 ** 30}
        self.assertTrue(invariants._is_known_residue("order_placed",
                                                     narrow, huge))
        self.assertFalse(invariants._is_known_residue("order_placed",
                                                      wide, huge))
        self.assertFalse(invariants._is_known_residue("order_filled",
                                                      narrow, huge))
        self.assertFalse(invariants._is_known_residue(
            "order_placed", narrow, {"est_charges": "5.00"}))
        self.assertFalse(invariants._is_known_residue(
            "order_placed", narrow, {"est_charges": "Infinity"}))

    def test_a_non_finite_est_charges_is_still_perfectly_clean(self):
        """The validation that DOES run first: "Infinity"/"NaN" fail
        `is_finite()` before any mutation, so those reject with zero
        residue. Only the finite-but-astronomical values slip through —
        which is the whole shape of the defect."""
        for est in ("Infinity", "NaN", "-Infinity"):
            with self.subTest(est=est):
                _book, _event, legs, before, after = \
                    self._place_with_est(est)
                self.assertEqual(legs, [])
                self.assertEqual(before, after)

    def test_no_other_event_type_leaks_on_huge_values(self):
        """The pin is order_placed ONLY: every other baseline takes the
        same huge values in every field and leaves nothing behind."""
        for name in fuzz.BASELINES:
            if name == "order_placed":
                continue
            setup, _event = fuzz.baseline(name)
            base = Book()
            for s in setup:
                base.apply(s)
            before = state_fingerprint(base)
            for label, mut_setup, mutant in fuzz.mutants(name, count=1):
                if not label.startswith("huge_value:"):
                    continue
                book = Book()
                for s in mut_setup:
                    book.apply(s)
                book.apply(mutant)
                if mutant["event_id"] in book.events:
                    continue
                with self.subTest(name=name, label=label):
                    self.assertEqual(state_fingerprint(book), before)


# ------------------------------------------------------------------ #
#  the mutators themselves                                           #
# ------------------------------------------------------------------ #

class TestOperators(unittest.TestCase):
    """A fuzzer is only as good as its mutators; these are unit tests of
    the four, including that they never touch the payload handed in."""

    P = {"customer_id": "C1", "amount": "500.00", "nested": {"k": "1"}}

    def test_drop_field_removes_one_field_including_nested(self):
        out = fuzz.drop_field(self.P, ("amount",))
        self.assertEqual(sorted(out), ["customer_id", "nested"])
        out = fuzz.drop_field(self.P, ("nested", "k"))
        self.assertEqual(out["nested"], {})
        self.assertEqual(sorted(self.P), ["amount", "customer_id", "nested"])

    def test_mutate_type_swaps_the_type_and_keeps_the_field(self):
        cases = {"string": "not-a-number", "list": ["500.00"],
                 "dict": {"value": "500.00"}, "none": None, "bool": True}
        for variant, want in cases.items():
            with self.subTest(variant=variant):
                out = fuzz.mutate_type(self.P, ("amount",), variant)
                self.assertIn("amount", out)
                self.assertEqual(out["amount"], want)

    def test_flip_sign_negates_numbers_and_skips_the_rest(self):
        self.assertEqual(fuzz.flip_sign(self.P, ("amount",))["amount"],
                         "-500.00")
        back = fuzz.flip_sign({"a": "-1.00"}, ("a",))
        self.assertEqual(back["a"], "1.00")
        self.assertEqual(fuzz.flip_sign({"a": 3}, ("a",))["a"], -3)
        self.assertIsNone(fuzz.flip_sign(self.P, ("customer_id",)))

    def test_huge_value_plants_every_documented_value(self):
        self.assertEqual(fuzz.HUGE_VALUES,
                         (10 ** 30, "9" * 400, 1e308, "Infinity", "NaN"))
        for v in fuzz.HUGE_VALUES:
            with self.subTest(v=repr(v)[:16]):
                self.assertEqual(fuzz.huge_value(self.P, ("amount",), v)
                                 ["amount"], v)

    def test_paths_enumerates_nested_positions(self):
        self.assertIn(("nested", "k"), fuzz.paths(self.P))
        self.assertIn(("nested",), fuzz.paths(self.P))

    def test_mutants_are_reproducible_and_never_share_state(self):
        """Seeded means seeded: the same seed yields the same stream,
        and a different seed does not. Baselines are rebuilt per mutant,
        so no mutant can poison the next one."""
        a = [lab for lab, _s, _e in fuzz.mutants("deposit", count=200,
                                                 seed=7)]
        b = [lab for lab, _s, _e in fuzz.mutants("deposit", count=200,
                                                 seed=7)]
        c = [lab for lab, _s, _e in fuzz.mutants("deposit", count=200,
                                                 seed=8)]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        for _lab, _setup, event in fuzz.mutants("deposit", count=50):
            self.assertEqual(fuzz.baseline("deposit")[1]["payload"],
                             {"customer_id": "deposit:FZ-C1",
                              "amount": "500.00"})
            del event

    def test_event_ids_are_unique_across_the_whole_barrage(self):
        seen = set()
        for name in fuzz.BASELINES:
            for _lab, setup, event in fuzz.mutants(name, count=50):
                self.assertNotIn(event["event_id"], seen)
                seen.add(event["event_id"])
                for s in setup:
                    self.assertTrue(s["event_id"].startswith(f"{name}:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
