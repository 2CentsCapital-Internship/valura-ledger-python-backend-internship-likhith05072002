#!/usr/bin/env python3
"""The deployment rule, executable.

A detector may only be ARMED for a graded run when it has shown ZERO
attributable disagreements across at least two full practice feeds. This
replays one banked feed twice — candidate detector ARMED, then OFF — and
diffs the two submission streams event by event.

    python tools/detector_ab.py runs/practice-A runs/practice-B --arm D2
    python tools/detector_ab.py runs/practice-* --arm D5,D6
    python tools/detector_ab.py --self-test

A disagreement is ATTRIBUTABLE when the ARMED run recorded a finding for
that very event: the detector rejected it, on purpose, and we can say
which one and why. A disagreement with no finding on it is DOWNSTREAM
fallout — the rejected event's absence changed a later event's outcome —
and is reported separately, because it means the blast radius is bigger
than the events the detector touched.

The verdict is deliberately blunt: any disagreement at all, of either
kind, means DO NOT ARM. A false positive costs a clean event's full
weight; the miss it would have prevented is a quarter-weight no-leg
event. There is no disagreement budget worth spending.

Both runs turn every OTHER detector OFF, so the diff cannot be
contaminated: whatever moved, the candidate moved it. Observe mode is
inert (tests/test_detectors.py proves it byte for byte), so this is the
same comparison as candidate-armed vs shipped-defaults, with one fewer
thing that can go wrong.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import book as book_module  # noqa: E402
import detectors  # noqa: E402
from book import Book  # noqa: E402
from defect_cluster import OFFLINE, read_feed  # noqa: E402

# Five detectors do not live in the DETECTOR_MODE-driven pass at all —
# they are enforced or observed inline in book.py at their handler's own
# validation site, and the mode table does not switch them. The rig says
# so out loud rather than silently reporting a vacuous zero.
#
# D2 is the exception that matters: book.ARM_D2 is its documented
# one-line policy flip, and D2 is the arming decision the practice bank
# exists to settle, so the rig drives THAT switch for it.
INLINE_NOT_SWITCHABLE = {
    "D1": "always armed inline (book._fill) - cannot be disarmed here",
    "D3": "always armed inline (book.on_interest_credited) - ditto",
    "D7": "observe-only inline (book.on_dividend_reinvested)",
    "D8": "observe-only inline (book._fill)",
}
# Pure-payload predicates, usable for attribution after the fact — the
# inline reject path records nothing, so a D2 rejection has to be
# recognised by re-deriving it from the event itself.
ATTRIBUTABLE_OFFLINE = ("D1", "D2", "D3", "D7")


def _order(dets) -> list:
    return sorted(dets, key=lambda d: int(d[1:]))


def replay(events: list[dict], modes: dict) -> tuple[list, Book]:
    """Drive a fresh Book through the feed under an exact mode table, and
    return (submissions, book). Both the table and book.ARM_D2 are
    restored afterwards — this tool must never leave a process with a
    rewritten arming policy."""
    saved = dict(detectors.DETECTOR_MODE)
    saved_d2 = book_module.ARM_D2
    try:
        detectors.DETECTOR_MODE.clear()
        detectors.DETECTOR_MODE.update(modes)
        book_module.ARM_D2 = modes.get("D2") == "ARMED"
        book = Book()
        subs = [{"event_id": ev.get("event_id"), "legs": book.apply(ev)}
                for ev in events]
    finally:
        detectors.DETECTOR_MODE.clear()
        detectors.DETECTOR_MODE.update(saved)
        book_module.ARM_D2 = saved_d2
    return subs, book


def armed_findings(book: Book) -> dict:
    """event_id -> the detectors that ARMED-rejected it in this run."""
    out: dict = {}
    for row in book.report_log:
        if len(row) == 5 and row[4] == "ARMED":
            out.setdefault(row[1], set()).add(row[0])
    return out


def _offline_hit(det: str, ev: dict) -> bool:
    """Would this candidate's pure-payload predicate have fired here? Used
    only to attribute the inline rejects, which leave no finding behind."""
    types, predicate = OFFLINE[det]
    if ev.get("type") not in types or not isinstance(ev.get("payload"), dict):
        return False
    try:
        return predicate(ev["payload"], None) is not None
    except Exception:
        return False


def ab(events: list[dict], candidates: list[str]) -> dict:
    """One feed, two replays, one diff."""
    candidates = _order(candidates)
    base = {d: "OFF" for d in detectors.DETECTOR_MODE}
    on = dict(base, **{d: "ARMED" for d in candidates})
    armed_subs, armed_book = replay(events, on)
    off_subs, _off_book = replay(events, base)

    per_det = {d: 0 for d in candidates}
    offline_only = [d for d in candidates if d in ATTRIBUTABLE_OFFLINE]
    by_id = {ev.get("event_id"): ev for ev in events}
    downstream: list = []
    findings = armed_findings(armed_book)
    diffs = 0
    for a, b in zip(armed_subs, off_subs):
        if json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True):
            continue
        diffs += 1
        hit = findings.get(a["event_id"], set()) & set(candidates)
        if not hit:
            ev = by_id.get(a["event_id"], {})
            hit = {d for d in offline_only if _offline_hit(d, ev)}
        if hit:
            for det in sorted(hit):
                per_det[det] += 1
        else:
            downstream.append(a["event_id"])
    return {"events": len(events), "diffs": diffs, "per_detector": per_det,
            "downstream": downstream,
            "armed_findings": sum(len(v) for v in findings.values())}


def render(rows: list[tuple[str, dict]], candidates: list[str]) -> str:
    candidates = _order(candidates)
    out = ["", "=" * 72,
           f"DETECTOR A/B - {','.join(candidates) or '(none)'} ARMED vs OFF",
           "=" * 72,
           f"{'feed':<34}{'events':>9}{'diffs':>8}{'attributable':>14}"
           f"{'downstream':>12}"]
    total = {d: 0 for d in candidates}
    total_down = total_diff = total_events = 0
    for name, r in rows:
        attributable = sum(r["per_detector"].values())
        out.append(f"{name[-34:]:<34}{r['events']:>9}{r['diffs']:>8}"
                   f"{attributable:>14}{len(r['downstream']):>12}")
        for det in candidates:
            total[det] += r["per_detector"][det]
        total_down += len(r["downstream"])
        total_diff += r["diffs"]
        total_events += r["events"]
    out.append("-" * 72)
    out.append(f"{'TOTAL (' + str(len(rows)) + ' feeds)':<34}"
               f"{total_events:>9}{total_diff:>8}"
               f"{sum(total.values()):>14}{total_down:>12}")
    out.append("")
    for det in candidates:
        note = INLINE_NOT_SWITCHABLE.get(det)
        out.append(f"  {det:<6} attributable disagreements: {total[det]}"
                   + (f"   NOTE: {note}" if note else ""))
    if total_down:
        head = [e for _n, r in rows for e in r["downstream"]][:5]
        out.append(f"  downstream (no finding on the event): {total_down}"
                   f"  e.g. {head}")
    enough = len(rows) >= 2
    clean = total_diff == 0
    out += ["", f"  feeds checked: {len(rows)} "
                f"({'meets' if enough else 'BELOW'} the >= 2 rule)",
            f"  VERDICT: {'ARM' if (clean and enough) else 'DO NOT ARM'}"
            + ("" if clean else " - a disagreement is a clean event lost"),
            "=" * 72]
    return "\n".join(out)


# ------------------------------------------------------------------ #
#  self-test                                                         #
# ------------------------------------------------------------------ #
def _synthetic_feed() -> list[dict]:
    """A short, entirely clean feed with exactly ONE planted D5 event: a
    fill whose principal is a cent off money(quantity x price).

    The planted fill is deliberately terminal — nothing settles its
    trade, nothing sells its lot — so arming D5 can produce exactly one
    disagreement and no downstream fallout. That is the point of the
    fixture: if the rig reported 2, it would be counting cascades as
    detector hits.
    """
    def ev(i, etype, payload):
        return {"offset": i, "event_id": f"evt-ab-{i}", "type": etype,
                "payload": payload}

    def fill(i, oid, tid, principal):
        return ev(i, "order_filled",
                  {"order_id": oid, "trade_id": tid, "customer_id": "C1",
                   "side": "buy", "symbol": "ACME", "quantity": "10",
                   "price": "10.00", "principal": principal,
                   "broker": "BRK-A", "asset_class": "equity",
                   "partner_rate": "0.5"})

    def placed(i, oid):
        return ev(i, "order_placed",
                  {"order_id": oid, "customer_id": "C1", "side": "buy",
                   "symbol": "ACME", "quantity": "10", "limit_price": "10.00",
                   "asset_class": "equity", "est_charges": "1.00"})

    return [ev(0, "deposit", {"customer_id": "C1", "amount": "5000.00"}),
            placed(1, "ord-1"),
            fill(2, "ord-1", "trd-1", "100.00"),        # clean
            ev(3, "trade_settled", {"trade_id": "trd-1"}),
            ev(4, "fee_charged", {"customer_id": "C1", "amount": "3.00"}),
            placed(5, "ord-2"),
            fill(6, "ord-2", "trd-2", "100.01"),        # THE planted D5
            ev(7, "deposit", {"customer_id": "C2", "amount": "10.00"}),
            ev(8, "transfer_between_customers",
               {"from_customer_id": "C1", "to_customer_id": "C2",
                "amount": "1.00"})]


def _synthetic_d2_feed() -> list[dict]:
    """One dividend whose net breaks gross - withholding_tax, and one that
    holds it. D2 rejects inline via book.ARM_D2, not through the mode
    table, so this fixture is what proves the rig drives the switch that
    actually exists for the one arming decision still open."""
    def ev(i, etype, payload):
        return {"offset": i, "event_id": f"evt-d2-{i}", "type": etype,
                "payload": payload}

    def div(i, net):
        return ev(i, "dividend_cash",
                  {"customer_id": "C1", "symbol": "ACME",
                   "gross_amount": "100.00", "withholding_tax": "15.00",
                   "net_amount": net})

    return [ev(0, "deposit", {"customer_id": "C1", "amount": "500.00"}),
            div(1, "85.00"),                 # identity holds
            div(2, "84.00"),                 # THE planted D2
            ev(3, "fee_charged", {"customer_id": "C1", "amount": "1.00"})]


def self_test(verbose: bool = True) -> bool:
    feed = _synthetic_feed()
    candidates = _order(detectors.DETECTOR_MODE)
    result = ab(feed, candidates)
    if verbose:
        print(render([("synthetic (1 planted D5)", result)], candidates))
    others = {d: n for d, n in result["per_detector"].items()
              if d != "D5" and n}
    checks = [
        ("exactly 1 attributable disagreement for D5",
         result["per_detector"].get("D5") == 1),
        ("0 for every other detector", others == {}),
        ("no downstream fallout", result["downstream"] == []),
        ("1 disagreement in total", result["diffs"] == 1),
        ("the planted event is the one that moved",
         ab(feed, ["D5"])["diffs"] == 1),
        ("an all-OFF A/B against itself is silent",
         ab(feed, [])["diffs"] == 0),
        ("the inline D2 switch is driven and attributed",
         ab(_synthetic_d2_feed(), ["D2"])["per_detector"] == {"D2": 1}),
        ("D2 left disarmed after the rig runs",
         book_module.ARM_D2 is False),
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
    ap.add_argument("--arm", default="D2",
                    help="comma-separated candidate detectors (default D2)")
    ap.add_argument("--self-test", action="store_true",
                    help="run on a synthetic feed with one planted D5 event")
    a = ap.parse_args()

    if a.self_test:
        return 0 if self_test() else 1
    candidates = [d.strip().upper() for d in a.arm.split(",") if d.strip()]
    unknown = [d for d in candidates if d not in detectors.DETECTOR_MODE]
    if unknown:
        ap.error(f"unknown detector(s): {unknown}")
    dirs: list[str] = []
    for pattern in a.run_dirs:
        dirs.extend(sorted(glob.glob(pattern)) or [pattern])
    if not dirs:
        ap.error("give at least one banked run directory, or --self-test")

    rows = []
    for run_dir in dirs:
        events = read_feed(run_dir)
        if not events:
            print(f"  skipping {run_dir}: no feed.jsonl", file=sys.stderr)
            continue
        rows.append((run_dir, ab(events, candidates)))
    if not rows:
        print("no banked feeds could be read", file=sys.stderr)
        return 1
    print(render(rows, candidates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
