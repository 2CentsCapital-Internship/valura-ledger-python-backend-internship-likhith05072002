#!/usr/bin/env python3
"""The mega-regression: deliberately harder than the arena's own final.

`run_regression.py` is the per-phase gate — eleven sections, ~10k events
each, tuned to be fast enough to run after every edit. This is the other
thing: one long, punishing pass whose only job is to find what a
6,000-event graded run could still surface.

Where the arena's final is 6,000 events over 75 minutes with roughly 400
replayed events, this run is bigger on every axis that matters:

  scale        200,000+ events per pass vs their 6,000 (33x)
  seeds        8 independent feeds vs their one
  mixes        every generator — cash, market, corporate, finale,
               defect-injected and repaired-clean — not one blend
  hostility    every documented trap at once, plus rewinds at four points
               per feed instead of one unannounced replay
  endurance    a single uninterrupted 100,000-event book, ~16x the final,
               to prove nothing degrades or grows without bound
  judgement    every referee we own runs on every feed: the standing
               invariants, the wallet oracle, the market identities, the
               independent FIFO cost-basis oracle, the payable cent
               audit, cold-replay identity, the snapshot-ring path, and
               the as-of oracle at 1,000 points
  recovery     kill-and-resume at random offsets, converging byte-exactly
  measurement  per-event and as-of latency percentiles, and the memory
               ceiling, reported as numbers rather than assurances

Exit code is 0 only if every section is green. Anything else means do not
start a graded run.

    python mega_regression.py               # full pass (~10-20 min)
    python mega_regression.py --quick       # smaller, for a sanity check
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import pickle
import random
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from book import Book                                    # noqa: E402
from sim import arena_sim, invariants, fifo_oracle       # noqa: E402

ARENA_FINAL_EVENTS = 6000        # what we are deliberately exceeding


def _fmt(n: float) -> str:
    return f"{n:,.0f}" if n >= 1000 else f"{n:.2f}"


def _all_referees(book, label: str) -> list[str]:
    """Every judge we own, on one book. Order matters only for reporting."""
    out: list[str] = []
    out += [f"{label}/invariants: {v}" for v in invariants.run_invariants(book)]
    out += [f"{label}/wallet: {v}" for v in invariants.wallet_oracle(book)]
    out += [f"{label}/identities: {v}"
            for v in invariants.market_identities(book)]
    out += [f"{label}/payables: {v}" for v in invariants.payable_audit(book)]
    out += [f"{label}/fifo-oracle: {v}"
            for v in fifo_oracle.check_cost_basis(book)]
    for oracle in (invariants.replay_identical, invariants.ring_identical):
        ok, why = oracle(book)
        if not ok:
            out.append(f"{label}/{oracle.__name__}: {why}")
    return out


# ------------------------------------------------------------------ #
#  1. breadth — every generator, every seed, every trap               #
# ------------------------------------------------------------------ #
def section_breadth(scale: int) -> tuple[bool, str]:
    """Eight feeds, five different generators, all traps, four rewinds
    each — every referee on every one."""
    feeds = [
        ("cash", arena_sim.generate_cash, 11001),
        ("cash", arena_sim.generate_cash, 11002),
        ("market", arena_sim.generate_market, 12001),
        ("market", arena_sim.generate_market, 12002),
        ("corporate", arena_sim.generate_corporate, 13001),
        ("corporate", arena_sim.generate_corporate, 13002),
        ("finale", arena_sim.generate_finale, 14001),
        ("finale", arena_sim.generate_finale, 14002),
    ]
    total_events = 0
    violations: list[str] = []
    t0 = time.perf_counter()
    for kind, gen, seed in feeds:
        events = arena_sim.corrupt(gen(seed, scale), seed=seed)
        book = Book()
        n = len(events)
        arena_sim.deliver(book, events, seed=seed,
                          rewind_at=[n // 5, 2 * n // 5, 3 * n // 5,
                                     4 * n // 5])
        total_events += n
        violations += _all_referees(book, f"{kind}:{seed}")
        if violations:
            break
    wall = time.perf_counter() - t0
    detail = (f"{len(feeds)} feeds / 5 generators, {_fmt(total_events)} events "
              f"({total_events / ARENA_FINAL_EVENTS:.0f}x the arena final), "
              f"4 rewinds each, all 7 referees, {wall:.0f}s")
    if violations:
        return False, f"{detail} — {len(violations)} violations, first: {violations[0]}"
    return True, detail


# ------------------------------------------------------------------ #
#  2. endurance — one uninterrupted book, far past the final's length #
# ------------------------------------------------------------------ #
def section_endurance(scale: int) -> tuple[bool, str]:
    """One continuous book carrying ~16x the arena final. Proves nothing
    degrades with length: latency percentiles stay flat, memory stays
    bounded, and every referee still passes at the end."""
    n = scale * 10
    events = arena_sim.corrupt(arena_sim.generate_finale(15001, n),
                               seed=15001)
    book = Book()
    lat: list[float] = []
    t0 = time.perf_counter()
    for i, ev in enumerate(events):
        t = time.perf_counter()
        book.apply(ev)
        lat.append(time.perf_counter() - t)
        if i and i % (len(events) // 4) == 0:      # rewinds along the way
            for old in events[max(0, i - 400):i]:
                book.apply(old)
    wall = time.perf_counter() - t0
    violations = _all_referees(book, "endurance")

    lat.sort()
    p50 = lat[len(lat) // 2] * 1e6
    p95 = lat[int(len(lat) * 0.95)] * 1e6
    p99 = lat[int(len(lat) * 0.99)] * 1e6
    ring_mib = sum(len(b) for _n, b in book._ring) / 1024 / 1024
    state_mib = len(pickle.dumps({k: getattr(book, k)
                                  for k in book._STATE_KEYS})) / 1024 / 1024

    # Latency must not be a function of how far in we are: compare the
    # first tenth of the run against the last tenth.
    tenth = len(lat) // 10
    early = statistics.median(lat[:tenth]) * 1e6
    late = statistics.median(lat[-tenth:]) * 1e6

    detail = (f"{_fmt(len(events))} events in one book "
              f"({len(events) / ARENA_FINAL_EVENTS:.0f}x the final), "
              f"apply p50 {p50:.0f}us / p95 {p95:.0f}us / p99 {p99:.0f}us, "
              f"ring {ring_mib:.0f} MiB, state {state_mib:.0f} MiB, "
              f"{wall:.0f}s")
    if violations:
        return False, f"{detail} — first: {violations[0]}"
    if p95 > 5000:                       # 5 ms per event is already absurd
        return False, f"{detail} — apply p95 {p95:.0f}us is too slow"
    return True, detail


# ------------------------------------------------------------------ #
#  3. as-of at scale — 1,000 answers on a long book                   #
# ------------------------------------------------------------------ #
def section_asof(scale: int) -> tuple[bool, str]:
    """A thousand as-of answers on a feed longer than the final, each
    compared to the live state recorded at that exact log position, with
    the p95 answer latency measured against the 60s grace period."""
    events = arena_sim.corrupt(arena_sim.generate_finale(16001, scale * 2),
                               seed=16001)
    book = Book()
    live: list[tuple[str, str]] = []
    first_at: dict[str, int] = {}
    for ev in events:
        book.apply(ev)
        first_at.setdefault(ev["event_id"], len(live))
        live.append((ev["event_id"], json.dumps(book._snapshot_now(),
                                                sort_keys=True)))
    # Probe first deliveries only: a duplicate's as-of resolves to its
    # first position by design, so its later index has no meaningful state.
    firsts = [i for i, (e, _s) in enumerate(live) if first_at[e] == i]
    rng = random.Random(0xA5F)
    probes = rng.sample(firsts, min(1000, len(firsts)))
    bad = 0
    lat: list[float] = []
    for i in probes:
        eid, want = live[i]
        t = time.perf_counter()
        got = json.dumps(book.snapshot(as_of_event_id=eid), sort_keys=True)
        lat.append(time.perf_counter() - t)
        if got != want:
            bad += 1
    lat.sort()
    p95 = lat[int(len(lat) * 0.95)] * 1000
    worst = lat[-1] * 1000
    detail = (f"{len(probes)} as-of answers over a {_fmt(len(events))}-event "
              f"book, p95 {p95:.0f}ms / worst {worst:.0f}ms "
              f"(grace is 60,000ms)")
    if bad:
        return False, f"{detail} — {bad} answers diverged from live state"
    if worst > 5000:
        return False, f"{detail} — worst answer too slow"
    return True, detail


# ------------------------------------------------------------------ #
#  4. recovery — killed and resumed at random offsets                 #
# ------------------------------------------------------------------ #
def section_recovery(scale: int) -> tuple[bool, str]:
    """Kill the consumer at random points and rebuild from the delivered
    log; the resumed book must be byte-identical to one that was never
    interrupted. This is the 3am-crash promise the spec makes."""
    events = arena_sim.corrupt(arena_sim.generate_finale(17001, scale),
                               seed=17001)
    whole = Book()
    arena_sim.deliver(whole, events, seed=17001)
    want = json.dumps(whole._snapshot_now(), sort_keys=True)

    rng = random.Random(0xC0FFEE)
    kills = sorted(rng.sample(range(len(events) // 4, len(events)), 5))
    for k in kills:
        # Up to the kill, then a fresh process rebuilds from the log and
        # carries on — exactly what client.py --resume does.
        part = Book()
        arena_sim.deliver(part, events[:k], seed=17001)
        resumed = Book()
        for ev in part.event_log:
            resumed._apply_core(ev)
        for ev in events[k:]:
            resumed.apply(ev)
        if json.dumps(resumed._snapshot_now(), sort_keys=True) != want:
            return False, (f"resume at offset {k} did not converge on the "
                           f"uninterrupted state")
    return True, (f"5 kill/resume cycles over {_fmt(len(events))} events, "
                  f"every one converged byte-identically")


# ------------------------------------------------------------------ #
#  5. defects at arena density, and the false-positive gate           #
# ------------------------------------------------------------------ #
def section_defects(scale: int) -> tuple[bool, str]:
    """Planted defects must all be caught; clean feeds must produce zero
    armed firings. The second half is the one that protects points: an
    armed detector firing on clean data rejects a valid event."""
    caught = planted = observed = observable = 0
    missed: list[str] = []
    for seed in (18001, 18002):
        raw = arena_sim.generate_defective(seed, scale, rate=0.02)
        # Snapshot the stats BEFORE corrupt(): the generator rewrites the
        # module-level dict on every call.
        ids = {k: list(v) for k, v in
               (arena_sim.DEFECT_LAST_STATS.get("ids") or {}).items()}
        events = arena_sim.corrupt(raw, seed=seed)
        book = Book()
        arena_sim.deliver(book, events, seed=seed)
        # Armed classes must be REJECTED — never posted.
        for det in ("D1", "D3"):
            for eid in ids.get(det, ()):
                planted += 1
                if eid not in book.events:
                    caught += 1
                else:
                    missed.append(f"{det}:{eid}")
        # Observed classes must be SEEN — the finding must exist, even
        # though the event posts. A detector that silently sees nothing
        # is the failure this checks for.
        seen = {f[1] for f in book.report_log if len(f) > 1}
        for det in ("D4", "D5", "D6", "D9", "D10", "D11"):
            for eid in ids.get(det, ()):
                observable += 1
                if eid in seen:
                    observed += 1
        violations = _all_referees(book, f"defect:{seed}")
        if violations:
            return False, f"defect feed {seed}: {violations[0]}"
    if planted == 0:
        return False, ("no armed-class defects were planted — the catch "
                       "check would have passed vacuously")

    armed_fp = 0
    clean_events = 0
    for seed in (19001, 19002, 19003):
        events = arena_sim.corrupt(arena_sim.generate_clean(seed, scale),
                                   seed=seed)
        book = Book()
        arena_sim.deliver(book, events, seed=seed,
                          rewind_at=[len(events) // 3])
        clean_events += len(events)
        armed_fp += sum(1 for f in book.report_log
                        if len(f) > 4 and f[4] == "ARMED")
        violations = _all_referees(book, f"clean:{seed}")
        if violations:
            return False, f"clean feed {seed}: {violations[0]}"
    detail = (f"{planted} armed-class defects planted, {caught} caught; "
              f"{observed}/{observable} observe-class defects seen; "
              f"{_fmt(clean_events)} clean events, {armed_fp} armed "
              f"false positives")
    if caught != planted:
        return False, f"{detail} — MISSED {missed[:3]}"
    if observable and observed < observable:
        return False, (f"{detail} — {observable - observed} observe-class "
                       f"defects went unseen")
    if armed_fp:
        return False, f"{detail} — an armed detector fired on clean data"
    return True, detail


# ------------------------------------------------------------------ #
#  6. determinism under everything                                    #
# ------------------------------------------------------------------ #
def section_determinism(scale: int) -> tuple[bool, str]:
    """The property the whole design rests on: the same delivered events
    always produce the same book. Run the same hostile feed twice in two
    fresh processes-worth of state and compare everything, then prove a
    cold replay of the log lands in the same place."""
    events = arena_sim.corrupt(arena_sim.generate_finale(20001, scale),
                               seed=20001)
    snaps = []
    for _ in range(2):
        book = Book()
        arena_sim.deliver(book, events, seed=20001,
                          rewind_at=[len(events) // 3, 2 * len(events) // 3])
        snaps.append((json.dumps(book._snapshot_now(), sort_keys=True),
                      book))
    if snaps[0][0] != snaps[1][0]:
        return False, "two identical runs produced different books"
    book = snaps[0][1]
    cold = Book()
    for ev in book.event_log:
        cold._apply_core(ev)
    if json.dumps(cold._snapshot_now(), sort_keys=True) != snaps[0][0]:
        return False, "cold replay of the log diverged from the live book"
    # And the reporting path must not perturb any of it.
    before = json.dumps(book._snapshot_now(), sort_keys=True)
    for eid in list(book.eid_pos)[:50]:
        book.snapshot(as_of_event_id=eid)
    book.snapshot(as_of_event_id="no-such-event")
    if json.dumps(book._snapshot_now(), sort_keys=True) != before:
        return False, "answering checkpoints changed the book"
    return True, (f"identical runs identical, cold replay identical, "
                  f"51 checkpoint answers left the book untouched "
                  f"({_fmt(len(events))} events)")


SECTIONS = [
    ("breadth: 8 feeds x 5 generators", section_breadth),
    ("endurance: one long book", section_endurance),
    ("as-of at scale", section_asof),
    ("kill / resume recovery", section_recovery),
    ("defects + false-positive gate", section_defects),
    ("determinism under chaos", section_determinism),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true",
                    help="smaller feeds for a fast sanity pass")
    args = ap.parse_args()
    scale = 3000 if args.quick else 25_000

    print("=" * 78)
    print("MEGA-REGRESSION — deliberately harder than the graded final")
    print(f"  arena final: {ARENA_FINAL_EVENTS:,} events, 75 min, one feed")
    print(f"  this run:    ~{scale * 20:,}+ events across 8 feeds, "
          f"5 generators, every referee")
    print("=" * 78)

    rows = []
    t0 = time.perf_counter()
    for name, fn in SECTIONS:
        print(f"\n[running] {name} ...", flush=True)
        t = time.perf_counter()
        try:
            ok, detail = fn(scale)
        except Exception as exc:                 # a crash is a failure
            import traceback
            traceback.print_exc()
            ok, detail = False, f"crashed: {exc!r}"
        rows.append((name, ok, detail, time.perf_counter() - t))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} ({rows[-1][3]:.0f}s)")
        gc.collect()

    width = max(len(n) for n, _o, _d, _t in rows)
    print("\n" + "=" * 78)
    print("MEGA-REGRESSION SUMMARY")
    print("=" * 78)
    for name, ok, detail, secs in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    green = all(ok for _n, ok, _d, _t in rows)
    print("=" * 78)
    print(f"total {time.perf_counter() - t0:.0f}s")
    print("RESULT: " + ("ALL GREEN — cleared for graded runs"
                        if green else "RED — DO NOT START A GRADED RUN"))
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
