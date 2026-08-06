#!/usr/bin/env python3
"""Arena transport. The Book does the accounting; this file only moves bytes.

Rewritten from the starter with nine live-run patches, each one a way the
starter would have lost points on a real run:

  1. --seconds is a 4-hour safety net, not a schedule. stream_end is what
     ends a run; the deadline only catches a server that never says so.
     (The starter's 1500s default would have cut the final off at a third.)
  2. Never crash. Every consume/checkpoint path traps unexpected exceptions,
     logs to stderr and keeps reading; Book.apply self-guards already. A
     rejected event costs one event; a dead process costs the rest of the run.
  3. Terminal drain: flush() loops until nothing is pending — the starter
     sent at most two 500-caps and silently dropped a longer tail. Bounded
     backoff; anything undeliverable is journaled, never lost in memory.
  4. Checkpoints forward the WHOLE request payload — as_of_event_id included
     (the starter dropped it, forfeiting every as-of checkpoint) — and still
     snapshot before any network round trip.
  5. Everything is journaled to JSONL, one flushed line per record: the raw
     feed before processing, every posting request/response (practice
     responses carry the correct legs — the diagnostics bank), every
     checkpoint exchange. A feed burned without logging is gone forever.
  6. The batch flush clock ticks on every SSE line, keepalives included,
     not only on complete messages: a quiet stream must not delay postings.
  7. --resume replays a prior run's feed.jsonl through the Book and moves
     the cursor past the highest logged offset: a crash+restart is a
     non-event. Replayed events are not resubmitted — their first
     submission already won, a duplicate is ignored anyway.
  8. A 409 from /v1/stream (reconnecting after a submission/final run has
     finished) is terminal: print the server's message and stop — never a
     retry storm. --new sends new=true on the first connect only, so a
     limited attempt starts on purpose, never by accident.
  9. Practice run budget: after stream_end we never reconnect — on practice
     a reconnect auto-opens the next of only 12 runs.

    pip install -r requirements.txt
    python client.py --key ak_... [--mode practice] [--url ...]

Read PROTOCOL.md first. It is the whole specification.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback

import httpx

from book import Book

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL = {"stream_open", "stream_reset", "stream_end"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class ArenaClient:
    def __init__(self, url: str, key: str, mode: str, log_dir: str,
                 batch: int = 100, flush_ms: int = 400,
                 fresh: bool = False) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.mode = mode
        self.batch = batch
        self.flush_ms = flush_ms
        self.fresh = fresh                # send new=true on the next connect, once
        self.book = Book()
        self.pending: list[dict] = []
        self.cursor = 0
        # checkpoint_id -> the reply we first sent for it. A rewind
        # re-delivers checkpoint requests; the answer must stay the one
        # taken at that checkpoint's own offset.
        self.answered: dict[str, dict] = {}
        self.stats = {"events": 0, "posted": 0, "checkpoints": 0,
                      "reconnects": 0, "resets": 0, "errors": 0}
        self.done = False
        # -- journals: append mode so --resume continues the same files -----
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._feed_log = open(os.path.join(log_dir, "feed.jsonl"),
                              "a", encoding="utf-8")
        self._post_log = open(os.path.join(log_dir, "postings.jsonl"),
                              "a", encoding="utf-8")
        self._cp_log = open(os.path.join(log_dir, "checkpoints.jsonl"),
                            "a", encoding="utf-8")

    # -- journaling ---------------------------------------------------------
    @staticmethod
    def _log(fh, rec: dict) -> None:
        """One JSON object per line, flushed immediately: the journal must
        survive a crash on the very next line of code."""
        fh.write(json.dumps(rec, default=str) + "\n")
        fh.flush()

    @staticmethod
    def _body(r: httpx.Response):
        """Best-effort response decode. Practice responses carry per-event
        diffs and the full correct legs — never lose one to a parse error."""
        try:
            return r.json()
        except ValueError:
            return r.text[:2000]

    def close(self) -> None:
        for fh in (self._feed_log, self._post_log, self._cp_log):
            try:
                fh.close()
            except OSError:
                pass

    # -- crash recovery -----------------------------------------------------
    def resume_from_log(self) -> None:
        """Rebuild the book from this log dir's feed.jsonl and set the cursor
        past the highest offset we ever received. Replayed events are NOT
        re-queued for submission: their postings went up before the crash,
        and the server keeps the first submission per event regardless."""
        path = os.path.join(self.log_dir, "feed.jsonl")
        if not os.path.exists(path):
            print(f"  resume: no feed.jsonl in {self.log_dir}", flush=True)
            return
        replayed = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # a torn final line from the crash itself
                ev = rec.get("data")
                if rec.get("event") in CONTROL or not isinstance(ev, dict):
                    continue
                off = ev.get("offset")
                if isinstance(off, int):
                    self.cursor = max(self.cursor, off + 1)
                if (ev.get("type") == "checkpoint_request"
                        or not isinstance(ev.get("event_id"), str)):
                    continue          # cursor advanced; nothing to re-book
                self.book.apply(ev)   # idempotent: duplicates in the log no-op
                replayed += 1
        print(f"  resume: replayed {replayed} events, "
              f"cursor at {self.cursor}", flush=True)

    # -- submitting ---------------------------------------------------------
    def flush(self, http: httpx.Client) -> bool:
        """Send ONE batch of up to 500 pending postings. True when the server
        accepted it; on any failure the batch goes back to the front of the
        queue so nothing is ever silently dropped."""
        if not self.pending:
            return True
        body, self.pending = {"postings": self.pending[:500]}, self.pending[500:]
        try:
            r = http.post(f"{self.url}/v1/postings", params={"mode": self.mode},
                          json=body, timeout=30)
            if r.status_code == 429:
                self._log(self._post_log, {"ts": utc_now(), "status": 429,
                                           "retry_after": r.headers.get("Retry-After")})
                # A server-supplied sleep is server-controlled: clamp it,
                # or one malformed header parks the client for hours.
                try:
                    wait = float(r.headers.get("Retry-After", 5))
                except (TypeError, ValueError):
                    wait = 5.0
                time.sleep(min(max(wait, 0.0), 30.0))
                self.pending = body["postings"] + self.pending
                return False
            # Log the exchange before raise_for_status: an error body is
            # exactly the diagnostic we would want to have kept.
            self._log(self._post_log, {"ts": utc_now(), "request": body,
                                       "status": r.status_code,
                                       "response": self._body(r)})
            r.raise_for_status()
            self.stats["posted"] += len(body["postings"])
            return True
        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            self.pending = body["postings"] + self.pending
            if not isinstance(exc, httpx.HTTPStatusError):  # already journaled
                self._log(self._post_log, {"ts": utc_now(),
                                           "error": f"{type(exc).__name__}: {exc}"})
            time.sleep(1)
            return False

    def drain(self, http: httpx.Client, max_failures: int = 60) -> None:
        """flush() until nothing is pending. Each failed flush already slept
        (Retry-After or 1s); we back off on top, and after max_failures
        consecutive failures we journal what could not be delivered and give
        up — an unreachable server must not hold the process hostage.

        Practice run 1 met a burst of HTTP 502s and the old ceiling of 8
        failures (~150s of retrying) gave up on 17 postings, each of which
        scores zero. A graded run is 60-75 minutes long: patience is
        nearly free and every abandoned posting is points on the floor.
        60 failures with the 30s cap is ~25 minutes of trying, and the
        postings stay in `pending` either way, so a later flush picks up
        whatever this one abandons."""
        failures = 0
        while self.pending:
            if self.flush(http):
                failures = 0
                continue
            failures += 1
            if failures >= max_failures:
                self._log(self._post_log, {"ts": utc_now(),
                                           "undelivered": self.pending})
                print(f"  UNDELIVERED: {len(self.pending)} postings "
                      f"(journaled to postings.jsonl)", file=sys.stderr)
                return
            time.sleep(min(2 ** failures, 30))

    def checkpoint(self, http: httpx.Client, ev: dict) -> None:
        """Snapshot FIRST, send second.

        The reply must describe the book as at the checkpoint's place in the
        stream — and an as-of checkpoint names an earlier event, so the whole
        payload goes to the Book, not just the id. Postings are drained on a
        short leash first: they matter, the 60s grace period matters more.
        """
        p = ev.get("payload") or {}
        cp_id = p.get("checkpoint_id")
        # A checkpoint describes the book AT THE CHECKPOINT'S OFFSET. The
        # server rewinds us mid-run and re-delivers the same
        # checkpoint_request, and answering it again from our now-advanced
        # state is strictly wrong — practice run 1 answered cp_postchaos
        # three times and scored 0.687, then 0.314, then 0.099 as our
        # state drifted further past the point it was asking about. Send
        # the FIRST answer again, verbatim: it was taken at the right
        # offset, and re-sending covers a first POST that never landed.
        if cp_id in self.answered:
            reply = self.answered[cp_id]
        else:
            snap = self.book.snapshot(p.get("as_of_event_id"))
            reply = {"checkpoint_id": cp_id, **snap}
            self.answered[cp_id] = reply
        self.drain(http, max_failures=2)
        # Checkpoints are 40 of 100 points and the grace period is 60s —
        # there is room for a second attempt, and one dropped connection
        # must not silently forfeit a whole checkpoint.
        for attempt in (1, 2):
            try:
                r = http.post(f"{self.url}/v1/checkpoint",
                              params={"mode": self.mode},
                              json=reply, timeout=20)
                self.stats["checkpoints"] += 1
                self._log(self._cp_log, {"ts": utc_now(), "request": p,
                                         "reply": reply,
                                         "status": r.status_code,
                                         "response": self._body(r)})
                return
            except httpx.HTTPError as exc:
                self.stats["errors"] += 1
                self._log(self._cp_log, {"ts": utc_now(), "request": p,
                                         "reply": reply, "attempt": attempt,
                                         "error": f"{type(exc).__name__}: {exc}"})
                if attempt == 1:
                    time.sleep(1)

    # -- consuming ----------------------------------------------------------
    def handle(self, ev: dict) -> None:
        # Belt and braces: Book.apply already traps everything it can, but
        # a submission is worth points and an escaped exception is worth
        # none. If the book somehow throws, submit no legs and carry on —
        # one event lost instead of the rest of the run.
        try:
            legs = self.book.apply(ev)
        except Exception:
            self.stats["errors"] += 1
            traceback.print_exc(file=sys.stderr)
            legs = []
        # An event you correctly reject still needs a submission, with no legs.
        self.pending.append({"event_id": ev["event_id"], "legs": legs or []})
        self.stats["events"] += 1

    def _dispatch(self, http: httpx.Client, etype: str | None,
                  data: str) -> bool:
        """Process one complete SSE message. Returns True when the read loop
        should end (rewind or run over). Journals the message BEFORE touching
        it: the feed log is the crash-recovery source of truth."""
        ts = utc_now()
        try:
            ev = json.loads(data)
        except ValueError as exc:
            self._log(self._feed_log, {"ts": ts, "event": etype,
                                       "raw": data, "error": str(exc)})
            return False              # unparseable frame: journal it, move on
        self._log(self._feed_log, {"ts": ts, "event": etype, "data": ev})

        if etype == "stream_open":
            nxt = ev.get("next_event_in_seconds")
            print(f"  connected: run {ev.get('run_id')}, "
                  f"resumed at {ev.get('resumed_from')}, "
                  f"next event in {nxt}s", flush=True)
            return False
        if etype == "stream_reset":
            # The server deliberately rewinds you and re-sends events you
            # have already seen. Reconnect and carry on: the Book is
            # idempotent, so this costs nothing.
            self.cursor = ev.get("resume_from", self.cursor)
            self.stats["resets"] += 1
            self.flush(http)
            return True
        if etype == "stream_end":
            self.done = True          # run() drains, and NEVER reconnects
            return True

        self.cursor = max(self.cursor, ev.get("offset", 0) + 1)
        if ev.get("type") == "checkpoint_request":
            self.checkpoint(http, ev)
        else:
            self.handle(ev)
        return False

    def consume(self, http: httpx.Client, deadline: float) -> None:
        params = {"mode": self.mode, "from": self.cursor}
        if self.fresh:
            params["new"] = "true"
        last_flush = time.time()
        # read=300: a dead connection must eventually raise and reconnect —
        # with no read timeout even the deadline cannot fire on silence.
        with http.stream("GET", f"{self.url}/v1/stream", params=params,
                         timeout=httpx.Timeout(30, connect=20, read=300)) as r:
            if r.status_code == 409:
                # Reconnected to a submission/final run that already ended.
                # Terminal by design: retrying spins forever, and new=true
                # here would burn a limited attempt by accident.
                r.read()
                print(f"  run already finished (409): {self._body(r)}",
                      flush=True)
                self.done = True
                return
            r.raise_for_status()
            self.fresh = False        # new=true is spent once a connect lands
            etype = data = None
            for line in r.iter_lines():
                if time.time() > deadline:
                    return
                # The flush clock ticks on EVERY line — SSE keepalive and
                # comment lines included — so pending postings never sit out
                # a quiet stretch of stream.
                if (len(self.pending) >= self.batch
                        or (self.pending
                            and (time.time() - last_flush) * 1000 > self.flush_ms)):
                    self.flush(http)
                    last_flush = time.time()
                if line.startswith("event:"):
                    etype = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                elif line == "" and data is not None:
                    try:
                        stop = self._dispatch(http, etype, data)
                    except Exception:
                        # Nothing a single message does may kill the run:
                        # journal the scream, keep consuming.
                        self.stats["errors"] += 1
                        traceback.print_exc(file=sys.stderr)
                        stop = False
                    etype = data = None
                    if stop:
                        return

    def run(self, max_seconds: float) -> dict:
        deadline = time.time() + max_seconds
        headers = {"Authorization": f"Bearer {self.key}"}
        with httpx.Client(headers=headers) as http:
            # done is sticky: once stream_end (or a 409) has been seen this
            # loop never reconnects — on practice a reconnect would silently
            # open the next of only 12 runs.
            while time.time() < deadline and not self.done:
                try:
                    self.consume(http, deadline)
                except httpx.HTTPError as exc:
                    self.stats["reconnects"] += 1
                    print(f"  reconnecting after {type(exc).__name__}",
                          flush=True)
                    time.sleep(1)
                except Exception:
                    # Only stream_end or the deadline ends the run. Anything
                    # else is a bug to read about in stderr afterwards.
                    self.stats["errors"] += 1
                    self.stats["reconnects"] += 1
                    traceback.print_exc(file=sys.stderr)
                    time.sleep(1)
            self.drain(http)          # terminal: everything pending goes up
            try:
                me = http.get(f"{self.url}/v1/me", params={"mode": self.mode},
                              timeout=20).json()
            except (httpx.HTTPError, ValueError):
                me = {}
        return {"stats": self.stats, "me": me}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://hiring-arena.twocc.in")
    ap.add_argument("--key", required=True, help="your API key from the portal")
    ap.add_argument("--mode", default="practice",
                    choices=["practice", "submission", "final"])
    ap.add_argument("--seconds", type=float, default=14400,
                    help="safety net only; stream_end is what ends a run")
    ap.add_argument("--log-dir", default=None,
                    help="journal dir (default runs/<mode>-<UTC timestamp>/); "
                         "pass a previous run's dir together with --resume")
    ap.add_argument("--resume", action="store_true",
                    help="replay feed.jsonl from --log-dir to rebuild state "
                         "and continue past the highest logged offset")
    ap.add_argument("--new", dest="fresh", action="store_true",
                    help="send new=true on the first connect: deliberately "
                         "start a fresh attempt")
    a = ap.parse_args()

    if a.mode != "practice":
        print(f"\n  You are about to start a {a.mode.upper()} run.")
        print("  Attempts are limited and this one will count.")
        if input("  Type the mode name to continue: ").strip() != a.mode:
            print("  Cancelled.")
            return 1

    log_dir = a.log_dir or os.path.join(
        REPO_DIR, "runs",
        f"{a.mode}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}")

    c = ArenaClient(a.url, a.key, a.mode, log_dir=log_dir, fresh=a.fresh)
    try:
        if a.resume:
            c.resume_from_log()
        print(f"connecting to {a.url} as {a.mode} ...", flush=True)
        print(f"journaling to {log_dir}", flush=True)
        out = c.run(a.seconds)
    finally:
        c.close()

    print("\nstats:", json.dumps(out["stats"]))
    todo = getattr(c.book, "todo", {})
    if todo:
        print(f"\nnot implemented yet ({sum(todo.values())} events skipped):")
        for t, n in sorted(todo.items(), key=lambda kv: -kv[1]):
            print(f"  {t:<30} {n:>5} events")

    me = out.get("me") or {}
    if me.get("score") is not None:
        print(f"score: {me['score']}")
        for k, v in (me.get("breakdown") or {}).items():
            print(f"  {k:<26} {v['points']:>6} / {v['max']}")
    else:
        print("score: withheld on this tier")
    return 0


if __name__ == "__main__":
    sys.exit(main())
