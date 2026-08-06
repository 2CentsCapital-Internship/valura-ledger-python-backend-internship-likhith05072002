"""One-command phase gate: unit suites + full-load chaos, exit 0 only if
everything is green.

Eleven sections, every one blocking:

  1. unittest discovery over tests/ — every test_*.py except the client
     drill (it spawns local servers; opt in with --with-drill).
  2. The fuzz barrage: the control group (every event type's valid
     baseline posts what the protocol says), then >= 1000 mutants per
     type through apply() — zero escaped exceptions, zero state residue
     on rejects, and a clean snapshot after the whole soak.
  3-7. Direct 10k chaos runs on DIFFERENT seeds than the unit suite uses,
     each with two mid-stream rewinds, checked by sim.invariants — so the
     gate never passes on a seed the tests were tuned against: raw chaos,
     then cash, market, corporate and finale feeds in turn.
  8. The as-of oracle: 500 as-of answers over a ~10k finale feed carrying
     checkpoint_requests, each byte-identical to the live state recorded at
     that log position and to a cold replay, plus the serialization canon
     on real snapshots and the measured p95 as-of latency.
  9. Planted defects: D1-D11 injected into two 10k feeds at the arena's own
     2 % rate and scored against ground truth — armed classes rejected with
     no legs and no residue, observed classes logged and byte-for-byte
     inert.
  10. The false-positive gate: 3 seeds x 10k of clean-mode chaos (every
     stream trap on, every defect repaired) where an armed detector firing
     even once is a red run.
  11. The two decision rigs prove themselves on synthetic banks before
     anyone points them at a real one.

A missing sim/ harness, a crash, or a single red test all exit non-zero:
the gate fails loudly or not at all.
"""
import argparse
import glob
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)  # `book`, `sim`, `tests` import from the repo root


def load_unit_suite(with_drill: bool) -> unittest.TestSuite:
    """Discover tests/test_*.py by filename so the drill exclusion is
    explicit and greppable, not hidden in a discovery pattern."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if "drill" in name and not with_drill:
            continue  # spawns servers; opt-in only
        suite.addTests(loader.loadTestsFromName(f"tests.{name}"))
    return suite


def run_units(with_drill: bool) -> tuple[bool, str]:
    result = unittest.TextTestRunner(verbosity=1).run(
        load_unit_suite(with_drill))
    detail = (f"{result.testsRun} tests, {len(result.failures)} failures, "
              f"{len(result.errors)} errors, {len(result.skipped)} skipped")
    return result.wasSuccessful(), detail


def run_fuzz() -> tuple[bool, str]:
    """Phase 7 gate: the fuzz harness, on a seed tests/test_fuzz.py does
    not use — a harness that only survives its own seed is a harness that
    was tuned, not proven.

    Three boxes in one call (sim.invariants.fuzz_barrage):

      * CONTROL GROUP first. Every one of the 24 event types has a VALID
        baseline that must be ACCEPTED and post exactly what the protocol
        says — the 13 graded buy legs at Dr = Cr = 2005.13, the 13 sell
        legs at 1101.16, deposit's two, `[]` from the no-leg types. A
        Book that rejected every event would otherwise score a perfect
        zero-crash, zero-residue fuzz run.
      * The barrage: field drops, type swaps, sign flips and huge values
        (10**30, "9"*400, 1e308, "Infinity", "NaN") at every position of
        every payload, plus seeded combinations — >= 1000 mutants per
        type. No exception may escape apply(); a survivor must post
        balanced legs; a reject must return [] and leave state
        byte-identical by state_fingerprint.
      * Snapshot under fuzz: one Book absorbs the whole barrage, then
        snapshot() raises nothing and passes the serialization canon and
        the standing invariants.
    """
    from sim import invariants
    seed = 20260807            # deliberately NOT tests/test_fuzz.py's seed
    stats, violations = invariants.fuzz_barrage(count=1000, seed=seed)
    if violations:
        return False, (f"seed {seed}: {len(violations)} violations, "
                       f"first: {violations[0]}")
    return True, (
        f"{stats['types']} baselines control-checked, "
        f"{stats['mutants']} mutants "
        f"({min(stats['per_type'].values())}+ per type) through apply() in "
        f"{stats['seconds']}s, 0 escaped exceptions, "
        f"{stats['posted']} survived validation (all balanced), "
        f"{stats['rejected']} rejected with byte-identical state "
        f"({stats['known_residue']} pinned on_order_placed hold-overflow "
        f"cases, see tests/test_fuzz.py::TestKnownResidue), snapshot clean "
        f"after a {stats['soak_events']}-event soak")


def run_chaos() -> tuple[bool, str]:
    from book import Book
    from sim import arena_sim, invariants
    events = arena_sim.corrupt(arena_sim.generate(1042, 10_000), seed=1042)
    book = Book()
    t0 = time.perf_counter()
    arena_sim.deliver(book, events, seed=1042,
                      rewind_at=[len(events) // 3, (2 * len(events)) // 3])
    wall = time.perf_counter() - t0
    # All three referees: accounting/format law, determinism, ring path.
    violations = list(invariants.run_invariants(book))
    for oracle in (invariants.replay_identical, invariants.ring_identical):
        ok, why = oracle(book)
        if not ok:
            violations.append(why)
    detail = (f"{len(events)} stream events (+chaos redeliveries) "
              f"delivered in {wall:.2f}s, "
              f"violations: {violations if violations else 'none'}")
    return not violations, detail


def run_cash_chaos() -> tuple[bool, str]:
    """Phase 1 gate: coherent cash chaos on three seeds none of the unit
    tests use, all trap toggles on, judged by all four referees — standing
    invariants, the wallet oracle, cold replay, and the ring path."""
    from book import Book
    from sim import arena_sim, invariants
    totals = []
    for seed in (2042, 3042, 4042):
        events = arena_sim.corrupt(arena_sim.generate_cash(seed, 10_000),
                                   seed=seed)
        book = Book()
        t0 = time.perf_counter()
        arena_sim.deliver(book, events, seed=seed,
                          rewind_at=[len(events) // 4,
                                     (3 * len(events)) // 4])
        wall = time.perf_counter() - t0
        violations = (list(invariants.run_invariants(book))
                      + list(invariants.wallet_oracle(book)))
        for oracle in (invariants.replay_identical,
                       invariants.ring_identical):
            ok, why = oracle(book)
            if not ok:
                violations.append(why)
        if violations:
            return False, (f"seed {seed}: {len(violations)} violations, "
                           f"first: {violations[0]}")
        totals.append(f"{seed}:{len(events)}ev/{wall:.1f}s")
    return True, f"3 seeds all green ({', '.join(totals)}), wallet oracle exact"


def run_market_chaos() -> tuple[bool, str]:
    """Phase 3 gate: full order-lifecycle chaos on two fresh seeds, every
    trap on, settle_all at end of feed, two mid-stream rewinds — judged by
    ALL SIX referees (standing invariants, wallet oracle, market identities,
    the dual FIFO cost-basis oracle, cold replay, ring path) plus the drain
    check: with settle_all fired, per-customer 2350/1150 must end exactly at
    the dup-quarantine-stuck amount (zero when no duplicate-trade_id fill
    posted; a corrupt()-injected conflicting duplicate reuses an event_id,
    posts nothing, and can never break drain)."""
    from book import Book, ZERO
    from sim import arena_sim, invariants, fifo_oracle
    if not hasattr(arena_sim, "generate_market"):
        return False, ("sim.arena_sim.generate_market not present yet — "
                       "section cannot run")
    totals = []
    for seed in (5042, 6042):
        events = arena_sim.corrupt(
            arena_sim.generate_market(seed, 10_000, settle_all=True),
            seed=seed)
        book = Book()
        t0 = time.perf_counter()
        arena_sim.deliver(book, events, seed=seed,
                          rewind_at=[len(events) // 4,
                                     (3 * len(events)) // 4])
        wall = time.perf_counter() - t0
        violations = (list(invariants.run_invariants(book))
                      + list(invariants.wallet_oracle(book))
                      + list(invariants.market_identities(book))
                      + list(fifo_oracle.check_cost_basis(book)))
        for oracle in (invariants.replay_identical,
                       invariants.ring_identical):
            ok, why = oracle(book)
            if not ok:
                violations.append(why)
        # Drain: settle_all=True was passed above, so the guard is armed.
        unsettled = sorted(tid for tid, t in book.trades.items()
                           if not t["settled"])
        if unsettled:
            violations.append(f"drain: {len(unsettled)} trades unsettled "
                              f"after settle_all, e.g. {unsettled[:3]}")
        stuck_2350, stuck_1150 = invariants.dup_fill_stuck(book)
        for (cid, acct), bal in sorted(book.balances.items()):
            if acct == "2350" and -bal != stuck_2350.get(cid, ZERO):
                violations.append(f"drain 2350[{cid}]: {-bal} != stuck "
                                  f"{stuck_2350.get(cid, ZERO)}")
            elif acct == "1150" and bal != stuck_1150.get(cid, ZERO):
                violations.append(f"drain 1150[{cid}]: {bal} != stuck "
                                  f"{stuck_1150.get(cid, ZERO)}")
        if violations:
            return False, (f"seed {seed}: {len(violations)} violations, "
                           f"first: {violations[0]}")
        totals.append(f"{seed}:{len(events)}ev/{wall:.1f}s")
    return True, (f"2 seeds all green ({', '.join(totals)}), dual FIFO "
                  f"oracle cent-identical, 2350/1150 drained")


def run_corporate_chaos() -> tuple[bool, str]:
    """Phase 4 gate: corporate-dense chaos (dividends, reinvests, splits,
    renames at ~a fifth of the mix, every corporate trap on — D2/D7
    mismatches, phantom dividends, zero-position no-ops, split->sell and
    rename->trade poisons, merge collisions, A->B->C chains, malformed
    corporate payloads) on two fresh seeds, settle_all at end of feed, two
    mid-stream rewinds — judged by the same six-referee battery plus drain
    as the market section. The dual FIFO oracle is the star witness here:
    it must stay cent-identical through splits, reinvest lots, and symbol
    merges it recomputes with its own machinery."""
    from book import Book, ZERO
    from sim import arena_sim, invariants, fifo_oracle
    if not hasattr(arena_sim, "generate_corporate"):
        return False, ("sim.arena_sim.generate_corporate not present yet — "
                       "section cannot run")
    totals = []
    for seed in (7042, 8042):
        events = arena_sim.corrupt(
            arena_sim.generate_corporate(seed, 10_000, settle_all=True),
            seed=seed)
        book = Book()
        t0 = time.perf_counter()
        arena_sim.deliver(book, events, seed=seed,
                          rewind_at=[len(events) // 4,
                                     (3 * len(events)) // 4])
        wall = time.perf_counter() - t0
        violations = (list(invariants.run_invariants(book))
                      + list(invariants.wallet_oracle(book))
                      + list(invariants.market_identities(book))
                      + list(fifo_oracle.check_cost_basis(book)))
        for oracle in (invariants.replay_identical,
                       invariants.ring_identical):
            ok, why = oracle(book)
            if not ok:
                violations.append(why)
        # Drain: settle_all=True was passed above, so the guard is armed.
        unsettled = sorted(tid for tid, t in book.trades.items()
                           if not t["settled"])
        if unsettled:
            violations.append(f"drain: {len(unsettled)} trades unsettled "
                              f"after settle_all, e.g. {unsettled[:3]}")
        stuck_2350, stuck_1150 = invariants.dup_fill_stuck(book)
        for (cid, acct), bal in sorted(book.balances.items()):
            if acct == "2350" and -bal != stuck_2350.get(cid, ZERO):
                violations.append(f"drain 2350[{cid}]: {-bal} != stuck "
                                  f"{stuck_2350.get(cid, ZERO)}")
            elif acct == "1150" and bal != stuck_1150.get(cid, ZERO):
                violations.append(f"drain 1150[{cid}]: {bal} != stuck "
                                  f"{stuck_1150.get(cid, ZERO)}")
        if violations:
            return False, (f"seed {seed}: {len(violations)} violations, "
                           f"first: {violations[0]}")
        s = arena_sim.CORP_LAST_STATS
        totals.append(f"{seed}:{len(events)}ev/{wall:.1f}s/"
                      f"{s['corporate_share_pct']}%corp")
    return True, (f"2 seeds all green ({', '.join(totals)}), dual FIFO "
                  f"oracle cent-identical through splits/renames/reinvests, "
                  f"2350/1150 drained")


def run_finale_chaos() -> tuple[bool, str]:
    """Phase 5 gate: the full mix — cash, orders, corporate actions — plus
    DENSE reversals (~8% of events: cash, buy fills partially consumed /
    settled / unsettled, sells across splits and renames, no-leg corporate
    events, settlements) and fee settlements (~5%, settle->accrue->settle
    cycles, re-raise-then-re-settle), every reversal/settlement trap on
    (unknown refs, reversals of rejected events, double reversals,
    reversal-of-reversal, reversal-before-original, zero-payable and
    unknown-broker settlements) on two fresh seeds, two mid-stream rewinds
    — judged by ALL the referees: standing invariants, wallet oracle
    (reversal-aware), market identities (reversal-aware), the M4 payable
    cent audit, the dual FIFO oracle (reversal-aware), cold replay, ring
    path, and the drain check."""
    from book import Book, ZERO
    from sim import arena_sim, invariants, fifo_oracle
    if not hasattr(arena_sim, "generate_finale"):
        return False, ("sim.arena_sim.generate_finale not present yet — "
                       "section cannot run")
    totals = []
    for seed in (9042, 9142):
        events = arena_sim.corrupt(
            arena_sim.generate_finale(seed, 10_000, settle_all=True),
            seed=seed)
        book = Book()
        t0 = time.perf_counter()
        arena_sim.deliver(book, events, seed=seed,
                          rewind_at=[len(events) // 4,
                                     (3 * len(events)) // 4])
        wall = time.perf_counter() - t0
        violations = (list(invariants.run_invariants(book))
                      + list(invariants.wallet_oracle(book))
                      + list(invariants.market_identities(book))
                      + list(invariants.payable_audit(book))
                      + list(fifo_oracle.check_cost_basis(book)))
        for oracle in (invariants.replay_identical,
                       invariants.ring_identical):
            ok, why = oracle(book)
            if not ok:
                violations.append(why)
        # Drain: settle_all drained every posted-and-unsettled, unreversed
        # trade; reversed-unsettled trades were deleted (R6), so nothing
        # settleable may remain open.
        unsettled = sorted(tid for tid, t in book.trades.items()
                           if not t["settled"])
        if unsettled:
            violations.append(f"drain: {len(unsettled)} trades unsettled "
                              f"after settle_all, e.g. {unsettled[:3]}")
        # 2350/1150 residue after the drain: exactly the dup-quarantine
        # stuck cents (unreversed) minus the designed residue of reversed
        # SETTLED fills (R7) — nothing else.
        stuck_2350, stuck_1150 = invariants.dup_fill_stuck(book)
        res_2350, res_1150 = invariants.reversed_settled_residues(book)
        for (cid, acct), bal in sorted(book.balances.items()):
            if acct == "2350":
                want = stuck_2350.get(cid, ZERO) - res_2350.get(cid, ZERO)
                if -bal != want:
                    violations.append(f"drain 2350[{cid}]: {-bal} != "
                                      f"stuck-residue {want}")
            elif acct == "1150":
                want = stuck_1150.get(cid, ZERO) - res_1150.get(cid, ZERO)
                if bal != want:
                    violations.append(f"drain 1150[{cid}]: {bal} != "
                                      f"stuck-residue {want}")
        if violations:
            return False, (f"seed {seed}: {len(violations)} violations, "
                           f"first: {violations[0]}")
        s = arena_sim.FINALE_LAST_STATS
        totals.append(f"{seed}:{len(events)}ev/{wall:.1f}s/"
                      f"{s['reversal_share_pct']}%rev/"
                      f"{s['settlement_share_pct']}%stl")
    return True, (f"2 seeds all green ({', '.join(totals)}), payable cent "
                  f"audit exact, dual FIFO oracle cent-identical through "
                  f"dense reversals, 2350/1150 drained")


def run_asof_oracle() -> tuple[bool, str]:
    """Phase 6 gate: 500 as-of points over a ~10k finale feed carrying
    checkpoint_requests.

    The feed is generate_finale_cp — the full finale mix plus
    checkpoint_request events (~1 per 300, half of them naming an
    as_of_event_id that is in turn a normal posted event, an event the Book
    rejected, an id delivered more than once, or an event adjacent to one
    carrying backdated_days) — then corrupt()ed, then delivered with point
    redeliveries and two mid-stream rewinds. checkpoint_requests are never
    applied (deliver() records them separately, exactly as client.py routes
    them).

    Sampling, to a ~500-point budget: every id an in-feed checkpoint names,
    every id the stream delivers twice (the wrapper's verbatim redeliveries
    and corrupt()'s conflicting duplicates — the sharp C2 cases), and
    deterministic random positions for the rest. A live snapshot is
    recorded ONLY at those positions, at the instant the event is applied —
    recording all ~10k would cost ~20 ms each and prove nothing more.

    Referees: asof_oracle (ring answer == recorded live state == cold
    replay, anchored to the FIRST delivery of every id, C2),
    serialization_canon on real snapshots (money 2dp, quantities minimal
    form, keys sorted), ring_identical, and the A9 probe (an as-of naming
    an id we never saw answers live state and quarantines, never raises).
    p95 as-of latency is measured and reported — it must be well under 1 s,
    which is the whole reason the snapshot ring exists.
    """
    import json
    import random
    from book import Book
    from sim import arena_sim, invariants
    if not hasattr(arena_sim, "generate_finale_cp"):
        return False, ("sim.arena_sim.generate_finale_cp not present yet — "
                       "section cannot run")
    seed = 9242
    events = arena_sim.corrupt(
        arena_sim.generate_finale_cp(seed, 10_000, settle_all=True),
        seed=seed)
    cp_stats = dict(arena_sim.CP_LAST_STATS)
    named = set(arena_sim.checkpoint_targets(events))
    counts: dict = {}
    for ev in events:
        counts[ev["event_id"]] = counts.get(ev["event_id"], 0) + 1
    dup_ids = {eid for eid, c in counts.items() if c > 1}

    # The oracle store: every id a checkpoint names, every id delivered
    # more than once, then deterministic random positions to fill the
    # 500-point budget. Recorded live, once, at first delivery.
    # First-delivery positions are known from the feed alone — deliver()
    # only ever re-sends events it has already sent, so the order of first
    # deliveries is the feed's own order of distinct ids (== book.eid_pos,
    # asserted point by point inside asof_oracle).
    first_pos: dict = {}
    for ev in events:
        if ev["type"] != "checkpoint_request":
            first_pos.setdefault(ev["event_id"], len(first_pos))
    rng = random.Random(seed ^ 0x0A50F)
    sampled = {first_pos[e] for e in (named | dup_ids) if e in first_pos}
    pool = [i for i in range(len(first_pos)) if i not in sampled]
    sampled |= set(rng.sample(pool, min(max(0, 500 - len(sampled)),
                                        len(pool))))
    live: list = []
    raw_snaps: list = []                   # un-round-tripped, for the canon

    def observer(bk, log_idx, ev):
        if log_idx in sampled:
            snap = bk.snapshot()
            live.append((log_idx, ev["event_id"],
                         json.dumps(snap, sort_keys=True)))
            if len(raw_snaps) < 40:
                raw_snaps.append(snap)

    book = Book()
    checkpoints: list = []
    t0 = time.perf_counter()
    arena_sim.deliver(book, events, seed=seed,
                      rewind_at=[len(events) // 4, (3 * len(events)) // 4],
                      checkpoints=checkpoints, observer=observer)
    wall = time.perf_counter() - t0

    violations = list(invariants.asof_oracle(book, live))
    stats = dict(invariants.ASOF_LAST_STATS)
    # The canon runs on real snapshots: the recorded live ones, the final
    # live one, and a handful of real as-of answers.
    canon_snaps = raw_snaps + [book.snapshot()] + [
        book.snapshot(eid) for _i, eid, _s in live[::max(1, len(live) // 10)]]
    violations += list(invariants.serialization_canon(book, canon_snaps))
    ok, why = invariants.ring_identical(book)
    if not ok:
        violations.append(why)
    # Every as-of id an in-feed checkpoint named must be answerable.
    unanswerable = sorted(e for e in named if e not in book.eid_pos)
    if unanswerable:
        violations.append(f"checkpoint targets never logged: "
                          f"{unanswerable[:3]}")
    # A9: an id we never saw answers live state, loudly, without raising.
    live_bytes = json.dumps(book.snapshot(), sort_keys=True)
    # report_log, not quarantine: a checkpoint is a READ and must never
    # make live state diverge from a replay of the same log.
    q0 = len(book.report_log)
    if json.dumps(book.snapshot(f"evt_{seed}_never_seen"),
                  sort_keys=True) != live_bytes:
        violations.append("A9: unknown as-of id did not degrade to live state")
    if not any(q[0] == "asof_unknown_id" for q in book.report_log[q0:]):
        violations.append("A9: unknown as-of id left no report record")
    if book.quarantine and book.quarantine[-1][0] in ("asof_unknown_id",
                                                      "snapshot_failed"):
        violations.append("A9: reporting wrote into replayed ledger state")

    if violations:
        return False, (f"seed {seed}: {len(violations)} violations, "
                       f"first: {violations[0]}")
    # C2 coverage: oracle points naming an id the stream delivered more
    # than once — their answer must anchor to the FIRST delivery. (A floor:
    # deliver()'s own 5% point redeliveries are not counted here.)
    dup_points = sum(1 for _i, eid, _s in live if eid in dup_ids)
    dup_named = cp_stats["target_duplicated"]
    return True, (
        f"{len(events)}ev/{wall:.1f}s delivered, {stats['points']} as-of "
        f"points (log {stats['log_len']}, {stats['rejected_targets']} on "
        f"rejected/unknown-type events, {dup_points} on redelivered ids), "
        f"ring == live == cold replay "
        f"({stats['cold_restarts']} independent restarts), "
        f"{len(checkpoints)} checkpoint_requests skipped "
        f"({cp_stats['checkpoints_with_asof']} with as-of: "
        f"{cp_stats['target_normal']} normal/"
        f"{cp_stats['target_rejected']} rejected/{dup_named} duplicated/"
        f"{cp_stats['target_backdated_adjacent']} backdated-adjacent), "
        f"as-of latency p50 {stats['p50_ms']}ms / p95 {stats['p95_ms']}ms / "
        f"max {stats['max_ms']}ms, canon clean on "
        f"{len(canon_snaps)} real snapshots")


def _detector_feed_run(feed, seed, modes=None):
    """One delivery of `feed` under an exact detector mode table (None =
    the shipped defaults), returning (book, submissions). The table is
    always restored: a regression section must not leak an arming policy
    into the section after it."""
    import detectors
    from book import Book
    from sim import arena_sim
    saved = dict(detectors.DETECTOR_MODE)
    try:
        if modes is not None:
            detectors.DETECTOR_MODE.clear()
            detectors.DETECTOR_MODE.update(modes)
        book = Book()
        subs = arena_sim.deliver(book, feed, seed=seed,
                                 rewind_at=[len(feed) // 4,
                                            (3 * len(feed)) // 4])
        return book, subs
    finally:
        detectors.DETECTOR_MODE.clear()
        detectors.DETECTOR_MODE.update(saved)


def _pass_findings(book) -> dict:
    """detector id -> the event ids the detector PASS flagged, with the
    mode it flagged them under."""
    out = {}
    for row in book.report_log:
        if isinstance(row[0], str) and len(row) == 5 and row[0][:1] == "D" \
                and row[0][1:].isdigit():
            out.setdefault(row[0], {})[row[1]] = row[4]
    return out


def run_planted_defects() -> tuple[bool, str]:
    """Phase 7 gate: known defects planted at the arena's own 2 % rate in
    two full 10k feeds, scored against ground truth.

    Armed half — every planted D1 (broker/asset-class mismatch) and D3
    (interest share above gross) must be rejected with `legs: []`, and the
    book must land byte-identical to a run of the same feed with those
    events deleted from it: the rejection contract, measured rather than
    asserted.

    Observe half — every planted D4/D5/D6/D9/D10/D11 must appear in
    report_log under mode OBSERVE, while the submission stream stays
    byte-identical to the same feed replayed with every detector OFF.
    Observe mode that changes one leg is not observe mode.

    Inline half — D2, D7 and D8 are observed at their handler's own site
    and land in book.quarantine; same 100 % requirement, other channel.
    """
    import json
    import detectors
    from sim import arena_sim
    if not hasattr(arena_sim, "generate_defective"):
        return False, ("sim.arena_sim.generate_defective not present yet — "
                       "section cannot run")
    pass_dets = ("D4", "D5", "D6", "D9", "D10", "D11")
    inline = {"D2": "D2", "D7": "D7"}   # D8 is ARMED (see below)
    totals = {d: [0, 0] for d in arena_sim.DEFECT_CLASSES}   # [planted, caught]
    notes = []
    for seed in (9500, 9501):
        events = arena_sim.generate_defective(seed, 10_000, rate=0.02)
        ids = {k: set(v) for k, v in arena_sim.DEFECT_LAST_STATS["ids"].items()}
        feed = arena_sim.corrupt(events, seed=seed)
        book, subs = _detector_feed_run(feed, seed)
        legs = {}
        for s in subs:
            legs.setdefault(s["event_id"], s["legs"])

        # -- armed: rejected, no legs, and no trace in the book -----------
        for det in ("D1", "D3", "D8"):
            for eid in ids[det]:
                totals[det][0] += 1
                if legs.get(eid) == [] and eid not in book.events:
                    totals[det][1] += 1
        planted_armed = ids["D1"] | ids["D3"]
        absent = [e for e in feed if e["event_id"] not in planted_armed]
        other, _s = _detector_feed_run(absent, seed)
        if json.dumps(other.snapshot(), sort_keys=True) != \
                json.dumps(book.snapshot(), sort_keys=True):
            return False, (f"seed {seed}: armed rejects left residue — "
                           f"snapshot differs from a feed without them")

        # -- observe: seen by the pass, felt by nothing --------------------
        found = _pass_findings(book)
        for det in pass_dets:
            for eid in ids[det]:
                totals[det][0] += 1
                if found.get(det, {}).get(eid) == "OBSERVE":
                    totals[det][1] += 1
        # Turn OFF only the observe-mode detectors; the armed ones keep
        # their shipping mode. D1/D2/D3 are now genuinely mode-driven (so
        # they can be A/B'd or disarmed without a code edit), so switching
        # them off would legitimately change submissions — that is the
        # armed detectors working, not observe mode leaking.
        off_modes = {d: ("OFF" if detectors.mode(d) != "ARMED"
                         else detectors.mode(d))
                     for d in detectors.DETECTOR_MODE}
        off_book, off_subs = _detector_feed_run(feed, seed, off_modes)
        if json.dumps(subs, sort_keys=True) != \
                json.dumps(off_subs, sort_keys=True):
            return False, f"seed {seed}: observe mode changed a submission"
        if json.dumps(off_book.snapshot(), sort_keys=True) != \
                json.dumps(book.snapshot(), sort_keys=True):
            return False, f"seed {seed}: observe mode changed the book"

        # -- inline observations (replayed quarantine) ---------------------
        quarantined = {}
        for row in book.quarantine:
            quarantined.setdefault(row[0], set()).update(
                x for x in row[1:] if isinstance(x, str) and x.startswith("evt"))
        for det, tag in inline.items():
            for eid in ids[det]:
                totals[det][0] += 1
                if eid in quarantined.get(tag, set()):
                    totals[det][1] += 1
        notes.append(f"{seed}:{len(feed)}ev")

    missed = {d: v for d, v in totals.items() if v[1] != v[0]}
    empty = sorted(d for d, v in totals.items() if v[0] == 0)
    if missed:
        return False, f"missed defects (planted, caught): {missed}"
    if empty:
        return False, f"no defects planted for {empty} — gate is vacuous"
    return True, (f"{', '.join(notes)}, "
                  f"{sum(v[0] for v in totals.values())} planted defects, "
                  f"100% caught ("
                  + " ".join(f"{d}:{totals[d][0]}"
                             for d in arena_sim.DEFECT_CLASSES)
                  + "), armed rejects leave zero residue, observe mode "
                    "byte-identical to detectors OFF")


def run_clean_feed_fp() -> tuple[bool, str]:
    """Phase 7 gate: 3 seeds x 10k clean-mode chaos — every stream trap on
    (duplicates, conflicting duplicates, rewinds, fill-before-placement,
    backdating, oversells, malformed payloads, out-of-order settles, dense
    reversal edges) and ZERO planted defects, with every fill priced
    inside its order's limit and its principal exactly money(qty x price).

    The armed detectors must fire zero times, and the detector pass must
    reject nothing at all — proved by replaying each feed with every
    detector OFF and diffing the submission streams byte for byte.

    D1 and D3 are enforced inline and leave no report_log row, so they are
    re-derived straight from the payloads. Observe-mode hits are counted,
    not failed: they are the deployment-rule evidence for NOTES.md, and
    the only detector that ever fires here is D10 (a dividend may
    legitimately precede the buy that creates the position).
    """
    import json
    import detectors
    import tariff
    from sim import arena_sim, invariants
    if not hasattr(arena_sim, "generate_clean"):
        return False, ("sim.arena_sim.generate_clean not present yet — "
                       "section cannot run")
    fp_posted, fp_all = {}, {}
    armed_hits = offline_d1 = offline_d3 = 0
    traps = {"fill_before_placement": 0, "dup_verbatim": 0, "backdated": 0,
             "fixed_price_buy": 0, "fixed_price_sell": 0, "fixed_d2": 0,
             "fixed_d7": 0, "fixed_dup_trade_id": 0}
    totals = []
    for seed in (7742, 7842, 7942):
        events = arena_sim.generate_clean(seed, 10_000)
        for k in traps:
            traps[k] += arena_sim.CLEAN_LAST_STATS[k]
        feed = arena_sim.corrupt(events, seed=seed)
        t0 = time.perf_counter()
        book, subs = _detector_feed_run(feed, seed)
        wall = time.perf_counter() - t0
        off_modes = {d: "OFF" for d in detectors.DETECTOR_MODE}
        _off_book, off_subs = _detector_feed_run(feed, seed, off_modes)
        if json.dumps(subs, sort_keys=True) != \
                json.dumps(off_subs, sort_keys=True):
            return False, (f"seed {seed}: the detector pass changed a "
                           f"submission on a CLEAN feed")
        for row in book.report_log:
            if not (isinstance(row[0], str) and len(row) == 5
                    and row[0][:1] == "D" and row[0][1:].isdigit()):
                continue
            fp_all[row[0]] = fp_all.get(row[0], 0) + 1
            if row[1] in book.events:
                fp_posted[row[0]] = fp_posted.get(row[0], 0) + 1
            if row[4] == "ARMED":
                armed_hits += 1
        for e in events:                     # D1 / D3 are inline: re-derive
            p = e["payload"]
            if not isinstance(p, dict):
                continue
            if e["type"] in ("order_filled", "order_partially_filled"):
                if (p.get("broker") in tariff.TARIFF
                        and not tariff.covers(p["broker"],
                                              p.get("asset_class"))):
                    offline_d1 += 1
            elif e["type"] == "interest_credited":
                try:
                    if (arena_sim._parse_cents(p["customer_share"])
                            > arena_sim._parse_cents(p["gross_amount"])):
                        offline_d3 += 1
                except (KeyError, ValueError, TypeError):
                    pass
        violations = list(invariants.run_invariants(book))
        ok, why = invariants.replay_identical(book)
        if not ok:
            violations.append(why)
        if violations:
            return False, (f"seed {seed}: clean feed broke the book — "
                           f"{violations[0]}")
        totals.append(f"{seed}:{len(feed)}ev/{wall:.1f}s")

    bad = {d: n for d, n in fp_posted.items() if d != "D10"}
    if armed_hits or offline_d1 or offline_d3:
        return False, (f"armed detectors fired on clean data: pass={armed_hits} "
                       f"D1={offline_d1} D3={offline_d3}")
    if bad:
        return False, f"false positives on clean posted events: {bad}"
    return True, (f"3 seeds all green ({', '.join(totals)}), armed FP = 0 "
                  f"(D1 0, D3 0, pass 0), observe FP on posted events: "
                  + (", ".join(f"{d} {n}" for d, n in sorted(fp_posted.items()))
                     or "none")
                  + f" [D5 {fp_all.get('D5', 0)} hits, all on events the Book "
                    f"rejects anyway], traps fired: "
                  + " ".join(f"{k}={v}" for k, v in sorted(traps.items())))


def run_detector_rigs() -> tuple[bool, str]:
    """Phase 7 gate: the two decision rigs prove themselves on synthetic
    banks before anyone trusts them with a live one — the cluster rig must
    name D2 as the dominant cluster of three posted-but-expected-empty
    dividends, and the A/B rig must report exactly one attributable
    disagreement for a single planted D5 and zero for everything else."""
    import contextlib
    import io
    try:
        from tools import defect_cluster, detector_ab
    except ImportError as exc:
        return False, f"tools not importable: {exc!r}"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cluster_ok = defect_cluster.self_test(verbose=False)
        ab_ok = detector_ab.self_test(verbose=False)
    failed = [ln.strip() for ln in buf.getvalue().splitlines()
              if "[FAIL]" in ln]
    if not (cluster_ok and ab_ok):
        return False, f"rig self-test failed: {failed}"
    return True, ("defect_cluster: D2 dominant on a 3-event planted bank, "
                  "no co-firing detector; detector_ab: 1 attributable "
                  "disagreement for D5, 0 for the other ten, 0 downstream, "
                  "inline D2 switch driven and restored")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--with-drill", action="store_true",
                    help="include tests/test_drill_client (spawns local "
                         "mock servers)")
    args = ap.parse_args()

    sections = [("unit suite", lambda: run_units(args.with_drill)),
                ("fuzz barrage 25x1000 mutants", run_fuzz),
                ("10k chaos + invariants", run_chaos),
                ("10k cash chaos x3 seeds", run_cash_chaos),
                ("10k market chaos x2 seeds", run_market_chaos),
                ("10k corporate chaos x2 seeds", run_corporate_chaos),
                ("10k finale chaos x2 seeds", run_finale_chaos),
                ("as-of oracle 500 points", run_asof_oracle),
                ("planted defects D1-D11 x2 seeds", run_planted_defects),
                ("clean-feed zero FP x3 seeds", run_clean_feed_fp),
                ("detector rig self-tests", run_detector_rigs)]
    rows = []
    for name, fn in sections:
        try:
            ok, detail = fn()
        except Exception as e:  # a crashed section is a failed section
            ok, detail = False, f"crashed: {e!r}"
        rows.append((name, ok, detail))

    width = max(len(name) for name, _, _ in rows)
    print("\n" + "=" * 68)
    print("PHASE GATE SUMMARY")
    print("=" * 68)
    for name, ok, detail in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    print("=" * 68)
    all_green = all(ok for _, ok, _ in rows)
    print("RESULT: " + ("ALL GREEN" if all_green else "RED — do not run live"))
    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
