"""Phase 4 gate — corporate actions, all strictly per-customer.

Covers every TEST GATE unit box: dividend_cash legs + D2 observe/arm +
zero-position posting + malformed rejects; dividend_reinvested legs + lot
at the back of the FIFO queue + partial-relief formula + rejects;
stock_split scaling (qty 6 dp, cost untouched, exact Fraction multiplier,
zombies included, per-customer only, zero-position no-op, rejects) and the
split->sell poison; symbol_change re-keying (merge interleave by global
sequence with the A6 existing_first flag verified switchable, chains
A->B->C, rename-then-trade, per-customer only, zero-position no-op); the
lot_ops R13-readiness contract for all four; and duplicate idempotency
with replay-twice byte-identity.
"""
import json
import os
import sys
import unittest
from decimal import Decimal
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import book as book_mod  # noqa: E402  (flag monkeypatching: ARM_D2, SYMBOL_MERGE)
from book import Book, ZERO, money, qnum  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_ca_{_SEQ}",
            "type": etype, "payload": payload}


def fingerprint(b: Book) -> str:
    """Everything a reject must leave untouched, in one comparable string —
    the Phase 3 helper extended with merge_seq (the only new mutable)."""
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


class CorporateBase(unittest.TestCase):
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

    def buy_lot(self, *, cid="C1", symbol="SYM", qty="10", cost="1000.00",
                eid=None):
        CorporateBase._LOT += 1
        n = CorporateBase._LOT
        return self.post("order_filled", fill_payload(
            f"O-ca-{n}", f"T-ca-{n}", cid=cid, symbol=symbol, qty=qty,
            principal=cost, price="1"), eid)

    def sell(self, qty, principal, *, cid="C1", symbol="SYM", eid=None):
        CorporateBase._LOT += 1
        n = CorporateBase._LOT
        return self.post("order_filled", fill_payload(
            f"O-ca-{n}", f"T-ca-{n}", cid=cid, side="sell", symbol=symbol,
            qty=qty, principal=principal, price="1"), eid)


# --------------------------------------------------------------------- #
#  dividend_cash                                                        #
# --------------------------------------------------------------------- #
class TestDividendCash(CorporateBase):
    def test_dividend_cash_legs(self):
        """Net 12.34 → exactly Dr 1100 / Cr 2010, wallet +12.34, and NO
        other account — no gross, no tax payable, ever."""
        legs = self.post("dividend_cash", {
            "customer_id": "C1", "gross_amount": "14.51",
            "withholding_tax": "2.17", "net_amount": "12.34"}, "E-DC")
        self.assertEqual(legs, [dr("1100", "12.34"), cr("2010", "12.34")])
        self.assertEqual(self.b.accounts_touched, {"1100", "2010"})
        self.assertEqual(set(self.b.balances),
                         {("C1", "1100"), ("C1", "2010")})
        snap = self.b.snapshot()
        self.assertEqual(snap["customers"]["C1"]["wallet_cash"], "12.34")
        self.assertEqual(self.b.quarantine, [])          # 14.51 − 2.17 = net

    def test_dividend_cash_net_mismatch_observed(self):
        """D2: gross 10.00, tax 1.50, net 9.00 → the NET posts verbatim and
        quarantine gains exactly one D2 record; with ARM_D2=True the same
        event is Rejected, legs [], book byte-identical."""
        legs = self.post("dividend_cash", {
            "customer_id": "C1", "gross_amount": "10.00",
            "withholding_tax": "1.50", "net_amount": "9.00"}, "E-D2")
        self.assertEqual(legs, [dr("1100", "9.00"), cr("2010", "9.00")])
        self.assertEqual(self.b.quarantine,
                         [("D2", "E-D2", "10.00", "1.50", "9.00")])
        book_mod.ARM_D2 = True
        try:
            self.assert_rejected("dividend_cash", {
                "customer_id": "C1", "gross_amount": "10.00",
                "withholding_tax": "1.50", "net_amount": "9.00"}, "E-D2b")
        finally:
            book_mod.ARM_D2 = False
        self.assertEqual(len(self.b.quarantine), 1)      # armed → no new log

    def test_dividend_cash_zero_position_posts(self):
        """A dividend on a customer holding no shares still posts — the
        phantom-dividend detector is observe-only (dividend-before-buy)."""
        self.assertEqual(self.b.lots, {})
        legs = self.post("dividend_cash", {
            "customer_id": "C-EMPTY", "net_amount": "3.21"}, "E-ZP")
        self.assertEqual(legs, [dr("1100", "3.21", "C-EMPTY"),
                                cr("2010", "3.21", "C-EMPTY")])
        self.assertIn("E-ZP", self.b.events)
        snap = self.b.snapshot()
        self.assertEqual(snap["customers"]["C-EMPTY"]["wallet_cash"], "3.21")
        self.assertEqual(snap["customers"]["C-EMPTY"]["positions"], {})

    def test_dividend_cash_malformed_rejects(self):
        """S9/S10: missing / non-numeric / zero / negative net_amount →
        Rejected, apply returns [], zero mutation (byte-identical)."""
        self.post("dividend_cash",
                  {"customer_id": "C1", "net_amount": "1.00"})  # real state
        for payload in ({"customer_id": "C1"},
                        {"customer_id": "C1", "net_amount": "abc"},
                        {"customer_id": "C1", "net_amount": None},
                        {"customer_id": "C1", "net_amount": "0"},
                        {"customer_id": "C1", "net_amount": "-5.00"},
                        {"net_amount": "5.00"}):        # no customer_id
            self.assert_rejected("dividend_cash", payload)


# --------------------------------------------------------------------- #
#  dividend_reinvested                                                  #
# --------------------------------------------------------------------- #
class TestDividendReinvested(CorporateBase):
    def test_reinvest_legs_and_lot(self):
        """Net 47.50, qty 0.5 → Dr 1200 / Cr 2100 only; new lot (0.5,
        47.50); CASH NEVER INVOLVED — 1100 and 2010 exactly unchanged."""
        self.post("deposit", {"customer_id": "C1", "amount": "100.00"})
        b1100 = self.b.balances[("C1", "1100")]
        b2010 = self.b.balances[("C1", "2010")]
        legs = self.post("dividend_reinvested", {
            "customer_id": "C1", "symbol": "SYM", "net_amount": "47.50",
            "reinvest_price": "95.00", "reinvest_quantity": "0.5"}, "E-DR")
        self.assertEqual(legs, [dr("1200", "47.50"), cr("2100", "47.50")])
        self.assertEqual(self.b.balances[("C1", "1100")], b1100)
        self.assertEqual(self.b.balances[("C1", "2010")], b2010)
        self.assertEqual(len(self.b.lots), 1)
        lot = self.b.lots[1]
        self.assertEqual(lot["qty"], qnum("0.5"))
        self.assertEqual(lot["cost_total"], money("47.50"))
        self.assertEqual(lot["split_mult"], Fraction(1))
        self.assertEqual(lot["symbol"], "SYM")
        self.assertEqual(self.b.events["E-DR"]["lot_ops"], [("add", 1)])
        self.assertEqual(self.b.quarantine, [])   # 95.00 × 0.5 = net: no D7

    def test_reinvest_back_of_fifo(self):
        """L8: buy A → reinvest B → buy C; a sell spanning all three
        consumes A, B, C in global-sequence order."""
        self.buy_lot(qty="5", cost="50.00")                       # lot 1
        self.post("dividend_reinvested", {
            "customer_id": "C1", "symbol": "SYM", "net_amount": "60.00",
            "reinvest_quantity": "5"})                            # lot 2
        self.buy_lot(qty="5", cost="70.00")                       # lot 3
        self.sell("12", "120.00", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("5"), money("50.00"), Fraction(1)),
            ("consume", 2, qnum("5"), money("60.00"), Fraction(1)),
            ("consume", 3, qnum("2"), money("28.00"), Fraction(1))])

    def test_reinvest_then_partial_sell_relief(self):
        """Reinvest lot (3, 100.00), sell 1 → relieved money(100 × 1/3) =
        33.33, remainder 66.67 stays with the lot — formula, never
        per-share."""
        self.post("dividend_reinvested", {
            "customer_id": "C1", "symbol": "SYM", "net_amount": "100.00",
            "reinvest_quantity": "3"})
        self.sell("1", "40.00", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("1"), money("33.33"), Fraction(1))])
        self.assertEqual(self.b.lots[1]["qty"], qnum("2"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("66.67"))

    def test_reinvest_malformed_rejects(self):
        """reinvest_quantity ≤ 0 or net_amount ≤ 0 → Rejected: no lot, no
        legs, byte-identical book."""
        base = {"customer_id": "C1", "symbol": "SYM"}
        for payload in (dict(base, net_amount="10.00",
                             reinvest_quantity="0"),
                        dict(base, net_amount="10.00",
                             reinvest_quantity="-1"),
                        dict(base, net_amount="0",
                             reinvest_quantity="1"),
                        dict(base, net_amount="-9.99",
                             reinvest_quantity="1"),
                        dict(base, reinvest_quantity="1"),   # missing net
                        dict(base, net_amount="10.00")):     # missing qty
            self.assert_rejected("dividend_reinvested", payload)
        self.assertEqual(self.b.lots, {})


# --------------------------------------------------------------------- #
#  stock_split                                                          #
# --------------------------------------------------------------------- #
class TestStockSplit(CorporateBase):
    def split(self, r_from, r_to, *, cid="C1", symbol="SYM", eid=None):
        return self.post("stock_split", {
            "customer_id": cid, "symbol": symbol,
            "ratio_from": r_from, "ratio_to": r_to}, eid)

    def test_split_scales_qty_cost_unchanged(self):
        """Lot 10 @ 150.00, split 1→2 → qty 20, cost 150.00 UNTOUCHED,
        legs [] — only cost per share moves."""
        self.buy_lot(qty="10", cost="150.00")
        legs = self.split("1", "2", eid="E-SP")
        self.assertEqual(legs, [])
        self.assertEqual(self.b.lots[1]["qty"], qnum("20"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("150.00"))
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(2))

    def test_split_repeating_decimal_6dp(self):
        """L3: qty 1, split 3→1 → 1/3 quantized to 0.333333 (6 dp HALF_UP),
        cost unchanged, snapshot in minimal form."""
        self.buy_lot(qty="1", cost="30.00")
        self.split("3", "1")
        self.assertEqual(self.b.lots[1]["qty"], qnum("0.333333"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("30.00"))
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(1, 3))
        snap = self.b.snapshot()
        self.assertEqual(snap["customers"]["C1"]["positions"]["SYM"],
                         {"quantity": "0.333333", "cost_basis": "30.00"})

    def test_split_of_partially_consumed_lot(self):
        """L4: 10 @ 100.00, sell 4 (relieved 40.00 → 6 @ 60.00), split 1→2
        → qty 12, cost 60.00: the split scales the REMAINDER."""
        self.buy_lot(qty="10", cost="100.00")
        self.sell("4", "40.00", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("4"), money("40.00"), Fraction(1))])
        self.split("1", "2")
        self.assertEqual(self.b.lots[1]["qty"], qnum("12"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("60.00"))

    def test_split_then_sell_cost_per_share_moved(self):
        """The poison that catches per-share caching: 10 @ 100.00, split
        1→2, sell 5 → relieved money(100.00 × 5/20) = 25.00 — divide by
        the lot's CURRENT qty, never a cached per-share cost."""
        self.buy_lot(qty="10", cost="100.00")
        self.split("1", "2")
        self.sell("5", "60.00", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("5"), money("25.00"), Fraction(2))])
        self.assertEqual(self.b.lots[1]["qty"], qnum("15"))
        self.assertEqual(self.b.lots[1]["cost_total"], money("75.00"))

    def test_split_per_customer_only(self):
        """L12: CUST-1 and CUST-2 both hold SYM; a split for CUST-1 leaves
        CUST-2's lot bit-for-bit untouched."""
        self.buy_lot(cid="CUST-1", qty="10", cost="100.00")       # lot 1
        self.buy_lot(cid="CUST-2", qty="10", cost="100.00")       # lot 2
        before = repr(sorted(self.b.lots[2].items()))
        self.split("1", "2", cid="CUST-1")
        self.assertEqual(self.b.lots[1]["qty"], qnum("20"))       # acted
        self.assertEqual(repr(sorted(self.b.lots[2].items())), before)
        self.assertEqual(self.b.lots[2]["qty"], qnum("10"))
        self.assertEqual(self.b.lots[2]["split_mult"], Fraction(1))
        self.assertEqual(self.b.lot_index[("CUST-2", "SYM")], [2])

    def test_split_updates_zombie_multiplier(self):
        """A fully-consumed lot keeps qty 0 through a split but its EXACT
        multiplier still updates — Phase 5 reversal-across-split reads it."""
        self.buy_lot(qty="5", cost="50.00")
        self.sell("5", "60.00")
        self.assertEqual(self.b.lots[1]["qty"], qnum(0))          # zombie
        self.split("1", "2", eid="E-SP")
        self.assertEqual(self.b.lots[1]["qty"], qnum(0))
        self.assertEqual(self.b.lots[1]["cost_total"], ZERO)
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(2))
        self.assertEqual(self.b.events["E-SP"]["lot_ops"],
                         [("split", "C1", "SYM", [(1, qnum(0))])])
        snap = self.b.snapshot()                                  # C7
        self.assertEqual(snap["customers"]["C1"]["positions"], {})

    def test_split_zero_position_noop(self):
        """No lots at all for (cid, symbol) → a VALID no-op: posts with
        legs [], zero state change, no exception escapes apply."""
        before = (repr(self.b.lots), repr(self.b.lot_index),
                  repr(self.b.balances))
        legs = self.split("1", "2", eid="E-NOP")
        self.assertEqual(legs, [])
        self.assertEqual((repr(self.b.lots), repr(self.b.lot_index),
                          repr(self.b.balances)), before)
        self.assertIn("E-NOP", self.b.events)         # posted, NOT rejected
        self.assertEqual(self.b.events["E-NOP"]["lot_ops"],
                         [("split", "C1", "SYM", [])])
        self.assertNotIn("C1", self.b.snapshot()["customers"])  # no phantom

    def test_split_malformed_rejects(self):
        """S9/S10: zero / negative / missing / non-numeric ratios →
        Rejected, zero mutation."""
        self.buy_lot(qty="10", cost="100.00")
        base = {"customer_id": "C1", "symbol": "SYM"}
        for payload in (dict(base, ratio_from="0", ratio_to="2"),
                        dict(base, ratio_from="-1", ratio_to="2"),
                        dict(base, ratio_from="1", ratio_to="0"),
                        dict(base, ratio_from="1", ratio_to="-2"),
                        dict(base, ratio_to="2"),           # missing from
                        dict(base, ratio_from="1"),         # missing to
                        dict(base, ratio_from="x", ratio_to="2"),
                        dict(base, ratio_from="1")):        # repeat guard
            self.assert_rejected("stock_split", payload)
        self.assertEqual(self.b.lots[1]["qty"], qnum("10"))
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(1))


# --------------------------------------------------------------------- #
#  symbol_change                                                        #
# --------------------------------------------------------------------- #
class TestSymbolChange(CorporateBase):
    def rename(self, old, new, *, cid="C1", eid=None):
        return self.post("symbol_change", {
            "customer_id": cid, "old_symbol": old, "new_symbol": new}, eid)

    def test_symbol_change_rekey(self):
        """Lots move OLD→NEW keeping seq and multiplier; the snapshot shows
        NEW with identical qty and cost_basis and no OLD at all."""
        self.buy_lot(symbol="OLD", qty="4", cost="40.00")
        self.buy_lot(symbol="OLD", qty="6", cost="66.00")
        legs = self.rename("OLD", "NEW", eid="E-RN")
        self.assertEqual(legs, [])
        self.assertEqual(self.b.lot_index[("C1", "NEW")], [1, 2])
        self.assertNotIn(("C1", "OLD"), self.b.lot_index)
        for lot_id, seq in ((1, 1), (2, 2)):
            self.assertEqual(self.b.lots[lot_id]["symbol"], "NEW")
            self.assertEqual(self.b.lots[lot_id]["seq"], seq)   # untouched
            self.assertEqual(self.b.lots[lot_id]["split_mult"], Fraction(1))
        pos = self.b.snapshot()["customers"]["C1"]["positions"]
        self.assertEqual(pos, {"NEW": {"quantity": "10",
                                       "cost_basis": "106.00"}})

    def _merge_fixture(self):
        """Global seqs 3 and 7 under OLD, seq 5 under NEW — distinct costs
        so the relief amounts identify the consumption order."""
        self.buy_lot(cid="C9", symbol="ZZZ", qty="1", cost="1.00")  # seq 1
        self.buy_lot(cid="C9", symbol="ZZZ", qty="1", cost="1.00")  # seq 2
        self.buy_lot(symbol="OLD", qty="2", cost="20.00")           # seq 3
        self.buy_lot(cid="C9", symbol="ZZZ", qty="1", cost="1.00")  # seq 4
        self.buy_lot(symbol="NEW", qty="2", cost="30.00")           # seq 5
        self.buy_lot(cid="C9", symbol="ZZZ", qty="1", cost="1.00")  # seq 6
        self.buy_lot(symbol="OLD", qty="2", cost="40.00")           # seq 7
        for lot_id, sym in ((3, "OLD"), (5, "NEW"), (7, "OLD")):
            self.assertEqual(self.b.lots[lot_id]["seq"], lot_id)
            self.assertEqual(self.b.lots[lot_id]["symbol"], sym)

    def test_symbol_change_merge_interleaves_by_sequence(self):
        """A6 poison: OLD lots seq 3, 7 merge into NEW holding seq 5 →
        FIFO consumes 3, 5, 7 (global sequence). Under
        SYMBOL_MERGE="existing_first" the same fixture consumes 5, 3, 7 —
        the flag is a real switch."""
        self._merge_fixture()
        self.rename("OLD", "NEW")
        self.sell("5", "500.00", symbol="NEW", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 3, qnum("2"), money("20.00"), Fraction(1)),
            ("consume", 5, qnum("2"), money("30.00"), Fraction(1)),
            ("consume", 7, qnum("1"), money("20.00"), Fraction(1))])
        # -- same fixture, existing_first: moved lots queue BEHIND ---------
        self.b = Book()
        self._merge_fixture()
        book_mod.SYMBOL_MERGE = "existing_first"
        try:
            self.rename("OLD", "NEW")
            self.sell("5", "500.00", symbol="NEW", eid="E-S2")
            self.assertEqual(self.b.events["E-S2"]["lot_ops"], [
                ("consume", 5, qnum("2"), money("30.00"), Fraction(1)),
                ("consume", 3, qnum("2"), money("20.00"), Fraction(1)),
                ("consume", 7, qnum("1"), money("20.00"), Fraction(1))])
        finally:
            book_mod.SYMBOL_MERGE = "sequence"

    def test_symbol_change_chain_a_b_c(self):
        """Chain poison: rename A→B, split under B, rename B→C — the lot
        lives under C with the split applied exactly once, FIFO and cost
        intact; a sell under C relieves by current qty."""
        self.buy_lot(symbol="A", qty="10", cost="100.00")
        self.rename("A", "B")
        self.post("stock_split", {"customer_id": "C1", "symbol": "B",
                                  "ratio_from": "1", "ratio_to": "2"})
        self.rename("B", "C")
        lot = self.b.lots[1]
        self.assertEqual((lot["symbol"], lot["seq"]), ("C", 1))
        self.assertEqual(lot["qty"], qnum("20"))            # split ONCE
        self.assertEqual(lot["cost_total"], money("100.00"))
        self.assertEqual(lot["split_mult"], Fraction(2))
        for sym in ("A", "B"):
            self.assertNotIn(("C1", sym), self.b.lot_index)
        self.sell("5", "60.00", symbol="C", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("5"), money("25.00"), Fraction(2))])
        pos = self.b.snapshot()["customers"]["C1"]["positions"]
        self.assertEqual(pos, {"C": {"quantity": "15",
                                     "cost_basis": "75.00"}})

    def test_rename_then_trade_new_symbol(self):
        """Rename OLD→NEW then buy NEW: the fresh lot sequences AFTER the
        moved one; a spanning sell consumes moved-then-new."""
        self.buy_lot(symbol="OLD", qty="5", cost="50.00")   # lot 1
        self.rename("OLD", "NEW")
        self.buy_lot(symbol="NEW", qty="5", cost="70.00")   # lot 2
        self.assertGreater(self.b.lots[2]["seq"], self.b.lots[1]["seq"])
        self.assertEqual(self.b.lot_index[("C1", "NEW")], [1, 2])
        self.sell("8", "160.00", symbol="NEW", eid="E-S")
        self.assertEqual(self.b.events["E-S"]["lot_ops"], [
            ("consume", 1, qnum("5"), money("50.00"), Fraction(1)),
            ("consume", 2, qnum("3"), money("42.00"), Fraction(1))])

    def test_symbol_change_per_customer_only(self):
        """L12: renaming OLD for C1 leaves every other holder of OLD
        exactly where they were."""
        self.buy_lot(cid="C1", symbol="OLD", qty="5", cost="50.00")   # lot 1
        self.buy_lot(cid="C2", symbol="OLD", qty="5", cost="55.00")   # lot 2
        before = repr(sorted(self.b.lots[2].items()))
        self.rename("OLD", "NEW", cid="C1")
        self.assertEqual(self.b.lots[1]["symbol"], "NEW")
        self.assertEqual(repr(sorted(self.b.lots[2].items())), before)
        self.assertEqual(self.b.lots[2]["symbol"], "OLD")
        self.assertEqual(self.b.lot_index[("C2", "OLD")], [2])
        self.assertNotIn(("C2", "NEW"), self.b.lot_index)
        pos = self.b.snapshot()["customers"]["C2"]["positions"]
        self.assertEqual(pos, {"OLD": {"quantity": "5",
                                       "cost_basis": "55.00"}})

    def test_symbol_change_zero_position_noop(self):
        """No lots under OLD → valid no-op: legs [], no state change, no
        phantom (cid, NEW) index entry."""
        before = (repr(self.b.lots), repr(self.b.lot_index),
                  repr(self.b.balances))
        legs = self.rename("OLD", "NEW", eid="E-NOP")
        self.assertEqual(legs, [])
        self.assertEqual((repr(self.b.lots), repr(self.b.lot_index),
                          repr(self.b.balances)), before)
        self.assertIn("E-NOP", self.b.events)         # posted, NOT rejected
        self.assertEqual(self.b.events["E-NOP"]["lot_ops"],
                         [("rekey", "C1", "OLD", "NEW", [])])
        self.assertNotIn("C1", self.b.snapshot()["customers"])


# --------------------------------------------------------------------- #
#  the R13 contract + idempotency                                       #
# --------------------------------------------------------------------- #
class TestEventRecordsAndIdempotency(CorporateBase):
    def test_lot_ops_recorded_for_all_four(self):
        """R13 readiness: every corporate event's store entry carries its
        exact legs plus lot_ops sufficient to undo — add by lot_id, split
        with (lot_id, prior_qty), rekey with moved ids."""
        self.buy_lot(qty="10", cost="100.00")                     # lot 1
        legs_dc = self.post("dividend_cash", {
            "customer_id": "C1", "net_amount": "5.00"}, "E-DC")
        legs_dr = self.post("dividend_reinvested", {
            "customer_id": "C1", "symbol": "SYM", "net_amount": "10.00",
            "reinvest_quantity": "1"}, "E-DR")                    # lot 2
        legs_sp = self.post("stock_split", {
            "customer_id": "C1", "symbol": "SYM",
            "ratio_from": "1", "ratio_to": "2"}, "E-SP")
        legs_rn = self.post("symbol_change", {
            "customer_id": "C1", "old_symbol": "SYM",
            "new_symbol": "NEWS"}, "E-RN")
        for eid, legs in (("E-DC", legs_dc), ("E-DR", legs_dr),
                          ("E-SP", legs_sp), ("E-RN", legs_rn)):
            rec = self.b.events[eid]
            self.assertEqual(rec["legs"], legs)
            self.assertIn("lot_ops", rec)
            self.assertFalse(rec["reversed"])
        self.assertEqual(self.b.events["E-DC"]["lot_ops"], [])
        self.assertEqual(self.b.events["E-DR"]["lot_ops"], [("add", 2)])
        self.assertEqual(self.b.events["E-SP"]["lot_ops"],
                         [("split", "C1", "SYM",
                           [(1, qnum("10")), (2, qnum("1"))])])  # PRIOR qtys
        self.assertEqual(self.b.events["E-RN"]["lot_ops"],
                         [("rekey", "C1", "SYM", "NEWS", [1, 2])])

    def test_corporate_duplicates_idempotent(self):
        """Each of the four types redelivered under its first event_id is a
        no-op; replaying the whole log twice yields byte-identical
        snapshots."""
        self.buy_lot(qty="10", cost="100.00")
        dupes = [
            ev("dividend_cash",
               {"customer_id": "C1", "net_amount": "5.00"}, "E-DC"),
            ev("dividend_reinvested",
               {"customer_id": "C1", "symbol": "SYM", "net_amount": "10.00",
                "reinvest_quantity": "1"}, "E-DR"),
            ev("stock_split",
               {"customer_id": "C1", "symbol": "SYM",
                "ratio_from": "1", "ratio_to": "2"}, "E-SP"),
            ev("symbol_change",
               {"customer_id": "C1", "old_symbol": "SYM",
                "new_symbol": "NEWS"}, "E-RN"),
        ]
        for e in dupes:
            self.b.apply(e)
        before = fingerprint(self.b)
        for e in dupes:                       # exact redelivery → no-op
            self.assertEqual(self.b.apply(e), [])
        self.assertEqual(fingerprint(self.b), before)
        self.assertEqual(len(self.b.lots), 2)         # applied once each
        self.assertEqual(self.b.lots[1]["qty"], qnum("20"))   # not ×4
        self.assertEqual(self.b.lots[1]["split_mult"], Fraction(2))
        self.assertEqual(self.b.lot_index[("C1", "NEWS")], [1, 2])
        # replay-twice ⇒ byte-identical snapshots
        log = list(self.b.event_log)
        b2 = Book()
        for e in log:
            b2.apply(e)
        snap_once = json.dumps(b2.snapshot(), sort_keys=True)
        for e in log:
            b2.apply(e)
        snap_twice = json.dumps(b2.snapshot(), sort_keys=True)
        self.assertEqual(snap_once, snap_twice)
        self.assertEqual(snap_once,
                         json.dumps(self.b.snapshot(), sort_keys=True))


if __name__ == "__main__":
    unittest.main()
