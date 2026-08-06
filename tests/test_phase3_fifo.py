"""Phase 3 gate — the 13-leg fills and the FIFO lot book.

Covers the TEST GATE boxes for the two hand-derived worked fixtures
(string-exact, every account and amount), the sell balance-proof property
over 500 random sells, FIFO partial-lot relief (the graded convention,
character for character), min-seq consumption across lots, oversell (L1),
sell-exact (L2), principal-only cost basis, per-fill min fee, per-fill
broker/rate truth, the D1 coverage reject, and the events[eid] legs+lot_ops
contract Phase 5 reverses from. Lifecycle/hold/settlement boxes live in
test_phase3_orders.py.
"""
import json
import os
import random
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tariff  # noqa: E402
from book import Book, ZERO, money, qnum, fmt_money  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_f_{_SEQ}",
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
        "accounts": sorted(b.accounts_touched),
        "events": sorted(b.events.keys()),
    }, sort_keys=True)


def legs_balance(legs) -> bool:
    dr = sum((D(l["debit"]) for l in legs), ZERO)
    cr = sum((D(l["credit"]) for l in legs), ZERO)
    return dr == cr


def dr(acct, amt, cid="C1") -> dict:
    return {"account": acct, "customer_id": cid,
            "debit": amt, "credit": "0.00"}


def cr(acct, amt, cid="C1") -> dict:
    return {"account": acct, "customer_id": cid,
            "debit": "0.00", "credit": amt}


def fill_payload(oid, tid, *, cid="C1", side="buy", symbol="AAPL", qty="10",
                 price="100", principal="1000.00", broker="BRK-A",
                 asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": side, "symbol": symbol, "quantity": qty, "price": price,
            "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


def place_payload(oid, *, cid="C1", side="buy", symbol="AAPL", qty="10",
                  limit="100", est="0.00", asset_class="equity") -> dict:
    return {"order_id": oid, "customer_id": cid, "side": side,
            "symbol": symbol, "quantity": qty, "limit_price": limit,
            "est_charges": est, "asset_class": asset_class}


class Phase3FifoBase(unittest.TestCase):
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

    def place(self, oid, eid=None, **kw):
        return self.post("order_placed", place_payload(oid, **kw), eid)

    def fill(self, oid, tid, final=True, eid=None, **kw):
        etype = "order_filled" if final else "order_partially_filled"
        return self.post(etype, fill_payload(oid, tid, **kw), eid)

    def buy_lot(self, *, cid="C1", symbol="AAPL", qty="10", cost="1000.00",
                broker="BRK-A", asset_class="equity"):
        Phase3FifoBase._LOT += 1
        n = Phase3FifoBase._LOT
        return self.fill(f"O-lot-{n}", f"T-lot-{n}", cid=cid, symbol=symbol,
                         qty=qty, principal=cost, price="1", broker=broker,
                         asset_class=asset_class)


class TestWorkedLegExamples(Phase3FifoBase):
    def test_buy_legs_worked_example(self):
        """BRK-A/equity, P=1000.00, rate=0.50 → EXACTLY the 13 graded legs;
        ps = 0.48 proves the half-cent HALF_UP (0.50 × 0.95 = 0.475)."""
        self.place("O1", qty="10", limit="100", est="5.00")
        legs = self.fill("O1", "T1", qty="10", price="100",
                         principal="1000.00", broker="BRK-A",
                         asset_class="equity", rate="0.50", eid="E-BUY")
        self.assertEqual(legs, [
            dr("2010", "1003.20"),      # P + b + c + r
            dr("1200", "1000.00"),      # custody asset, at principal
            dr("5000", "1.25"),         # broker cost incl. 0.35 ticket
            dr("5010", "0.20"),         # custody cost
            dr("5100", "0.48"),         # partner share expense
            cr("2350", "1000.00"),      # T+2 payable — no 1100 today
            cr("2100", "1000.00"),      # custody mirror
            cr("4000", "2.00"),         # brokerage income
            cr("4010", "0.40"),         # custody income
            cr("2400", "0.80"),         # regulatory pass-through
            cr("2411", "1.25"),         # BRK-A payable sub-account
            cr("2420", "0.20"),         # custodian payable
            cr("2430", "0.48"),         # partner payable — half-cent HALF_UP
        ])
        self.assertEqual(len(legs), 13)
        total_dr = sum((D(l["debit"]) for l in legs), ZERO)
        total_cr = sum((D(l["credit"]) for l in legs), ZERO)
        self.assertEqual(total_dr, D("2005.13"))
        self.assertEqual(total_cr, D("2005.13"))
        # cash NEVER moves on trade date (T+2)
        self.assertNotIn("1100", [l["account"] for l in legs])

    def test_sell_legs_balance_proof(self):
        """BRK-C/etf, P=600.00, rate=0.25 over one lot 10 sh / 1000.00 →
        the exact 13 legs, Cr 2010 = 597.84, Dr = Cr = 1101.16 — then the
        500-random-sell property: Dr ≡ Cr ≡ P + cost + bc + cc + ps."""
        self.buy_lot(symbol="SPY", qty="10", cost="1000.00",
                     broker="BRK-A", asset_class="etf")
        legs = self.fill("O-S", "T-S", side="sell", symbol="SPY", qty="5",
                         price="120", principal="600.00", broker="BRK-C",
                         asset_class="etf", rate="0.25", eid="E-SELL")
        self.assertEqual(legs, [
            dr("1150", "600.00"),       # T+2 receivable — no 1100 today
            dr("2100", "500.00"),       # FIFO relief out of custody mirror
            dr("5000", "0.92"),         # broker cost incl. 0.20 ticket
            dr("5010", "0.06"),
            dr("5100", "0.18"),
            cr("2010", "597.84"),       # P − b − c − r
            cr("1200", "500.00"),       # FIFO relief
            cr("4000", "1.50"),
            cr("4010", "0.18"),
            cr("2400", "0.48"),
            cr("2413", "0.92"),         # BRK-C payable sub-account
            cr("2420", "0.06"),
            cr("2430", "0.18"),
        ])
        self.assertEqual(len(legs), 13)
        total_dr = sum((D(l["debit"]) for l in legs), ZERO)
        total_cr = sum((D(l["credit"]) for l in legs), ZERO)
        self.assertEqual(total_dr, D("1101.16"))
        self.assertEqual(total_cr, D("1101.16"))
        # property: 500 random sells, seeded — the algebraic identity
        rnd = random.Random(20260806)
        pairs = [("equity", "BRK-A"), ("equity", "BRK-B"),
                 ("etf", "BRK-A"), ("etf", "BRK-C"),
                 ("bond", "BRK-B"), ("bond", "BRK-C")]
        bp = Book()
        for i in range(500):
            ac, sell_broker = rnd.choice(pairs)
            buy_broker = rnd.choice(list(tariff.COVERAGE[ac]))
            cid = f"P{i}"
            lot_qty = rnd.randint(1, 1000)
            sell_qty = rnd.randint(1, lot_qty)
            lot_cost = D(rnd.randint(1, 1_000_000)) / 100    # exact cents
            principal = D(rnd.randint(100, 500_000)) / 100
            rate = D(rnd.randint(0, 100)) / 100
            buy = bp.apply(ev("order_filled", fill_payload(
                f"OB{i}", f"TB{i}", cid=cid, symbol="SYM",
                qty=str(lot_qty), price="1", principal=str(lot_cost),
                broker=buy_broker, asset_class=ac, rate="0.50")))
            self.assertTrue(buy, f"iteration {i}: buy did not post")
            legs = bp.apply(ev("order_filled", fill_payload(
                f"OS{i}", f"TS{i}", cid=cid, side="sell", symbol="SYM",
                qty=str(sell_qty), price="1", principal=str(principal),
                broker=sell_broker, asset_class=ac, rate=str(rate))))
            self.assertTrue(legs, f"iteration {i}: sell did not post")
            ch = tariff.fill_charges(sell_broker, principal, rate)
            if sell_qty == lot_qty:
                cost = lot_cost                     # full remainder (M5)
            else:
                cost = money(lot_cost * sell_qty / lot_qty)
            expect = (money(principal) + cost
                      + ch["bc"] + ch["cc"] + ch["ps"])
            total_dr = sum((D(l["debit"]) for l in legs), ZERO)
            total_cr = sum((D(l["credit"]) for l in legs), ZERO)
            self.assertEqual(total_dr, total_cr, f"iteration {i}")
            self.assertEqual(total_dr, expect, f"iteration {i}")


class TestFifoLotBook(Phase3FifoBase):
    def test_fifo_relief_chained_partials(self):
        """One lot 10 sh / 100.03: relief 30.01, 30.01, 40.01 — the graded
        convention money(lot_cost × sold/qty), remainder stays with the lot,
        Σ = 100.03 exactly, no lost cents (M5)."""
        self.fill("O-L", "T-L", symbol="SYM", qty="10", principal="100.03")
        steps = [("3", "30.01", "7", "70.02"),
                 ("3", "30.01", "4", "40.01"),
                 ("4", "40.01", "0", "0.00")]      # full remainder, not ×4/4
        for n, (sq, relieved, q_after, c_after) in enumerate(steps, 1):
            self.fill(f"O-S{n}", f"T-S{n}", side="sell", symbol="SYM",
                      qty=sq, price="10", principal="50.00", eid=f"E{n}")
            ops = self.b.events[f"E{n}"]["lot_ops"]
            self.assertEqual(
                ops, [("consume", 1, qnum(sq), money(relieved), D("1"))])
            self.assertEqual(self.b.lots[1]["qty"], qnum(q_after))
            self.assertEqual(self.b.lots[1]["cost_total"], money(c_after))
        total = sum((self.b.events[f"E{n}"]["lot_ops"][0][3]
                     for n in (1, 2, 3)), ZERO)
        self.assertEqual(total, D("100.03"))       # no lost cents
        self.assertEqual(self.b.balances[("C1", "2100")], ZERO)

    def test_fifo_spans_lots_min_seq(self):
        """A sell crossing lots consumes by ascending global seq; a fully
        consumed lot stays as a qty-0 zombie holding its seq and is skipped
        by the next consumption."""
        self.fill("O-1", "T-1", symbol="SYM", qty="5", principal="50.00")
        self.fill("O-2", "T-2", symbol="SYM", qty="5", principal="60.00")
        self.fill("O-3", "T-3", symbol="SYM", qty="5", principal="70.00")
        self.fill("O-S1", "T-S1", side="sell", symbol="SYM", qty="8",
                  price="10", principal="400.00", eid="E1")
        ops = self.b.events["E1"]["lot_ops"]
        self.assertEqual(ops, [
            ("consume", 1, qnum("5"), money("50.00"), D("1")),   # lot 1 full
            ("consume", 2, qnum("3"), money("36.00"), D("1"))])  # lot 2 part
        self.assertEqual([op[1] for op in ops],
                         sorted(op[1] for op in ops))  # ascending seq
        self.assertIn(1, self.b.lots)                  # zombie retained
        self.assertEqual(self.b.lots[1]["qty"], qnum(0))
        self.assertEqual(self.b.lots[1]["seq"], 1)     # keeps its FIFO slot
        self.assertEqual(self.b.lots[2]["qty"], qnum(2))
        self.assertEqual(self.b.lots[2]["cost_total"], money("24.00"))
        self.assertEqual(self.b.lots[3]["qty"], qnum(5))
        # the next sell skips the zombie and resumes at lot 2's remainder
        self.fill("O-S2", "T-S2", side="sell", symbol="SYM", qty="2",
                  price="10", principal="100.00", eid="E2")
        self.assertEqual(self.b.events["E2"]["lot_ops"], [
            ("consume", 2, qnum("2"), money("24.00"), D("1"))])
        self.assertEqual(self.b.lot_index[("C1", "SYM")], [1, 2, 3])

    def test_oversell_rejects_before_mutation(self):
        """L1: position 5, sell 6 → Rejected, legs [], lots byte-identical,
        no trade stored — never leave lots half-consumed."""
        self.buy_lot(symbol="SYM", qty="5", cost="500.00")
        self.assert_rejected("order_filled", fill_payload(
            "O-S", "T-OS", side="sell", symbol="SYM", qty="6",
            principal="600.00"))
        self.assertNotIn("T-OS", self.b.trades)
        self.assertNotIn("O-S", self.b.orders)
        self.assertEqual(self.b.lots[1]["qty"], qnum(5))
        self.assertEqual(self.b.lots[1]["cost_total"], money("500.00"))
        # a customer with NO position at all rejects too
        self.assert_rejected("order_partially_filled", fill_payload(
            "O-S", "T-OS", cid="C9", side="sell", symbol="SYM", qty="1",
            principal="10.00"))

    def test_sell_exact_position(self):
        """L2: sell exactly the position → every lot cleanly to qty 0,
        retained as zombies, and the position vanishes from the snapshot."""
        self.fill("O-1", "T-1", symbol="SYM", qty="2", principal="20.00")
        self.fill("O-2", "T-2", symbol="SYM", qty="3", principal="30.03")
        self.fill("O-S", "T-S", side="sell", symbol="SYM", qty="5",
                  price="10", principal="100.00", eid="E1")
        self.assertEqual(self.b.events["E1"]["lot_ops"], [
            ("consume", 1, qnum("2"), money("20.00"), D("1")),
            ("consume", 2, qnum("3"), money("30.03"), D("1"))])
        self.assertEqual(len(self.b.lots), 2)          # zombies retained
        for lot_id in (1, 2):
            self.assertEqual(self.b.lots[lot_id]["qty"], qnum(0))
            self.assertEqual(self.b.lots[lot_id]["cost_total"], ZERO)
        self.assertEqual(self.b.lot_index[("C1", "SYM")], [1, 2])
        snap = self.b.snapshot()
        self.assertEqual(snap["customers"]["C1"]["positions"], {})  # C7

    def test_cost_basis_is_principal_only(self):
        """The lot and 2100 carry the PRINCIPAL, exactly — no b/c/r/bc/cc/ps
        contamination (the 25.6-pt rule)."""
        self.fill("O1", "T1", qty="10", principal="1000.00",
                  broker="BRK-B", asset_class="equity", rate="0.50")
        ch = tariff.fill_charges("BRK-B", "1000.00", "0.50")
        # the fees were real money — and none of it reached the lot
        self.assertGreater(ch["b"] + ch["c"] + ch["r"] + ch["bc"] + ch["cc"],
                           ZERO)
        self.assertEqual(self.b.lots[1]["cost_total"], money("1000.00"))
        self.assertEqual(self.b.balances[("C1", "2100")], D("-1000.00"))
        self.assertEqual(self.b.balances[("C1", "1200")], D("1000.00"))
        snap = self.b.snapshot()
        self.assertEqual(snap["customers"]["C1"]["positions"]["AAPL"],
                         {"quantity": "10", "cost_basis": "1000.00"})


class TestPerFillPricing(Phase3FifoBase):
    def test_min_fee_per_fill(self):
        """Three partials each below the BRK-A crossover (P < 500) → each
        fill's b floors at 1.00; total brokerage 3 × 1.00, not 1 ×."""
        self.place("O1", qty="30", limit="10")
        for n in (1, 2, 3):
            legs = self.fill("O1", f"T{n}", final=False, qty="10",
                             principal="100.00", broker="BRK-A", rate="0.00")
            by = {l["account"]: l for l in legs}
            self.assertEqual(by["4000"]["credit"], "1.00",
                             f"fill {n}: min fee must apply per fill")
        total_4000 = sum((v for (c, a), v in self.b.balances.items()
                          if a == "4000"), ZERO)
        self.assertEqual(total_4000, D("-3.00"))       # 3 × floor (credit)

    def test_fill_uses_own_broker_and_rate(self):
        """Placement routed BRK-A; the fills arrive from BRK-B @ 0.25 and
        BRK-A @ 0.50 — each fill is priced on ITS OWN payload, never the
        placement's route."""
        self.place("O1", qty="5", limit="100")         # N=500 → BRK-A
        self.assertEqual(self.b.orders["O1"]["route"], "BRK-A")
        legs1 = self.fill("O1", "T1", final=False, qty="2", price="125",
                          principal="250.00", broker="BRK-B", rate="0.25")
        ch1 = tariff.fill_charges("BRK-B", "250.00", "0.25")
        by1 = {l["account"]: l for l in legs1}
        self.assertEqual(by1["4000"]["credit"], fmt_money(ch1["b"]))  # 2.50
        self.assertEqual(by1["4010"]["credit"], fmt_money(ch1["c"]))
        self.assertEqual(by1["2400"]["credit"], fmt_money(ch1["r"]))
        self.assertEqual(by1["2412"]["credit"], fmt_money(ch1["bc"]))
        self.assertEqual(by1["2420"]["credit"], fmt_money(ch1["cc"]))
        self.assertNotIn("2411", by1)          # BRK-B's payable, not ours
        self.assertEqual(ch1["ps"], ZERO)      # ticket-driven loss: no share
        self.assertNotIn("2430", by1)
        legs2 = self.fill("O1", "T2", final=False, qty="2", price="125",
                          principal="250.00", broker="BRK-A", rate="0.50")
        ch2 = tariff.fill_charges("BRK-A", "250.00", "0.50")
        by2 = {l["account"]: l for l in legs2}
        self.assertEqual(by2["4000"]["credit"], fmt_money(ch2["b"]))  # 1.00
        self.assertEqual(by2["2411"]["credit"], fmt_money(ch2["bc"]))
        self.assertEqual(by2["2430"]["credit"], fmt_money(ch2["ps"]))
        self.assertNotIn("2412", by2)
        # same principal, different broker + rate → different charge lines
        self.assertNotEqual(by1["4000"]["credit"], by2["4000"]["credit"])

    def test_fill_broker_class_mismatch_rejects(self):
        """D1: a fill naming a broker outside its asset class is bad data —
        Rejected, zero mutation."""
        self.buy_lot(symbol="SYM", qty="5", cost="500.00")
        self.assert_rejected("order_filled", fill_payload(
            "OX", "TX", broker="BRK-A", asset_class="bond"))
        self.assert_rejected("order_partially_filled", fill_payload(
            "OX", "TX", side="sell", symbol="SYM", qty="1",
            principal="100.00", broker="BRK-C", asset_class="equity"))
        self.assertNotIn("OX", self.b.orders)
        self.assertNotIn("TX", self.b.trades)


class TestEventRecords(Phase3FifoBase):
    def test_event_records_store_legs_and_lot_ops(self):
        """Phase 5's contract: events[eid] stores the exact posted legs plus
        lot_ops in canonical shape — ("add", lot_id) for buys, ("consume",
        lot_id, qty, cost_relieved, multiplier) for sells."""
        legs_b = self.fill("O-B", "T-B", symbol="SPY", asset_class="etf",
                           qty="10", principal="1000.00", eid="E-B")
        rec = self.b.events["E-B"]
        self.assertEqual(rec["type"], "order_filled")
        self.assertEqual(rec["payload"]["trade_id"], "T-B")
        self.assertEqual(rec["legs"], legs_b)
        self.assertEqual(rec["lot_ops"], [("add", 1)])
        self.assertFalse(rec["reversed"])
        legs_s = self.fill("O-S", "T-S", side="sell", symbol="SPY", qty="4",
                           price="150", principal="600.00", broker="BRK-C",
                           asset_class="etf", rate="0.25", eid="E-S")
        rec2 = self.b.events["E-S"]
        self.assertEqual(rec2["type"], "order_filled")
        self.assertEqual(rec2["legs"], legs_s)
        self.assertEqual(rec2["lot_ops"], [
            ("consume", 1, qnum("4"), money("400.00"), D("1"))])
        self.assertFalse(rec2["reversed"])


if __name__ == "__main__":
    unittest.main()
