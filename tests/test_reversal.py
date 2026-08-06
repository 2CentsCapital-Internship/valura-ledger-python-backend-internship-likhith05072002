"""Phase 5 gate — the generic reversal handler.

Covers every reversal TEST GATE unit box: the reject matrix (R1 unknown
ref, R2 reversal-of-rejected, R3 double reversal, R4 reversal-of-reversal,
rejected-reversal-stays-rejected incl. redelivery after the original
arrives); inverse legs = the STORED legs with sides swapped, asserted by
string-object identity (no recomputation path exists); the surgical lot
undo (L5 partially-consumed buy with the zero clamp, L6 three-lot sell
restored in place with cents verbatim and FIFO preserved, L7 both
directions across a split, R13 no-leg events, undo-wherever-the-lot-lives
after a rename); and side-table hygiene (R5 reversed fee, R6 reversed
unsettled fill + the A12 one-line flag companion, R7 reversed settled
fill, reversed withdrawal request, holds never restored).

Note on R13 scope: the reference card groups dividend_reinvested with the
no-leg events, but its STORED legs are Dr 1200 / Cr 2100 — posting [] for
its reversal would strand those balances and break the involution matrix.
Work-order step 3 limits the []-legs rule to stock_split and
symbol_change; the reinvest reversal posts the stored-legs inverse and is
asserted as such here.
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import book as book_mod  # noqa: E402  (flag monkeypatching: SETTLE_REVERSED_FILL)
from book import Book, ZERO, money, qnum  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_rv_{_SEQ}",
            "type": etype, "payload": payload}


def fingerprint(b: Book) -> str:
    """Everything a reject must leave untouched, in one comparable string —
    the Phase 4 helper extended with the Phase 5 mutables: the fees /
    refunded / withdrawals side tables and every event's reversed flag."""
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
        "merge_seq": b.merge_seq,
        "quarantine": repr(b.quarantine),
        "accounts": sorted(b.accounts_touched),
        "events": sorted(b.events.keys()),
        "reversed": sorted((k, v["reversed"]) for k, v in b.events.items()),
        "fees": sorted((k, repr(sorted(v.items())))
                       for k, v in b.fees.items()),
        "refunded": sorted(b.refunded),
        "withdrawals": sorted((k, repr(sorted(v.items())))
                              for k, v in b.withdrawals.items()),
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
                 price="100", principal="1000.00", broker="BRK-A",
                 asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": side, "symbol": symbol, "quantity": qty, "price": price,
            "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


class ReversalBase(unittest.TestCase):
    _LOT = 0

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

    def reverse(self, src, eid=None):
        return self.post("reversal", {"reverses_event_id": src}, eid)

    def buy_lot(self, *, cid="C1", symbol="SYM", qty="10", cost="1000.00",
                eid=None):
        ReversalBase._LOT += 1
        n = ReversalBase._LOT
        return self.post("order_filled", fill_payload(
            f"O-rv-{n}", f"T-rv-{n}", cid=cid, symbol=symbol, qty=qty,
            principal=cost, price="1"), eid)

    def sell(self, qty, principal, *, cid="C1", symbol="SYM", eid=None):
        ReversalBase._LOT += 1
        n = ReversalBase._LOT
        return self.post("order_filled", fill_payload(
            f"O-rv-{n}", f"T-rv-{n}", cid=cid, side="sell", symbol=symbol,
            qty=qty, principal=principal, price="1"), eid)


# --------------------------------------------------------------------- #
#  the reject matrix                                                    #
# --------------------------------------------------------------------- #
class TestReversalRejects(ReversalBase):
    def test_unknown_ref_rejects(self):
        """R1: a reversal naming an event id we have never seen → legs [],
        book byte-identical."""
        self.post("deposit", {"customer_id": "C1", "amount": "50.00"})
        self.assert_rejected("reversal", {"reverses_event_id": "NEVER-SEEN"},
                             "REV-U")
        self.assertNotIn("REV-U", self.b.events)

    def test_reversal_of_rejected_event_rejects(self):
        """R2: the original was rejected BY US — it is in seen but not in
        the posted set, so its reversal rejects too."""
        self.assertEqual(self.post(
            "deposit", {"customer_id": "C1", "amount": "-5"}, "E-BAD"), [])
        self.assertIn("E-BAD", self.b.seen)          # logged first delivery
        self.assertNotIn("E-BAD", self.b.events)     # but never posted
        self.assert_rejected("reversal", {"reverses_event_id": "E-BAD"},
                             "REV-B")

    def test_double_reversal_rejects(self):
        """R3: a second reversal (distinct eid) of the same original →
        legs [], and the FIRST reversal's effects stand untouched."""
        self.post("deposit", {"customer_id": "C1", "amount": "100.00"}, "E-D")
        rev = self.reverse("E-D", "REV-1")
        self.assertEqual(rev, [cr("1100", "100.00"), dr("2010", "100.00")])
        self.assertTrue(self.b.events["E-D"]["reversed"])
        self.assert_rejected("reversal", {"reverses_event_id": "E-D"},
                             "REV-2")
        self.assertIn("REV-1", self.b.events)        # first reversal intact
        self.assertNotIn("REV-2", self.b.events)
        self.assertEqual(self.b.balances[("C1", "1100")], ZERO)
        self.assertEqual(self.b.balances[("C1", "2010")], ZERO)

    def test_reversal_of_reversal_rejects(self):
        """R4/A5 default: a reversal may never be the target of another."""
        self.post("deposit", {"customer_id": "C1", "amount": "100.00"}, "E-D")
        self.reverse("E-D", "REV-1")
        self.assert_rejected("reversal", {"reverses_event_id": "REV-1"},
                             "REV-OF-REV")

    def test_rejected_reversal_stays_rejected(self):
        """A reversal that arrives BEFORE its original rejects — and stays
        rejected forever: redelivering the same event id after the original
        has arrived posts nothing (seen set, first delivery wins)."""
        early = ev("reversal", {"reverses_event_id": "E-LATE"}, "REV-EARLY")
        self.assertEqual(self.b.apply(early), [])
        self.assertNotIn("REV-EARLY", self.b.events)
        self.post("deposit", {"customer_id": "C1", "amount": "75.00"},
                  "E-LATE")                          # the original arrives
        before = fingerprint(self.b)
        self.assertEqual(self.b.apply(early), [])    # exact redelivery
        self.assertEqual(self.b.apply(                # fresh copy, same id
            ev("reversal", {"reverses_event_id": "E-LATE"}, "REV-EARLY")), [])
        self.assertEqual(fingerprint(self.b), before)
        self.assertNotIn("REV-EARLY", self.b.events)
        self.assertFalse(self.b.events["E-LATE"]["reversed"])
        self.assertEqual(self.b.balances[("C1", "2010")], -money("75.00"))


# --------------------------------------------------------------------- #
#  inverse legs = stored legs, sides swapped                            #
# --------------------------------------------------------------------- #
class TestInverseLegs(ReversalBase):
    def test_inverse_is_stored_legs(self):
        """The reversal's legs are the original's STORED legs with debit and
        credit swapped — asserted by string-OBJECT identity against
        book.events[src]["legs"], proving there is no recomputation path
        (a recomputed amount would be a fresh string)."""
        self.post("order_filled", fill_payload(
            "O-inv", "T-inv", principal="10000.00"), "E-F")
        stored = self.b.events["E-F"]["legs"]
        self.assertEqual(len(stored), 13)            # the graded 13-leg buy
        rev = self.reverse("E-F", "E-R")
        self.assertEqual(len(rev), len(stored))
        for r, s in zip(rev, stored):
            self.assertIs(r["account"], s["account"])
            self.assertIs(r["customer_id"], s["customer_id"])
            self.assertIs(r["debit"], s["credit"])   # the SAME string object
            self.assertIs(r["credit"], s["debit"])   # — provably not remade
        # spot-check the amounts verbatim (P=10000 BRK-A rate 0.50 fixture)
        self.assertEqual(rev[0], cr("2010", "10032.00"))
        self.assertEqual(rev[5], dr("2350", "10000.00"))
        self.assertEqual(rev[10], dr("2411", "9.35"))
        self.assertEqual(rev[12], dr("2430", "6.33"))
        # and the reversal's own record stores exactly these legs, plus the
        # undo's ACTUAL lot deltas (work-order item 7: "its undo ops") —
        # what the referees need to reconcile a partially-consumed undo.
        self.assertEqual(self.b.events["E-R"]["legs"], rev)
        self.assertEqual(self.b.events["E-R"]["lot_ops"],
                         [("undo_add", 1, D("10.000000"), D("10000.00"))])


# --------------------------------------------------------------------- #
#  the surgical lot undo                                                #
# --------------------------------------------------------------------- #
class TestLotUndo(ReversalBase):
    def test_reverse_buy_partially_consumed(self):
        """L5/R15: buy 100 → sell 40 → reverse the buy: the lot's REMAINDER
        (60 qty / 1800.00 cost) is removed, never the original quantity;
        2100 and 1200 land exactly; a fully-consumed lot clamps at zero
        without crashing."""
        self.buy_lot(qty="100", cost="3000.00", eid="E-B")     # lot 1
        self.sell("40", "1600.00")            # relieves money(3000×40/100)
        self.assertEqual(self.b.lots[1]["qty"], qnum("60"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("1800.00"))
        self.reverse("E-B", "R-B")
        self.assertEqual(self.b.lots[1]["qty"], qnum(0))       # remainder
        self.assertEqual(self.b.lots[1]["cost_total"], ZERO)   # removed
        self.assertIn(1, self.b.lots)                          # zombie stays
        # 2100: −3000 (buy Cr) + 1200 (sell Dr) + 3000 (inverse Dr) = +1200
        self.assertEqual(self.b.balances[("C1", "2100")], money("1200.00"))
        self.assertEqual(self.b.balances[("C1", "1200")], -money("1200.00"))
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["positions"], {})
        # clamp: a FULLY consumed lot reverses without crashing, stays 0/0
        self.buy_lot(symbol="FUL", qty="10", cost="500.00", eid="E-B2")
        self.sell("10", "600.00", symbol="FUL")
        self.assertEqual(self.b.lots[2]["qty"], qnum(0))
        legs = self.reverse("E-B2", "R-B2")
        self.assertTrue(legs)                                  # posted
        self.assertEqual(self.b.lots[2]["qty"], qnum(0))       # clamped
        self.assertEqual(self.b.lots[2]["cost_total"], ZERO)

    def test_reverse_sell_three_lots_in_place(self):
        """L6/M5: lot 1 previously partially relieved; a sell spans all
        three lots; its reversal restores every portion IN PLACE on its own
        lot — cost cents verbatim, FIFO order preserved, and the portion on
        lot 3 merges with lot 3's surviving remainder."""
        self.buy_lot(qty="10", cost="100.00")                  # lot 1
        self.buy_lot(qty="5", cost="77.77")                    # lot 2
        self.buy_lot(qty="8", cost="55.55")                    # lot 3
        self.sell("4", "160.00")              # pre-relief: lot 1 → 6 / 60.00
        self.assertEqual(self.b.lots[1]["qty"], qnum("6"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("60.00"))
        self.sell("14", "700.00", eid="E-S")  # spans 6 + 5 + 3
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("6"), money("60.00"), Fraction(1)),
            ("consume", 2, qnum("5"), money("77.77"), Fraction(1)),
            ("consume", 3, qnum("3"), money("20.83"), Fraction(1))])
        self.assertEqual(self.b.lots[3]["qty"], qnum("5"))     # survivor
        self.assertEqual(self.b.lots[3]["cost_total"], money("34.72"))
        self.reverse("E-S", "R-S")
        for lot_id, qty, cost in ((1, "6", "60.00"), (2, "5", "77.77"),
                                  (3, "8", "55.55")):
            self.assertEqual(self.b.lots[lot_id]["qty"], qnum(qty))
            self.assertEqual(self.b.lots[lot_id]["cost_total"], money(cost))
        # lot 3: 34.72 + 20.83 = 55.55 — merged with the survivor, no lost
        # cents (M5). FIFO preserved: the next sell hits lot 1 first at its
        # restored cost.
        self.assertEqual(self.b.lot_index[("C1", "SYM")], [1, 2, 3])
        self.sell("2", "90.00", eid="E-S2")
        self.assertEqual(self.b.events["E-S2"]["lot_ops"], [
            ("consume", 1, qnum("2"), money("20.00"), Fraction(1))])

    def test_reverse_across_split(self):
        """L7/A8 both directions. (a) buy → split 1:2 → reverse buy removes
        the POST-SPLIT remainder. (b) buy → sell → split → reverse sell
        restores qty × (current ÷ recorded multiplier), cost unchanged."""
        # (a) reverse the buy after the split
        self.buy_lot(qty="10", cost="100.00", eid="E-B")       # lot 1
        self.post("stock_split", {"customer_id": "C1", "symbol": "SYM",
                                  "ratio_from": "1", "ratio_to": "2"})
        self.assertEqual(self.b.lots[1]["qty"], qnum("20"))
        self.reverse("E-B", "R-B")
        self.assertEqual(self.b.lots[1]["qty"], qnum(0))       # remainder
        self.assertEqual(self.b.lots[1]["cost_total"], ZERO)   # removed
        self.assertEqual(self.b.balances[("C1", "2100")], ZERO)
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["positions"], {})
        # (b) reverse the sell after the split: the smoke fixture — buy 10
        # @ 100.03, sell all 10, split 1:2, reverse the sell → 20 shares /
        # 100.03 exact (qty × 2/1, cost verbatim)
        self.b = Book()
        self.buy_lot(symbol="SPL", qty="10", cost="100.03")    # lot 1
        self.sell("10", "150.00", symbol="SPL", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("10"), money("100.03"), Fraction(1))])
        self.post("stock_split", {"customer_id": "C1", "symbol": "SPL",
                                  "ratio_from": "1", "ratio_to": "2"})
        self.assertEqual(self.b.lots[1]["qty"], qnum(0))       # zombie split
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(2))
        self.reverse("E-S", "R-S")
        self.assertEqual(self.b.lots[1]["qty"], qnum("20"))    # 10 × (2/1)
        self.assertEqual(self.b.lots[1]["cost_total"], money("100.03"))
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["positions"]["SPL"],
            {"quantity": "20", "cost_basis": "100.03"})

    def test_reverse_no_leg_events(self):
        """R13: reversals of stock_split and symbol_change post [] but MUST
        undo the lot book byte-exactly; a dividend_reinvested reversal
        posts its stored-legs inverse (see module docstring) and zombifies
        the reinvest lot. Compared on balances + lots, not full snapshots —
        accounts_touched legitimately accumulates."""
        def lots_state(b):
            return (repr(sorted((k, sorted(v.items()))
                                for k, v in b.lots.items())),
                    repr(sorted(b.lot_index.items())))

        # (1) stock_split — byte-exact undo, Fraction multiplier restored
        self.buy_lot(qty="10", cost="100.00")                  # lot 1
        bal = dict(self.b.balances)
        lots = lots_state(self.b)
        self.post("stock_split", {"customer_id": "C1", "symbol": "SYM",
                                  "ratio_from": "2", "ratio_to": "3"},
                  "E-SP")
        legs = self.reverse("E-SP", "R-SP")
        self.assertEqual(legs, [])
        self.assertEqual(self.b.events["R-SP"]["legs"], [])
        self.assertEqual(dict(self.b.balances), bal)
        self.assertEqual(lots_state(self.b), lots)
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(1))
        # (2) symbol_change — [] legs, index and symbol restored
        lots = lots_state(self.b)
        self.post("symbol_change", {"customer_id": "C1", "old_symbol": "SYM",
                                    "new_symbol": "OTH"}, "E-RN")
        self.assertEqual(self.b.lots[1]["symbol"], "OTH")
        legs = self.reverse("E-RN", "R-RN")
        self.assertEqual(legs, [])
        self.assertEqual(self.b.events["R-RN"]["legs"], [])
        self.assertEqual(dict(self.b.balances), bal)
        self.assertEqual(lots_state(self.b), lots)
        self.assertEqual(self.b.lot_index[("C1", "SYM")], [1])
        self.assertNotIn(("C1", "OTH"), self.b.lot_index)
        # (3) dividend_reinvested — stored-legs inverse, lot undone
        bal = dict(self.b.balances)
        self.post("dividend_reinvested", {
            "customer_id": "C1", "symbol": "SYM", "net_amount": "10.00",
            "reinvest_quantity": "1"}, "E-DR")                 # lot 2
        legs = self.reverse("E-DR", "R-DR")
        self.assertEqual(legs, [cr("1200", "10.00"), dr("2100", "10.00")])
        self.assertEqual(self.b.lots[2]["qty"], qnum(0))       # zombified
        self.assertEqual(self.b.lots[2]["cost_total"], ZERO)
        self.assertEqual({k: v for k, v in self.b.balances.items() if v != 0},
                         {k: v for k, v in bal.items() if v != 0})
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["positions"]["SYM"],
            {"quantity": "10", "cost_basis": "100.00"})

    def test_reverse_after_symbol_change(self):
        """Buy under A → rename A→B → reverse the buy: the remainder is
        removed WHEREVER the lot now lives (under B), by lot id."""
        self.buy_lot(symbol="A", qty="10", cost="100.00", eid="E-B")
        self.post("symbol_change", {"customer_id": "C1", "old_symbol": "A",
                                    "new_symbol": "B"})
        self.reverse("E-B", "R-B")
        lot = self.b.lots[1]
        self.assertEqual(lot["symbol"], "B")         # still lives under B
        self.assertEqual(lot["qty"], qnum(0))
        self.assertEqual(lot["cost_total"], ZERO)
        self.assertEqual(self.b.lot_index[("C1", "B")], [1])   # zombie slot
        self.assertEqual(self.b.balances[("C1", "2100")], ZERO)
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["positions"], {})


# --------------------------------------------------------------------- #
#  side-table hygiene                                                   #
# --------------------------------------------------------------------- #
class TestSideTables(ReversalBase):
    def test_refund_of_reversed_fee_rejects(self):
        """R5: reversing a fee_charged deletes it from the refund lookup —
        a later fee_refund naming it rejects."""
        self.post("fee_charged", {"customer_id": "C1", "amount": "12.00"},
                  "E-FEE")
        self.assertIn("E-FEE", self.b.fees)
        self.reverse("E-FEE", "R-FEE")
        self.assertNotIn("E-FEE", self.b.fees)
        self.assert_rejected("fee_refund", {"refunds_source_id": "E-FEE"})

    def test_settle_trade_of_reversed_fill_rejects(self):
        """R6/A12 default: reversing an UNSETTLED fill deletes its trade —
        the later trade_settled rejects."""
        self.assertFalse(book_mod.SETTLE_REVERSED_FILL)        # the default
        self.post("order_filled", fill_payload(
            "O-r6", "T-r6", principal="1000.00"), "E-F")
        self.assertIn("T-r6", self.b.trades)
        self.reverse("E-F", "R-F")
        self.assertNotIn("T-r6", self.b.trades)
        self.assert_rejected("trade_settled", {"trade_id": "T-r6"})

    def test_settle_trade_of_reversed_fill_posts_with_flag(self):
        """A12 companion: SETTLE_REVERSED_FILL = True is a one-line module
        flag flip — the trade survives the reversal and its settlement
        posts."""
        book_mod.SETTLE_REVERSED_FILL = True
        try:
            self.post("order_filled", fill_payload(
                "O-r6b", "T-r6b", principal="1000.00"), "E-F")
            self.reverse("E-F", "R-F")
            self.assertIn("T-r6b", self.b.trades)   # kept under the flag
            self.assertFalse(self.b.trades["T-r6b"]["settled"])
            legs = self.post("trade_settled", {"trade_id": "T-r6b"}, "E-TS")
            self.assertEqual(legs, [dr("2350", "1000.00"),
                                    cr("1100", "1000.00")])
            self.assertTrue(self.b.trades["T-r6b"]["settled"])
        finally:
            book_mod.SETTLE_REVERSED_FILL = False

    def test_reverse_settled_fill(self):
        """R7: reversing a SETTLED fill posts the inverse legs and undoes
        the lot, but the trade stays settled and 1100 is untouched — the
        cash moved exactly once, at settlement."""
        self.post("order_filled", fill_payload(
            "O-r7", "T-r7", principal="1000.00"), "E-F")
        self.post("trade_settled", {"trade_id": "T-r7"}, "E-TS")
        cash = self.b.balances[("C1", "1100")]
        self.assertEqual(cash, -money("1000.00"))    # moved ONCE, at settle
        self.assertEqual(self.b.balances[("C1", "2350")], ZERO)
        rev = self.reverse("E-F", "R-F")
        self.assertEqual(len(rev), 13)
        self.assertNotIn("1100", [l["account"] for l in rev])
        self.assertTrue(self.b.trades["T-r7"]["settled"])      # stays
        self.assertEqual(self.b.balances[("C1", "1100")], cash)  # no double
        # 2350: Cr at fill, Dr at settle, Dr again on the inverse → +1000 —
        # and a redelivered settlement (fresh id) rejects: no second cash
        # movement is possible.
        self.assertEqual(self.b.balances[("C1", "2350")], money("1000.00"))
        self.assert_rejected("trade_settled", {"trade_id": "T-r7"})
        self.assertEqual(self.b.lots[self.b.lot_seq]["qty"], qnum(0))

    def test_reverse_withdrawal_request(self):
        """Reversing a withdrawal_requested closes the wid state machine
        (terminal): both closers for that wid reject afterwards."""
        self.post("deposit", {"customer_id": "C1", "amount": "100.00"})
        self.post("withdrawal_requested", {
            "customer_id": "C1", "withdrawal_id": "W1", "amount": "40.00"},
            "E-W")
        self.reverse("E-W", "R-W")
        self.assertEqual(self.b.withdrawals["W1"]["state"], "reversed")
        self.assert_rejected("withdrawal_settled", {"withdrawal_id": "W1"})
        self.assert_rejected("withdrawal_rejected", {"withdrawal_id": "W1"})
        self.assertEqual(self.b.balances[("C1", "2300")], ZERO)
        self.assertEqual(self.b.balances[("C1", "2010")], -money("100.00"))

    def test_holds_never_restored(self):
        """Holds are lifecycle, not postings: a hold the fill released stays
        released through the fill's reversal — partially released stays at
        the released level, fully released stays at zero."""
        self.post("order_placed", {
            "order_id": "O-H", "customer_id": "C1", "side": "buy",
            "symbol": "SYM", "quantity": "10", "limit_price": "100",
            "est_charges": "5.00", "asset_class": "equity"})
        o = self.b.orders["O-H"]
        self.assertEqual(o["hold_rem"], money("1005.00"))
        self.post("order_partially_filled", fill_payload(
            "O-H", "T-H1", qty="4", principal="400.00"), "E-PF")
        self.assertEqual(o["hold_rem"], money("603.00"))       # 402 released
        self.reverse("E-PF", "R-PF")
        self.assertEqual(o["hold_rem"], money("603.00"))       # NOT restored
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["cash_hold"], "603.00")
        self.post("order_filled", fill_payload(
            "O-H", "T-H2", qty="6", principal="600.00"), "E-FF")
        self.assertEqual(o["hold_rem"], ZERO)        # final fill: hold 0
        self.reverse("E-FF", "R-FF")
        self.assertEqual(o["hold_rem"], ZERO)        # hold still 0
        self.assertTrue(o["closed"])
        self.assertEqual(
            self.b.snapshot()["customers"]["C1"]["cash_hold"], "0.00")


if __name__ == "__main__":
    unittest.main()
