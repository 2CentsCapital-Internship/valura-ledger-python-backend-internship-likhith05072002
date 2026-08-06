"""Phase 1 gate: the cash engine, box by box.

Naming convention: tests named test_R10 .. test_R18 map straight onto the
edge-matrix rows in phases/PHASE-1-cash.md, so a practice-run diff points
back to the exact rule it contradicts.

Every reject test asserts TWO things (the graded pair): apply() returned []
AND the book is byte-identical — balances, side-tables, snapshot — because
"reject" means zero residue, not merely no legs.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, ZERO, money  # noqa: E402
from decimal import Decimal  # noqa: E402


_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_t_{_SEQ}",
            "type": etype, "payload": payload}


def fingerprint(b: Book) -> str:
    """Everything a reject must leave untouched, in one comparable string."""
    return json.dumps({
        "snap": b._snapshot_now(),
        "balances": sorted((f"{k}", str(v)) for k, v in b.balances.items()),
        "fees": sorted((k, str(v)) for k, v in b.fees.items()),
        "refunded": sorted(b.refunded),
        "withdrawals": sorted((k, str(v)) for k, v in b.withdrawals.items()),
        "accounts": sorted(b.accounts_touched),
    }, sort_keys=True)


def legs_balance(legs) -> bool:
    dr = sum((Decimal(l["debit"]) for l in legs), ZERO)
    cr = sum((Decimal(l["credit"]) for l in legs), ZERO)
    return dr == cr


class Phase1Base(unittest.TestCase):
    def setUp(self):
        self.b = Book()

    # -- tiny DSL ------------------------------------------------------
    def post(self, etype, payload, eid=None):
        legs = self.b.apply(ev(etype, payload, eid))
        if legs:
            self.assertTrue(legs_balance(legs), f"unbalanced: {legs}")
        return legs

    def assert_rejected(self, etype, payload, eid=None):
        before = fingerprint(self.b)
        legs = self.post(etype, payload, eid)
        self.assertEqual(legs, [])
        self.assertEqual(fingerprint(self.b), before,
                         f"{etype} reject left residue")

    def deposit(self, cid, amount):
        return self.post("deposit", {"customer_id": cid, "amount": amount})

    def wallet(self, cid):
        return -self.b.balances.get((cid, "2010"), ZERO)


class TestFees(Phase1Base):
    def test_fee_charged_happy(self):
        legs = self.post("fee_charged",
                         {"customer_id": "C1", "amount": "12.34"}, "F1")
        self.assertEqual(legs, [
            {"account": "2010", "customer_id": "C1",
             "debit": "12.34", "credit": "0.00"},
            {"account": "1100", "customer_id": "C1",
             "debit": "0.00", "credit": "12.34"}])
        self.assertEqual(self.b.fees["F1"],
                         {"customer_id": "C1", "amount": money("12.34")})

    def test_fee_charged_rejects(self):
        for payload in ({"customer_id": "C1"},                      # missing
                        {"customer_id": "C1", "amount": "abc"},     # NaN
                        {"customer_id": "C1", "amount": "0.00"},    # zero
                        {"customer_id": "C1", "amount": "-5.00"},   # negative
                        "not a dict"):                              # type
            self.assert_rejected("fee_charged", payload)
        self.assertEqual(self.b.fees, {})

    def test_fee_refund_happy_uses_stored_amount_and_cid(self):
        self.post("fee_charged",
                  {"customer_id": "C1", "amount": "12.00"}, "F1")
        legs = self.post("fee_refund",
                         {"refunds_source_id": "F1",
                          "customer_id": "SOMEONE-ELSE"}, "R1")
        self.assertEqual(legs, [
            {"account": "1100", "customer_id": "C1",
             "debit": "12.00", "credit": "0.00"},
            {"account": "2010", "customer_id": "C1",
             "debit": "0.00", "credit": "12.00"}])
        self.assertIn("F1", self.b.refunded)      # keyed by SOURCE id
        self.assertNotIn("R1", self.b.refunded)

    def test_fee_refund_unknown_and_double(self):
        self.assert_rejected("fee_refund",
                             {"refunds_source_id": "NOPE",
                              "customer_id": "C1"})
        self.post("fee_charged",
                  {"customer_id": "C1", "amount": "9.99"}, "F1")
        self.post("fee_refund",
                  {"refunds_source_id": "F1", "customer_id": "C1"}, "R1")
        # second DISTINCT refund of the same source: the error, rejected
        self.assert_rejected("fee_refund",
                             {"refunds_source_id": "F1",
                              "customer_id": "C1"}, "R2")
        # REDELIVERED refund (same eid): a duplicate, silent no-op
        before = fingerprint(self.b)
        self.assertEqual(self.post("fee_refund",
                                   {"refunds_source_id": "F1",
                                    "customer_id": "C1"}, "R1"), [])
        self.assertEqual(fingerprint(self.b), before)

    def test_conflicting_duplicate_first_amount_governs(self):
        self.post("fee_charged",
                  {"customer_id": "C1", "amount": "10.00"}, "F1")
        # same event_id, different amount: first delivery wins forever
        self.assertEqual(self.post("fee_charged",
                                   {"customer_id": "C1",
                                    "amount": "99.00"}, "F1"), [])
        legs = self.post("fee_refund",
                         {"refunds_source_id": "F1", "customer_id": "C1"})
        self.assertEqual(legs[0]["debit"], "10.00")

    def test_R11(self):
        """Refund before its fee: rejected, and STAYS rejected."""
        self.assert_rejected("fee_refund",
                             {"refunds_source_id": "F2",
                              "customer_id": "C1"}, "R-early")
        self.post("fee_charged",
                  {"customer_id": "C1", "amount": "7.00"}, "F2")
        # the old refund id is spent — redelivery is a no-op, not a retry
        before = fingerprint(self.b)
        self.assertEqual(self.post("fee_refund",
                                   {"refunds_source_id": "F2",
                                    "customer_id": "C1"}, "R-early"), [])
        self.assertEqual(fingerprint(self.b), before)
        # a NEW refund event for the (existing, unrefunded) fee posts fine
        legs = self.post("fee_refund",
                         {"refunds_source_id": "F2",
                          "customer_id": "C1"}, "R-new")
        self.assertEqual(legs[0]["debit"], "7.00")


class TestInterest(Phase1Base):
    def test_interest_happy_three_legs(self):
        legs = self.post("interest_credited",
                         {"customer_id": "C1", "gross_amount": "10.00",
                          "customer_share": "3.33"})
        self.assertEqual(legs, [
            {"account": "1100", "customer_id": "C1",
             "debit": "10.00", "credit": "0.00"},
            {"account": "2010", "customer_id": "C1",
             "debit": "0.00", "credit": "3.33"},
            {"account": "4200", "customer_id": "C1",
             "debit": "0.00", "credit": "6.67"}])

    def test_R16(self):
        """share == gross: no 4200 leg, but 4200 still counted as touched."""
        legs = self.post("interest_credited",
                         {"customer_id": "C1", "gross_amount": "5.00",
                          "customer_share": "5.00"})
        self.assertEqual(len(legs), 2)
        self.assertNotIn("4200", [l["account"] for l in legs])
        self.assertIn("4200", self.b.accounts_touched)
        self.assertEqual(self.b.snapshot()["trial_balance"]["4200"], "0.00")

    def test_interest_share_zero_omits_2010(self):
        legs = self.post("interest_credited",
                         {"customer_id": "C1", "gross_amount": "4.00",
                          "customer_share": "0.00"})
        self.assertEqual([l["account"] for l in legs], ["1100", "4200"])
        self.assertIn("2010", self.b.accounts_touched)

    def test_interest_rejects(self):
        self.assert_rejected("interest_credited",
                             {"customer_id": "C1", "gross_amount": "5.00",
                              "customer_share": "5.01"})     # share > gross
        self.assert_rejected("interest_credited",
                             {"customer_id": "C1", "gross_amount": "0.00",
                              "customer_share": "0.00"})     # gross <= 0
        self.assert_rejected("interest_credited",
                             {"customer_id": "C1", "gross_amount": "5.00",
                              "customer_share": "-0.01"})    # share < 0


class TestTransfer(Phase1Base):
    def test_transfer_happy(self):
        self.deposit("A", "100.00")
        self.post("transfer_between_customers",
                  {"from_customer_id": "A", "to_customer_id": "B",
                   "amount": "40.00"})
        self.assertEqual(self.wallet("A"), money("60.00"))
        self.assertEqual(self.wallet("B"), money("40.00"))
        # the ACCOUNT total never moved — only per-customer keys see it
        acct_total = sum((v for (c, a), v in self.b.balances.items()
                          if a == "2010"), ZERO)
        self.assertEqual(acct_total, money("-100.00"))

    def test_R17(self):
        """from == to posts mechanically and nets to zero."""
        self.deposit("A", "10.00")
        legs = self.post("transfer_between_customers",
                         {"from_customer_id": "A", "to_customer_id": "A",
                          "amount": "3.00"})
        self.assertEqual(len(legs), 2)
        self.assertEqual(self.wallet("A"), money("10.00"))

    def test_R18(self):
        """A customer whose whole life is one incoming credit must appear
        in the checkpoint customers dict."""
        self.deposit("A", "50.00")
        self.post("transfer_between_customers",
                  {"from_customer_id": "A", "to_customer_id": "GHOST",
                   "amount": "5.00"})
        snap = self.b.snapshot()
        self.assertIn("GHOST", snap["customers"])
        self.assertEqual(snap["customers"]["GHOST"]["wallet_cash"], "5.00")


class TestFx(Phase1Base):
    def test_fx_happy_three_legs(self):
        legs = self.post("fx_deposit",
                         {"customer_id": "C1", "amount_foreign": "1000.00",
                          "currency": "EUR", "market_rate": "1.0845",
                          "customer_rate": "1.0800",
                          "usd_at_market_rate": "1084.50",
                          "usd_at_customer_rate": "1080.00"})
        self.assertEqual(legs, [
            {"account": "1100", "customer_id": "C1",
             "debit": "1084.50", "credit": "0.00"},
            {"account": "2010", "customer_id": "C1",
             "debit": "0.00", "credit": "1080.00"},
            {"account": "4100", "customer_id": "C1",
             "debit": "0.00", "credit": "4.50"}])

    def test_R10(self):
        """Zero spread posts (no 4100 leg); strictly-better-by-a-cent
        rejects; and the comparison reads USD amounts, never raw rates."""
        legs = self.post("fx_deposit",
                         {"customer_id": "C1", "amount_foreign": "1.00",
                          "currency": "EUR",
                          # raw rates LIE here: customer_rate looks better
                          "market_rate": "0.90", "customer_rate": "1.20",
                          "usd_at_market_rate": "1084.50",
                          "usd_at_customer_rate": "1084.50"})
        self.assertEqual(len(legs), 2)          # posts, no spread leg
        self.assertIn("4100", self.b.accounts_touched)
        self.assert_rejected("fx_deposit",
                             {"customer_id": "C1", "amount_foreign": "1.00",
                              "currency": "EUR",
                              # raw rates LIE the other way: look worse
                              "market_rate": "1.20", "customer_rate": "0.80",
                              "usd_at_market_rate": "1084.50",
                              "usd_at_customer_rate": "1084.51"})


class TestWithdrawals(Phase1Base):
    def test_request_happy_and_duplicate_wid(self):
        legs = self.post("withdrawal_requested",
                         {"withdrawal_id": "W1", "customer_id": "C1",
                          "amount": "250.00"})
        self.assertEqual([l["account"] for l in legs], ["2010", "2300"])
        self.assertEqual(self.b.withdrawals["W1"]["state"], "requested")
        # duplicate wid on a NEW event: reject, first request stands
        self.assert_rejected("withdrawal_requested",
                             {"withdrawal_id": "W1", "customer_id": "C2",
                              "amount": "999.00"})
        self.assertEqual(self.b.withdrawals["W1"]["amount"], money("250.00"))

    def test_settle_and_reject_happy(self):
        self.post("withdrawal_requested",
                  {"withdrawal_id": "W1", "customer_id": "C1",
                   "amount": "250.00"})
        legs = self.post("withdrawal_settled", {"withdrawal_id": "W1"})
        self.assertEqual(legs, [
            {"account": "2300", "customer_id": "C1",
             "debit": "250.00", "credit": "0.00"},
            {"account": "1100", "customer_id": "C1",
             "debit": "0.00", "credit": "250.00"}])
        self.post("withdrawal_requested",
                  {"withdrawal_id": "W2", "customer_id": "C1",
                   "amount": "80.00"})
        legs = self.post("withdrawal_rejected", {"withdrawal_id": "W2"})
        self.assertEqual([l["account"] for l in legs], ["2300", "2010"])
        self.assertEqual(self.wallet("C1"), money("-250.00"))

    def test_R12(self):
        """The state machine is terminal, in both orders."""
        # settled -> rejected
        self.post("withdrawal_requested",
                  {"withdrawal_id": "W1", "customer_id": "C1",
                   "amount": "10.00"})
        self.post("withdrawal_settled", {"withdrawal_id": "W1"}, "S1")
        self.assert_rejected("withdrawal_rejected", {"withdrawal_id": "W1"})
        # rejected -> settled
        self.post("withdrawal_requested",
                  {"withdrawal_id": "W2", "customer_id": "C1",
                   "amount": "10.00"})
        self.post("withdrawal_rejected", {"withdrawal_id": "W2"})
        self.assert_rejected("withdrawal_settled", {"withdrawal_id": "W2"})
        # unknown wid, double settle, redelivered settle
        self.assert_rejected("withdrawal_settled", {"withdrawal_id": "??"})
        self.assert_rejected("withdrawal_settled",
                             {"withdrawal_id": "W1"}, "S2")
        before = fingerprint(self.b)
        self.assertEqual(self.post("withdrawal_settled",
                                   {"withdrawal_id": "W1"}, "S1"), [])
        self.assertEqual(fingerprint(self.b), before)


class TestCashChaos(Phase1Base):
    def test_cash_chaos_2k_all_referees(self):
        from sim import arena_sim, invariants
        events = arena_sim.corrupt(arena_sim.generate_cash(777, 2000),
                                   seed=777)
        arena_sim.deliver(self.b, events, seed=777,
                          rewind_at=[len(events) // 2])
        self.assertEqual(invariants.run_invariants(self.b), [])
        self.assertEqual(invariants.wallet_oracle(self.b), [])
        ok, why = invariants.replay_identical(self.b)
        self.assertTrue(ok, why)
        ok, why = invariants.ring_identical(self.b)
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
