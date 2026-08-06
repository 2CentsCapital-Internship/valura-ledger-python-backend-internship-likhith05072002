"""Phase 6 gate — checkpoints, as-of answers, routing report.

Covers every unit box of the Phase 6 TEST GATE:

  * trial balance: seeded from `accounts_touched`, so accounts netted back
    to zero still report "0.00" (C5); debit-positive, sorted, sums to zero.
  * the customer universe: `customers_seen` is its own store, so a
    recipient-only (R18) and a hold-only (C6) customer both appear; cash
    holds count BUY holds only (a sell hold is shares); zero-quantity
    positions are omitted (C7).
  * `open_order_routes`: open-only (C8), never-placed stubs excluded (A10),
    and the route is the one computed AT PLACEMENT on the original
    quantity x limit_price, even when the remaining-qty notional would flip
    the winner across a min-fee crossover (A14).
  * serialization canon: fmt_qty minimal form, no scientific notation
    anywhere in a real snapshot.
  * snapshot() can never raise (client patch 2 contract) — poisoned log,
    poisoned lot, poisoned formatter all degrade + quarantine.
  * as-of: basic prefix (C1), rejected + duplicated targets (C2), backdated
    exclusion (C4), routes under as-of, unknown id (A9), and the ring
    aliasing audit (answer, mutate the live book hard, re-answer).

House style follows tests/test_reversal.py: module-level `ev` builder with
a global sequence, D = Decimal, snapshots compared as sorted-key JSON bytes.
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import book as book_mod  # noqa: E402  (fmt_qty monkeypatching, RING_INTERVAL)
import tariff  # noqa: E402  (routing crossovers asserted directly)
from book import Book, ZERO, fmt_money, fmt_qty, money, qnum  # noqa: E402

D = Decimal

MONEY_RE = re.compile(r"^-?\d+\.\d\d$")

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_cp_{_SEQ}",
            "type": etype, "payload": payload}


def snap_bytes(x) -> str:
    """A snapshot (or a Book's live snapshot) as one comparable string."""
    if isinstance(x, Book):
        x = x._snapshot_now()
    return json.dumps(x, sort_keys=True)


def dep(cid: str, amount, **extra) -> dict:
    p = {"customer_id": cid, "amount": amount}
    p.update(extra)
    return p


def place(oid, *, cid="C1", side="buy", symbol="SYM", qty="10",
          limit="100", est="5.00", asset_class="equity") -> dict:
    return {"order_id": oid, "customer_id": cid, "side": side,
            "symbol": symbol, "quantity": qty, "limit_price": limit,
            "est_charges": est, "asset_class": asset_class}


def fill_payload(oid, tid, *, cid="C1", side="buy", symbol="SYM", qty="10",
                 price="100", principal="1000.00", broker="BRK-A",
                 asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": side, "symbol": symbol, "quantity": qty, "price": price,
            "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


def walk_strings(obj):
    """Every string that reaches the wire in a snapshot, keys included."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v)
    elif isinstance(obj, str):
        yield obj


class CheckpointBase(unittest.TestCase):
    def setUp(self):
        self.b = Book()

    def post(self, etype, payload, eid=None):
        return self.b.apply(ev(etype, payload, eid))

    def snap(self, as_of=None) -> dict:
        return self.b.snapshot(as_of)

    def sbytes(self, as_of=None) -> str:
        return snap_bytes(self.b.snapshot(as_of))

    def cold(self, upto: int) -> str:
        """A cold replay of the log prefix into a fresh Book — the
        independent witness the ring answer must match."""
        fresh = Book()
        for e in self.b.event_log[:upto]:
            fresh._apply_core(e)
        return snap_bytes(fresh)


# ---------------------------------------------------------------- #
#  trial balance                                                    #
# ---------------------------------------------------------------- #
class TrialBalanceTest(CheckpointBase):
    def test_tb_includes_netted_to_zero(self):
        """C5: deposit 100 then a 100 fee nets 1100 and 2010 to zero — both
        must still be reported, at "0.00"; a trial balance built from
        NONZERO balances drops them.

        The second half is the harder one and the reason `accounts_touched`
        exists at all: an interest credit whose customer share IS the gross
        (R16) touches 4200 with a leg that is omitted from the wire (A3),
        so 4200 has no balance key anywhere — only the seed can put it in
        the report (decision 8: "account ever touched" is tracked
        separately from "nonzero leg posted"). The matching fee returns
        1100/2010 to zero so both halves are visible at once."""
        self.post("deposit", dep("C1", "100.00"))
        self.post("fee_charged", {"customer_id": "C1", "amount": "100.00"})
        self.post("interest_credited", {"customer_id": "C2",
                                        "gross_amount": "50.00",
                                        "customer_share": "50.00"})
        self.post("fee_charged", {"customer_id": "C2", "amount": "50.00"})
        tb = self.snap()["trial_balance"]

        # -- netted back to zero, balance key present
        self.assertEqual(self.b.balances[("C1", "1100")], ZERO)
        self.assertEqual(self.b.balances[("C1", "2010")], ZERO)
        self.assertIn("1100", tb)
        self.assertIn("2010", tb)
        self.assertEqual(tb["1100"], "0.00")
        self.assertEqual(tb["2010"], "0.00")
        # -- touched by an omitted zero leg: NO balance key exists
        self.assertFalse([k for k in self.b.balances if k[1] == "4200"])
        self.assertIn("4200", self.b.accounts_touched)
        self.assertIn("4200", tb)
        self.assertEqual(tb["4200"], "0.00")
        # Not "0E-2", not "-0.00", not "0" — the canon string.
        for v in tb.values():
            self.assertRegex(v, MONEY_RE)

    def test_tb_debit_positive_sorted(self):
        """Assets debit-positive, liabilities negative, keys sorted, and the
        whole column sums to exactly zero (double entry, per account)."""
        self.post("deposit", dep("C1", "5000.00"))
        self.post("deposit", dep("C2", "2500.00"))
        self.post("order_placed", place("ORD-1"))
        self.post("order_partially_filled",
                  fill_payload("ORD-1", "T1", qty="4", principal="400.00"))
        self.post("withdrawal_requested",
                  {"customer_id": "C2", "amount": "300.00",
                   "withdrawal_id": "W1"})
        self.post("interest_credited",
                  {"customer_id": "C1", "gross_amount": "10.00",
                   "customer_share": "6.00"})
        tb = self.snap()["trial_balance"]

        self.assertEqual(list(tb.keys()), sorted(tb.keys()))
        self.assertGreater(D(tb["1100"]), 0)        # firm cash: asset, debit
        self.assertGreater(D(tb["1200"]), 0)        # securities at cost
        self.assertLess(D(tb["2010"]), 0)           # wallet payable: liability
        self.assertLess(D(tb["2100"]), 0)           # custody mirror
        self.assertLess(D(tb["2300"]), 0)           # withdrawals in transit
        self.assertLess(D(tb["4200"]), 0)           # interest income
        self.assertEqual(sum((D(v) for v in tb.values()), ZERO), ZERO)
        for v in tb.values():
            self.assertRegex(v, MONEY_RE)


# ---------------------------------------------------------------- #
#  the customer universe                                            #
# ---------------------------------------------------------------- #
class CustomerUniverseTest(CheckpointBase):
    def test_customers_recipient_only(self):
        """R18: the recipient of a transfer has never been named by anything
        else — one credit leg is its entire existence — and must still be a
        full customer block."""
        self.post("deposit", dep("C1", "500.00"))
        self.post("transfer_between_customers",
                  {"from_customer_id": "C1", "to_customer_id": "C-RECIP",
                   "amount": "125.50"})
        customers = self.snap()["customers"]
        self.assertIn("C-RECIP", customers)
        self.assertIn("C-RECIP", self.b.customers_seen)
        self.assertEqual(customers["C-RECIP"],
                         {"wallet_cash": "125.50", "cash_hold": "0.00",
                          "positions": {}})

    def test_customers_hold_only(self):
        """C6: a customer whose ONLY event is an open placement posts no
        legs at all — no balance key, no lot, no trade. It is registered by
        the handler itself, and reports a hold against an empty wallet."""
        self.post("order_placed",
                  place("ORD-H", cid="C-HOLD", qty="10", limit="20.25",
                        est="3.33"))
        # The proof that the universe is not derived from balances:
        self.assertFalse([k for k in self.b.balances if k[0] == "C-HOLD"])
        self.assertIn("C-HOLD", self.b.customers_seen)
        block = self.snap()["customers"]["C-HOLD"]
        self.assertEqual(block["wallet_cash"], "0.00")
        self.assertEqual(block["cash_hold"],
                         fmt_money(D("10") * D("20.25") + D("3.33")))
        self.assertEqual(block["cash_hold"], "205.83")
        self.assertEqual(block["positions"], {})

    def test_cash_hold_buy_only(self):
        """A sell hold is SHARES. It exists in the lifecycle and must never
        appear in cash_hold — the reported hold is the buy hold exactly."""
        # Shares to sell, acquired without an order (no hold of its own).
        self.post("dividend_reinvested",
                  {"customer_id": "C1", "symbol": "SYM",
                   "net_amount": "300.00", "reinvest_quantity": "3",
                   "reinvest_price": "100"})
        self.post("order_placed",
                  place("ORD-BUY", side="buy", qty="5", limit="10",
                        est="1.25"))
        self.post("order_placed",
                  place("ORD-SELL", side="sell", qty="3", limit="40",
                        est="2.00"))
        # The sell hold is real and non-zero...
        self.assertEqual(self.b.orders["ORD-SELL"]["share_hold_rem"],
                         qnum(3))
        self.assertEqual(self.b.orders["ORD-SELL"]["hold_rem"], ZERO)
        # ...and contributes nothing to the reported cash hold.
        block = self.snap()["customers"]["C1"]
        self.assertEqual(block["cash_hold"], "51.25")
        self.assertEqual(self.b._cash_hold("C1"),
                         self.b.orders["ORD-BUY"]["hold_rem"])

    def test_positions_omit_zero_qty(self):
        """C7: phantom positions are penalized. A fully-sold symbol leaves
        zombie lots (kept for FIFO order) and a renamed-away symbol leaves
        an empty index — neither may surface. A partial sell reports the
        remaining quantity and the remainder cost."""
        # (1) bought and fully sold -> zombie lots remain
        self.post("order_partially_filled",
                  fill_payload("ORD-G", "TG1", symbol="GONE", qty="10",
                               principal="1000.00"))
        self.post("order_partially_filled",
                  fill_payload("ORD-G", "TG2", side="sell", symbol="GONE",
                               qty="10", principal="1100.00"))
        # (2) bought then renamed away
        self.post("order_partially_filled",
                  fill_payload("ORD-R", "TR1", symbol="OLD", qty="5",
                               principal="500.00"))
        self.post("symbol_change", {"customer_id": "C1",
                                    "old_symbol": "OLD",
                                    "new_symbol": "NEW"})
        # (3) bought then partially sold
        self.post("order_partially_filled",
                  fill_payload("ORD-K", "TK1", symbol="KEEP", qty="10",
                               principal="1000.00"))
        self.post("order_partially_filled",
                  fill_payload("ORD-K", "TK2", side="sell", symbol="KEEP",
                               qty="4", principal="500.00"))

        positions = self.snap()["customers"]["C1"]["positions"]
        # The zombie lots and the emptied index really are still there:
        self.assertTrue(any(l["symbol"] == "GONE" and l["qty"] == 0
                            for l in self.b.lots.values()))
        self.assertIn(("C1", "GONE"), self.b.lot_index)
        self.assertNotIn("GONE", positions)
        self.assertNotIn("OLD", positions)
        self.assertEqual(positions["NEW"],
                         {"quantity": "5", "cost_basis": "500.00"})
        self.assertEqual(positions["KEEP"],
                         {"quantity": "6", "cost_basis": "600.00"})
        self.assertEqual(sorted(positions), ["KEEP", "NEW"])


# ---------------------------------------------------------------- #
#  open_order_routes                                                #
# ---------------------------------------------------------------- #
class RoutesTest(CheckpointBase):
    def test_routes_open_only(self):
        """C8 + A10: placed-unfilled and partially-filled-still-open are IN;
        filled, cancelled and rejected are OUT; a fill-only stub that was
        never placed is OUT — it has no limit_price, so no route exists."""
        self.post("order_placed", place("ORD-OPEN"))
        self.post("order_placed", place("ORD-PART"))
        self.post("order_partially_filled",
                  fill_payload("ORD-PART", "TP1", qty="4",
                               principal="400.00"))
        self.post("order_placed", place("ORD-FILLED"))
        self.post("order_filled",
                  fill_payload("ORD-FILLED", "TF1", qty="10",
                               principal="1000.00"))
        self.post("order_placed", place("ORD-CANC"))
        self.post("order_cancelled", {"order_id": "ORD-CANC"})
        self.post("order_placed", place("ORD-REJ"))
        self.post("order_rejected", {"order_id": "ORD-REJ"})
        self.post("order_partially_filled",
                  fill_payload("ORD-STUB", "TS1", qty="2",
                               principal="200.00"))

        routes = self.snap()["open_order_routes"]
        self.assertEqual(sorted(routes), ["ORD-OPEN", "ORD-PART"])
        self.assertEqual(list(routes.keys()), sorted(routes.keys()))
        # The stub is a live, open order record — excluded on A10 grounds
        # (never placed => no limit_price => route uncomputable), not
        # because it is closed.
        stub = self.b.orders["ORD-STUB"]
        self.assertFalse(stub["placed"])
        self.assertFalse(stub["closed"])
        self.assertIsNone(stub["route"])
        # ...and the partially-filled one is genuinely still open.
        self.assertFalse(self.b.orders["ORD-PART"]["closed"])
        self.assertEqual(self.b.orders["ORD-PART"]["filled_qty"], qnum(4))

    def test_route_is_placement_route(self):
        """A14: the reported broker is the one chosen AT PLACEMENT on the
        original quantity x limit_price. Both legs below straddle a min-fee
        crossover, so recomputing on the remaining quantity would report the
        OTHER broker — the assertion is that it does not.

        equity A/B crossover at N ~= 1315.79 (A: 24 bps vs B: 2.50 floor +
        5 bps); etf A/C crossover at N ~= 416.67 (A: 1.00 floor + 4 bps vs
        C: 28 bps)."""
        # -- equity: original N = 2000 -> BRK-B; remaining N = 800 -> BRK-A
        self.assertEqual(tariff.route("equity", D("100"), D("20")), "BRK-B")
        self.assertEqual(tariff.route("equity", D("40"), D("20")), "BRK-A")
        self.post("order_placed",
                  place("ORD-EQ", symbol="EQ", qty="100", limit="20",
                        est="4.00", asset_class="equity"))
        self.post("order_partially_filled",
                  fill_payload("ORD-EQ", "TE1", symbol="EQ", qty="60",
                               price="20", principal="1200.00",
                               broker="BRK-B", asset_class="equity"))

        # -- etf: original N = 500 -> BRK-A; remaining N = 300 -> BRK-C
        self.assertEqual(tariff.route("etf", D("50"), D("10")), "BRK-A")
        self.assertEqual(tariff.route("etf", D("30"), D("10")), "BRK-C")
        self.post("order_placed",
                  place("ORD-ETF", symbol="ETF", qty="50", limit="10",
                        est="1.00", asset_class="etf"))
        self.post("order_partially_filled",
                  fill_payload("ORD-ETF", "TT1", symbol="ETF", qty="20",
                               price="10", principal="200.00",
                               broker="BRK-A", asset_class="etf"))

        routes = self.snap()["open_order_routes"]
        self.assertEqual(routes["ORD-EQ"], "BRK-B")
        self.assertEqual(routes["ORD-ETF"], "BRK-A")
        # Both orders are open and partially filled — the flip is live.
        for oid, orig_q, rem_q in (("ORD-EQ", 100, 40), ("ORD-ETF", 50, 30)):
            o = self.b.orders[oid]
            self.assertFalse(o["closed"])
            self.assertEqual(o["qty_ordered"], qnum(orig_q))
            self.assertEqual(o["qty_ordered"] - o["filled_qty"], qnum(rem_q))
        self.assertNotEqual(routes["ORD-EQ"],
                            tariff.route("equity", D("40"), D("20")))
        self.assertNotEqual(routes["ORD-ETF"],
                            tariff.route("etf", D("30"), D("10")))


# ---------------------------------------------------------------- #
#  serialization canon                                              #
# ---------------------------------------------------------------- #
class SerializationTest(CheckpointBase):
    def test_fmt_qty_minimal(self):
        """Quantities are minimal-form decimal strings and money is always
        2 dp — on the formatters directly, and then on a real snapshot
        carrying split residue, a fractional reinvest lot and accounts
        netted to zero (the "0E-2" trap)."""
        self.assertEqual(fmt_qty(D("8.000000")), "8")
        self.assertEqual(fmt_qty(D("0.333333")), "0.333333")
        self.assertEqual(fmt_qty(D("0E-6")), "0")
        self.assertEqual(fmt_qty(D("1E+2")), "100")
        self.assertEqual(fmt_qty(D("10.500000")), "10.5")
        self.assertEqual(fmt_money(D("0E-2")), "0.00")
        self.assertEqual(fmt_money(D("-0.001")), "0.00")   # never "-0.00"
        self.assertEqual(fmt_money(D("1E+3")), "1000.00")

        # A snapshot with every awkward shape in it at once.
        self.post("deposit", dep("C1", "100.00"))
        self.post("fee_charged", {"customer_id": "C1",
                                  "amount": "100.00"})     # nets to zero
        self.post("order_partially_filled",
                  fill_payload("ORD-S", "TS", symbol="SPL", qty="3",
                               principal="1000.00"))
        self.post("stock_split", {"customer_id": "C1", "symbol": "SPL",
                                  "ratio_from": "3", "ratio_to": "1"})
        self.post("dividend_reinvested",
                  {"customer_id": "C2", "symbol": "FRC",
                   "net_amount": "33.33", "reinvest_quantity": "0.333333",
                   "reinvest_price": "100"})
        self.post("order_partially_filled",
                  fill_payload("ORD-BIG", "TB", cid="C3", symbol="BIG",
                               qty="10000000", price="0.0001",
                               principal="1000.00"))
        self.post("order_placed", place("ORD-O", cid="C4"))

        snap = self.snap()
        blob = json.dumps(snap)
        # No scientific notation anywhere, and — the work order's blunt
        # form — no "E" in any string the snapshot emits at all.
        self.assertIsNone(re.search(r"\d[eE][+-]?\d", blob), blob)
        for s in walk_strings(snap):
            self.assertNotIn("E", s, f"scientific-notation risk in {s!r}")
        self.assertEqual(snap["customers"]["C1"]["positions"]["SPL"]
                         ["quantity"], "1")
        self.assertEqual(snap["customers"]["C2"]["positions"]["FRC"],
                         {"quantity": "0.333333", "cost_basis": "33.33"})
        self.assertEqual(snap["customers"]["C3"]["positions"]["BIG"]
                         ["quantity"], "10000000")
        self.assertEqual(snap["trial_balance"]["1100"], "0.00")
        for acct, v in snap["trial_balance"].items():
            self.assertRegex(v, MONEY_RE, f"trial_balance[{acct}]")
        for cid, blk in snap["customers"].items():
            self.assertRegex(blk["wallet_cash"], MONEY_RE, cid)
            self.assertRegex(blk["cash_hold"], MONEY_RE, cid)
            for sym, pos in blk["positions"].items():
                self.assertRegex(pos["cost_basis"], MONEY_RE, f"{cid}/{sym}")
                q = pos["quantity"]
                self.assertNotIn("E", q)
                if "." in q:
                    self.assertFalse(q.endswith("0"), f"{cid}/{sym}: {q}")
                self.assertEqual(q, fmt_qty(D(q)))

    def test_snapshot_never_raises(self):
        """Client patch 2 contract: snapshot() answers, always. Three
        poisons — a corrupt log entry on the as-of path, a lot dict missing
        a key, and a formatter that raises — each returns a dict, grows the
        report log where the failure is ours to record, and lets nothing
        escape."""
        eids = []
        for i in range(6):
            e = ev("deposit", dep("C1", f"{10 + i}.00"), f"nr_{i}")
            eids.append(e["event_id"])
            self.b.apply(e)
        live = self.sbytes()

        # (1) as-of path poisoned: the log entry it must replay is garbage.
        self.b.event_log[2] = "not-an-event"
        before = len(self.b.report_log)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = self.b.snapshot(eids[4])
        self.assertIsInstance(out, dict)
        self.assertEqual(snap_bytes(out), live)       # degraded to live
        self.assertGreater(len(self.b.report_log), before)
        self.assertIn("snapshot_failed",
                      repr(self.b.report_log[before:]))
        self.assertIn("Traceback", err.getvalue())

        # (2) internal state poisoned: a lot dict missing "qty".
        b2 = Book()
        b2.apply(ev("deposit", dep("C1", "10.00"), "nr_p1"))
        b2.apply(ev("order_partially_filled",
                    fill_payload("ORD-P", "TP", qty="3",
                                 principal="300.00"), "nr_p2"))
        lot = b2.lots[max(b2.lots)]
        del lot["qty"]
        before = len(b2.report_log)
        with contextlib.redirect_stderr(io.StringIO()):
            out = b2.snapshot()
        self.assertIsInstance(out, dict)
        self.assertEqual(set(out),
                         {"trial_balance", "customers", "open_order_routes"})
        self.assertGreater(len(b2.report_log), before)

        # (3) the formatter itself raises — nothing survives except the
        #     promise to return a dict.
        b3 = Book()
        b3.apply(ev("deposit", dep("C1", "10.00"), "nr_f1"))
        b3.apply(ev("order_partially_filled",
                    fill_payload("ORD-F", "TF", qty="3",
                                 principal="300.00"), "nr_f2"))
        original = book_mod.fmt_qty

        def boom(_x):
            raise RuntimeError("poisoned formatter")

        book_mod.fmt_qty = boom
        try:
            before = len(b3.report_log)
            with contextlib.redirect_stderr(io.StringIO()):
                out = b3.snapshot("nr_f2")
            self.assertIsInstance(out, dict)
            self.assertEqual(set(out), {"trial_balance", "customers",
                                        "open_order_routes"})
            self.assertGreater(len(b3.report_log), before)
        finally:
            book_mod.fmt_qty = original
        # The formatter is back and the book is still answerable.
        self.assertIsInstance(b3.snapshot(), dict)


# ---------------------------------------------------------------- #
#  as-of answers                                                    #
# ---------------------------------------------------------------- #
class AsOfTest(CheckpointBase):
    def script(self) -> tuple[list, list]:
        """A ten-event script, recording the live snapshot after each one.
        Returns (event_ids, snapshots-as-bytes), both 0-indexed by delivery
        position."""
        steps = [
            ("deposit", dep("C1", "1000.00")),
            ("deposit", dep("C2", "500.00")),
            ("order_placed", place("ORD-1")),
            ("order_partially_filled",
             fill_payload("ORD-1", "T1", qty="4", principal="400.00")),
            ("transfer_between_customers",
             {"from_customer_id": "C1", "to_customer_id": "C2",
              "amount": "100.00"}),
            ("fee_charged", {"customer_id": "C2", "amount": "20.00"}),
            ("order_filled",
             fill_payload("ORD-1", "T2", qty="6", principal="600.00")),
            ("trade_settled", {"trade_id": "T1"}),
            ("stock_split", {"customer_id": "C1", "symbol": "SYM",
                             "ratio_from": "1", "ratio_to": "2"}),
            ("deposit", dep("C3", "77.00")),
        ]
        eids, snaps = [], []
        for i, (etype, payload) in enumerate(steps):
            e = ev(etype, payload, f"as_{i}")
            eids.append(e["event_id"])
            self.b.apply(e)
            snaps.append(self.sbytes())
        return eids, snaps

    def test_asof_basic(self):
        """C1: as-of the 5th event is the book as it stood right after the
        5th event — byte-identical to the live snapshot recorded then, and
        to a cold replay of the same prefix."""
        eids, snaps = self.script()
        self.assertEqual(self.sbytes(eids[4]), snaps[4])
        self.assertEqual(self.sbytes(eids[4]), self.cold(5))
        self.assertNotEqual(snaps[4], snaps[9])       # the probe is real
        # Every position, not just the fifth.
        for i, eid in enumerate(eids):
            self.assertEqual(self.sbytes(eid), snaps[i], f"as-of #{i}")
            self.assertEqual(self.sbytes(eid), self.cold(i + 1), f"cold #{i}")
        # as_of=None is the live book, and the last as-of equals it.
        self.assertEqual(self.sbytes(), snaps[-1])
        self.assertEqual(self.sbytes(eids[-1]), self.sbytes())

    def test_asof_rejected_and_duplicate(self):
        """C2: a rejected event is IN the log (we were processed through
        it) but changed nothing, so its answer equals its predecessor's; a
        duplicated id resolves to its FIRST delivery, not the second."""
        snaps = []

        def step(etype, payload, eid):
            self.b.apply(ev(etype, payload, eid))
            snaps.append(self.sbytes())

        step("deposit", dep("C1", "100.00"), "rd_0")
        # Rejected on its own merits: no such withdrawal.
        step("withdrawal_settled", {"withdrawal_id": "NOPE"}, "rd_rej")
        step("deposit", dep("C1", "50.00"), "rd_2")
        step("deposit", dep("C2", "7.00"), "rd_dup")       # first delivery
        step("deposit", dep("C1", "1.00"), "rd_4")
        # A conflicting redelivery of rd_dup: same id, different content.
        self.b.apply(ev("deposit", dep("C2", "9999.00"), "rd_dup"))
        snaps.append(self.sbytes())
        step("deposit", dep("C1", "2.00"), "rd_6")

        # -- rejected target: logged, no `events` record, no state change
        self.assertIn("rd_rej", self.b.eid_pos)
        self.assertIn("rd_rej", self.b.seen)
        self.assertNotIn("rd_rej", self.b.events)
        self.assertEqual(self.sbytes("rd_rej"), snaps[1])
        self.assertEqual(snaps[1], snaps[0])        # ... which IS its parent
        self.assertEqual(self.sbytes("rd_rej"), self.cold(2))

        # -- duplicated target: resolves to the FIRST delivered position
        self.assertEqual(self.b.eid_pos["rd_dup"], 3)
        self.assertEqual(self.sbytes("rd_dup"), snaps[3])
        self.assertNotEqual(self.sbytes("rd_dup"), snaps[5])
        # The redelivery posted nothing at all.
        self.assertEqual(snaps[5], snaps[4])
        self.assertEqual(self.snap()["customers"]["C2"]["wallet_cash"],
                         "7.00")
        self.assertEqual(len(self.b.event_log), 6)   # duplicate not re-logged

    def test_asof_backdated_excluded(self):
        """C4: a backdated event posts in DELIVERY order, so one delivered
        after the as-of point is simply absent from the answer — and the
        very same event delivered before it is present. No special code:
        that is the point of the test."""
        backdated = dep("C1", "1000.00", backdated_days=5)

        # (a) delivered AFTER the as-of target -> excluded
        self.b.apply(ev("deposit", dep("C1", "100.00"), "bd_a0"))
        self.b.apply(ev("deposit", dep("C1", "25.00"), "bd_a1"))   # target
        self.b.apply(ev("deposit", backdated, "bd_a2"))
        self.b.apply(ev("deposit", dep("C1", "5.00"), "bd_a3"))
        answer_a = self.snap("bd_a1")
        self.assertEqual(answer_a["customers"]["C1"]["wallet_cash"],
                         "125.00")
        self.assertEqual(snap_bytes(answer_a), self.cold(2))
        # It IS in the book, just after the point we were asked about.
        self.assertEqual(self.snap()["customers"]["C1"]["wallet_cash"],
                         "1130.00")

        # (b) the same event delivered BEFORE the target -> present
        b2 = Book()
        b2.apply(ev("deposit", dep("C1", "100.00"), "bd_b0"))
        b2.apply(ev("deposit", dict(backdated), "bd_b1"))
        b2.apply(ev("deposit", dep("C1", "25.00"), "bd_b2"))       # target
        b2.apply(ev("deposit", dep("C1", "5.00"), "bd_b3"))
        answer_b = b2.snapshot("bd_b2")
        self.assertEqual(answer_b["customers"]["C1"]["wallet_cash"],
                         "1125.00")

    def test_asof_routes(self):
        """Routes under as-of: an order placed before the point is open in
        the answer even though it was filled (or cancelled) afterwards, and
        one already filled before the point is absent."""
        self.b.apply(ev("order_placed", place("ORD-A"), "rt_0"))
        self.b.apply(ev("order_placed", place("ORD-B"), "rt_1"))
        self.b.apply(ev("order_placed", place("ORD-C"), "rt_2"))
        self.b.apply(ev("order_filled",
                        fill_payload("ORD-C", "TC", qty="10",
                                     principal="1000.00"), "rt_3"))
        self.b.apply(ev("deposit", dep("C1", "10.00"), "rt_mark"))  # target
        at_mark = self.sbytes()
        # ...everything below happens AFTER the as-of point.
        self.b.apply(ev("order_filled",
                        fill_payload("ORD-A", "TA", qty="10",
                                     principal="1000.00"), "rt_5"))
        self.b.apply(ev("order_cancelled", {"order_id": "ORD-B"}, "rt_6"))
        self.b.apply(ev("order_placed", place("ORD-D"), "rt_7"))

        answer = self.snap("rt_mark")
        self.assertEqual(snap_bytes(answer), at_mark)
        self.assertEqual(sorted(answer["open_order_routes"]),
                         ["ORD-A", "ORD-B"])
        self.assertNotIn("ORD-C", answer["open_order_routes"])   # filled before
        self.assertNotIn("ORD-D", answer["open_order_routes"])   # placed after
        # The live book disagrees with the answer, as it should.
        self.assertEqual(sorted(self.snap()["open_order_routes"]), ["ORD-D"])

    def test_asof_unknown_id(self):
        """A9: an id we never received cannot be located, so it is treated
        as not-yet-processed — the live state is returned, loudly. Never a
        raise, never a stall."""
        self.b.apply(ev("deposit", dep("C1", "100.00"), "uk_0"))
        self.b.apply(ev("deposit", dep("C2", "40.00"), "uk_1"))
        live = self.sbytes()
        before = len(self.b.report_log)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            answer = self.b.snapshot("NEVER-DELIVERED-42")
        self.assertEqual(snap_bytes(answer), live)
        self.assertEqual(len(self.b.report_log), before + 1)
        record = self.b.report_log[-1]
        self.assertEqual(record[0], "asof_unknown_id")
        self.assertIn("NEVER-DELIVERED-42", repr(record))
        self.assertIn("NEVER-DELIVERED-42", err.getvalue())
        # Loud, but not a failure: the book is untouched and still answers.
        self.assertEqual(self.sbytes(), live)
        self.assertEqual(self.sbytes("uk_0"), self.cold(1))


# ---------------------------------------------------------------- #
#  the ring                                                         #
# ---------------------------------------------------------------- #
class RingTest(CheckpointBase):
    def build(self, n_pre: int) -> list:
        """A feed long enough to cross RING_INTERVAL, with a real lot in it
        so the aliasing audit has mutable objects to compare."""
        eids = []

        def go(etype, payload, eid):
            eids.append(eid)
            self.b.apply(ev(etype, payload, eid))

        for i in range(n_pre - 50):
            go("deposit", dep(f"C{i % 5}", f"{10 + (i % 7)}.00"), f"rg_{i}")
        go("order_placed", place("ORD-RG", qty="10", limit="100"),
           "rg_place")
        go("order_partially_filled",
           fill_payload("ORD-RG", "T-RG", qty="4", principal="400.00"),
           "rg_fill")
        for i in range(48):
            go("deposit", dep(f"C{i % 5}", f"{20 + (i % 3)}.00"),
               f"rg_tail_{i}")
        return eids

    def mutate_hard(self) -> None:
        """Everything that could plausibly corrupt a shared ring entry:
        hundreds more postings, lot consumption, a split, a rename, new
        orders and closures."""
        self.b.apply(ev("stock_split", {"customer_id": "C1", "symbol": "SYM",
                                        "ratio_from": "1", "ratio_to": "2"},
                        "mu_split"))
        self.b.apply(ev("order_partially_filled",
                        fill_payload("ORD-MU-SELL", "T-RG2", side="sell",
                                     qty="3", principal="500.00"),
                        "mu_sell"))
        self.b.apply(ev("symbol_change", {"customer_id": "C1",
                                          "old_symbol": "SYM",
                                          "new_symbol": "SYM2"},
                        "mu_rename"))
        for i in range(400):
            self.b.apply(ev("deposit", dep(f"C{i % 5}", f"{1 + (i % 9)}.00"),
                            f"mu_{i}"))
        self.b.apply(ev("order_placed", place("ORD-LATE"), "mu_place"))
        self.b.apply(ev("order_cancelled", {"order_id": "ORD-RG"},
                        "mu_cancel"))

    def test_ring_no_aliasing(self):
        eids = self.build(300)
        self.assertEqual(len(self.b.event_log), 300)
        self.assertTrue(self.b._ring, "no ring entry after 300 events")
        self.assertEqual(self.b._ring[0][0], book_mod.RING_INTERVAL)

        target = eids[-1]                       # log position 299
        self.assertEqual(self.b.eid_pos[target], 299)
        first = self.sbytes(target)
        self.assertEqual(first, self.cold(300))  # ring path == cold replay

        # The restored book must share NOTHING mutable with the live book.
        restored = self.b._book_as_of(300)
        live_ids = {id(o) for o in self.b.lots.values()}
        self.assertTrue(restored.lots, "no lots restored")
        for lot in restored.lots.values():
            self.assertNotIn(id(lot), live_ids)
        for name in ("balances", "lots", "lot_index", "orders", "trades",
                     "seen", "events", "fees", "refunded", "withdrawals",
                     "quarantine", "customers_seen", "accounts_touched"):
            self.assertIsNot(getattr(restored, name), getattr(self.b, name),
                             f"{name} aliased into the restored book")
        # Factories/behaviour survive the round trip: the restored book is
        # a working Book, not a frozen shell.
        self.assertIsInstance(restored.lot_index, dict)
        self.assertIsInstance(restored.customers_seen, set)
        restored.apply(ev("deposit", dep("C-ONLY-RESTORED", "5.00"),
                          "ring_probe"))
        self.assertIn("C-ONLY-RESTORED", restored.customers_seen)
        self.assertNotIn("C-ONLY-RESTORED", self.b.customers_seen)

        # Scribble on the restored book's lots; the live book must not care.
        live_before = self.sbytes()
        for lot in restored.lots.values():
            lot["qty"] = qnum(lot["qty"] + 1000)
            lot["cost_total"] = lot["cost_total"] + money("999.99")
        self.assertEqual(self.sbytes(), live_before)

        # Now mutate the live book hard and re-ask the SAME question.
        self.mutate_hard()
        self.assertGreater(len(self.b.event_log), 700)
        self.assertGreaterEqual(len(self.b._ring), 2)
        again = self.sbytes(target)
        self.assertEqual(again, first)
        self.assertEqual(again, self.cold(300))
        # And a third time, after the ring has grown past the target.
        self.assertEqual(self.sbytes(target), first)
        # The live book genuinely moved on.
        self.assertNotEqual(self.sbytes(), first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
