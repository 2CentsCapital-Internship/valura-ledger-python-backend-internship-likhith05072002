"""A local stand-in for the arena server — every behaviour the real one has,
on demand and in miniature.

Why a mock at all: the real arena gives us 12 practice runs, total. Transport
behaviour (rewinds, resumes, 409s, terminal flushes) must be proven *before*
the first live connection, so this server reproduces, deterministically, every
documented stream behaviour:

  * SSE framing exactly as PROTOCOL.md shows it: `event:` / `id:` / `data:`
    lines, blank-line terminated, with control events stream_open /
    stream_reset / stream_end and occasional ':keepalive' comment lines.
  * `?from=` resume: events below the requested offset are not re-sent.
  * A scripted mid-run stream_reset that rewinds the client and re-serves
    events it has already seen (each reset marker fires exactly once, so the
    reconnect proceeds past it instead of looping forever).
  * A scripted silent drop that marks the run finished WITHOUT stream_end —
    the trap that makes a client reconnect into the 409.
  * Submission/final mode: reconnecting after the run finished without
    `new=true` gets HTTP 409 + JSON, like the real server refusing to burn
    another graded attempt.
  * /v1/postings with the 500-postings-per-request cap (413 above it),
    /v1/checkpoint, /v1/me.

Everything received is recorded under a lock (handlers run on server threads):
`received_postings` (flat), `received_checkpoints`, `stream_connects` (query
dict per connect). The drill reads these to assert what the client actually
sent, not what it claims it sent. Stdlib only.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def make_deposits(n: int, start_offset: int = 0, prefix: str = "evt_dep") -> list[dict]:
    """n well-formed deposit events at consecutive offsets. Amounts and
    customers vary deterministically so different prefixes of a scenario
    produce visibly different book states (an as-of reply must differ from a
    final one for the drill to prove anything)."""
    out = []
    for i in range(n):
        off = start_offset + i
        out.append({
            "offset": off,
            "event_id": f"{prefix}_{off:05d}",
            "type": "deposit",
            "payload": {"customer_id": f"CUST-{i % 3 + 1:04d}",
                        "amount": f"{100 + i % 7}.25",
                        "currency": "USD"},
        })
    return out


def reset_marker(resume_from: int) -> dict:
    """Scenario item: send stream_reset {resume_from} and drop the connection.
    Fires once — the reconnect skips it and continues to the rest of the feed."""
    return {"__control__": "reset", "resume_from": resume_from}


def finish_silently() -> dict:
    """Scenario item: mark the run finished and drop the connection WITHOUT a
    stream_end. A resilient client must reconnect (mid-run drops are normal);
    in submission mode that reconnect meets the 409. This is the only way to
    exercise the 409 path against a client that correctly never reconnects
    after a stream_end."""
    return {"__control__": "finish_silent"}


class MockArena:
    """One arena run served on 127.0.0.1:<ephemeral port>.

    scenario: list of ledger-event dicts ({offset, event_id, type, payload} —
    checkpoint_request is just an event whose type says so) interleaved with
    reset_marker()/finish_silently() control items. Default: 20 deposits.
    """

    def __init__(self, scenario: list | None = None, keepalive_every: int = 5) -> None:
        self.scenario = scenario if scenario is not None else make_deposits(20)
        self.keepalive_every = keepalive_every
        # -- everything the client sent us, for the drill's assertions ------
        self.received_postings: list[dict] = []    # flat, in arrival order
        self.received_checkpoints: list[dict] = [] # full bodies, in order
        self.stream_connects: list[dict] = []      # query params per connect
        self.rejected_413 = 0                      # postings requests over the cap
        self.sent_409 = 0                          # post-finish reconnects refused
        self.run_finished = False
        self.connects_at_finish: int | None = None # len(stream_connects) when finished
        self._fired: set[int] = set()              # once-only markers, by index
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    #  lifecycle                                                         #
    # ------------------------------------------------------------------ #
    def start(self) -> "MockArena":
        arena = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"   # keep-alive for the POST traffic

            def log_message(self, *a):      # keep drill output readable
                pass

            def do_GET(self):
                arena._get(self)

            def do_POST(self):
                arena._post(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockArena":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    #  request routing                                                   #
    # ------------------------------------------------------------------ #
    def _json(self, h, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _get(self, h) -> None:
        url = urlparse(h.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path == "/v1/stream":
            self._stream(h, q)
        elif url.path == "/v1/me":
            self._json(h, 200, {"score": None})
        else:
            self._json(h, 404, {"error": f"no such path {url.path}"})

    def _post(self, h) -> None:
        url = urlparse(h.path)
        n = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._json(h, 400, {"error": "body is not JSON"})
            return
        if url.path == "/v1/postings":
            postings = body.get("postings", [])
            if len(postings) > 500:      # the documented per-request cap
                with self._lock:
                    self.rejected_413 += 1
                self._json(h, 413, {"error": "max 500 postings per request"})
                return
            with self._lock:
                self.received_postings.extend(postings)
            self._json(h, 200, {"results": [{"event_id": p.get("event_id"),
                                             "ok": True} for p in postings]})
        elif url.path == "/v1/checkpoint":
            with self._lock:
                self.received_checkpoints.append(body)
            self._json(h, 200, {"ok": True})
        else:
            self._json(h, 404, {"error": f"no such path {url.path}"})

    # ------------------------------------------------------------------ #
    #  the stream                                                        #
    # ------------------------------------------------------------------ #
    def _finish(self) -> None:
        with self._lock:
            if not self.run_finished:
                self.run_finished = True
                self.connects_at_finish = len(self.stream_connects)

    def _stream(self, h, q: dict) -> None:
        with self._lock:
            self.stream_connects.append(dict(q))
            finished = self.run_finished
        if finished and q.get("mode") in ("submission", "final") \
                and q.get("new") != "true":
            # The real server refuses to reopen a finished graded run unless
            # a fresh attempt is asked for explicitly. A client that retries
            # this in a loop is a retry storm burning limited attempts.
            with self._lock:
                self.sent_409 += 1
            self._json(h, 409, {"error": "run already finished; "
                                         "pass new=true to start a new attempt"})
            return

        frm = int(q.get("from", 0))
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("Connection", "close")   # EOF-delimited SSE body
        h.end_headers()
        h.close_connection = True

        def frame(etype: str, obj: dict, offset: int | None = None) -> None:
            lines = [f"event: {etype}"]
            if offset is not None:
                lines.append(f"id: {offset}")
            lines.append(f"data: {json.dumps(obj)}")
            h.wfile.write(("\n".join(lines) + "\n\n").encode())

        try:
            frame("stream_open", {"run_id": "run_mock_1", "resumed_from": frm,
                                  "next_event_in_seconds": 0})
            sent = 0
            for idx, item in enumerate(self.scenario):
                ctl = item.get("__control__")
                if ctl == "reset":
                    with self._lock:
                        if idx in self._fired:
                            continue       # already rewound once: carry on past it
                        self._fired.add(idx)
                    frame("stream_reset", {"resume_from": item["resume_from"]})
                    return                 # drop; client reconnects with ?from=
                if ctl == "finish_silent":
                    self._finish()
                    return                 # drop with NO stream_end (409 bait)
                if item["offset"] < frm:
                    continue               # honour resume: already delivered
                sent += 1
                if self.keepalive_every and sent % self.keepalive_every == 0:
                    h.wfile.write(b":keepalive\n\n")
                frame(item["type"], item, offset=item["offset"])
            self._finish()
            frame("stream_end", {"run_id": "run_mock_1"})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass    # client hung up mid-stream; nothing to record
