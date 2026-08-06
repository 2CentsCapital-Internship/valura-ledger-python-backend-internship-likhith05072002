"""Phase 7 gate — conflicting duplicates (S2 / R11).

The stream redelivers events, and it does not promise the redelivery is
byte-identical. Three rules, and every one of them falls out of a single
line at the very top of `Book.apply`:

    if eid in self.seen: return []

  * FIRST DELIVERY WINS. E(amount 100) then E'(same id, amount 999) — the
    second is a no-op. Not "the later value corrects the earlier": an id
    we have seen is an id we have seen, whatever it says the second time.
  * A REJECTED ID STAYS REJECTED (R11). A malformed E is still an E we
    have seen. A repaired redelivery of it must never post — a "fixed"
    duplicate posting would double-count against whatever the server
    already scored for that id.
  * DETERMINISM. A feed containing both cases replays to a byte-identical
    snapshot, twice, from the log and from the ring.

The trap this suite exists to catch is the tempting refactor: moving the
`seen`-add after validation so that "only successful events count". That
one edit breaks R11 and S2 together, silently, and only under redelivery
— which is to say, only in the graded run.
"""
import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book  # noqa: E402
from sim import arena_sim as sim, invariants  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_cd_{_SEQ}",
            "type": etype, "payload": payload}


def wallet(b: Book, cid: str) -> Decimal:
    return -b.balances.get((cid, "2010"), D("0.00"))


def snap_bytes(b: Book) -> str:
    return json.dumps(b.snapshot(), sort_keys=True)


class FirstDeliveryWins(unittest.TestCase):

    def test_conflicting_amount_is_a_noop(self):
        b = Book()
        first = ev("deposit", {"customer_id": "C1", "amount": "100.00"},
                   "evt-dup-1")
        legs = b.apply(first)
        self.assertEqual(len(legs), 2)
        self.assertEqual(wallet(b, "C1"), D("100.00"))

        clash = ev("deposit", {"customer_id": "C1", "amount": "999.00"},
                   "evt-dup-1")
        self.assertEqual(b.apply(clash), [],
                         "a conflicting duplicate must submit no legs")
        self.assertEqual(wallet(b, "C1"), D("100.00"),
                         "the second delivery changed the wallet")
        self.assertEqual(b.balances[("C1", "1100")], D("100.00"))
        self.assertEqual(b.events["evt-dup-1"]["payload"]["amount"], "100.00")

    def test_conflicting_duplicate_is_not_logged_twice(self):
        """The log holds FIRST deliveries only — an as-of naming that id
        must resolve to the position it was first seen at (C2)."""
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "100.00"},
                   "evt-dup-2"))
        b.apply(ev("deposit", {"customer_id": "C2", "amount": "7.00"}))
        pos = b.eid_pos["evt-dup-2"]
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "999.00"},
                   "evt-dup-2"))
        self.assertEqual(b.eid_pos["evt-dup-2"], pos)
        self.assertEqual(len(b.event_log), 2)
        self.assertEqual(
            b.snapshot("evt-dup-2")["customers"]["C1"]["wallet_cash"],
            "100.00")

    def test_conflicting_duplicate_across_types(self):
        """The id is the key, not the type: a redelivery that changes the
        event's TYPE is still the same id and still a no-op."""
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "100.00"},
                   "evt-dup-3"))
        self.assertEqual(
            b.apply(ev("fee_charged", {"customer_id": "C1",
                                       "amount": "100.00"}, "evt-dup-3")), [])
        self.assertEqual(wallet(b, "C1"), D("100.00"))
        self.assertEqual(b.events["evt-dup-3"]["type"], "deposit")
        self.assertEqual(b.fees, {}, "the redelivery registered a fee")

    def test_first_delivery_wins_even_when_the_first_was_the_smaller(self):
        b = Book()
        b.apply(ev("withdrawal_requested",
                   {"customer_id": "C1", "withdrawal_id": "wd-1",
                    "amount": "10.00"}, "evt-dup-4"))
        b.apply(ev("withdrawal_requested",
                   {"customer_id": "C1", "withdrawal_id": "wd-1",
                    "amount": "9000.00"}, "evt-dup-4"))
        self.assertEqual(b.withdrawals["wd-1"]["amount"], D("10.00"))
        b.apply(ev("withdrawal_settled", {"withdrawal_id": "wd-1"}))
        self.assertEqual(b.balances[("C1", "1100")], D("-10.00"))


class RejectedStaysRejected(unittest.TestCase):

    def test_malformed_first_then_valid_never_posts(self):
        b = Book()
        bad = ev("deposit", {"customer_id": "C1", "amount": "-5.00"},
                 "evt-rej-1")
        self.assertEqual(b.apply(bad), [], "a negative amount must reject")
        self.assertNotIn("evt-rej-1", b.events)
        self.assertIn("evt-rej-1", b.seen)

        fixed = ev("deposit", {"customer_id": "C1", "amount": "5.00"},
                   "evt-rej-1")
        self.assertEqual(b.apply(fixed), [],
                         "a repaired redelivery must stay rejected (R11)")
        self.assertEqual(b.balances, {})
        self.assertNotIn("evt-rej-1", b.events)

    def test_every_malformed_shape_stays_rejected_on_redelivery(self):
        shapes = [{"customer_id": "C1"},                       # missing
                  {"customer_id": "C1", "amount": "twelve"},   # non-numeric
                  "this is not a payload object",              # wrong type
                  {"customer_id": "C1", "amount": "0.00"},     # non-positive
                  {"customer_id": "", "amount": "5.00"}]       # bad cid
        for i, payload in enumerate(shapes):
            b = Book()
            eid = f"evt-rej-shape-{i}"
            self.assertEqual(b.apply(ev("deposit", payload, eid)), [])
            self.assertEqual(b.apply(ev("deposit",
                                        {"customer_id": "C1",
                                         "amount": "5.00"}, eid)), [],
                             f"shape {i} posted on redelivery")
            self.assertEqual(b.balances, {})

    def test_refund_before_fee_stays_rejected_when_the_fee_arrives(self):
        """R11 in its canonical form: the refund names a fee that does not
        exist yet, so it rejects. The fee then arrives. The refund's id has
        been seen, so redelivering it — even unchanged — posts nothing, and
        a NEW refund event for the same fee posts normally."""
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "100.00"}))
        early = ev("fee_refund", {"refunds_source_id": "evt-fee-1"},
                   "evt-refund-1")
        self.assertEqual(b.apply(early), [])
        b.apply(ev("fee_charged", {"customer_id": "C1", "amount": "10.00"},
                   "evt-fee-1"))
        self.assertEqual(wallet(b, "C1"), D("90.00"))
        self.assertEqual(b.apply(dict(early)), [],
                         "the rejected refund posted on redelivery")
        self.assertEqual(wallet(b, "C1"), D("90.00"))
        self.assertNotIn("evt-fee-1", b.refunded)
        self.assertEqual(len(b.apply(ev("fee_refund",
                                        {"refunds_source_id": "evt-fee-1"},
                                        "evt-refund-2"))), 2)
        self.assertEqual(wallet(b, "C1"), D("100.00"))

    def test_a_rejected_id_is_still_as_of_answerable(self):
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "100.00"}))
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "-1.00"},
                   "evt-rej-asof"))
        b.apply(ev("deposit", {"customer_id": "C1", "amount": "5.00"}))
        snap = b.snapshot("evt-rej-asof")
        self.assertEqual(snap["customers"]["C1"]["wallet_cash"], "100.00")


class Determinism(unittest.TestCase):
    """A feed carrying both cases, replayed twice, byte for byte."""

    @staticmethod
    def mixed_feed() -> list:
        feed = [ev("deposit", {"customer_id": "C1", "amount": "100.00"},
                   "evt-mix-1"),
                ev("deposit", {"customer_id": "C1", "amount": "-5.00"},
                   "evt-mix-2"),                       # rejected
                ev("fee_charged", {"customer_id": "C1", "amount": "10.00"},
                   "evt-mix-3"),
                ev("deposit", {"customer_id": "C1", "amount": "999.00"},
                   "evt-mix-1"),                       # conflicting dup
                ev("deposit", {"customer_id": "C1", "amount": "5.00"},
                   "evt-mix-2"),                       # repaired reject
                ev("fee_refund", {"refunds_source_id": "evt-mix-3"},
                   "evt-mix-4"),
                ev("fee_refund", {"refunds_source_id": "evt-mix-3"},
                   "evt-mix-4"),                       # verbatim dup
                ev("deposit", {"customer_id": "C2", "amount": "42.00"},
                   "evt-mix-5")]
        return [dict(e) for e in feed]

    def test_replay_twice_is_byte_identical(self):
        a, b = Book(), Book()
        for e in self.mixed_feed():
            a.apply(dict(e))
        for e in self.mixed_feed():
            b.apply(dict(e))
        self.assertEqual(snap_bytes(a), snap_bytes(b))
        self.assertEqual(a.balances, b.balances)
        ok, why = invariants.replay_identical(a)
        self.assertTrue(ok, why)

    def test_the_mixed_feed_actually_exercises_both_cases(self):
        b = Book()
        legs = [b.apply(dict(e)) for e in self.mixed_feed()]
        self.assertEqual([len(x) for x in legs], [2, 0, 2, 0, 0, 2, 0, 2])
        self.assertEqual(wallet(b, "C1"), D("100.00"))
        self.assertEqual(wallet(b, "C2"), D("42.00"))
        self.assertEqual(len(b.event_log), 5)          # 8 delivered, 3 dups

    def test_chaos_feed_with_conflicting_duplicates_replays_identically(self):
        """The same property under load: corrupt() splices conflicting
        duplicates into a real feed at ~1 %, deliver() adds point
        redeliveries and two rewinds on top."""
        seed = 3311
        feed = sim.corrupt(sim.generate_cash(seed, 3000), seed=seed)
        ids = [e["event_id"] for e in feed]
        self.assertGreater(len(ids) - len(set(ids)), 0,
                           "corrupt() spliced no duplicate ids")
        first, second = Book(), Book()
        rw = [len(feed) // 3, (2 * len(feed)) // 3]
        subs_a = sim.deliver(first, feed, seed=seed, rewind_at=rw)
        subs_b = sim.deliver(second, feed, seed=seed, rewind_at=rw)
        self.assertEqual(json.dumps(subs_a, sort_keys=True),
                         json.dumps(subs_b, sort_keys=True))
        self.assertEqual(snap_bytes(first), snap_bytes(second))
        ok, why = invariants.replay_identical(first)
        self.assertTrue(ok, why)
        ok, why = invariants.ring_identical(first)
        self.assertTrue(ok, why)
        self.assertEqual(list(invariants.run_invariants(first)), [])

    def test_seen_is_checked_before_anything_else(self):
        """Structural guard for the refactor that breaks R11 and S2: the
        `seen` check is the first statement of apply(), before the log
        append and before dispatch."""
        import inspect
        code = [ln.strip() for ln in inspect.getsource(Book.apply).splitlines()
                if ln.strip().startswith(("eid =", "if eid in self.seen",
                                          "self.event_log.append",
                                          "legs = self._apply_core"))]
        self.assertEqual(code[0], 'eid = ev["event_id"]')
        self.assertEqual(code[1], "if eid in self.seen:")
        self.assertTrue(code[2].startswith("self.event_log.append"))
        self.assertTrue(code[3].startswith("legs = self._apply_core"))
        self.assertIn('self.seen.add(ev["event_id"])',
                      inspect.getsource(Book._apply_core))


if __name__ == "__main__":
    unittest.main(verbosity=2)
