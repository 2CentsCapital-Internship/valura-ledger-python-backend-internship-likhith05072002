#!/usr/bin/env python3
"""Read a banked run directory and say exactly where the points went.

    python tools/run_report.py runs/p2
    python tools/run_report.py runs/p1 runs/p2      # compare two runs

Every graded attempt is expensive, so the whole point is to extract every
answerable question from one bank: which events the reference disagreed
with, which of those it wanted empty (its planted-defect class), which
checkpoint parts lost score, and which customers and accounts drove it.
"""
from __future__ import annotations

import collections
import json
import os
import sys


def load(run_dir: str) -> dict:
    def lines(name):
        path = os.path.join(run_dir, name)
        if not os.path.exists(path):
            return []
        out = []
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass            # a torn last line from a kill
        return out

    feed, ctrl = {}, []
    for r in lines("feed.jsonl"):
        ev = r.get("data")
        if r.get("event") in ("stream_open", "stream_reset", "stream_end"):
            ctrl.append((r.get("event"), ev))
            continue
        if isinstance(ev, dict) and ev.get("event_id"):
            feed.setdefault(ev["event_id"], ev)

    sent, verdict = {}, {}
    for r in lines("postings.jsonl"):
        for p in (r.get("request") or {}).get("postings", []):
            sent.setdefault(p["event_id"], p["legs"])
        resp = r.get("response")
        if isinstance(resp, dict):
            for res in resp.get("results", []):
                # A later "duplicate: true" must not overwrite the graded
                # verdict of the first submission.
                if res.get("duplicate"):
                    continue
                verdict[res["event_id"]] = res
    return {"dir": run_dir, "feed": feed, "sent": sent, "verdict": verdict,
            "ctrl": ctrl, "checkpoints": lines("checkpoints.jsonl")}


def report(run: dict) -> None:
    feed, verdict, sent = run["feed"], run["verdict"], run["sent"]
    print("=" * 74)
    print(f"RUN {run['dir']}")
    print("=" * 74)

    graded = len(verdict)
    right = sum(1 for v in verdict.values() if v.get("correct") is True)
    wrong = [(eid, v) for eid, v in verdict.items() if v.get("correct") is False]
    print(f"events graded {graded}   correct {right}   "
          f"incorrect {len(wrong)}   ({right / max(graded,1):.2%})")

    # Which of the disagreements are "the reference wanted NOTHING"? Those
    # are its rejects — the planted defect class plus anything we should
    # have refused on its merits.
    by_type = collections.Counter()
    want_empty = collections.Counter()
    for eid, v in wrong:
        t = feed.get(eid, {}).get("type", "?")
        by_type[t] += 1
        if v.get("missing") == [] and v.get("unexpected"):
            want_empty[t] += 1
    if wrong:
        print("\nDISAGREEMENTS BY EVENT TYPE   (want-empty = reference rejected it)")
        for t, n in by_type.most_common():
            print(f"  {t:<26} {n:>4}   want-empty {want_empty[t]:>4}")

    # Settlement amounts are state-dependent: if they are wrong, an
    # upstream fee is wrong. Worth calling out separately.
    settle = [(eid, v) for eid, v in wrong
              if feed.get(eid, {}).get("type", "").endswith(
                  ("_settled", "_remitted", "_payout"))]
    if settle:
        print(f"\nSETTLEMENT MISMATCHES ({len(settle)}) — these mean an "
              f"upstream fee accrual is off:")
        for eid, v in settle[:6]:
            legs = sent.get(eid, [])
            amt = legs[0]["debit"] if legs else "-"
            print(f"  {feed[eid]['type']:<24} we paid {amt:>10}   "
                  f"accounts_differ={v.get('accounts_differ')}")

    cps = [c for c in run["checkpoints"]
           if isinstance(c.get("response"), dict)
           and "diff" in c["response"]]
    if cps:
        print(f"\nCHECKPOINTS ({len(cps)})")
        agg = collections.defaultdict(list)
        for c in cps:
            resp = c["response"]
            for k, v in resp["diff"]["parts"].items():
                agg[k].append(v)
            print(f"  {resp['checkpoint_id']:<14} score {resp['score']:.4f}"
                  f"  on_time={resp.get('on_time')}")
        print("\n  MEAN BY PART (1.0 is perfect):")
        for k, vals in sorted(agg.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
            mean = sum(vals) / len(vals)
            bar = "#" * int(mean * 30)
            print(f"    {k:<22} {mean:.4f}  {bar}")
        # Who keeps being wrong?
        cust = collections.Counter(); accts = collections.Counter()
        for c in cps:
            d = c["response"]["diff"]
            for cid, fields in (d.get("customers") or {}).items():
                for f in fields:
                    cust[f"{cid} {f.split(':')[0]}"] += 1
            for a in d.get("trial_balance_accounts") or []:
                accts[a] += 1
        if cust:
            print("\n  MOST-WRONG CUSTOMER FIELDS:")
            for k, n in cust.most_common(8):
                print(f"    {k:<34} wrong in {n}/{len(cps)} checkpoints")
        if accts:
            print("\n  MOST-WRONG TRIAL-BALANCE ACCOUNTS:")
            for k, n in accts.most_common(10):
                print(f"    {k:<8} wrong in {n}/{len(cps)} checkpoints")


def main() -> int:
    dirs = sys.argv[1:] or ["runs/p1"]
    runs = [load(d) for d in dirs]
    for r in runs:
        report(r)
    if len(runs) == 2:
        a, b = runs
        print("\n" + "=" * 74)
        print("COMPARISON")
        print("=" * 74)
        for r in (a, b):
            g = len(r["verdict"])
            ok = sum(1 for v in r["verdict"].values() if v.get("correct") is True)
            cps = [c["response"] for c in r["checkpoints"]
                   if isinstance(c.get("response"), dict) and "diff" in c["response"]]
            mean_cp = sum(c["score"] for c in cps) / max(len(cps), 1)
            print(f"  {r['dir']:<12} postings {ok}/{g} ({ok/max(g,1):.2%})   "
                  f"mean checkpoint {mean_cp:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
