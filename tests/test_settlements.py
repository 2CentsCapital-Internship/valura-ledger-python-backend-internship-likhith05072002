"""Phase 5 gate — the four fee-settlement events.

Covers every settlement TEST GATE unit box: the four happy paths against
hand-computed Phase 2 fixtures (P=10000: BRK-A bc 9.35 / cc 2.00 / r 8.00
/ ps 6.33 at rate 0.50; BRK-B 11.00 / 3.00 / 8.00 / 3.00; BRK-C 12.20 /
1.00 / 8.00 / 7.40); the per-broker account mapping with isolation;
zero-payable rejects (never-accrued AND already-settled, R9); the
NEGATIVE-payable reject (settle, then reverse an accrual fill so the
payable goes debit — proves the amount > 0 check, not just the zero
path); the settle→accrue→settle cent audit (M4); R8 reversal-of-
settlement re-raising the payable generically; and per-customer keying.
"""
import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, ZERO, money  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_st_{_SEQ}",
            "type": etype, "payload": payload}


def fingerprint(b: Book) -> str:
    """Everything a reject must leave untouched, in one comparable string."""
    return json.dumps({
        "snap": b._snapshot_now(),
        "balances": sorted((f"{k}", str(v)) for k, v in b.balances.items()),
        "trades": sorted((k, repr(sorted(v.items())))
                         for k, v in b.trades.items()),
        "lots": sorted((str(k), repr(sorted(v.items())))
                       for k, v in b.lots.items()),
        "accounts": sorted(b.accounts_touched),
        "events": sorted(b.events.keys()),
        "reversed": sorted((k, v["reversed"]) for k, v in b.events.items()),
    }, sort_keys=True)


def legs_balance(legs) -> bool:
    dr_ = sum((D(l["debit"]) for l in legs), ZERO)
    cr_ = sum((D(l["credit"]) for l in legs), ZERO)
    return dr_ == cr_


def dr(acct, amt, cid="C1") -> dict:
    return {"account": acct, "customer_id": cid,
            "debit": amt, "credit": "0.00"}


def cr(acct, amt, cid="C1") -> dict:
    return {"account": acct, "customer_id": cid,
            "debit": "0.00", "credit": amt}


def fill_payload(oid, tid, *, cid="C1", side="buy", symbol="SYM", qty="10",
                 price="1000", principal="10000.00", broker="BRK-A",
                 asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": side, "symbol": symbol, "quantity": qty, "price": price,
            "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


# The four settlement types with their payable accounts and the
# hand-computed P=10000 / rate 0.50 per-fill accruals per broker.
SETTLE_TYPES = (("broker_fees_settled", None),      # acct depends on broker
                ("custodian_fees_settled", "2420"),
                ("reg_fees_remitted", "2400"),
                ("partner_payout", "2430"))
FIX_10K = {"BRK-A": {"payable": "2411", "bc": "9.35", "cc": "2.00",
                     "r": "8.00", "ps": "6.33"},
           "BRK-B": {"payable": "2412", "bc": "11.00", "cc": "3.00",
                     "r": "8.00", "ps": "3.00"},
           "BRK-C": {"payable": "2413", "bc": "12.20", "cc": "1.00",
                     "r": "8.00", "ps": "7.40"}}


class SettlementBase(unittest.TestCase):
    _N = 0

    def setUp(self):
        self.b = Book()

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

    def accrue_fill(self, *, cid="C1", broker="BRK-A", asset_class="equity",
                    principal="10000.00", rate="0.50", eid=None):
        SettlementBase._N += 1
        n = SettlementBase._N
        return self.post("order_filled", fill_payload(
            f"O-st-{n}", f"T-st-{n}", cid=cid, broker=broker,
            asset_class=asset_class, principal=principal, rate=rate), eid)

    def payable(self, acct, cid="C1"):
        """The outstanding payable as a positive number (credit balance)."""
        return -self.b.balances.get((cid, acct), ZERO)


class TestSettlementHappy(SettlementBase):
    def test_each_settlement_happy(self):
        """×4: one P=10000 BRK-A fill accrues 9.35 / 2.00 / 8.00 / 6.33;
        each settlement posts Dr payable / Cr 1100 at the exact accumulated
        balance and drives it to 0."""
        self.accrue_fill()
        for acct, amt in (("2411", "9.35"), ("2420", "2.00"),
                          ("2400", "8.00"), ("2430", "6.33")):
            self.assertEqual(self.payable(acct), money(amt))
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C1", "broker": "BRK-A"}, "E-BS")
        self.assertEqual(legs, [dr("2411", "9.35"), cr("1100", "9.35")])
        legs = self.post("custodian_fees_settled",
                         {"customer_id": "C1"}, "E-CS")
        self.assertEqual(legs, [dr("2420", "2.00"), cr("1100", "2.00")])
        legs = self.post("reg_fees_remitted", {"customer_id": "C1"}, "E-RS")
        self.assertEqual(legs, [dr("2400", "8.00"), cr("1100", "8.00")])
        legs = self.post("partner_payout", {"customer_id": "C1"}, "E-PS")
        self.assertEqual(legs, [dr("2430", "6.33"), cr("1100", "6.33")])
        for acct in ("2411", "2420", "2400", "2430"):
            self.assertEqual(self.payable(acct), ZERO)         # paid in full
        # each settlement stored its legs + EMPTY lot_ops: the R8 reversal
        # stays fully generic
        for eid in ("E-BS", "E-CS", "E-RS", "E-PS"):
            rec = self.b.events[eid]
            self.assertEqual(rec["lot_ops"], [])
            self.assertFalse(rec["reversed"])
        # 1100 paid out exactly the sum: 9.35+2.00+8.00+6.33 = 25.68
        self.assertEqual(self.b.balances[("C1", "1100")], -money("25.68"))

    def test_broker_account_mapping(self):
        """BRK-A→2411, BRK-B→2412, BRK-C→2413 — and settling one broker
        leaves the other brokers' payables bit-for-bit untouched."""
        self.accrue_fill(broker="BRK-A", asset_class="equity")
        self.accrue_fill(broker="BRK-B", asset_class="equity")
        self.accrue_fill(broker="BRK-C", asset_class="etf")
        for brk, fx in FIX_10K.items():
            self.assertEqual(self.payable(fx["payable"]), money(fx["bc"]),
                             f"{brk} accrual")
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C1", "broker": "BRK-A"})
        self.assertEqual(legs, [dr("2411", "9.35"), cr("1100", "9.35")])
        self.assertEqual(self.payable("2411"), ZERO)
        self.assertEqual(self.payable("2412"), money("11.00"))  # untouched
        self.assertEqual(self.payable("2413"), money("12.20"))  # untouched
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C1", "broker": "BRK-B"})
        self.assertEqual(legs, [dr("2412", "11.00"), cr("1100", "11.00")])
        self.assertEqual(self.payable("2413"), money("12.20"))  # untouched
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C1", "broker": "BRK-C"})
        self.assertEqual(legs, [dr("2413", "12.20"), cr("1100", "12.20")])
        # an unknown broker never reaches the settle helper
        self.assert_rejected("broker_fees_settled",
                             {"customer_id": "C1", "broker": "BRK-X"})


class TestSettlementRejects(SettlementBase):
    def _all_four(self):
        return [("broker_fees_settled",
                 {"customer_id": "C1", "broker": "BRK-A"}),
                ("custodian_fees_settled", {"customer_id": "C1"}),
                ("reg_fees_remitted", {"customer_id": "C1"}),
                ("partner_payout", {"customer_id": "C1"})]

    def test_zero_payable_rejects(self):
        """R9 both flavors: never-accrued AND already-settled → legs [],
        no state change."""
        for etype, payload in self._all_four():        # never accrued
            self.assert_rejected(etype, payload)
        self.accrue_fill()
        for etype, payload in self._all_four():        # settle all four
            self.assertTrue(self.post(etype, payload))
        for etype, payload in self._all_four():        # already settled
            self.assert_rejected(etype, payload)

    def test_negative_payable_rejects(self):
        """A DEBIT-balance payable must reject too — the check is amount >
        0, not amount != 0. Seeded by settling, then reversing an accrual
        fill: the inverse Dr swings the payable to +9.35 debit."""
        self.accrue_fill(eid="E-F")
        self.post("broker_fees_settled",
                  {"customer_id": "C1", "broker": "BRK-A"})    # payable → 0
        self.post("reversal", {"reverses_event_id": "E-F"}, "R-F")
        self.assertEqual(self.b.balances[("C1", "2411")], money("9.35"))
        self.assertEqual(self.payable("2411"), -money("9.35"))  # negative
        self.assert_rejected("broker_fees_settled",
                             {"customer_id": "C1", "broker": "BRK-A"})


class TestSettlementCycles(SettlementBase):
    def test_settle_accrue_settle(self):
        """M4 cent audit: fill → settle → two more fills → settle. Each
        settlement pays exactly the interim balance (9.35 then 18.70 on
        the broker line; 6.33 then 12.66 partner; 8.00 then 16.00 reg) and
        Σ settled ≡ Σ per-fill cent-rounded fees."""
        self.accrue_fill()
        first = {"2411": "9.35", "2420": "2.00", "2400": "8.00",
                 "2430": "6.33"}
        second = {"2411": "18.70", "2420": "4.00", "2400": "16.00",
                  "2430": "12.66"}
        pays = []
        for etype, acct in (("broker_fees_settled", "2411"),
                            ("custodian_fees_settled", "2420"),
                            ("reg_fees_remitted", "2400"),
                            ("partner_payout", "2430")):
            payload = {"customer_id": "C1"}
            if etype == "broker_fees_settled":
                payload["broker"] = "BRK-A"
            legs = self.post(etype, payload)
            self.assertEqual(legs, [dr(acct, first[acct]),
                                    cr("1100", first[acct])])
            pays.append((acct, D(first[acct])))
        self.accrue_fill()
        self.accrue_fill()                     # two more P=10000 fills
        for etype, acct in (("broker_fees_settled", "2411"),
                            ("custodian_fees_settled", "2420"),
                            ("reg_fees_remitted", "2400"),
                            ("partner_payout", "2430")):
            payload = {"customer_id": "C1"}
            if etype == "broker_fees_settled":
                payload["broker"] = "BRK-A"
            legs = self.post(etype, payload)
            self.assertEqual(legs, [dr(acct, second[acct]),
                                    cr("1100", second[acct])])
            pays.append((acct, D(second[acct])))
            self.assertEqual(self.payable(acct), ZERO)
        # Σ settled ≡ Σ per-fill cent-rounded fees over the 3 fills
        per_fill = {"2411": D("9.35"), "2420": D("2.00"),
                    "2400": D("8.00"), "2430": D("6.33")}
        for acct, expect in per_fill.items():
            settled = sum((amt for a, amt in pays if a == acct), ZERO)
            self.assertEqual(settled, 3 * expect, f"cent audit {acct}")

    def test_reversal_of_settlement_re_raises(self):
        """R8: the generic stored-legs inverse credits the payable back —
        re-raised at the prior amount, re-settleable under a new eid. No
        settlement-specific reversal code needed."""
        self.accrue_fill()
        self.post("broker_fees_settled",
                  {"customer_id": "C1", "broker": "BRK-A"}, "E-BS")
        self.assertEqual(self.payable("2411"), ZERO)
        rev = self.post("reversal", {"reverses_event_id": "E-BS"}, "R-BS")
        self.assertEqual(rev, [cr("2411", "9.35"), dr("1100", "9.35")])
        self.assertEqual(self.payable("2411"), money("9.35"))  # re-raised
        self.assertTrue(self.b.events["E-BS"]["reversed"])
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C1", "broker": "BRK-A"}, "E-BS2")
        self.assertEqual(legs, [dr("2411", "9.35"), cr("1100", "9.35")])
        self.assertEqual(self.payable("2411"), ZERO)

    def test_settlement_per_customer(self):
        """Balances are keyed (cid, acct): two customers accrue on the same
        broker; settling one leaves the other's payable intact."""
        self.accrue_fill(cid="C1")
        self.accrue_fill(cid="C2")
        self.assertEqual(self.payable("2411", "C1"), money("9.35"))
        self.assertEqual(self.payable("2411", "C2"), money("9.35"))
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C1", "broker": "BRK-A"})
        self.assertEqual(legs, [dr("2411", "9.35", "C1"),
                                cr("1100", "9.35", "C1")])
        self.assertEqual(self.payable("2411", "C1"), ZERO)
        self.assertEqual(self.payable("2411", "C2"), money("9.35"))  # intact
        legs = self.post("broker_fees_settled",
                         {"customer_id": "C2", "broker": "BRK-A"})
        self.assertEqual(legs, [dr("2411", "9.35", "C2"),
                                cr("1100", "9.35", "C2")])
        self.assertEqual(self.payable("2411", "C2"), ZERO)


if __name__ == "__main__":
    unittest.main()
