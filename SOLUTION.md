# Ledger Arena — solution

My submission. `README.md` is the original starter kit brief; this is what
I built, why it is shaped this way, and how to check it.

    python run_regression.py      # the gate: 263 tests + 10 chaos sections
    python mega_regression.py     # the long pass, deliberately harder than the final
    python tests/drill_client.py  # transport drills against a local mock arena

`NOTES.md` is the running decision log — every ambiguity, the default I
chose, the reasoning, and what evidence later confirmed or corrected it.

---

## The one idea everything rests on

**The book is a pure function of the delivered event sequence.** Same
events in, byte-identical state out — no wall clock, no randomness, no
dependence on dict iteration order.

That single property buys four things that would otherwise each need
their own machinery:

- **as-of checkpoints** — "describe your book as it stood after event X"
  is answered by replaying a prefix, not by keeping parallel history;
- **idempotency** — re-delivery is provably harmless, because replaying
  the log reproduces the same state;
- **crash recovery** — a killed process rebuilds from its own journal;
- **testability** — every referee below can rebuild the book
  independently and compare.

`apply()` is the only method that mutates. Every lifecycle store — seen
ids, refunded fees, withdrawal state machines, trade ownership, order
tombstones — is derived by replay, never poked from outside. Reporting
(`snapshot()`) is a pure read: it writes diagnostics to a channel that is
deliberately *not* part of replayed state, because a read that mutates
would make live state diverge from a replay of the same log.

## Layout

| File | What it is |
| --- | --- |
| `book.py` | The ledger. State, 24 event handlers, the lot book, snapshot and as-of. |
| `tariff.py` | Pure fee and routing math. Zero imports from the book, which is what makes the exact-arithmetic oracle trustworthy. Owns the single `money()` rounding canon. |
| `detectors.py` | The defect detectors and their arming policy. |
| `client.py` | Transport only. The starter's, rewritten — see below. |
| `sim/` | The private arena: chaos generators, seven referees, an independent FIFO oracle, and the fuzz harness. |
| `tools/` | `run_report.py` (read a banked run), `defect_cluster.py` (find the planted class), `detector_ab.py` (the arming rule). |

## Decisions worth defending

**Money.** `Decimal(str(x)).quantize(0.01, ROUND_HALF_UP)` — two places,
half away from zero, every derived amount rounded independently. The
`str()` hop matters: a float that slips out of `json.loads` must convert
by its printed value, not its binary expansion. One implementation, in
one module, imported everywhere.

**Balances are keyed `(customer, account)`.** `transfer_between_customers`
moves money between two customers on the same account — at account level
nothing appears to happen. It also gives the four settlement events their
per-customer payables for free.

**The lot book uses global acquisition sequence numbers, and never
deletes a lot.** FIFO means delivery order, so a `symbol_change` that
merges two holdings must interleave by arrival — free with sequence
numbers, broken with per-symbol queues. Fully-consumed lots stay as
zero-quantity markers so a reversed sale restores its shares into their
original position, and each lot carries an exact `Fraction` split
multiplier so a reversal across a split scales correctly.

**Cost relief is the graded formula, character for character:**
`round(lot_total × sold_qty / lot_qty)`, remainder stays with the lot.
Cost-per-share × quantity is also a FIFO and disagrees by a cent.

**Reversals invert the *stored* legs.** Never recomputed — recomputation
re-rounds and reads drifted state. The lot undo walks recorded effects
backwards, and reversing a no-leg event (a split, a rename) still undoes
its lot effects.

**Never stop.** Malformed payloads, unknown references, and even a bug of
mine cost one event, never the run.

## Detectors: armed on evidence, not on theory

The arena guarantees a planted defect class and describes nothing about
it. The payoff is lopsided — a clean event wrongly rejected costs full
weight, a defect correctly rejected earns quarter weight — so a false
positive costs roughly four times what the catch saves. Everything
observes by default.

Practice run 1's diagnostics identified the class: **a second fill
reusing a `trade_id` an earlier fill already claimed.** The payloads are
perfect in every visible way — principal equals quantity × price, the
broker genuinely trades that asset class — and the flaw only appears
when fills are compared to each other.

The near-miss is the part worth reading: the obvious hypothesis, "fill
price beyond the order's limit," explained both defects *and* fired on
eleven fills the reference accepted. It stayed disarmed. So did D6, which
fired 106 times because my own FX identity was inverted.

A later subtlety: a fill that is settled and then **reversed** frees its
trade in the live store, so a duplicate slipped through. An identifier
that has ever been claimed stays claimed — the check moved to a permanent
set that reversals never clear.

## How it is verified

`run_regression.py` — 263 unit tests plus ten blocking sections: a
25,000-mutant fuzz barrage *with a control group* (a book that rejected
everything would otherwise pass a fuzzer perfectly), five chaos suites,
a 500-point as-of oracle, planted defects scored against ground truth,
and a clean-feed gate where one armed detector firing is a red run.

`mega_regression.py` — the long pass: 200,000+ events across eight feeds
and five generators, one uninterrupted 260,000-event book (43× the
graded final), 1,000 as-of answers, five kill-and-resume cycles
converging byte-identically, and determinism checks.

Seven independent referees judge every feed, including two that catch
the failure mode a trial balance cannot: an **independent FIFO oracle**
(a second, deliberately different cost-basis implementation) and a
**payable cent-audit** that recomputes every fee from stored payloads.
A wrong-but-balanced book passes a trial balance; it does not pass those.

## client.py

The brief says transport is done for you and is not assessed. The copy in
this repo predates the current spec: it dropped `as_of_event_id` (which
silently forfeits every as-of checkpoint), defaulted to a 25-minute
timer against a 75-minute final, abandoned postings past two batches, and
had none of the `new=true`/409 handling the task sheet describes. I fixed
those and added JSONL journaling of the feed and every server response,
because the practice diagnostics are the only feedback channel that
exists and the starter discarded them.

Two fixes came from reading run 1's own data: a re-delivered checkpoint
is now answered with the *first* reply (scores were decaying 0.687 →
0.314 → 0.099 as our state drifted past the offset being asked about),
and the posting drain is far more patient after a burst of 502s cost 17
postings.

## Campaign

| Run | Score | What changed |
| --- | --- | --- |
| Practice 1 | 77.52 | baseline; identified the planted defect |
| Practice 2 | 96.93 | defect detector armed |
| Practice 3 | **99.71** | trade-id permanence; checkpoint caching |

Practice 3: 772 of 772 graded events correct, and all seven checkpoints —
including both as-of queries and the one after a rewind — at a perfect
1.0000 on every part, with full marks on checkpoints, liveness and final
reconciliation.

## What I would do next

- **Resolve the remaining observe-mode detectors with more feeds.** D5,
  D9, D10 and D11 have never justified arming; with more banked runs the
  A/B rig (`tools/detector_ab.py`) could settle each one properly rather
  than leaving them permanently disarmed.
- **Correct the FX identity in D6** so it becomes a usable detector
  rather than a known-inverted one — the observation is real, the formula
  is mine and wrong.
- **Bound the snapshot ring by bytes, not just by count.** At the graded
  scale it is 24 MiB, but the blobs grow with the event store, so a much
  longer run would want a size cap as well.
- **Push the payable audit into production** as a cheap runtime
  assertion, so a fee drift would be visible during a run rather than
  only in the harness.
