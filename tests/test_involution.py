"""Phase 5 gate — the involution property matrix (R14).

For EVERY implemented leg-or-lot event type: build a seeded random prefix,
capture the book, apply one event E, apply reversal(E), and require
balances, the lot book (per-lot qty + cost + symbol + FIFO order) and
holds to equal the pre-E capture exactly.

Comparison basis (the documented choices):

  * balances — zero-valued keys are filtered before comparing: posting E
    and then its exact inverse legitimately leaves a 0.00 entry on a
    (cid, acct) key that did not exist before E.
  * lot book — the LIVE view: zombie (qty 0) lots are excluded from the
    equality. A reversed buy/reinvest deliberately leaves a zombie holding
    its FIFO slot; the live view — which is exactly what every later
    consume and every report sees — must match the pre-E capture per lot
    (id, qty, cost, symbol) in FIFO order.
  * holds — lifecycle-adjusted per the work order: a hold the original
    fill released stays released, so for fill events the hold capture is
    taken AFTER E (post-release); for every other event type it is taken
    before E and must be untouched end to end.

Scenario count: 20 event-type matrix tests × 6 seeds = 120 seeded random
scenarios (>= 100 required), plus the four named inclusions run as
dedicated deterministic tests: reversal-after-split, a sell spanning
three lots (one previously partially relieved), dividend_reinvested, and
symbol-changed lots (including a merge into an existing holding).
"""
import os
import random
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from book import Book, ZERO, money, qnum  # noqa: E402

D = Decimal

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_iv_{_SEQ}",
            "type": etype, "payload": payload}


def bal_map(b: Book) -> dict:
    return {f"{k}": str(v) for k, v in b.balances.items() if v != 0}


def live_lots(b: Book) -> dict:
    """The live lot book: per (cid, symbol), every non-zombie lot as
    (lot_id, qty, cost_total, symbol) in FIFO order."""
    view = {}
    for key, index in b.lot_index.items():
        rows = [(i, str(b.lots[i]["qty"]), str(b.lots[i]["cost_total"]),
                 b.lots[i]["symbol"])
                for i in sorted(index, key=b._fifo_key)
                if b.lots[i]["qty"] > 0]
        if rows:
            view[f"{key}"] = rows
    return view


def holds_map(b: Book) -> dict:
    return {oid: (str(o["hold_rem"]), str(o["share_hold_rem"]))
            for oid, o in b.orders.items()}


def capture(b: Book) -> tuple:
    return (bal_map(b), live_lots(b), holds_map(b))


def fill_payload(oid, tid, *, cid="C1", side="buy", symbol="AAA", qty="10",
                 price="1", principal="1000.00", broker="BRK-A",
                 asset_class="equity", rate="0.50") -> dict:
    return {"order_id": oid, "trade_id": tid, "customer_id": cid,
            "side": side, "symbol": symbol, "quantity": qty, "price": price,
            "principal": principal, "broker": broker,
            "asset_class": asset_class, "partner_rate": rate}


BROKER_CLASSES = (("BRK-A", "equity"), ("BRK-A", "etf"),
                  ("BRK-B", "equity"), ("BRK-B", "bond"),
                  ("BRK-C", "etf"), ("BRK-C", "bond"))
SYMS = ("AAA", "BBB")
RATIOS = (("1", "2"), ("2", "1"), ("1", "3"), ("3", "2"))


def cents(rng, lo=100, hi=5_000_000) -> str:
    """A random money amount in [lo, hi) cents, as a canonical string."""
    return str(D(rng.randrange(lo, hi)) / 100)


class InvolutionBase(unittest.TestCase):
    N_SEEDS = 6
    _N = 0

    @classmethod
    def next_n(cls) -> int:
        InvolutionBase._N += 1
        return InvolutionBase._N

    # -- appliers --------------------------------------------------------
    def apply_ok(self, b, etype, payload, eid=None) -> str:
        e = ev(etype, payload, eid)
        b.apply(e)
        self.assertIn(e["event_id"], b.events,
                      f"{etype} was rejected — fixture bug: {payload}")
        return e["event_id"]

    def buy(self, b, rng=None, *, cid="C1", symbol="AAA", qty=None,
            principal=None, broker=None, asset_class=None) -> str:
        n = self.next_n()
        if rng is not None:
            qty = qty or str(rng.randint(2, 40))
            principal = principal or cents(rng, 10_000, 5_000_000)
            if broker is None:
                broker, asset_class = rng.choice(BROKER_CLASSES)
        return self.apply_ok(b, "order_filled", fill_payload(
            f"O-iv-{n}", f"T-iv-{n}", cid=cid, symbol=symbol,
            qty=qty or "10", principal=principal or "1000.00",
            broker=broker or "BRK-A", asset_class=asset_class or "equity"))

    def sell_payload(self, rng, *, cid="C1", symbol="AAA", qty="1",
                     principal=None) -> dict:
        n = self.next_n()
        broker, asset_class = rng.choice(BROKER_CLASSES)
        return fill_payload(
            f"O-iv-{n}", f"T-iv-{n}", cid=cid, side="sell", symbol=symbol,
            qty=qty, principal=principal or cents(rng, 10_000, 5_000_000),
            broker=broker, asset_class=asset_class)

    @staticmethod
    def available(b, cid, symbol) -> Decimal:
        return sum((b.lots[i]["qty"]
                    for i in b.lot_index.get((cid, symbol), [])), qnum(0))

    # -- the seeded random prefix ---------------------------------------
    def prefix(self, b, rng, *, cid="C1") -> list:
        """Deposits, one-to-three buys, maybe a sell, maybe a split —
        seeded, valid by construction. Returns the symbols bought."""
        self.apply_ok(b, "deposit", {
            "customer_id": cid, "amount": cents(rng, 100_000, 10_000_000)})
        syms = []
        for _ in range(rng.randint(1, 3)):
            sym = rng.choice(SYMS)
            self.buy(b, rng, cid=cid, symbol=sym)
            syms.append(sym)
        if rng.random() < 0.4:
            sym = rng.choice(syms)
            half = int(self.available(b, cid, sym)) // 2
            if half >= 1:
                self.apply_ok(b, "order_filled", self.sell_payload(
                    rng, cid=cid, symbol=sym,
                    qty=str(rng.randint(1, half))))
        if rng.random() < 0.35:
            r_from, r_to = rng.choice(RATIOS)
            self.apply_ok(b, "stock_split", {
                "customer_id": cid, "symbol": rng.choice(syms),
                "ratio_from": r_from, "ratio_to": r_to})
        return syms

    # -- the involution check -------------------------------------------
    def check(self, b, etype, payload, *, holds_after=False) -> str:
        pre = capture(b)
        eid = self.apply_ok(b, etype, payload)
        if holds_after:            # lifecycle adjustment for fill events
            pre = (pre[0], pre[1], holds_map(b))
        self.apply_ok(b, "reversal", {"reverses_event_id": eid})
        post = capture(b)
        self.assertEqual(post[0], pre[0], f"{etype}: balances not restored")
        self.assertEqual(post[1], pre[1], f"{etype}: lot book not restored")
        self.assertEqual(post[2], pre[2], f"{etype}: holds drifted")
        return eid

    def run_matrix(self, name, build, *, holds_after=False):
        for seed in range(self.N_SEEDS):
            with self.subTest(seed=seed):
                rng = random.Random(f"{name}:{seed}")
                b = Book()
                etype, payload = build(b, rng)
                self.check(b, etype, payload, holds_after=holds_after)


# --------------------------------------------------------------------- #
#  the matrix: every implemented leg-or-lot event type                  #
# --------------------------------------------------------------------- #
class TestInvolutionMatrix(InvolutionBase):
    def test_involution_deposit(self):
        def build(b, rng):
            self.prefix(b, rng)
            return "deposit", {"customer_id": "C1", "amount": cents(rng)}
        self.run_matrix("deposit", build)

    def test_involution_fee_charged(self):
        def build(b, rng):
            self.prefix(b, rng)
            return "fee_charged", {"customer_id": "C1",
                                   "amount": cents(rng)}
        self.run_matrix("fee_charged", build)

    def test_involution_fee_refund(self):
        def build(b, rng):
            self.prefix(b, rng)
            fee = self.apply_ok(b, "fee_charged", {
                "customer_id": "C1", "amount": cents(rng)})
            return "fee_refund", {"refunds_source_id": fee}
        self.run_matrix("fee_refund", build)

    def test_involution_interest(self):
        def build(b, rng):
            self.prefix(b, rng)
            gross = money(D(cents(rng)))
            roll = rng.random()
            share = gross if roll < 0.2 else \
                (ZERO if roll < 0.4 else money(gross / 2))
            return "interest_credited", {
                "customer_id": "C1", "gross_amount": str(gross),
                "customer_share": str(share)}
        self.run_matrix("interest_credited", build)

    def test_involution_transfer(self):
        def build(b, rng):
            self.prefix(b, rng)
            return "transfer_between_customers", {
                "from_customer_id": "C1", "to_customer_id": "C2",
                "amount": cents(rng)}
        self.run_matrix("transfer_between_customers", build)

    def test_involution_fx_deposit(self):
        def build(b, rng):
            self.prefix(b, rng)
            m = money(D(cents(rng)))
            c = m if rng.random() < 0.25 else money(m * D("0.97"))
            return "fx_deposit", {
                "customer_id": "C1", "usd_at_market_rate": str(m),
                "usd_at_customer_rate": str(c)}
        self.run_matrix("fx_deposit", build)

    def test_involution_withdrawal_requested(self):
        def build(b, rng):
            self.prefix(b, rng)
            return "withdrawal_requested", {
                "customer_id": "C1",
                "withdrawal_id": f"W-iv-{self.next_n()}",
                "amount": cents(rng)}
        self.run_matrix("withdrawal_requested", build)

    def test_involution_withdrawal_settled(self):
        def build(b, rng):
            self.prefix(b, rng)
            wid = f"W-iv-{self.next_n()}"
            self.apply_ok(b, "withdrawal_requested", {
                "customer_id": "C1", "withdrawal_id": wid,
                "amount": cents(rng)})
            return "withdrawal_settled", {"withdrawal_id": wid}
        self.run_matrix("withdrawal_settled", build)

    def test_involution_withdrawal_rejected(self):
        def build(b, rng):
            self.prefix(b, rng)
            wid = f"W-iv-{self.next_n()}"
            self.apply_ok(b, "withdrawal_requested", {
                "customer_id": "C1", "withdrawal_id": wid,
                "amount": cents(rng)})
            return "withdrawal_rejected", {"withdrawal_id": wid}
        self.run_matrix("withdrawal_rejected", build)

    def test_involution_buy_fill(self):
        def build(b, rng):
            self.prefix(b, rng)
            n = self.next_n()
            qty = rng.randint(2, 30)
            ordered = qty + (rng.randint(1, 5) if rng.random() < 0.5 else 0)
            broker, ac = rng.choice(BROKER_CLASSES)
            placed = rng.random() < 0.5
            if placed:
                self.apply_ok(b, "order_placed", {
                    "order_id": f"O-iv-{n}", "customer_id": "C1",
                    "side": "buy", "symbol": "AAA",
                    "quantity": str(ordered), "limit_price": "50",
                    "est_charges": "10.00", "asset_class": ac})
            etype = ("order_partially_filled"
                     if placed and qty < ordered else "order_filled")
            return etype, fill_payload(
                f"O-iv-{n}", f"T-iv-{n}", qty=str(qty),
                principal=cents(rng, 10_000, 2_000_000),
                broker=broker, asset_class=ac)
        self.run_matrix("buy_fill", build, holds_after=True)

    def test_involution_sell_fill(self):
        def build(b, rng):
            syms = self.prefix(b, rng)
            sym = max(set(syms),
                      key=lambda s: self.available(b, "C1", s))
            avail = self.available(b, "C1", sym)
            q = D(rng.randint(1, int(avail))) if avail >= 2 else avail
            return "order_filled", self.sell_payload(
                rng, symbol=sym, qty=str(q))
        self.run_matrix("sell_fill", build, holds_after=True)

    def test_involution_trade_settled(self):
        def build(b, rng):
            self.prefix(b, rng)
            n = self.next_n()
            broker, ac = rng.choice(BROKER_CLASSES)
            self.apply_ok(b, "order_filled", fill_payload(
                f"O-iv-{n}", f"T-iv-{n}",
                qty=str(rng.randint(1, 20)),
                principal=cents(rng, 10_000, 2_000_000),
                broker=broker, asset_class=ac))
            return "trade_settled", {"trade_id": f"T-iv-{n}"}
        self.run_matrix("trade_settled", build)

    def test_involution_dividend_cash(self):
        def build(b, rng):
            self.prefix(b, rng)
            return "dividend_cash", {"customer_id": "C1",
                                     "net_amount": cents(rng)}
        self.run_matrix("dividend_cash", build)

    def test_involution_dividend_reinvested(self):
        def build(b, rng):
            syms = self.prefix(b, rng)
            return "dividend_reinvested", {
                "customer_id": "C1", "symbol": rng.choice(syms),
                "net_amount": cents(rng, 1_000, 100_000),
                "reinvest_quantity": str(rng.randint(1, 5))}
        self.run_matrix("dividend_reinvested", build)

    def test_involution_stock_split(self):
        def build(b, rng):
            syms = self.prefix(b, rng)
            r_from, r_to = rng.choice(RATIOS)
            return "stock_split", {
                "customer_id": "C1", "symbol": rng.choice(syms),
                "ratio_from": r_from, "ratio_to": r_to}
        self.run_matrix("stock_split", build)

    def test_involution_symbol_change(self):
        def build(b, rng):
            syms = self.prefix(b, rng)
            old = rng.choice(syms)
            others = [s for s in set(syms) if s != old]
            new = (rng.choice(others)          # merge into a live holding
                   if others and rng.random() < 0.4
                   else f"NEW-{self.next_n()}")
            return "symbol_change", {"customer_id": "C1",
                                     "old_symbol": old, "new_symbol": new}
        self.run_matrix("symbol_change", build)

    def _settlement_build(self, etype):
        def build(b, rng):
            self.prefix(b, rng)
            broker, ac = rng.choice((("BRK-A", "equity"), ("BRK-B", "bond"),
                                     ("BRK-C", "etf")))
            # a big fill guarantees every payable line is strictly positive
            self.buy(b, rng, symbol="AAA", qty="10",
                     principal=cents(rng, 500_000, 5_000_000),
                     broker=broker, asset_class=ac)
            payload = {"customer_id": "C1"}
            if etype == "broker_fees_settled":
                payload["broker"] = broker
            return etype, payload
        return build

    def test_involution_broker_fees_settled(self):
        self.run_matrix("broker_fees_settled",
                        self._settlement_build("broker_fees_settled"))

    def test_involution_custodian_fees_settled(self):
        self.run_matrix("custodian_fees_settled",
                        self._settlement_build("custodian_fees_settled"))

    def test_involution_reg_fees_remitted(self):
        self.run_matrix("reg_fees_remitted",
                        self._settlement_build("reg_fees_remitted"))

    def test_involution_partner_payout(self):
        self.run_matrix("partner_payout",
                        self._settlement_build("partner_payout"))


# --------------------------------------------------------------------- #
#  the four named inclusions                                            #
# --------------------------------------------------------------------- #
class TestInvolutionNamed(InvolutionBase):
    def test_involution_named_reversal_after_split(self):
        """A sell applied to POST-SPLIT lots (multiplier 2, then 1/3):
        reversing it restores the split-scaled quantities and cents."""
        for r_from, r_to in (("1", "2"), ("3", "1")):
            b = Book()
            self.apply_ok(b, "deposit",
                          {"customer_id": "C1", "amount": "50000.00"})
            self.buy(b, symbol="AAA", qty="10", principal="100.03")
            self.apply_ok(b, "stock_split", {
                "customer_id": "C1", "symbol": "AAA",
                "ratio_from": r_from, "ratio_to": r_to})
            avail = self.available(b, "C1", "AAA")
            rng = random.Random(f"named-split:{r_from}:{r_to}")
            self.check(b, "order_filled", self.sell_payload(
                rng, symbol="AAA", qty=str(avail)), holds_after=True)

    def test_involution_named_sell_three_lots(self):
        """A sell spanning three lots, the first previously partially
        relieved: reversal restores every portion on its own lot."""
        b = Book()
        rng = random.Random("named-three-lots")
        self.apply_ok(b, "deposit",
                      {"customer_id": "C1", "amount": "50000.00"})
        self.buy(b, symbol="AAA", qty="10", principal="100.00")
        self.buy(b, symbol="AAA", qty="5", principal="77.77")
        self.buy(b, symbol="AAA", qty="8", principal="55.55")
        self.apply_ok(b, "order_filled", self.sell_payload(
            rng, symbol="AAA", qty="4"))          # pre-relieves lot 1
        self.check(b, "order_filled", self.sell_payload(
            rng, symbol="AAA", qty="14"), holds_after=True)

    def test_involution_named_dividend_reinvested(self):
        """A reinvest lot at the back of the FIFO queue: its reversal
        zombifies the lot and restores 1200/2100 exactly."""
        b = Book()
        self.apply_ok(b, "deposit",
                      {"customer_id": "C1", "amount": "50000.00"})
        self.buy(b, symbol="AAA", qty="10", principal="1000.00")
        self.check(b, "dividend_reinvested", {
            "customer_id": "C1", "symbol": "AAA", "net_amount": "47.50",
            "reinvest_quantity": "0.5"})

    def test_involution_named_symbol_changed_lots(self):
        """(a) A rename that MERGES into an existing holding reverses back
        to two separate holdings with FIFO intact; (b) a sell of
        symbol-changed lots reverses in place under the new symbol."""
        b = Book()
        rng = random.Random("named-rename")
        self.apply_ok(b, "deposit",
                      {"customer_id": "C1", "amount": "50000.00"})
        self.buy(b, symbol="NEW", qty="2", principal="30.00")   # existing
        self.buy(b, symbol="OLD", qty="2", principal="20.00")
        self.buy(b, symbol="OLD", qty="2", principal="40.00")
        self.check(b, "symbol_change", {
            "customer_id": "C1", "old_symbol": "OLD", "new_symbol": "NEW"})
        # (b) sell lots that live under a changed symbol, then reverse
        b2 = Book()
        self.apply_ok(b2, "deposit",
                      {"customer_id": "C1", "amount": "50000.00"})
        self.buy(b2, symbol="A", qty="10", principal="100.00")
        self.apply_ok(b2, "symbol_change", {
            "customer_id": "C1", "old_symbol": "A", "new_symbol": "B"})
        self.check(b2, "order_filled", self.sell_payload(
            rng, symbol="B", qty="7"), holds_after=True)


if __name__ == "__main__":
    unittest.main()
