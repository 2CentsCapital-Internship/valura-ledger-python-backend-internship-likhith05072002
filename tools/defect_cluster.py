#!/usr/bin/env python3
"""The master move: make the planted defect class identify itself.

The arena rejects the events in its planted defect class, so every
practice response where WE submitted legs and the SERVER expected none is
a sample of that class — no guessing, no theory. This reads the JSONL
bank client.py writes on every run (`runs/<dir>/feed.jsonl` +
`postings.jsonl`), selects exactly those events, replays the feed to get
the book state each one saw, runs all eleven detector predicates over
them, and prints a detector x event-type cluster table.

If one detector lights up a column, that is the planted class and the
NOTES.md defect-hunt log has its evidence. If nothing lights up, the
defect is something no predicate models yet — which is worth knowing in
the same five seconds.

    python tools/defect_cluster.py runs/practice-20250806T101500Z
    python tools/defect_cluster.py runs/*/ --min-hits 2
    python tools/defect_cluster.py --self-test

Read-only on the bank. Never writes, never posts, never touches a Book
that a live run is using.

RESPONSE SCHEMA: PROTOCOL.md documents the posting REQUEST exactly and
says only that practice responses "tell you the correct legs and diff
them against yours" — the shape is not specified, and guessing wrong
means silently selecting nothing. So the verdict reader is deliberately
structural rather than literal: it walks the whole response tree for any
object carrying an `event_id`, and accepts an expected-empty verdict from
any of the field names a diffing API plausibly uses (an empty expected-
legs list, a zero expected-leg count, or a diagnostic that says no legs
are expected). Whatever the field is called, the cluster still forms.
`--dump-verdicts` prints what it actually understood, so a schema
surprise shows up as an empty selection with a visible reason.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import detectors  # noqa: E402
import tariff  # noqa: E402
from book import Book  # noqa: E402
from tariff import money  # noqa: E402

CENT = tariff.CENT
D = tariff.D

# Field names a per-event diff could plausibly use for "what we should
# have sent". Any of them holding an empty list means: no legs expected.
EXPECTED_LIST_KEYS = ("expected_legs", "expected", "correct_legs",
                      "legs_expected", "expected_postings", "correct",
                      "reference_legs", "should_be")
EXPECTED_COUNT_KEYS = ("expected_leg_count", "expected_legs_count",
                       "expected_count", "n_expected")
# Free-text diagnostics that say the same thing in prose.
EMPTY_PHRASES = ("no legs", "expected none", "expects none", "empty legs",
                 "should produce no legs", "should be empty",
                 "expected no postings")


# ------------------------------------------------------------------ #
#  the bank                                                          #
# ------------------------------------------------------------------ #
def _lines(path: str):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue          # a torn final line from a crash: skip it


def read_feed(run_dir: str) -> list[dict]:
    """Every ledger event the bank recorded, in delivery order — control
    frames and checkpoint requests dropped, redeliveries KEPT (the Book
    must see the stream exactly as the server sent it)."""
    out: list[dict] = []
    for rec in _lines(os.path.join(run_dir, "feed.jsonl")):
        ev = rec.get("data")
        if rec.get("event") in ("stream_open", "stream_reset", "stream_end"):
            continue
        if not isinstance(ev, dict) or not isinstance(ev.get("event_id"), str):
            continue
        if ev.get("type") == "checkpoint_request":
            continue
        out.append(ev)
    return out


def read_submissions(run_dir: str) -> dict:
    """event_id -> the legs WE submitted. First submission per id wins,
    exactly as the server scores it."""
    out: dict = {}
    for rec in _lines(os.path.join(run_dir, "postings.jsonl")):
        req = rec.get("request")
        if not isinstance(req, dict):
            continue
        for post in req.get("postings") or ():
            if isinstance(post, dict) and isinstance(post.get("event_id"), str):
                out.setdefault(post["event_id"], post.get("legs") or [])
    return out


def _walk(obj):
    """Every dict anywhere in a decoded response body."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _expects_empty(node: dict):
    """True / False / None (no opinion) for one per-event verdict node."""
    for key in EXPECTED_LIST_KEYS:
        val = node.get(key)
        if isinstance(val, list):
            return len(val) == 0
    for key in EXPECTED_COUNT_KEYS:
        val = node.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val == 0
    for val in node.values():
        if isinstance(val, str):
            low = val.lower()
            if any(phrase in low for phrase in EMPTY_PHRASES):
                return True
    return None


def read_verdicts(run_dir: str) -> dict:
    """event_id -> True when the server's response says that event should
    have produced no legs. First opinion per id wins."""
    out: dict = {}
    for rec in _lines(os.path.join(run_dir, "postings.jsonl")):
        for node in _walk(rec.get("response")):
            eid = node.get("event_id")
            if not isinstance(eid, str) or eid in out:
                continue
            verdict = _expects_empty(node)
            if verdict is not None:
                out[eid] = verdict
    return out


# ------------------------------------------------------------------ #
#  the eleven predicates, offline                                    #
# ------------------------------------------------------------------ #
# D4/D5/D6/D9/D10/D11 come straight from detectors.py — one
# implementation each, and the tool must score exactly what ships. D1,
# D2, D3, D7 and D8 are enforced or observed inline in book.py at their
# handler's site, where a registry entry would be a second copy; they are
# restated here as pure read-only predicates with the same arithmetic, so
# the cluster table can cover all eleven.

def d1_broker_class(p, book):
    broker, cls = p.get("broker"), p.get("asset_class")
    if broker not in tariff.TARIFF:
        return (str(broker), "a broker in the tariff table")
    if not tariff.covers(broker, cls):
        return (f"{broker}/{cls}", f"{broker} covers "
                                   f"{sorted(tariff.TARIFF[broker]['classes'])}")
    return None


def d2_dividend_identity(p, book):
    net = money(p["net_amount"])
    want = money(p["gross_amount"]) - money(p["withholding_tax"])
    return (str(net), str(want)) if net != want else None


def d3_interest_share(p, book):
    gross, share = money(p["gross_amount"]), money(p["customer_share"])
    return (str(share), f"<= {gross}") if share > gross else None


def d7_reinvest_identity(p, book):
    net = money(p["net_amount"])
    want = money(D(str(p["reinvest_price"])) * D(str(p["reinvest_quantity"])))
    return (str(net), str(want)) if abs(net - want) > CENT else None


def d8_duplicate_trade(p, book):
    tid = p.get("trade_id")
    return (str(tid), "an unused trade_id") if tid in book.trades else None


OFFLINE = {"D1": (("order_filled", "order_partially_filled"), d1_broker_class),
           "D2": (("dividend_cash",), d2_dividend_identity),
           "D3": (("interest_credited",), d3_interest_share),
           "D7": (("dividend_reinvested",), d7_reinvest_identity),
           "D8": (("order_filled", "order_partially_filled"),
                  d8_duplicate_trade)}


def all_predicates() -> dict:
    """detector id -> (event types, predicate), all eleven, in order."""
    merged = dict(OFFLINE)
    merged.update(detectors.DETECTORS)
    return {k: merged[k] for k in sorted(merged, key=lambda s: int(s[1:]))}


def probe(ev_type: str, payload, book) -> list:
    """Every detector that fires on this event, given this book state.
    Mode-blind on purpose: the cluster table is evidence for an arming
    decision, so it must see what a detector WOULD have said."""
    hits = []
    if not isinstance(payload, dict):
        return hits
    for det_id, (types, predicate) in all_predicates().items():
        if ev_type not in types:
            continue
        try:
            found = predicate(payload, book)
        except Exception:
            continue          # a malformed payload is not a defect sample
        if found:
            hits.append((det_id, found[0], found[1]))
    return hits


# ------------------------------------------------------------------ #
#  the cluster                                                       #
# ------------------------------------------------------------------ #
def cluster(run_dirs: list[str]) -> dict:
    """Replay each bank and tally detector x event-type over the events we
    posted legs for and the server expected none of."""
    table: dict = {}          # (detector, event type) -> count
    by_type: dict = {}        # event type -> candidates
    unexplained: dict = {}    # event type -> candidates no predicate fired on
    samples: dict = {}        # detector -> up to 3 (eid, observed, expected)
    totals = {"feed_events": 0, "submitted_with_legs": 0,
              "verdicts": 0, "expected_empty": 0, "candidates": 0,
              "runs": 0, "missing_feed": []}

    for run_dir in run_dirs:
        events = read_feed(run_dir)
        if not events:
            totals["missing_feed"].append(run_dir)
            continue
        totals["runs"] += 1
        submitted = read_submissions(run_dir)
        verdicts = read_verdicts(run_dir)
        totals["feed_events"] += len(events)
        totals["submitted_with_legs"] += sum(1 for v in submitted.values() if v)
        totals["verdicts"] += len(verdicts)
        totals["expected_empty"] += sum(1 for v in verdicts.values() if v)
        candidates = {eid for eid, legs in submitted.items()
                      if legs and verdicts.get(eid) is True}
        totals["candidates"] += len(candidates)

        book = Book()
        for ev in events:
            eid = ev.get("event_id")
            if eid in candidates and eid not in book.seen:
                t = str(ev.get("type"))
                by_type[t] = by_type.get(t, 0) + 1
                hits = probe(t, ev.get("payload"), book)
                for det_id, observed, expected in hits:
                    table[(det_id, t)] = table.get((det_id, t), 0) + 1
                    samples.setdefault(det_id, []).append(
                        (eid, observed, expected))
                if not hits:
                    unexplained[t] = unexplained.get(t, 0) + 1
            book.apply(ev)
    return {"table": table, "by_type": by_type, "unexplained": unexplained,
            "samples": samples, "totals": totals}


def render(result: dict, min_hits: int = 1) -> str:
    table, by_type = result["table"], result["by_type"]
    totals = result["totals"]
    lines = ["", "=" * 72,
             "DEFECT CLUSTER - events we posted, the server expected none",
             "=" * 72,
             f"banks read            {totals['runs']}",
             f"feed events           {totals['feed_events']}",
             f"submitted with legs   {totals['submitted_with_legs']}",
             f"per-event verdicts    {totals['verdicts']} "
             f"({totals['expected_empty']} expected-empty)",
             f"candidate defects     {totals['candidates']}"]
    if totals["missing_feed"]:
        lines.append(f"no feed.jsonl in      {totals['missing_feed']}")
    if not by_type:
        lines += ["", "  nothing selected. Either the run was clean, or the "
                      "response schema", "  carries its verdicts under field "
                      "names this reader does not know:",
                  "  re-run with --dump-verdicts to see what it understood.",
                  "=" * 72]
        return "\n".join(lines)

    types = sorted(by_type, key=lambda t: (-by_type[t], t))
    detectors_hit = sorted({d for d, _t in table},
                           key=lambda s: -sum(v for (dd, _t), v in table.items()
                                              if dd == s))
    width = max([10] + [len(t) for t in types])
    lines += ["", "  " + "detector".ljust(10)
              + "".join(t.rjust(width + 2) for t in types) + "     total"]
    lines.append("  " + "-" * (10 + (width + 2) * len(types) + 10))
    for det in detectors_hit:
        row = [table.get((det, t), 0) for t in types]
        total = sum(row)
        if total < min_hits:
            continue
        lines.append("  " + det.ljust(10)
                     + "".join(str(v or "-").rjust(width + 2) for v in row)
                     + str(total).rjust(10))
    lines.append("  " + "-" * (10 + (width + 2) * len(types) + 10))
    lines.append("  " + "candidates".ljust(10)
                 + "".join(str(by_type[t]).rjust(width + 2) for t in types)
                 + str(sum(by_type.values())).rjust(10))
    if result["unexplained"]:
        lines.append("  " + "no hit".ljust(10)
                     + "".join(str(result["unexplained"].get(t, 0) or "-")
                               .rjust(width + 2) for t in types)
                     + str(sum(result["unexplained"].values())).rjust(10))

    dom = dominant(result)
    if dom:
        lines += ["", f"  dominant cluster: {dom[0]} ({dom[1]} of "
                      f"{sum(by_type.values())} candidates explained)"]
    lines.append("")
    for det in detectors_hit[:3]:
        for eid, observed, expected in result["samples"].get(det, [])[:3]:
            lines.append(f"    {det}  {eid}  observed {observed}  "
                         f"expected {expected}")
    lines.append("=" * 72)
    return "\n".join(lines)


def dominant(result: dict):
    """(detector, hits) for the detector explaining the most candidates,
    or None. This is the answer the master move exists to produce. A tie
    breaks toward the lower-numbered detector — D1-D3 are exact
    identities, so when two predicates explain a cluster equally well the
    specific one is the better story."""
    per_det: dict = {}
    for (det, _t), count in result["table"].items():
        per_det[det] = per_det.get(det, 0) + count
    if not per_det:
        return None
    best = min(per_det, key=lambda d: (-per_det[d], int(d[1:])))
    return best, per_det[best]


# ------------------------------------------------------------------ #
#  self-test                                                         #
# ------------------------------------------------------------------ #
def _synthetic_bank(path: str) -> None:
    """A bank with three posted-but-expected-empty dividend_cash events
    whose net breaks gross - withholding_tax (D2), plus clean traffic the
    server was happy with, plus one expected-empty event no predicate
    models — so "dominant" has to mean something.

    The customer BUYS the symbol first, on purpose: without a position
    every one of those dividends would also trip D10 (phantom dividend),
    and a cluster rig that cannot tell two co-firing detectors apart is
    not evidence for anything. Only the selection logic is exercised
    here, so the banked legs are stand-ins, not graded postings.
    """
    os.makedirs(path, exist_ok=True)
    feed, postings, verdicts = [], [], []

    def add(ev, legs, expected):
        feed.append({"ts": "t", "event": "event", "data": ev})
        postings.append({"event_id": ev["event_id"], "legs": legs})
        verdicts.append({"event_id": ev["event_id"], "expected_legs": expected,
                         "match": legs == expected})

    two = [{"account": "1100", "customer_id": "C1", "debit": "85.00",
            "credit": "0.00"},
           {"account": "2010", "customer_id": "C1", "debit": "0.00",
            "credit": "85.00"}]
    for i in range(6):
        add({"offset": len(feed), "event_id": f"evt-ok-{i}", "type": "deposit",
             "payload": {"customer_id": "C1", "amount": "85.00"}}, two, two)
    add({"offset": len(feed), "event_id": "evt-buy", "type": "order_filled",
         "payload": {"order_id": "ord-1", "trade_id": "trd-1",
                     "customer_id": "C1", "side": "buy", "symbol": "ACME",
                     "quantity": "10", "price": "10.00",
                     "principal": "100.00", "broker": "BRK-A",
                     "asset_class": "equity", "partner_rate": "0.5"}},
        two, two)
    for i in range(3):                       # the planted class
        add({"offset": len(feed), "event_id": f"evt-d2-{i}",
             "type": "dividend_cash",
             "payload": {"customer_id": "C1", "symbol": "ACME",
                         "gross_amount": "100.00", "withholding_tax": "15.00",
                         "net_amount": f"8{i}.00"}}, two, [])
    add({"offset": len(feed), "event_id": "evt-mystery",   # no predicate
         "type": "transfer_between_customers",
         "payload": {"from_customer_id": "C1", "to_customer_id": "C2",
                     "amount": "5.00"}},
        [{"account": "2010", "customer_id": "C1", "debit": "5.00",
          "credit": "0.00"},
         {"account": "2010", "customer_id": "C2", "debit": "0.00",
          "credit": "5.00"}], [])

    with open(os.path.join(path, "feed.jsonl"), "w", encoding="utf-8") as fh:
        for rec in feed:
            fh.write(json.dumps(rec) + "\n")
    with open(os.path.join(path, "postings.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "t", "request": {"postings": postings},
                             "status": 200,
                             "response": {"results": verdicts}}) + "\n")


def self_test(verbose: bool = True) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = os.path.join(tmp, "practice-selftest")
        _synthetic_bank(run_dir)
        result = cluster([run_dir])
        if verbose:
            print(render(result))
        checks = [
            ("4 candidates selected", result["totals"]["candidates"] == 4),
            ("clean events not selected",
             all(not k[1] == "deposit" for k in result["table"])),
            ("D2 hit 3 dividend_cash events",
             result["table"].get(("D2", "dividend_cash")) == 3),
            ("D2 is the dominant cluster", dominant(result) == ("D2", 3)),
            ("no other detector co-fired",
             sorted({d for d, _t in result["table"]}) == ["D2"]),
            ("the unmodelled candidate is reported",
             result["unexplained"].get("transfer_between_customers") == 1),
        ]
    ok = all(passed for _name, passed in checks)
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"  self-test: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="*",
                    help="banked run directories (runs/<dir>/), globs ok")
    ap.add_argument("--min-hits", type=int, default=1,
                    help="hide detectors with fewer total hits")
    ap.add_argument("--dump-verdicts", action="store_true",
                    help="print the per-event verdicts the reader understood")
    ap.add_argument("--self-test", action="store_true",
                    help="run on a synthetic bank with 3 planted D2 events")
    a = ap.parse_args()

    if a.self_test:
        return 0 if self_test() else 1
    dirs: list[str] = []
    for pattern in a.run_dirs:
        dirs.extend(sorted(glob.glob(pattern)) or [pattern])
    if not dirs:
        ap.error("give at least one banked run directory, or --self-test")
    if a.dump_verdicts:
        for run_dir in dirs:
            verdicts = read_verdicts(run_dir)
            submitted = read_submissions(run_dir)
            print(f"\n{run_dir}: {len(verdicts)} verdicts understood")
            for eid, empty in sorted(verdicts.items())[:40]:
                print(f"  {eid:<28} expected_empty={empty!s:<5} "
                      f"we_sent={len(submitted.get(eid, []))} legs")
    print(render(cluster(dirs), a.min_hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
