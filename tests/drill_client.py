"""Client transport drill — proves the patched client survives everything the
real server does, before a single live run is spent finding out.

Four drills, each a real `python client.py` subprocess against a MockArena:

  1. basic     — every event exactly one submission (legs [] for the
                 unimplemented type), both checkpoint replies present, the
                 as-of reply matches a Book replayed to that exact event and
                 differs from the final reply, clean exit on stream_end with
                 NO reconnect after it, JSONL logs exist and parse.
  2. reset     — a scripted mid-run stream_reset rewinds the client 250
                 offsets; it must reconnect from exactly that offset and the
                 re-served events must not double-post (idempotent book).
  3. 409       — submission mode; the server drops the connection with the
                 run finished but no stream_end. The reconnect meets HTTP 409
                 and the client must treat it as terminal: prompt exit, at
                 most one connect after the finish, no retry storm.
  4. burst     — 1200 events immediately before stream_end; the terminal
                 flush-until-empty loop must deliver every one of them
                 despite the 500-postings-per-request cap.

Expected checkpoint bodies are recomputed with the real Book over the same
scenario prefix — the drill and the client share one oracle, so a mismatch is
a transport bug, not an accounting disagreement. Prints PASS/FAIL per
assertion; exits 0 only if every assertion passed. Stdlib only, no sleeps in
the server: the whole drill finishes in well under a minute.

    python tests/drill_client.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO))        # book.py — the oracle for expected replies
sys.path.insert(0, str(TESTS_DIR))   # mock_arena

from book import Book                                          # noqa: E402
from mock_arena import (MockArena, finish_silently,            # noqa: E402
                        make_deposits, reset_marker)

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(bool(ok))
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail:
        line += f" -- {detail}"
    print(line, flush=True)


def tail(proc, n: int = 400) -> str:
    """Last bytes of a subprocess's output, for FAIL diagnostics. Works on a
    CompletedProcess and on a TimeoutExpired alike."""
    out = ""
    for s in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if isinstance(s, bytes):
            s = s.decode(errors="replace")
        if s:
            out += s
    return out[-n:].replace("\n", " | ")


def run_client(port: int, mode: str, log_dir: Path, timeout: float = 45):
    """client.py as a real subprocess, exactly as a live run would start it.
    stdin pre-feeds the mode name so the graded-run confirmation prompt (kept
    in the patched client on purpose) cannot hang the drill. Returns
    (proc, seconds, timed_out); on timeout the process is already killed."""
    cmd = [sys.executable, "-u", "client.py",
           "--url", f"http://127.0.0.1:{port}",
           "--key", "test", "--mode", mode, "--log-dir", str(log_dir)]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(REPO), input=f"{mode}\n",
                              capture_output=True, text=True, timeout=timeout)
        return proc, time.time() - t0, False
    except subprocess.TimeoutExpired as exc:
        return exc, time.time() - t0, True


def expected_snapshot(events: list[dict]) -> dict:
    """What a correct client must report after these events: the same Book the
    client runs, replayed over the same prefix."""
    b = Book()
    for ev in events:
        b.apply(ev)
    return b.snapshot()


def snap_of(cp_body: dict) -> dict:
    """The state portion of a checkpoint reply (drop checkpoint_id and any
    echo fields the client may add)."""
    return {k: cp_body.get(k)
            for k in ("trial_balance", "customers", "open_order_routes")}


def new_log_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="arena_drill_"))


# --------------------------------------------------------------------- #
#  drill 1: basic run                                                   #
# --------------------------------------------------------------------- #
def drill_basic() -> None:
    print("\nDrill 1: basic run — postings, as-of + final checkpoints, logs, clean exit")
    first = make_deposits(30)                       # offsets 0..29
    # A type no handler will ever exist for: its submission must be legs [],
    # forever — the quarter-weight "correctly no legs" path.
    mystery = {"offset": 30, "event_id": "evt_mys_00030", "type": "mystery_type",
               "payload": {"anything": "at all"}}
    asof_id = first[10]["event_id"]                 # book after 11 deposits
    cp_asof = {"offset": 31, "event_id": "evt_cp_00031",
               "type": "checkpoint_request",
               "payload": {"checkpoint_id": "cp_asof",
                           "respond_within_seconds": 60,
                           "as_of_event_id": asof_id}}
    second = make_deposits(20, start_offset=32, prefix="evt_dep2")  # 32..51
    cp_final = {"offset": 52, "event_id": "evt_cp_00052",
                "type": "checkpoint_request",
                "payload": {"checkpoint_id": "cp_final",
                            "respond_within_seconds": 60}}
    scenario = first + [mystery, cp_asof] + second + [cp_final]
    ledger = first + [mystery] + second             # everything needing a posting

    log_dir = new_log_dir()
    with MockArena(scenario) as arena:
        proc, took, timed_out = run_client(arena.port, "practice", log_dir)

    check("client exited on stream_end",
          not timed_out and proc.returncode == 0, tail(proc))

    by_id: dict[str, list] = {}
    for p in arena.received_postings:
        by_id.setdefault(p.get("event_id"), []).append(p.get("legs"))
    check("every event got exactly one submission",
          len(by_id) == len(ledger)
          and all(len(by_id.get(ev["event_id"], [])) == 1 for ev in ledger),
          f"{len(by_id)} distinct ids for {len(ledger)} events")
    check("unimplemented type submitted with legs: []",
          by_id.get(mystery["event_id"]) == [[]],
          f"got {by_id.get(mystery['event_id'])!r}")
    check("deposits submitted with two-leg postings",
          all(len(by_id.get(ev["event_id"], [[]])[0]) == 2
              for ev in first + second))

    cps = {c.get("checkpoint_id"): c for c in arena.received_checkpoints}
    check("both checkpoint replies received", set(cps) == {"cp_asof", "cp_final"},
          f"got ids {sorted(cps)}")
    exp_asof = expected_snapshot(first[:11])        # through the as-of event
    exp_final = expected_snapshot(ledger)
    got_asof = snap_of(cps.get("cp_asof", {}))
    got_final = snap_of(cps.get("cp_final", {}))
    check("as-of reply matches Book replayed to that event",
          got_asof == exp_asof, f"got {got_asof!r}")
    check("final reply matches Book over the whole feed",
          got_final == exp_final, f"got {got_final!r}")
    check("as-of reply differs from the final reply", got_asof != got_final)

    check("no reconnect after stream_end", len(arena.stream_connects) == 1,
          f"{len(arena.stream_connects)} connects")

    for name in ("feed.jsonl", "postings.jsonl", "checkpoints.jsonl"):
        p = log_dir / name
        ok, why = p.exists(), "missing"
        if ok:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
                for ln in lines:
                    json.loads(ln)
                ok, why = bool(lines), "empty"
            except (json.JSONDecodeError, OSError) as exc:
                ok, why = False, f"unparseable: {exc}"
        check(f"{name} exists and parses", ok, why)


# --------------------------------------------------------------------- #
#  drill 2: mid-run rewind                                              #
# --------------------------------------------------------------------- #
def drill_reset() -> None:
    print("\nDrill 2: stream_reset rewind — resume offset honoured, no double-posting")
    deposits = make_deposits(400)                   # offsets 0..399
    # Rewind from offset 350 back to 100: 250 already-seen events re-served.
    scenario = deposits[:350] + [reset_marker(100)] + deposits[350:]

    with MockArena(scenario) as arena:
        proc, took, timed_out = run_client(arena.port, "practice", new_log_dir())

    check("client exited on stream_end",
          not timed_out and proc.returncode == 0, tail(proc))
    check("client reconnected from the reset offset",
          len(arena.stream_connects) == 2
          and arena.stream_connects[1].get("from") == "100",
          f"connects {arena.stream_connects}")

    submitted: dict[str, int] = {}                  # eid -> total submissions
    posted: dict[str, int] = {}                     # eid -> submissions WITH legs
    for p in arena.received_postings:
        eid = p.get("event_id")
        submitted[eid] = submitted.get(eid, 0) + 1
        if p.get("legs"):
            posted[eid] = posted.get(eid, 0) + 1
    check("every event submitted at least once",
          all(ev["event_id"] in submitted for ev in deposits),
          f"{sum(ev['event_id'] not in submitted for ev in deposits)} missing")
    # The server keeps the first submission per event, so a re-served event
    # may legitimately be re-submitted — but only ever with legs [] (the book
    # is idempotent). A second submission WITH legs is the double-post bug.
    check("no double-posting after rewind (deposit legs submitted exactly once each)",
          len(posted) == len(deposits) and all(v == 1 for v in posted.values()),
          f"{len(posted)} ids with legs, max count "
          f"{max(posted.values(), default=0)}")


# --------------------------------------------------------------------- #
#  drill 3: submission-mode 409                                         #
# --------------------------------------------------------------------- #
def drill_409() -> None:
    print("\nDrill 3: submission 409 — terminal, prompt exit, no retry storm")
    scenario = make_deposits(5) + [finish_silently()]

    with MockArena(scenario) as arena:
        proc, took, timed_out = run_client(arena.port, "submission",
                                           new_log_dir(), timeout=30)

    check("client terminated promptly after the 409",
          not timed_out and took < 20, f"took {took:.1f}s, {tail(proc)}")
    check("the 409 path was actually exercised", arena.sent_409 >= 1,
          "client never reconnected into the 409 (drill proved nothing)")
    extra = (len(arena.stream_connects) - arena.connects_at_finish
             if arena.connects_at_finish is not None
             else len(arena.stream_connects))
    check("at most one connect after the run finished", extra <= 1,
          f"{extra} post-finish connects: retry storm")


# --------------------------------------------------------------------- #
#  drill 4: terminal burst flush                                        #
# --------------------------------------------------------------------- #
def drill_burst() -> None:
    print("\nDrill 4: 1200-event burst before stream_end — flush until empty")
    deposits = make_deposits(1200)

    with MockArena(deposits) as arena:
        proc, took, timed_out = run_client(arena.port, "practice",
                                           new_log_dir(), timeout=60)

    check("client exited on stream_end",
          not timed_out and proc.returncode == 0, tail(proc))
    got = {p.get("event_id") for p in arena.received_postings}
    missing = [ev["event_id"] for ev in deposits if ev["event_id"] not in got]
    check("all 1200 burst events were submitted", not missing,
          f"{len(missing)} missing, e.g. {missing[:3]}")
    check("500-postings-per-request cap never violated",
          arena.rejected_413 == 0, f"{arena.rejected_413} requests over the cap")


def drill_checkpoint_rewind() -> None:
    """A checkpoint describes the book AT ITS OFFSET. The server rewinds
    mid-run and re-delivers the same checkpoint_request; answering it a
    second time from our now-advanced state is strictly wrong — practice
    run 1 answered cp_postchaos three times and scored 0.687, 0.314, then
    0.099 as the state drifted past the point being asked about. The
    first answer must be re-sent verbatim."""
    print("\nDrill 5: re-delivered checkpoint — the first answer is re-sent")
    deposits = make_deposits(40)
    cp = {"offset": 20, "event_id": "evt_cp_drill",
          "type": "checkpoint_request",
          "payload": {"checkpoint_id": "cpX", "respond_within_seconds": 60}}
    # The rewind re-serves the checkpoint along with the events around it.
    scenario = (deposits[:20] + [cp] + deposits[20:30]
                + [reset_marker(10)] + deposits[10:])

    with MockArena(scenario) as arena:
        proc, took, timed_out = run_client(arena.port, "practice",
                                           new_log_dir())
    reps = list(arena.received_checkpoints)
    check("the checkpoint was delivered more than once",
          len(reps) >= 2, f"got {len(reps)} replies")
    bodies = {json.dumps(r, sort_keys=True) for r in reps}
    check("every reply is the FIRST answer, byte-identical",
          len(bodies) == 1, f"{len(bodies)} distinct replies")


def main() -> int:
    t0 = time.time()
    for drill in (drill_basic, drill_reset, drill_409, drill_burst,
                  drill_checkpoint_rewind):
        try:
            drill()
        except Exception as exc:        # a broken drill is a failed drill
            check(f"{drill.__name__} completed", False, repr(exc))
    good, total = sum(RESULTS), len(RESULTS)
    print(f"\n{good}/{total} assertions passed in {time.time() - t0:.1f}s")
    return 0 if good == total else 1


if __name__ == "__main__":
    sys.exit(main())
