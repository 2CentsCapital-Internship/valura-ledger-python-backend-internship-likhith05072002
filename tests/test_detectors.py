"""Phase 7 gate — detector calibration.

The arena guarantees one systematic defect class: "an event that is
internally well-formed and wrong". A detector that catches it converts a
lost event into a quarter-weight no-leg event. A detector that fires on a
CLEAN event throws away that event's full weight. The miss is worth a
quarter of what the false positive costs, so this suite is built to prove
one thing above all others: **the armed detectors do not fire on clean
data, and the observing ones change nothing at all.**

Six properties, one test class each:

  1. ARMED CATCH — plant D1 (broker/asset-class mismatch) and D3 (interest
     share above gross) and require 100 % rejection with `legs: []`, and
     the book left in exactly the state a feed WITHOUT those events
     produces. D1 and D3 are enforced inline in book.py at their handler's
     validation site; this suite scores them from the outside, through
     apply(), which is the only interface the arena sees.
  2. OBSERVE CATCH — plant D4/D5/D6/D9/D10/D11 and require 100 % of them
     in `book.report_log`, while the submissions, the snapshot and the
     replayed quarantine stay byte-identical to the same feed replayed
     with every detector OFF. Observe mode is inert or it is nothing.
  3. INLINE OBSERVE — D2 and D7 are observed inside their handlers and
     (D8 was promoted to ARMED after practice run 1 identified it as the
     arena's planted defect class; it is enforced in _fill's validation.)
     land in `book.quarantine` (replayed state, posting paths only). Same
     100 % requirement, different channel.
  4. ZERO FALSE POSITIVES ON CLEAN FEEDS — three seeds of generate_clean:
     every stream trap on, every planted defect repaired, fills priced
     inside their limits with exact principals. No ARMED finding, no
     detector-pass rejection (proved by byte-identical submissions against
     an all-OFF replay), and zero hits for D1/D3/D4/D5/D6/D9/D11 on any
     event that posted. D10's hits are counted, not asserted away: they
     are the measured cost of arming it, and the reason it never will be.
  5. ZERO FP UNDER OUT-OF-ORDER DELIVERY — a fill delivered before its
     placement has no limit and no ordered quantity to violate, so D4 and
     D9 must stay silent; the same order's later fills must still fire, or
     the "skip" is really a dead predicate.
  6. KNOWN-FP SEQUENCES STAY OBSERVE-ONLY — dividend-before-buy (D10) and
     the fee-overdrawn wallet (D11) are legal, ordinary, and fire their
     predicates. Both post their legs. Each also gets its counterfactual:
     armed in a test-only mode table, the very same clean event is
     rejected — which is precisely the full-weight loss the arming policy
     refuses to pay.

Plus QuarantineIsNotState: the JSONL side-channel is write-only, and the
Book is byte-identical with it on and off — proved by replay, and by
reading the source of book.py and detectors.py for any read path back.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detectors  # noqa: E402
import tariff  # noqa: E402
from book import Book  # noqa: E402
from sim import arena_sim as sim  # noqa: E402

D = Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SEQ = 0


def ev(etype: str, payload, eid: str | None = None) -> dict:
    global _SEQ
    _SEQ += 1
    return {"offset": _SEQ, "event_id": eid or f"evt_dt_{_SEQ}",
            "type": etype, "payload": payload}


class Modes:
    """Rewrite the detector mode table for the duration of a `with` block.

    `DETECTOR_MODE` is a plain dict read live by `detectors.mode()`, which
    is exactly what the DETECTOR_MODES env var rewrites at import — so
    doing it in-process is the same switch the A/B rig throws, without a
    subprocess. Always restored, even on failure.
    """

    def __init__(self, **modes) -> None:
        self.modes = {k.upper(): v for k, v in modes.items()}
        self.saved: dict = {}

    @classmethod
    def all_off(cls) -> "Modes":
        return cls(**{d: "OFF" for d in detectors.DETECTOR_MODE})

    def __enter__(self) -> "Modes":
        self.saved = dict(detectors.DETECTOR_MODE)
        detectors.DETECTOR_MODE.update(self.modes)
        return self

    def __exit__(self, *exc) -> None:
        detectors.DETECTOR_MODE.clear()
        detectors.DETECTOR_MODE.update(self.saved)


# ---------------------------------------------------------------- #
#  comparison bases                                                #
# ---------------------------------------------------------------- #

def snap_bytes(book: Book) -> str:
    return json.dumps(book.snapshot(), sort_keys=True)


def sub_bytes(subs: list) -> str:
    return json.dumps(subs, sort_keys=True)


def state_view(book: Book) -> dict:
    """Everything a rejected event must leave untouched — deliberately
    EXCLUDING `seen`, `event_log` and `todo`, all three of which grow on a
    rejection by design (an id we have seen is an id we have seen)."""
    return {
        "balances": {f"{k}": str(v) for k, v in sorted(
            book.balances.items(), key=lambda kv: str(kv[0])) if v != 0},
        "lots": {str(i): [l["cid"], l["symbol"], str(l["qty"]),
                          str(l["cost_total"]), str(l["split_mult"])]
                 for i, l in sorted(book.lots.items())},
        "lot_index": {f"{k}": v for k, v in sorted(
            book.lot_index.items(), key=lambda kv: str(kv[0]))},
        "orders": {oid: [str(o["hold_rem"]), str(o["share_hold_rem"]),
                         str(o["filled_qty"]), o["placed"], o["closed"],
                         o["route"]]
                   for oid, o in sorted(book.orders.items())},
        "trades": {t: [v["side"], str(v["principal"]), v["cid"],
                       v["settled"]] for t, v in sorted(book.trades.items())},
        "fees": {f: [v["customer_id"], str(v["amount"])]
                 for f, v in sorted(book.fees.items())},
        "refunded": sorted(book.refunded),
        "withdrawals": {w: [v["customer_id"], str(v["amount"]), v["state"]]
                        for w, v in sorted(book.withdrawals.items())},
        "accounts_touched": sorted(book.accounts_touched),
        "customers_seen": sorted(book.customers_seen),
        "quarantine": [[str(x) for x in row] for row in book.quarantine],
    }


def pass_findings(book: Book) -> dict:
    """detector id -> set of event ids the DETECTOR PASS flagged (the
    non-replayed report_log channel). Snapshot bookkeeping also lands in
    report_log, so only the D-prefixed rows are ours."""
    out: dict = {}
    for row in book.report_log:
        if isinstance(row[0], str) and re.fullmatch(r"D\d+", row[0]):
            out.setdefault(row[0], set()).add(row[1])
    return out


def pass_modes(book: Book) -> set:
    return {row[4] for row in book.report_log
            if isinstance(row[0], str) and re.fullmatch(r"D\d+", row[0])}


def inline_findings(book: Book) -> dict:
    """The inline, replayed observations in book.quarantine, keyed by tag
    -> set of the event ids that carry them. The rows are tuples whose
    shape differs per tag; the event id is the only field they share, and
    it is always a `evt_`-prefixed member."""
    out: dict = {}
    for row in book.quarantine:
        ids = {x for x in row[1:] if isinstance(x, str) and x.startswith("evt")}
        out.setdefault(row[0], set()).update(ids)
    return out


def deliver_feed(feed: list, seed: int, rewinds: bool = True) -> tuple:
    book = Book()
    kw = {}
    if rewinds:
        kw["rewind_at"] = [len(feed) // 4, (3 * len(feed)) // 4]
    subs = sim.deliver(book, feed, seed=seed, **kw)
    return book, subs


# ---------------------------------------------------------------- #
#  1. armed catch                                                  #
# ---------------------------------------------------------------- #
class ArmedCatch(unittest.TestCase):
    """D1 and D3: a table lookup and an economic impossibility. Both hard
    reject. The injection rate is turned well above the arena's 1-2 % on
    purpose — this test measures a catch RATE, and a rate needs samples."""

    SEED = 4711
    N = 3000
    RATE = 0.25

    @classmethod
    def setUpClass(cls) -> None:
        base = sim.generate_defective(cls.SEED, cls.N,
                                      classes=("D1", "D3"), rate=cls.RATE)
        cls.ids = {k: list(v) for k, v in sim.DEFECT_LAST_STATS["ids"].items()}
        cls.feed = sim.corrupt(base, seed=cls.SEED)
        cls.book, cls.subs = deliver_feed(cls.feed, cls.SEED)
        cls.legs = {}
        for s in cls.subs:                 # first submission per id wins
            cls.legs.setdefault(s["event_id"], s["legs"])

    def test_injection_actually_happened(self):
        """A catch-rate test that injected nothing passes vacuously."""
        for det in ("D1", "D3"):
            self.assertGreater(len(self.ids[det]), 0,
                               f"{det} planted nothing — fixture is dead")

    def test_every_planted_defect_is_rejected_with_no_legs(self):
        for det in ("D1", "D3"):
            for eid in self.ids[det]:
                self.assertEqual(self.legs.get(eid), [],
                                 f"{det} {eid}: submitted legs, expected []")
                self.assertNotIn(eid, self.book.events,
                                 f"{det} {eid}: posted into the book")

    def test_book_identical_to_a_feed_without_those_events(self):
        """The rejection contract, end to end: a rejected event leaves the
        book exactly as a feed that never carried it would."""
        planted = {e for det in ("D1", "D3") for e in self.ids[det]}
        absent = [e for e in self.feed if e["event_id"] not in planted]
        other, _subs = deliver_feed(absent, self.SEED)
        self.assertEqual(snap_bytes(other), snap_bytes(self.book))
        self.assertEqual(json.dumps(state_view(other), sort_keys=True),
                         json.dumps(state_view(self.book), sort_keys=True))

    def test_rejected_ids_are_still_seen_and_answerable(self):
        """A rejected id stays rejected (R11) and stays as-of answerable:
        `seen` and the event log grow, the ledger does not."""
        for eid in self.ids["D1"][:5]:
            self.assertIn(eid, self.book.seen)
            self.assertIn(eid, self.book.eid_pos)
            self.assertEqual(self.book.snapshot(eid)["trial_balance"],
                             self.book._book_as_of(
                                 self.book.eid_pos[eid] + 1)
                             ._snapshot_now()["trial_balance"])


# ---------------------------------------------------------------- #
#  2. observe catch — and the inertness proof                      #
# ---------------------------------------------------------------- #
class ObserveCatch(unittest.TestCase):
    """D4/D5/D6/D9/D10/D11 run in the detector pass. Every one of them
    must be seen, and none of them may be felt."""

    SEED = 5711
    N = 3000
    RATE = 0.25
    CLASSES = ("D4", "D5", "D6", "D9", "D10", "D11")

    @classmethod
    def setUpClass(cls) -> None:
        base = sim.generate_defective(cls.SEED, cls.N, classes=cls.CLASSES,
                                      rate=cls.RATE)
        cls.ids = {k: list(v) for k, v in sim.DEFECT_LAST_STATS["ids"].items()}
        cls.feed = sim.corrupt(base, seed=cls.SEED)
        with Modes(**{d: "OBSERVE" for d in cls.CLASSES}):
            cls.on_book, cls.on_subs = deliver_feed(cls.feed, cls.SEED)
        # The control run turns OFF only the observe-mode detectors. Armed
        # ones (D1, D3, D8) keep firing in both runs: switching them off
        # would legitimately change submissions, which is the armed
        # detectors working — not observe mode leaking into the ledger.
        off = {d: ("OBSERVE" if d in cls.CLASSES else detectors.mode(d))
               for d in detectors.DETECTOR_MODE}
        off.update({d: "OFF" for d in cls.CLASSES})
        with Modes(**off):
            cls.off_book, cls.off_subs = deliver_feed(cls.feed, cls.SEED)
        cls.found = pass_findings(cls.on_book)

    def test_injection_actually_happened(self):
        for det in self.CLASSES:
            self.assertGreater(len(self.ids[det]), 0,
                               f"{det} planted nothing — fixture is dead")

    def test_every_planted_defect_is_observed(self):
        for det in self.CLASSES:
            planted = set(self.ids[det])
            missed = sorted(planted - self.found.get(det, set()))
            self.assertEqual(missed, [], f"{det} missed {len(missed)} of "
                                         f"{len(planted)}: {missed[:3]}")

    def test_observations_are_recorded_as_posted_not_rejected(self):
        self.assertEqual(pass_modes(self.on_book), {"OBSERVE"})
        for det in self.CLASSES:
            for eid in self.ids[det]:
                self.assertEqual(self.on_subs and
                                 next(s["legs"] for s in self.on_subs
                                      if s["event_id"] == eid),
                                 next(s["legs"] for s in self.off_subs
                                      if s["event_id"] == eid),
                                 f"{det} {eid}: observe changed the legs")

    def test_observe_mode_is_byte_identical_to_detectors_off(self):
        self.assertEqual(sub_bytes(self.on_subs), sub_bytes(self.off_subs))
        self.assertEqual(snap_bytes(self.on_book), snap_bytes(self.off_book))
        self.assertEqual(json.dumps(state_view(self.on_book), sort_keys=True),
                         json.dumps(state_view(self.off_book), sort_keys=True))

    def test_detectors_off_records_nothing(self):
        self.assertEqual(pass_findings(self.off_book), {})

    def test_findings_never_touch_replayed_state(self):
        """A finding lives in report_log, which is not pickled, not part of
        _STATE_KEYS and not in the ring — so a cold replay of the same log
        lands on the same state whether or not anything was flagged."""
        cold = Book()
        for logged in self.on_book.event_log:
            cold._apply_core(logged)
        self.assertEqual(json.dumps(state_view(cold), sort_keys=True),
                         json.dumps(state_view(self.on_book), sort_keys=True))
        for key in Book._STATE_KEYS:
            self.assertNotEqual(key, "report_log")


# ---------------------------------------------------------------- #
#  3. inline observe — D2 / D7 / D8                                #
# ---------------------------------------------------------------- #
class InlineObserveCatch(unittest.TestCase):
    """D2 (dividend net != gross - tax), D7 (reinvest net != price x qty)
    are observed at their handler's own site
    and recorded in book.quarantine — replayed state, written only on
    paths that post. Same 100 % requirement, different channel."""

    SEED = 6711
    N = 3000
    RATE = 0.25

    @classmethod
    def setUpClass(cls) -> None:
        base = sim.generate_defective(cls.SEED, cls.N,
                                      classes=("D2", "D7"),
                                      rate=cls.RATE)
        cls.ids = {k: list(v) for k, v in sim.DEFECT_LAST_STATS["ids"].items()}
        cls.feed = sim.corrupt(base, seed=cls.SEED)
        cls.book, cls.subs = deliver_feed(cls.feed, cls.SEED)
        cls.inline = inline_findings(cls.book)

    def test_injection_actually_happened(self):
        for det in ("D2", "D7"):
            self.assertGreater(len(self.ids[det]), 0,
                               f"{det} planted nothing — fixture is dead")

    def test_every_planted_defect_is_quarantined(self):
        for det, tag in (("D2", "D2"), ("D7", "D7")):
            planted = set(self.ids[det])
            missed = sorted(planted - self.inline.get(tag, set()))
            self.assertEqual(missed, [], f"{det} missed {len(missed)} of "
                                         f"{len(planted)}: {missed[:3]}")

    def test_inline_observations_still_post(self):
        legs = {}
        for s in self.subs:
            legs.setdefault(s["event_id"], s["legs"])
        for det in ("D2", "D7"):
            for eid in self.ids[det]:
                self.assertTrue(legs.get(eid),
                                f"{det} {eid}: observed AND rejected")
                self.assertIn(eid, self.book.events)


# ---------------------------------------------------------------- #
#  4. zero false positives on clean feeds                          #
# ---------------------------------------------------------------- #
class CleanFeedNoFalsePositives(unittest.TestCase):
    """Three seeds of generate_clean: every stream trap on, every planted
    defect repaired. The armed detectors must fire zero times, and the
    detector pass must reject nothing — proved by replaying the same feed
    with every detector OFF and diffing the submission streams byte for
    byte, which is the same evidence tools/detector_ab.py collects."""

    SEEDS = (7411, 7412, 7413)
    N = 3000
    # Every detector that is armed today, or that the phase plan calls
    # zero-false-positive by construction. D10 is excluded on purpose:
    # its false positives are real, expected, and measured below.
    MUST_BE_SILENT = ("D4", "D5", "D6", "D9", "D11")

    @classmethod
    def setUpClass(cls) -> None:
        cls.fp: dict = {}          # detector -> hits on events that POSTED
        cls.fp_all: dict = {}      # detector -> hits including rejects
        cls.armed_hits = 0
        cls.stream_diffs = 0
        cls.offline_d1 = cls.offline_d3 = 0
        cls.events = 0
        for seed in cls.SEEDS:
            feed = sim.corrupt(sim.generate_clean(seed, cls.N), seed=seed)
            cls.events += len(feed)
            book, subs = deliver_feed(feed, seed)
            with Modes.all_off():
                _off_book, off_subs = deliver_feed(feed, seed)
            if sub_bytes(subs) != sub_bytes(off_subs):
                cls.stream_diffs += 1
            for row in book.report_log:
                if not (isinstance(row[0], str)
                        and re.fullmatch(r"D\d+", row[0])):
                    continue
                det, eid, mode = row[0], row[1], row[4]
                cls.fp_all[det] = cls.fp_all.get(det, 0) + 1
                if eid in book.events:
                    cls.fp[det] = cls.fp.get(det, 0) + 1
                if mode == "ARMED":
                    cls.armed_hits += 1
            d1, d3 = cls._offline_armed(feed)
            cls.offline_d1 += d1
            cls.offline_d3 += d3

    @staticmethod
    def _offline_armed(feed: list) -> tuple:
        """D1 and D3 live inline in book.py, so they leave no report_log
        row to count. Re-derive them straight from the payloads: a clean
        feed must contain none at all."""
        d1 = d3 = 0
        for e in feed:
            p = e["payload"]
            if not isinstance(p, dict):
                continue
            if e["type"] in ("order_filled", "order_partially_filled"):
                broker = p.get("broker")
                if (broker in tariff.TARIFF
                        and not tariff.covers(broker, p.get("asset_class"))):
                    d1 += 1
            elif e["type"] == "interest_credited":
                try:
                    if (sim._parse_cents(p["customer_share"])
                            > sim._parse_cents(p["gross_amount"])):
                        d3 += 1
                except (KeyError, ValueError, TypeError):
                    pass
        return d1, d3

    def test_feeds_are_substantial(self):
        self.assertGreater(self.events, 3 * self.N)

    def test_no_armed_finding_anywhere(self):
        self.assertEqual(self.armed_hits, 0)
        self.assertEqual(self.offline_d1, 0, "D1 fired on clean data")
        self.assertEqual(self.offline_d3, 0, "D3 fired on clean data")

    def test_detector_pass_rejected_nothing(self):
        self.assertEqual(self.stream_diffs, 0,
                         "detectors changed a submission on a clean feed")

    def test_zero_false_positives_for_the_silent_detectors(self):
        for det in self.MUST_BE_SILENT:
            self.assertEqual(self.fp.get(det, 0), 0,
                             f"{det} false-positived on clean posted events "
                             f"({self.fp_all.get(det, 0)} hits in total)")

    def test_d5_only_ever_fires_on_events_the_book_rejects(self):
        """The clean feed keeps its malformed fills (principal '0.00' with
        a real quantity and price). D5 sees them — and every one of them is
        rejected on validation before it could ever post, so the finding
        costs nothing. That distinction is why the FP measure counts hits
        on POSTED events only."""
        self.assertEqual(self.fp.get("D5", 0), 0)

    def test_d10_false_positive_rate_is_measured_not_assumed(self):
        """Dividends legitimately arrive before the buy that creates the
        position. This is the evidence line for the NOTES.md defect-hunt
        log: D10 fires on clean data, so D10 never arms."""
        self.assertGreater(self.fp.get("D10", 0), 0,
                           "the clean feed lost its dividend-ordering trap — "
                           "D10's FP measurement is no longer meaningful")


# ---------------------------------------------------------------- #
#  5. zero FP under out-of-order delivery                          #
# ---------------------------------------------------------------- #
class OutOfOrderDeliveryZeroFP(unittest.TestCase):
    """D4 and D9 compare a fill against the FIRST-DELIVERED placement. A
    fill that beats its placement has nothing to compare against, so both
    return None — that is what makes them zero-FP by construction, and
    what this test pins down."""

    def fill(self, oid, tid, qty, price, principal, eid, final=False):
        return ev("order_filled" if final else "order_partially_filled",
                  {"order_id": oid, "trade_id": tid, "customer_id": "C-OOO",
                   "side": "buy", "symbol": "ACME", "quantity": qty,
                   "price": price, "principal": principal,
                   "broker": "BRK-A", "asset_class": "equity",
                   "partner_rate": "0.5"}, eid)

    def test_fill_before_placement_fires_neither_d4_nor_d9(self):
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C-OOO", "amount": "500000.00"}))
        # 100 shares at 500.00 — 10x the quantity and 50x the limit the
        # placement will turn out to carry. Delivered FIRST.
        early = self.fill("ord-OOO", "trd-OOO-1", "100", "500.00",
                          "50000.00", "evt-ooo-early")
        legs = b.apply(early)
        self.assertTrue(legs, "the early fill must still post — it is money")
        found = pass_findings(b)
        self.assertNotIn("evt-ooo-early", found.get("D4", set()))
        self.assertNotIn("evt-ooo-early", found.get("D9", set()))
        self.assertEqual(b.orders["ord-OOO"]["placed"], False)

        # The placement lands late: 10 shares, limit 10.00.
        b.apply(ev("order_placed",
                   {"order_id": "ord-OOO", "customer_id": "C-OOO",
                    "side": "buy", "symbol": "ACME", "quantity": "10",
                    "limit_price": "10.00", "asset_class": "equity",
                    "est_charges": "1.00"}, "evt-ooo-place"))
        self.assertTrue(b.orders["ord-OOO"]["placed"])

        # Now the very same shape of fill IS comparable — and must fire
        # both, or the skip above was a dead predicate, not a decision.
        late = self.fill("ord-OOO", "trd-OOO-2", "5", "500.00", "2500.00",
                         "evt-ooo-late")
        self.assertTrue(b.apply(late))
        found = pass_findings(b)
        self.assertIn("evt-ooo-late", found.get("D4", set()))
        self.assertIn("evt-ooo-late", found.get("D9", set()))

    def test_clean_feed_carries_real_out_of_order_placements(self):
        """The property is only worth testing if the FP suite's feeds
        actually exercise it."""
        sim.generate_clean(7411, 3000)
        self.assertGreater(sim.CLEAN_LAST_STATS["fill_before_placement"], 0)


# ---------------------------------------------------------------- #
#  6. known-FP sequences stay observe-only                         #
# ---------------------------------------------------------------- #
class KnownFalsePositivesStayObserve(unittest.TestCase):
    """Two sequences that are perfectly legal and fire a predicate. Each
    one is asserted twice: the finding exists (so the log line is there
    for the defect hunt), the legs post anyway — and then, armed, the same
    clean event is rejected, which is the full-weight loss the policy
    exists to refuse."""

    def dividend_before_buy(self, b: Book, eid: str) -> list:
        return b.apply(ev("dividend_cash",
                          {"customer_id": "C-DIV", "symbol": "ACME",
                           "gross_amount": "100.00",
                           "withholding_tax": "15.00",
                           "net_amount": "85.00"}, eid))

    def test_d10_dividend_before_buy_observes_and_posts(self):
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C-DIV", "amount": "1000.00"}))
        legs = self.dividend_before_buy(b, "evt-div-early")
        self.assertEqual(len(legs), 2, "a phantom dividend still posts")
        self.assertIn("evt-div-early", pass_findings(b).get("D10", set()))
        self.assertEqual(pass_modes(b), {"OBSERVE"})
        self.assertIn("evt-div-early", b.events)
        # The buy that the dividend preceded — after it, D10 goes quiet.
        b.apply(ev("order_filled",
                   {"order_id": "ord-DIV", "trade_id": "trd-DIV",
                    "customer_id": "C-DIV", "side": "buy", "symbol": "ACME",
                    "quantity": "10", "price": "10.00", "principal": "100.00",
                    "broker": "BRK-A", "asset_class": "equity",
                    "partner_rate": "0.5"}))
        self.dividend_before_buy(b, "evt-div-late")
        self.assertNotIn("evt-div-late", pass_findings(b).get("D10", set()))

    def test_d10_armed_would_reject_a_clean_event(self):
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C-DIV", "amount": "1000.00"}))
        with Modes(D10="ARMED"):
            legs = self.dividend_before_buy(b, "evt-div-armed")
        self.assertEqual(legs, [], "armed D10 should reject — that is the cost")
        self.assertNotIn("evt-div-armed", b.events)
        self.assertEqual(b.balances.get(("C-DIV", "1100")), D("1000.00"))

    def overdrawn_withdrawal(self, b: Book, eid: str) -> list:
        return b.apply(ev("withdrawal_requested",
                          {"customer_id": "C-FEE", "withdrawal_id": eid,
                           "amount": "50.00"}, eid))

    def test_d11_fee_overdrawn_wallet_observes_and_posts(self):
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C-FEE", "amount": "100.00"}))
        b.apply(ev("fee_charged", {"customer_id": "C-FEE",
                                   "amount": "500.00"}))
        self.assertEqual(-b.balances[("C-FEE", "2010")], D("-400.00"))
        legs = self.overdrawn_withdrawal(b, "evt-wd-obs")
        self.assertEqual(len(legs), 2, "an overdrawn withdrawal still posts")
        self.assertIn("evt-wd-obs", pass_findings(b).get("D11", set()))
        self.assertEqual(pass_modes(b), {"OBSERVE"})
        self.assertEqual(b.withdrawals["evt-wd-obs"]["state"], "requested")

    def test_d11_armed_would_reject_a_clean_event(self):
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C-FEE", "amount": "100.00"}))
        b.apply(ev("fee_charged", {"customer_id": "C-FEE",
                                   "amount": "500.00"}))
        with Modes(D11="ARMED"):
            legs = self.overdrawn_withdrawal(b, "evt-wd-armed")
        self.assertEqual(legs, [])
        self.assertNotIn("evt-wd-armed", b.withdrawals)
        self.assertNotIn("evt-wd-armed", b.events)


# ---------------------------------------------------------------- #
#  quarantine is a write-only side channel                         #
# ---------------------------------------------------------------- #
class QuarantineIsNotState(unittest.TestCase):
    """If anything ever read the quarantine file back, a live run and a
    replay of its own log would diverge, and every as-of answer and every
    crash recovery would quietly start lying. Two proofs: behavioural
    (identical books with logging on and off) and structural (nothing
    opens that path for reading, anywhere)."""

    SEED = 8411
    N = 1500

    def feed(self) -> list:
        return sim.corrupt(sim.generate_defective(self.SEED, self.N,
                                                  rate=0.2), seed=self.SEED)

    def test_logging_on_equals_logging_off(self):
        feed = self.feed()
        saved = detectors.QUARANTINE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quarantine.jsonl")
            try:
                detectors.QUARANTINE_PATH = path
                on_book, on_subs = deliver_feed(feed, self.SEED)
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            finally:
                detectors.QUARANTINE_PATH = saved
            off_book, off_subs = deliver_feed(feed, self.SEED)
            self.assertEqual(sub_bytes(on_subs), sub_bytes(off_subs))
            self.assertEqual(snap_bytes(on_book), snap_bytes(off_book))
            self.assertEqual(
                json.dumps(state_view(on_book), sort_keys=True),
                json.dumps(state_view(off_book), sort_keys=True))
            self.assertGreater(len(lines), 0, "nothing was logged at all")
            rec = json.loads(lines[0])
            self.assertEqual(sorted(rec), ["action", "detector", "event_id",
                                           "expected", "mode", "observed",
                                           "type"])
            self.assertTrue({r["action"] for r in map(json.loads, lines)}
                            <= {"posted", "rejected"})

    def test_replay_twice_with_logging_on_is_identical(self):
        feed = self.feed()
        saved = detectors.QUARANTINE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                detectors.QUARANTINE_PATH = os.path.join(tmp, "q.jsonl")
                a_book, a_subs = deliver_feed(feed, self.SEED)
                b_book, b_subs = deliver_feed(feed, self.SEED)
            finally:
                detectors.QUARANTINE_PATH = saved
        self.assertEqual(sub_bytes(a_subs), sub_bytes(b_subs))
        self.assertEqual(snap_bytes(a_book), snap_bytes(b_book))

    @staticmethod
    def _src(*parts) -> str:
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_no_handler_ever_reads_the_file(self):
        """The grep-assert. `QUARANTINE_PATH` may exist in exactly one
        module, that module may open it in exactly one mode, and no
        reading API may be applied to it anywhere."""
        det_src = self._src("detectors.py")
        book_src = self._src("book.py")
        self.assertNotIn("QUARANTINE", book_src,
                         "book.py names the quarantine file")
        opens = re.findall(r"open\(\s*([^)]*?)\)", det_src)
        self.assertEqual(len(opens), 1, f"detectors.py opens {len(opens)} "
                                        f"files, expected exactly 1")
        self.assertIn("QUARANTINE_PATH", opens[0])
        self.assertRegex(opens[0], r"""["']a["']""")
        for reader in (".read()", ".readline", ".readlines", "json.load(",
                       "for line in"):
            self.assertNotIn(reader, det_src,
                             f"detectors.py contains a read path: {reader}")
        # And nothing else in the shipped ledger even knows the path.
        for name in ("book.py", "client.py", "tariff.py"):
            self.assertNotIn("QUARANTINE_PATH", self._src(name),
                             f"{name} reads the path")
        for name in sorted(os.listdir(os.path.join(ROOT, "sim"))):
            if name.endswith(".py"):
                self.assertNotIn("QUARANTINE_PATH", self._src("sim", name),
                                 f"sim/{name}")

    def test_report_log_is_not_part_of_persisted_state(self):
        self.assertNotIn("report_log", Book._STATE_KEYS)
        b = Book()
        b.apply(ev("deposit", {"customer_id": "C-Q", "amount": "10.00"}))
        blob = b._dump_state()
        b.report_log.append(("D5", "evt-x", "1", "2", "OBSERVE"))
        b._load_state(blob)
        self.assertEqual(len(b.report_log), 1,
                         "a state restore must not resurrect or drop findings")


if __name__ == "__main__":
    unittest.main(verbosity=2)
