"""Phase 3 gate — order lifecycle, holds, T+2 settlement.

Covers the TEST GATE boxes for placement holds (A13), the A15 hold-formula
flag, partial-vs-final release discipline, cancel/reject zeroing, overfill
clamping (L11), the out-of-order edges S4/S5/S7, trade_settled discharge and
its reject rules (S6/A7, double settle), and malformed-payload rejection
(S9/S10). FIFO/lot-book/leg-construction boxes live in test_phase3_fifo.py.

Every reject test asserts TWO things (the graded pair): apply() returned []
AND the book is byte-identical — balances, orders, trades, lots, quarantine,
snapshot — because "reject" means zero residue, not merely no legs.
"""
import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tariff  # noqa: E402
from book import Book, ZERO, money, qnum  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_o_{_SEQ}",
            "type": etype, "payload": payload}


def fingerprint(b: Book) -> str:
    """Everything a reject must leave untouched, in one comparable string —
    the Phase 1 helper extended with the Phase 3 stores."""
    return json.dumps({
        "snap": b._snapshot_now(),
        "balances": sorted((f"{k}", str(v)) for k, v in b.balances.items()),
        "orders": sorted((k, repr(sorted(v.items())))
                         for k, v in b.orders.items()),
        "trades": sorted((k, repr(sorted(v.items())))
                         for k, v in b.trades.items()),
        "lots": sorted((str(k), repr(sorted(v.items())))
                       for k, v in b.lots.items()),
        "lot_index": sorted((repr(k), repr(v))
                            for k, v in b.lot_index.items()),
        "lot_seq": b.lot_seq,
        "quarantine": repr(b.quarantine),
        "fees": sorted((k, str(v)) for k, v in b.fees.items()),
        "withdrawals": sorted((k, str(v)) for k, v in b.withdrawals.items()),
        "accounts": sorted(b.accounts_touched),
        "events": sorted(b.events.keys()),
    }, sort_keys=True)


def legs_balance(legs) -> bool:
    dr = sum((D(l["debit"]) for l in legs), ZERO)
    cr = sum((D(l["credit"]) for l in legs), ZERO)
    return dr == cr


def place_payload(oid, *, cid="C1", side="buy", symbol="AAPL", qty="10",
                  limit="100", est="0.00", asset_class="equity") -> dict:
    return {"order_id": oid, "customer_id": cid, "side": side,
            "symbol": symbol, "quantity": qty, "limit_price": limit,
            "est_charges": est, "asset_class": asset_class}


def fill_payload(oid, tid, *, cid="C1", side="buy", symbol="AAPL", qty="10",
                 price="100", principal="1000.00", broker="BRK-A",
                 asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": side, "symbol": symbol, "quantity": qty, "price": price,
            "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


class Phase3Base(unittest.TestCase):
    _LOT = 0

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

    def place(self, oid, eid=None, **kw):
        return self.post("order_placed", place_payload(oid, **kw), eid)

    def fill(self, oid, tid, final=True, eid=None, **kw):
        etype = "order_filled" if final else "order_partially_filled"
        return self.post(etype, fill_payload(oid, tid, **kw), eid)

    def buy_lot(self, *, cid="C1", symbol="AAPL", qty="10", cost="1000.00",
                broker="BRK-A", asset_class="equity"):
        """A throwaway final buy fill whose only purpose is the lot."""
        Phase3Base._LOT += 1
        n = Phase3Base._LOT
        return self.fill(f"O-lot-{n}", f"T-lot-{n}", cid=cid, symbol=symbol,
                         qty=qty, principal=cost, price="1", broker=broker,
                         asset_class=asset_class)


class TestHolds(Phase3Base):
    def test_hold_single_quantize(self):
        """A13: hold = money(qty × limit + est_charges) — ONE quantize of
        the sum — stored at placement together with the route."""
        self.assertEqual(self.place("O1", qty="3", limit="33.335"), [])
        o = self.b.orders["O1"]
        self.assertEqual(o["hold_init"], money(D("3") * D("33.335")
                                               + D("0.00")))
        self.assertEqual(o["hold_init"], money("100.01"))  # 100.005 HALF_UP
        self.assertEqual(o["hold_rem"], money("100.01"))
        self.assertEqual(o["route"], tariff.route("equity", "3", "33.335"))
        self.assertEqual(o["route"], "BRK-A")
        snap = self.b.snapshot()
        self.assertEqual(snap["customers"]["C1"]["cash_hold"], "100.01")
        self.assertEqual(snap["open_order_routes"]["O1"], "BRK-A")
        # est_charges enters the same single quantize, as given (A11)
        self.place("O2", qty="2", limit="10", est="1.50")
        self.assertEqual(self.b.orders["O2"]["hold_rem"], money("21.50"))

    def test_hold_formulas_a_and_b(self):
        """A15 divergence fixture: hold_init 100.01, qty 3, two unit fills
        → (b) leaves 33.33, (a) leaves 33.34; both run to closed-hold-0."""
        saved = tariff.HOLD_FORMULA
        try:
            for formula, after_two in (("b", "33.33"), ("a", "33.34")):
                tariff.HOLD_FORMULA = formula
                b = Book()
                b.apply(ev("order_placed",
                           place_payload("O1", qty="3", limit="33.335")))
                self.assertEqual(b.orders["O1"]["hold_init"], money("100.01"))
                b.apply(ev("order_partially_filled",
                           fill_payload("O1", "T1", qty="1", price="33.335",
                                        principal="33.34")))
                b.apply(ev("order_partially_filled",
                           fill_payload("O1", "T2", qty="1", price="33.335",
                                        principal="33.34")))
                o = b.orders["O1"]
                self.assertEqual(o["hold_rem"], money(after_two),
                                 f"formula {formula!r}")
                self.assertFalse(o["closed"])
                b.apply(ev("order_filled",
                           fill_payload("O1", "T3", qty="1", price="33.335",
                                        principal="33.33")))
                self.assertTrue(o["closed"])
                self.assertEqual(o["hold_rem"], ZERO)
                self.assertEqual(
                    b.snapshot()["customers"]["C1"]["cash_hold"], "0.00")
        finally:
            tariff.HOLD_FORMULA = saved

    def test_partial_fill_never_releases_remainder(self):
        """The starter's delegation is gone: a partial releases ONLY its
        proportional share and never closes the order."""
        self.place("O1", qty="10", limit="10")            # hold 100.00
        self.fill("O1", "T1", final=False, qty="4", principal="400.00")
        o = self.b.orders["O1"]
        self.assertFalse(o["closed"])   # on_order_partially_filled ≠ close
        self.assertEqual(o["hold_rem"], money("60.00"))   # its share only
        self.assertIn("O1", self.b.snapshot()["open_order_routes"])
        self.fill("O1", "T2", final=False, qty="4", principal="400.00")
        self.assertFalse(o["closed"])
        self.assertEqual(o["hold_rem"], money("20.00"))
        self.assertGreater(o["hold_rem"], ZERO)  # > 0 until the final fill

    def test_final_fill_closes_and_zeroes_hold(self):
        # buy: money hold
        self.place("O1", qty="10", limit="10")
        self.fill("O1", "T1", final=False, qty="4", principal="400.00")
        self.fill("O1", "T2", qty="6", principal="600.00")       # final
        o = self.b.orders["O1"]
        self.assertTrue(o["closed"])
        self.assertEqual(o["hold_rem"], ZERO)
        self.assertNotIn("O1", self.b.snapshot()["open_order_routes"])
        self.assertEqual(self.b.snapshot()["customers"]["C1"]["cash_hold"],
                         "0.00")
        # sell: share hold
        self.buy_lot(qty="5", cost="50.00")
        self.place("O2", side="sell", qty="5", limit="10")
        o2 = self.b.orders["O2"]
        self.assertEqual(o2["share_hold_rem"], qnum(5))
        self.fill("O2", "T3", final=False, side="sell", qty="2",
                  principal="200.00")
        self.assertEqual(o2["share_hold_rem"], qnum(3))
        self.assertFalse(o2["closed"])
        self.fill("O2", "T4", side="sell", qty="3", principal="300.00")
        self.assertTrue(o2["closed"])
        self.assertEqual(o2["share_hold_rem"], qnum(0))

    def test_cancel_zeroes_hold(self):
        self.place("O1", qty="10", limit="10")
        self.fill("O1", "T1", final=False, qty="3", principal="300.00")
        self.assertEqual(self.b.orders["O1"]["hold_rem"], money("70.00"))
        self.assertEqual(self.post("order_cancelled", {"order_id": "O1"}), [])
        o = self.b.orders["O1"]
        self.assertTrue(o["closed"])
        self.assertEqual(o["hold_rem"], ZERO)     # exactly 0, not 70 − ε
        self.assertNotIn("O1", self.b.snapshot()["open_order_routes"])
        self.assertEqual(self.b.snapshot()["customers"]["C1"]["cash_hold"],
                         "0.00")
        # sell share hold zeroes too
        self.place("O2", side="sell", qty="5", limit="10")
        self.post("order_cancelled", {"order_id": "O2"})
        self.assertTrue(self.b.orders["O2"]["closed"])
        self.assertEqual(self.b.orders["O2"]["share_hold_rem"], qnum(0))

    def test_rejected_zeroes_hold(self):
        self.place("O1", qty="10", limit="10")
        self.fill("O1", "T1", final=False, qty="3", principal="300.00")
        self.assertEqual(self.post("order_rejected", {"order_id": "O1"}), [])
        o = self.b.orders["O1"]
        self.assertTrue(o["closed"])
        self.assertEqual(o["hold_rem"], ZERO)
        self.assertEqual(self.b.snapshot()["customers"]["C1"]["cash_hold"],
                         "0.00")
        self.place("O2", side="sell", qty="5", limit="10")
        self.post("order_rejected", {"order_id": "O2"})
        self.assertTrue(self.b.orders["O2"]["closed"])
        self.assertEqual(self.b.orders["O2"]["share_hold_rem"], qnum(0))

    def test_overfill_clamps_at_zero_posts_legs(self):
        """L11: Σ fills > ordered → hold clamps at 0, the legs post anyway
        (the money is real), and the oddity lands in quarantine."""
        self.place("O1", qty="2", limit="10")             # hold 20.00
        self.fill("O1", "T1", final=False, qty="1", principal="100.00")
        legs = self.fill("O1", "T2", final=False, qty="2",
                         principal="200.00", eid="E-OF")
        self.assertEqual(len(legs), 13)                   # posted in full
        o = self.b.orders["O1"]
        self.assertEqual(o["hold_rem"], ZERO)             # clamped, never < 0
        self.assertIn(("overfill", "O1", "E-OF"), self.b.quarantine)
        self.assertIn("T2", self.b.trades)


class TestOutOfOrder(Phase3Base):
    def test_fill_after_cancel_posts(self):
        """S7: the lifecycle is over but the money is real — post the legs,
        hold stays 0, note the oddity."""
        self.place("O1", qty="10", limit="10")
        self.post("order_cancelled", {"order_id": "O1"})
        legs = self.fill("O1", "T1", final=False, qty="2",
                         principal="200.00", eid="E-S7")
        self.assertEqual(len(legs), 13)
        o = self.b.orders["O1"]
        self.assertTrue(o["closed"])
        self.assertEqual(o["hold_rem"], ZERO)
        self.assertIn(("fill_after_close", "O1", "E-S7"), self.b.quarantine)
        self.assertIn("T1", self.b.trades)
        self.assertEqual(self.b.snapshot()["customers"]["C1"]["cash_hold"],
                         "0.00")

    def test_fill_before_placement_stub(self):
        """S4: the fill posts its full 13 legs from its own payload; the
        late placement computes the hold net of the stub fill's release."""
        legs = self.fill("O1", "T1", final=False, qty="1", price="33.335",
                         principal="33.34", eid="E-S4")
        self.assertEqual(len(legs), 13)     # nothing missing from the payload
        o = self.b.orders["O1"]
        self.assertFalse(o["placed"])
        self.assertFalse(o["closed"])
        self.assertEqual(o["hold_rem"], ZERO)
        # A10: open-but-never-placed stub has no route
        self.assertNotIn("O1", self.b.snapshot()["open_order_routes"])
        self.place("O1", qty="3", limit="33.335")
        self.assertEqual(o["hold_init"], money("100.01"))
        self.assertEqual(o["hold_rem"], money("66.67"))   # net of stub (b)
        self.assertIn("O1", self.b.snapshot()["open_order_routes"])
        self.assertEqual(self.b.snapshot()["customers"]["C1"]["cash_hold"],
                         "66.67")
        # order_filled-first: the stub closes; the placement finds a tombstone
        self.fill("O2", "T2", qty="5", principal="500.00")
        o2 = self.b.orders["O2"]
        self.assertTrue(o2["closed"])
        self.place("O2", qty="5", limit="100")
        self.assertTrue(o2["closed"])
        self.assertEqual(o2["hold_init"], ZERO)
        self.assertEqual(o2["hold_rem"], ZERO)
        self.assertNotIn("O2", self.b.snapshot()["open_order_routes"])

    def test_cancel_before_placement_tombstone(self):
        """S5: cancel on an unknown oid → closed stub; the late placement
        creates no hold and no open route."""
        self.assertEqual(self.post("order_cancelled", {"order_id": "O1"}), [])
        o = self.b.orders["O1"]
        self.assertTrue(o["closed"])
        self.assertFalse(o["placed"])
        self.place("O1", qty="10", limit="10")
        self.assertTrue(o["closed"])
        self.assertEqual(o["hold_init"], ZERO)
        self.assertEqual(o["hold_rem"], ZERO)
        self.assertNotIn("O1", self.b.snapshot()["open_order_routes"])
        self.assertEqual(self.b.snapshot()["customers"]["C1"]["cash_hold"],
                         "0.00")


class TestSettlement(Phase3Base):
    def test_trade_settled_buy_sell_legs(self):
        """T+2 discharge at the STORED principal and cid:
        buy Dr 2350 / Cr 1100 · sell Dr 1100 / Cr 1150."""
        self.fill("O-B", "T-B", qty="10", principal="1000.00")
        legs = self.post("trade_settled", {"trade_id": "T-B"})
        self.assertEqual(legs, [
            {"account": "2350", "customer_id": "C1",
             "debit": "1000.00", "credit": "0.00"},
            {"account": "1100", "customer_id": "C1",
             "debit": "0.00", "credit": "1000.00"}])
        self.assertTrue(self.b.trades["T-B"]["settled"])
        self.buy_lot(symbol="SPY", qty="10", cost="1000.00",
                     asset_class="etf")
        self.fill("O-S", "T-S", side="sell", symbol="SPY", qty="5",
                  price="120", principal="600.00", broker="BRK-C",
                  asset_class="etf", rate="0.25")
        legs = self.post("trade_settled", {"trade_id": "T-S"})
        self.assertEqual(legs, [
            {"account": "1100", "customer_id": "C1",
             "debit": "600.00", "credit": "0.00"},
            {"account": "1150", "customer_id": "C1",
             "debit": "0.00", "credit": "600.00"}])
        self.assertTrue(self.b.trades["T-S"]["settled"])

    def test_trade_settled_unknown_rejects_and_stays_rejected(self):
        """S6/A7: settle-before-fill rejects — and the redelivered event id
        stays rejected forever, even once the fill exists."""
        self.assert_rejected("trade_settled", {"trade_id": "T-X"}, "S-early")
        self.fill("O1", "T-X", qty="10", principal="1000.00")
        before = fingerprint(self.b)
        self.assertEqual(self.post("trade_settled",
                                   {"trade_id": "T-X"}, "S-early"), [])
        self.assertEqual(fingerprint(self.b), before)
        self.assertFalse(self.b.trades["T-X"]["settled"])   # no legs, ever

    def test_trade_settled_double_rejects(self):
        self.fill("O1", "T1", qty="10", principal="1000.00")
        legs = self.post("trade_settled", {"trade_id": "T1"}, "S1")
        self.assertEqual(len(legs), 2)
        # second DISTINCT settle of the same trade: the error, rejected
        self.assert_rejected("trade_settled", {"trade_id": "T1"}, "S2")
        # REDELIVERED settle (same eid): a duplicate, silent no-op
        before = fingerprint(self.b)
        self.assertEqual(self.post("trade_settled",
                                   {"trade_id": "T1"}, "S1"), [])
        self.assertEqual(fingerprint(self.b), before)


class TestMalformed(Phase3Base):
    def test_malformed_order_events_reject(self):
        """S9/S10: every malformed payload rejects with ZERO mutation."""
        for payload in (
                {},                                        # missing order_id
                "not a dict",
                place_payload("OX", side="hold"),          # bad side
                place_payload("OX", qty="0"),
                place_payload("OX", qty="-1"),
                place_payload("OX", qty="abc"),
                place_payload("OX", limit="0"),
                place_payload("OX", limit="-5"),
                {k: v for k, v in place_payload("OX").items()
                 if k != "customer_id"},
                {k: v for k, v in place_payload("OX").items()
                 if k != "est_charges"},
                place_payload("OX", asset_class="crypto"),  # unroutable
        ):
            self.assert_rejected("order_placed", payload)
        self.assertNotIn("OX", self.b.orders)
        for etype in ("order_partially_filled", "order_filled"):
            for payload in (
                    "not a dict",
                    {k: v for k, v in fill_payload("OX", "TX").items()
                     if k != "order_id"},
                    {k: v for k, v in fill_payload("OX", "TX").items()
                     if k != "trade_id"},
                    {k: v for k, v in fill_payload("OX", "TX").items()
                     if k != "broker"},
                    {k: v for k, v in fill_payload("OX", "TX").items()
                     if k != "partner_rate"},
                    {k: v for k, v in fill_payload("OX", "TX").items()
                     if k != "customer_id"},
                    fill_payload("OX", "TX", side="short"),
                    fill_payload("OX", "TX", qty="0"),
                    fill_payload("OX", "TX", qty="-2"),
                    fill_payload("OX", "TX", price="0"),
                    fill_payload("OX", "TX", price="-1"),
                    fill_payload("OX", "TX", principal="0.00"),
                    fill_payload("OX", "TX", principal="-10.00"),
                    fill_payload("OX", "TX", broker="BRK-Z"),
                    fill_payload("OX", "TX", rate="-0.25"),
            ):
                self.assert_rejected(etype, payload)
        self.assertNotIn("OX", self.b.orders)
        self.assertNotIn("TX", self.b.trades)
        self.assert_rejected("trade_settled", {})          # missing trade_id
        self.assert_rejected("trade_settled", {"trade_id": ""})
        self.assert_rejected("trade_settled", "not a dict")


if __name__ == "__main__":
    unittest.main()
