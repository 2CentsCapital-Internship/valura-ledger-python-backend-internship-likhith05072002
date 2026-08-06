# Final run — the one attempt

One shot, no retry, score withheld until submissions close. This is the
sequence, written down so nothing is decided in a hurry.

## Before starting

- [ ] `python run_regression.py` exits 0 (11 sections)
- [ ] `python tests/drill_client.py` → 24/24
- [ ] `python mega_regression.py` → ALL GREEN
- [ ] `git status` clean; everything committed and pushed
- [ ] `GET /v1/rules` → confirm `final` duration and event count, and read
      `seconds_to_deadline` with your own eyes
- [ ] Machine: on power, no sleep, nothing else heavy running
- [ ] Detector modes are the shipping set: D1/D3/D8 ARMED, rest OBSERVE

## Timing

The final is ~75 minutes nominal, but the stream is staggered against
every other candidate's and drains its tail after the nominal duration.
**Budget 100 minutes and start with at least 2 hours of deadline left.**
`stream_end` decides when the run is over — never our own clock.

## The command

    cd valura-ledger-python-backend-internship-likhith05072002
    echo "final" | QUARANTINE_LOG=runs/final-quarantine.jsonl \
      python client.py --key ak_... --mode final --new --log-dir runs/final

- `--new` is REQUIRED: on submission and final the server will not open a
  run without it, and a reconnect without it correctly gets a 409 rather
  than silently spending the attempt.
- The confirmation prompt is fed on stdin (`echo "final"`), because the
  client refuses to start a graded tier without the mode typed back.
- `--seconds` defaults to 14400, a safety net far past the run length.

## While it runs

- Watch for `INCORRECT` — the final tier returns no per-event diffs, so
  the only live signals are acceptance, checkpoint `on_time`, and the
  client's own stats line.
- Do NOT touch the code. A graded run is not the place to improve
  anything.
- If the process dies: restart with `--resume --log-dir runs/final` and
  the SAME log dir. The feed journal rebuilds the book and the cursor
  continues; first-submission-wins makes the overlap harmless. Do NOT
  pass `--new` on a resume — that would try to spend an attempt that no
  longer exists.
- A 502 burst is survivable: the drain retries for ~25 minutes and the
  postings stay queued.

## After it ends

- [ ] Confirm the stats line shows `stream_end` was reached (not a
      deadline stop) and that `posted` equals `events`
- [ ] Run `python tools/run_report.py runs/final` and bank the numbers
- [ ] Commit the run's NOTES entry and push — the repo is what they read

## What is deliberately NOT being done

- No detector is armed beyond D1/D3/D8. D4 fired 58 times on a real feed
  where the reference rejected 2; D6 fired 106 times because our own FX
  identity is inverted. Arming on theory would cost roughly 4x what it
  saves.
- No re-tuning of the snapshot ring: at the final's scale it is 24 MiB
  and answers as-of in ~44 ms against a 60,000 ms grace. The 1.8 GiB seen
  in the mega-regression is an artifact of testing at 43x the real size.
- No last-minute "improvements". The build that scored 99.71 on practice
  with 772/772 events correct and seven perfect checkpoints is the build
  that runs.
