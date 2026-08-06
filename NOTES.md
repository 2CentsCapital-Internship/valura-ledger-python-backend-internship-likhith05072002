# Engineering notes — Ledger Arena

Decision log for this implementation. Kept current so every line of code has
its reasoning on record.

## Canon (decided Phase 0, used everywhere)

- **Money**: `Decimal(str(x)).quantize(0.01, ROUND_HALF_UP)` — 2 dp, half away
  from zero, each derived amount rounded independently before use (spec §4).
  The `str()` hop means a float that slipped out of `json.loads` converts by
  printed value, not binary expansion.
- **Quantities**: 6 dp; reported in minimal form (`"8"`, never `"8.000000"`,
  never scientific notation).
- **Balances** keyed `(customer_id, account)` — required by
  `transfer_between_customers` (account nets to zero) and it gives the four
  fee-settlement events their per-customer payable balances for free.

## Architecture (decided Phase 0)

- **Book is a pure function of the delivered event sequence.** No clock, no
  randomness. Enables: as-of checkpoints by replay, idempotency by
  construction, crash recovery, deterministic tests.
- **Append-only event log** of first deliveries (including rejected events —
  an as-of checkpoint may name one). Duplicates are not re-logged; first
  delivery wins forever, including conflicting duplicates.
- **Snapshot ring** every 250 log entries (pickle = deep copy). As-of answers
  restore the nearest snapshot ≤ target and replay ≤ 250 events: bounded well
  under the 60 s checkpoint grace.
- **Single mutation entry point** (`Book.apply`); handlers validate first,
  mutate last; any rejection leaves the book byte-identical.
- **Never stop.** Malformed payloads, unknown references, our own bugs: the
  event is dropped with `legs: []` and the stream keeps flowing. A server
  that stalls misses everything after it.

## Client transport fixes (vs. the starter)

The shipped `client.py` had gaps against the live spec; all fixed in Phase 0:
`--seconds` default too short for the final; only `httpx.HTTPError` caught;
terminal flush capped at ~1000 postings; `as_of_event_id` not forwarded;
response bodies (practice diagnostics) discarded; flush timing skipped on
keepalive lines; no 409 handling on submission/final reconnects; reconnect
after `stream_end` could auto-open a fresh practice run (12-run budget).
Run logs (feed / postings / checkpoints JSONL) are banked under `runs/`
(git-ignored) and used as an offline regression oracle.

## Ambiguity ledger (running)

| ID | Question | Default | Status |
|---|---|---|---|
| A1 | Ticket fee folded into broker-cost line (5000/241x) and into partner-share cost | fold in — the worked example shows no separate ticket leg | pending practice confirmation |
| A3 | Zero-amount legs | omit; "account touched" tracked separately for the trial balance | pending |
| A4 | Hold release: accumulate rounded releases (remainder at close) vs recompute | accumulate (FIFO-relief precedent) — both behind a flag | pending |
| A12 | trade_settled for a reversed fill | reject (one-line policy flip if practice disagrees) | pending |

(Full ledger A1–A14 in the phase plan; entries land here as they resolve.)

## Phase 6 decisions (checkpoints, as-of, routing)

- **A9 (as-of names an id we never received)**: answer with live state,
  record it loudly in `report_log`, never raise, never stall. Unreachable
  in principle — every first delivery is logged, rejects included.
- **Customer universe rule (one rule everywhere)**: register a customer
  when an accepted event leaves REAL state — a balance, a lot, or a live
  hold. A no-op corporate action (split/rename for a customer holding
  nothing) and a placement that was already dead on arrival register
  nothing; an all-zero customer entry would be the customer analogue of a
  phantom position. Practice diagnostics can flip this in one place.
- **Reporting never mutates ledger state**: `snapshot()` writes to
  `report_log`, which is deliberately NOT in `_STATE_KEYS`. A checkpoint
  is a read; if a read touched pickled state, live state would diverge
  from a replay of the same log (decision 10).
- **`snapshot()` cannot raise**: any internal failure degrades to the live
  snapshot (and, in the worst case, to empty dicts) with a loud record. A
  checkpoint answered badly still scores; an exception kills the run.
- **Snapshot is single-pass** over orders and lots, not per customer —
  the per-customer helpers remain as independent witnesses for tests and
  referees, but a checkpoint is O(orders + lots), not O(customers × …).
  Measured effect: as-of p95 123 ms → 96 ms.
- **Ring is bounded** (`RING_MAX = 24`, thinning the older half): blob
  size grows with the log, so an uncapped ring is O(n²) memory (71 MiB at
  10k events). Thinning is always safe — a sparser ring only lengthens
  replay, never changes an answer. Now 53 MiB, and bounded.
- A10 (fill-only stubs excluded from routes — no limit_price, no
  computable route) and A14 (routes fixed at placement on the original
  quantity) confirmed by a real crossover fixture in the gate.

## Phase 5 decisions (reversals + fee settlements)

- **Inverse legs are the STORED legs with sides swapped, verbatim.** Never
  recomputed — recomputation re-rounds or reads drifted state. Originals
  with omitted zero legs reverse to exactly what was posted, nothing more.
- **Reject matrix (all stay rejected forever)**: unknown reference (R1),
  reference we rejected (R2), already-reversed original (R3),
  reversal-of-reversal (R4/A5 default).
- **Lot undo is generic**, walking stored lot_ops backwards: add → zero
  the REMAINDER (a partially-consumed lot clamps, never negative — L5);
  consume → restore in place on the original lot, qty scaled by
  (current ÷ recorded) exact-Fraction multiplier, cost cents verbatim
  (L6/L7/M5); split → recorded prior quantities + exact ratio division;
  rekey → move back by lot id wherever each lot now lives.
- **Side tables**: reversed fee leaves the refund lookup (R5); reversed
  UNSETTLED fill's trade is deleted so its settlement rejects
  (`SETTLE_REVERSED_FILL = False`, the A12 one-line flip); a reversed
  SETTLED fill stays settled — cash already moved (R7); reversed
  withdrawal requests close terminally. Reversal of a settlement needs no
  special code: the generic inverse re-raises the payable (R8).
- **Holds are never restored** by any reversal.
- **Settlements**: amount = the accumulated per-(customer, account)
  balance — never the payload; cent-rounded per-fill accruals make the
  settlement audit exact (M4). Strictly-positive outstanding required
  (zero AND negative reject, R9). Per-broker accounts 2411/2412/2413.
- **Known spec-literal skew (documented)**: reversing a buy whose lot was
  partially consumed posts the full-principal inverse while only the
  remainder leaves the lot book — 2100 and Σ lot costs then differ by the
  consumed cost. The spec's exact-inverse rule forces this; the arena
  most likely never reverses partially-consumed buys. The reversal now
  records its ACTUAL undo deltas (`undo_add` / `undo_consume`) so the
  referees reconcile the gap from state instead of hiding it.
- **A8 refined by review**: split-undo restores the recorded prior
  quantity only when the lot is untouched since the split; a lot sold in
  between un-scales its CURRENT quantity. Restoring the recorded quantity
  there would resurrect consumed shares as a phantom position with no
  cost behind it (and phantom positions are penalized). Immediate
  reverse-split still restores exactly (3→1 of 10 → 10).
- Consume-undo scaling runs in a 60-digit context so no chain of splits
  can round before the 6 dp quantize.

## Phase 4 decisions (corporate actions)

- **All four events are strictly per-customer** (the spec repeats it): every
  lot lookup filters (customer_id, symbol); a split for one customer says
  nothing about anyone else's position in that symbol.
- **dividend_cash posts the net, raises no tax payable, ever.** net ≠
  gross − tax is defect class D2: quarantined now, armed only in Phase 7
  after ≥2 clean practice feeds (`ARM_D2` flag). Zero-position dividends
  post (phantom detector is observe-only — dividend-before-buy ordering).
- **dividend_reinvested never touches cash** — Dr 1200 / Cr 2100 only; the
  new lot (qty = reinvest_quantity, cost = net) queues at the back of the
  global FIFO like any buy (L8). D7 (net vs price×qty, cent tolerance)
  observes to quarantine.
- **stock_split scales qty per lot (6 dp), cost unchanged**; the lot's
  split multiplier is an **exact Fraction product, never quantized** —
  Phase 5's reversal-across-a-split depends on current ÷ recorded being
  exact. Zombies get the multiplier update too.
- **symbol_change re-keys, never renumbers**: seq and multiplier ride
  along; merge collisions interleave by global sequence (A6 default
  "sequence" — FIFO means delivery order and a rename doesn't change
  arrival; `SYMBOL_MERGE="existing_first"` behind the flag via merge_rank).
- Flags are frozen before any live run — flipping SYMBOL_MERGE mid-run
  would diverge replay identity (noted; not a live concern).

## Phase 3 decisions (orders, fills, FIFO, T+2)

- **Cost basis = principal only.** No fee ever touches a lot or 2100 — the
  25.6-pt block lives or dies on this line.
- **FIFO = global acquisition sequence, delivery order.** Lots are never
  deleted: fully-consumed lots stay as zero-qty zombies holding their seq
  (Phase 5 sell-reversals restore in place; Phase 4 symbol merges
  interleave correctly). Each lot carries a split multiplier (Phase 4/5).
- **Plan-then-commit sells**: the oversell check and the whole FIFO walk
  happen before any mutation — "do not leave lots half-consumed."
- **Fills price off their own payload** (broker/asset_class/partner_rate),
  never the placement's, never our route. Broker/class mismatch → reject
  (D1, cheap and safe). Min fee is per fill.
- **T+2**: no 1100 leg in any fill; 2350/1150 carry the obligation until
  trade_settled discharges it at the stored principal. Unknown/double
  settle → reject and stays rejected (A7). `SETTLE_REVERSED_FILL = False`
  (A12 one-line flip).
- **Holds**: buy = money(qty × limit + est_charges) (A13 single-quantize;
  est_charges read with est_commission fallback, A11); release per
  `tariff.HOLD_FORMULA` (A15, default "b" accumulate); clamp at zero;
  close zeroes both money and share holds. Partials never release the
  remainder (the starter's delegation trap — unpicked).
- **Routes** fixed at placement on the original quantity (A14); closed and
  never-placed orders excluded from open_order_routes (A10 noted).
- **Quarantine log** (observe-only, never affects posting): duplicate
  trade_ids (D8), fill-after-close (S7), overfills (L11) — the defect-hunt
  breadcrumbs for Phase 7.

## Phase 2 decisions (tariff + routing)

- **A1 (ticket fold)**: the flat ticket folds into the broker-cost line —
  `bc = money(P × broker_cost_bps) + ticket`, one 5000/241x amount, no
  separate ticket leg. Reasoning: the graded 13-leg buy example shows no
  ticket line, and the example must be complete. Confirm via practice P1
  buy-fill full legs (BRK-B's 3.00 ticket makes it unmissable).
- **A2 (partner margin includes ticket)**: ps = rate × ((b+c) − (bc+cc))
  with the FOLDED bc — the spec says "a quarter of fills are loss-making
  *because of the ticket*", which is only true if the ticket is in the
  margin. Same practice confirmation; the BRK-B P=100 fixture (margin
  −0.56 → ps 0.00) is the discriminator.
- **A13 (hold quantize)**: buy hold = `money(qty × limit + est_charges)` —
  single quantize of the sum. Practice cash_hold diffs decide vs
  `money(qty×limit) + est_charges` (they differ by at most a cent).
- **A14 (routing notional)**: original placement quantity, never remaining
  after partials — the spec's words are "quantity × limit_price"
  (placement fields).
- **A15 (hold release)**: accumulate per-fill rounded releases, remainder
  at close (formula "b", FIFO-relief precedent) behind `HOLD_FORMULA`;
  formula (a) is one constant away. One practice checkpoint with a
  partially-filled open order discriminates (333.34 vs 333.33 fixture).
- **money() canon moved to tariff.py**; book.py imports it (book → tariff,
  never the reverse). One rounding implementation in the repo.
- **Rounding placement law**: floor AFTER rounding b; ticket added AFTER
  rounding the bc bps part; margin/ps only ever computed from
  already-rounded components. The Phase 5 settlement events audit
  accumulated per-fill cents — one unrounded intermediate desynchronizes
  that audit and zeroes the all-or-nothing firm block.

### Phase 3 obligations inherited from the Phase 2 review

- Hold-release dispatch must branch on `tariff.HOLD_FORMULA` (the flag is
  caller-consulted by design) — never hardcode one formula function.
- Fill handlers pass `money(principal)`-normalized principals into
  `fill_charges` (they do via its own normalization; keep it that way).

## Phase 1 decisions (cash engine)

- **A3 applied**: zero-amount legs are omitted from the wire (interest with
  share == gross → no 4200 leg; share == 0 → no 2010 leg; fx zero spread →
  no 4100 leg) but the account is still registered as touched, so the trial
  balance reports it at 0.00. Falsifiable by practice run P1 diagnostics.
- **R10**: the fx reject condition compares `usd_at_customer_rate` vs
  `usd_at_market_rate` (the USD amounts), never the raw rate fields —
  quote orientation can invert raw-rate ordering. Only *strictly* better
  rejects; equal is a legal zero-spread deposit.
- **R11**: a refund arriving before its fee is rejected and stays rejected
  when the fee later arrives — "an id you have seen is an id you have
  seen." A NEW refund event for that fee posts normally.
- **R12**: withdrawal state machine is terminal (requested → settled |
  rejected, once); duplicate withdrawal_id requests reject, first amount
  stands.
- **Refund bookkeeping**: `refunded` is keyed by `refunds_source_id`, never
  the refund's own event id — a redelivered refund is a duplicate (no-op);
  a second distinct refund of the same source is the error (reject). Legs
  post at the STORED fee's amount and customer, not this payload's.
- **S10 on deposit**: the worked example gained strictly-positive amount
  validation (legs unchanged on the happy path). A negative deposit is
  bad data, same as a negative fee.
- **Wallet oracle**: sim/invariants.py recomputes every customer's wallet
  independently from first-delivered payloads and must match the ledger
  cent-for-cent after 10k chaos events — the referee for this phase's
  9 pts of wallet-cash checkpoint weight.

### Phase 3 review outcomes

- Two MAJOR fidelity findings, both fixed same-session: (1) a rejected
  placement missing only `symbol` left an inert stub (validation hoisted
  above stub creation — byte-identical rejects restored); (2) A13
  double-quantize — `est_charges` was money-rounded before the
  single-quantize hold sum (now used as given, one quantize of the sum).
- Observed, not changed (practice will rule): a tiny sell where the
  min-fee floor exceeds proceeds posts a negative wallet credit (literal
  reading of Cr 2010 = P−b−c−r) — quarantined as `negative_sell_net`.
- A duplicate-trade_id fill posts its legs (consistent with lot/balance
  identities) but stores no trade — its 2350/1150 can never settle;
  tracked by `invariants.dup_fill_stuck` and quarantined (D8 observe).
- Phase 5 must not assume "posted ⟺ mutated" around the (unreachable)
  `_post`-failure path.

### Phase 4 review outcomes

- One MEDIUM finding, fixed same-session: under the non-default
  `SYMBOL_MERGE="existing_first"` flag, a chained no-collision rename left
  stale merge ranks that let a newer buy jump ahead of older moved lots.
  Fix: rank per lot in current FIFO order on collision merges, and new
  buys inherit the holding's highest rank. Default-flag behavior
  bit-identical; reviewer's repro now relieves 25.00 correctly.
- Rekey lot_ops gain a shape-stable 6th element (prior ranks) only under
  the flag, so a Phase 5 reversal can restore them; the canonical 5-tuple
  is unchanged by default.
- Phase 5 obligations: split undo must reconstruct the exact ratio from
  the stored payload (`Fraction(Decimal(str(ratio)))` — exact, verified
  incl. "1.5" → 3/2); never a quantized value.
- Referee gap found+fixed in the rig (not the book): wallet oracle didn't
  credit dividend_cash.

### Phase 5 review outcomes

- **F1 (HIGH), fixed**: reversing a duplicate-trade_id fill deleted the
  FIRST fill's trade, stranding its settlement and its 2350 principal.
  Trades now record `src` (the owning fill's event id); only that fill's
  reversal may remove the trade.
- **F3 (MEDIUM), fixed**: split-undo could resurrect shares sold after the
  split as a zero-cost phantom position — see the A8 refinement above.
- **F5 (LOW), fixed**: 60-digit context around the consume-undo scaling.
- **F6 (LOW), fixed**: reversals now store their own undo ops (work-order
  item 7), which is also what lets the referees reconcile the partial-undo
  skew.
- **F2 (MEDIUM)**: ruled *book-correct, referee under-specified* — the
  identity referee now accounts for the partial-undo gap explicitly.
- Rig fixes landed alongside: reversal-aware wallet oracle, FIFO oracle,
  dup-stuck and identity referees, plus the new M4 payable cent audit.

### Phase 7 review outcomes (final review before the live campaign)

- **Regression I introduced, caught and fixed**: the detector pass read
  `ev["payload"]` OUTSIDE the broad-except window, so a payload-less
  event raised out of `apply()` — a violation of the one rule that
  outranks all others (nothing stops the stream). Now `ev.get(...)`.
- **Placement residue fixed**: `est_charges` can be finite yet
  un-quantizable (10**30, 1e308, a 400-digit string). The order stub was
  created before the hold was computed, so the rejection left a phantom
  order and customer. The hold is now computed before the stub exists.
- **The most valuable finding — D1 could have been catastrophic.** If the
  arena's `asset_class` vocabulary differs from ours at all, `covers()`
  returns False for EVERY fill and the armed D1 would have rejected the
  entire run. D1 now only fires when the class is one we recognise: an
  unknown vocabulary is evidence our assumption is wrong, not evidence of
  a defect.
- D1/D2/D3 are now driven by the mode table, so any of them can be
  disarmed (or D2 armed) from the command line without a code edit —
  which is what makes the A/B deployment rule usable mid-campaign.
- Client hardening: `handle()` traps anything escaping the book,
  `Retry-After` is clamped to 30 s (a server-controlled sleep must not
  park the client for hours), and **checkpoints now retry once** — the
  60 s grace has room, and one dropped connection was silently forfeiting
  a checkpoint out of a 40-point block.
- Measured, not assumed: book state at 12k events = 76 MiB (ring 69 MiB,
  capped by count — the earlier "53 MiB" note was measured on a shorter
  feed); `apply` p50 14 µs / p95 90 µs; worst-case as-of 17 ms. Nothing
  in the book is unbounded.

## The mega-regression (`mega_regression.py`)

Run before any graded attempt. `run_regression.py` is the fast per-phase
gate; this is the long punishing pass, deliberately harder than the
arena's own final on every axis:

| | arena final | mega-regression |
|---|---|---|
| events | 6,000 | 200,000+ across 8 feeds |
| feeds | 1 | 8, from 5 different generators |
| replays | ~400, once | 4 rewinds per feed |
| longest single book | 6,000 | 250,000 (~40×) |
| judges | their reference | all 7 of our referees on every feed |

Six sections: breadth (every generator × every trap × every referee),
endurance (one uninterrupted book many times the final's length, with
latency percentiles compared early-run vs late-run so degradation shows
up), as-of at scale (1,000 answers with p95 measured against the 60 s
grace), kill/resume recovery (5 cycles converging byte-identically),
defects + the false-positive gate, and determinism (identical runs,
cold replay, and proof that answering checkpoints does not perturb the
book).

**Full-scale result (2026-08-06): ALL GREEN.** 208,049 events across 8
feeds (35× the final) · one 260,193-event book (43×) with `apply`
p50 18 µs / p95 170 µs · 1,000 as-of answers · 5 kill/resume cycles all
converging byte-identically · 186/186 armed-class defects caught,
682/682 observe-class seen, 0 armed false positives over 78,158 clean
events · determinism intact.

**Scaling read honestly.** At 43× the final the ring reaches 1.8 GiB and
as-of p95 1.75 s — both are artifacts of testing far past the operating
point (ring blobs grow with the event store, and 24 kept snapshots
across 260k events means long replays). Measured at the ACTUAL final
scale — 6,193 log entries including a 400-event replay — the numbers are:
`apply` p50 15 µs / p95 94 µs, as-of p50 18 ms / p95 44 ms / worst 47 ms
against a 60,000 ms grace, ring 24 MiB, total state 2 MiB. Deliberately
NOT re-tuning the ring for a scale the graded run cannot reach: changing
proven code hours before a deadline is a risk with no payoff at the
operating point.

Caution note worth keeping: the first version of the defect section read
the wrong stats key and reported "0 planted, 0 caught" — a **vacuous
pass**. It now fails loudly if nothing was planted, and additionally
asserts that every observe-class defect was actually SEEN, not merely
that nothing broke.

## Phase log

- **Phase 7 — DONE, gate green (2026-08-06). BUILD COMPLETE.**
  Fuzz hardening + the D1–D11 defect rig (D1/D3 armed, everything else
  observing) + quarantine side-channel + cluster and A/B decision tools ·
  **263 unit tests** · 11 blocking gate sections · 25,000-mutant fuzz
  barrage with a per-type control group: zero escaped exceptions, zero
  state residue · 400 planted defects across 2 seeds: **100% caught**,
  armed rejects leave zero residue, observe mode byte-identical to
  detectors off · clean-feed false positives: **armed = 0** across 3×10k
  feeds with every stream trap on · both decision rigs self-tested ·
  drill 22/22 · 3 review findings fixed, incl. one regression of mine and
  one potentially catastrophic D1 vocabulary trap.
- **Phase 6 — DONE, gate green (2026-08-06).** Checkpoint reporting
  surface complete (TB seeded from accounts-ever-touched, explicit
  customer universe, buy-only holds, phantom-free positions,
  placement-stored routes) + as-of via the bounded snapshot ring ·
  193 unit tests OK (16 new) · NEW section 7: 500-point as-of oracle —
  ring == live == cold replay, canon clean on real snapshots, p95 96 ms ·
  the gate tests were themselves validated by a 12/12 mutation harness ·
  6 review findings fixed (registration consistency, reporting purity,
  empty-id handling, snapshot cost, ring memory, NOTES) · drill 22/22.
- **Phase 5 — DONE, gate green (2026-08-06).** Generic reversal engine
  (stored-legs inverse, four-op surgical lot undo, full reject matrix,
  side-table hygiene, holds never restored) + the four fee settlements
  (balance-derived, strictly-positive, per-broker) · 177 unit tests OK
  (48 new: 17 reversal, 7 settlement, involution matrix over 20 event
  types × 120 seeded scenarios) · NEW 10k finale chaos × 2 seeds (8%
  reversals, 5% settlements, every reversal trap): payable cent audit
  exact, dual FIFO oracle cent-identical through dense reversals, full
  drain · fidelity review: 4 findings fixed + re-verified, 1 ruled
  book-correct · client drill 22/22.
- **Phase 4 — DONE, gate green (2026-08-06).** Corporate actions
  per-customer with exact-Fraction split multipliers · 129 unit tests OK
  (24 new, all gate names) · 10k corporate chaos × 2 seeds (~20% corporate
  density: splits incl. reverse + repeating-decimal, rename merges +
  A→B→C chains, reinvests, D2/D7 mismatches observed to quarantine):
  dual FIFO oracle cent-identical through splits/renames/reinvests, full
  drain · fidelity review: handlers faithful; 1 flag-path FIFO bug fixed +
  re-verified · client drill 22/22.
- **Phase 3 — DONE, gate green (2026-08-06).** Order lifecycle, 13-leg
  fills, FIFO lot book (global seq, zombies, split multipliers),
  plan-then-commit sells, T+2 settlement · 105 unit tests OK (25 new,
  every gate box by name) · 10k market chaos × 2 seeds: dual FIFO oracle
  cent-identical, standing identities (2100 ≡ Σ lots, custody mirror,
  hold bounds, 2350/1150 reconciliation) zero violations, full T+2 drain ·
  fidelity review: cost-basis block verdict SAFE · client drill 22/22.
- **Phase 2 — DONE, gate green (2026-08-06).** tariff.py pure engine ·
  80 unit tests OK (34 new: every gate fixture, 9 min-fee boundaries,
  half-cent partner table, 120k-case Fraction oracle, 600k-point routing
  sweep + 3 exact ties, hold discriminators) · independent adversarial
  verification: 3,450,018 spec-derived exact-arithmetic evaluations,
  ZERO divergences · formula-fidelity review: FAITHFUL, no critical or
  major findings · money() canon unified into tariff.py.
- **Phase 1 — DONE, gate green (2026-08-05).** 46 unit tests OK (19 new:
  fees/refunds/interest/transfers/fx/withdrawals incl. named R10–R12,
  R16–R18 edge tests + 6 hand-worked fixtures) · 10k cash chaos × 3 seeds,
  all trap toggles on, zero violations, wallet oracle cent-exact ·
  replay/ring identity green · client drill still 22/22.
- **Phase 0 — DONE, gate green (2026-08-05).** 27 unit tests OK · 10k-event
  chaos sim (10,394 delivered + redeliveries) zero invariant violations ·
  mock-arena client drill 22/22 (postings coverage, as-of checkpoints,
  rewind without double-posting, 409 terminal, 1200-event terminal drain) ·
  kill-restart resume converges byte-identically (torn log line tolerated).

## Phase 7 decisions (hardening + defect rig)

- **The arming policy is deliberately timid, because the payoff is
  asymmetric.** A defect we wrongly post loses that event's weight, and a
  correctly-rejected event is a no-leg event scored at a QUARTER weight.
  A clean event we wrongly reject loses its FULL weight. A false positive
  therefore costs about four times what the miss it prevents would save,
  so an unproven detector is worth less than no detector.
- **Shipped armed: D1 and D3 only** — both enforced inline at their
  handler's validation step, where they have been tested since Phases 1
  and 3. D1 is a lookup against the arena's own published tariff
  coverage; D3 is an economic impossibility (paying out more interest
  than was received). Neither can false-positive by construction.
- **D2 ships OBSERVE — a deliberate deviation from the phase plan**,
  which lists it ARMED on the grounds that an arithmetic identity cannot
  false-positive. That does not survive the spec's own rule that "every
  amount is rounded to the cent independently": raw 10.005 / 1.004 /
  9.001 rounds to 10.01 / 1.00 / 9.00, where gross − tax = 9.01 ≠ 9.00 on
  perfectly clean data. One practice feed settles it in seconds; until
  then the cheap side of the asymmetry wins.
- **Detector findings never touch replayed state.** They go to
  `report_log`, not `quarantine` — an event can be flagged and then
  rejected downstream for an unrelated reason, and a rejection must
  leave the book byte-identical. (Caught by the reject-residue tests the
  moment the pass was wired in.)
- **Observe mode is provably inert**: detectors OFF vs OBSERVE over a 4k
  finale feed gives byte-identical submissions (4,957), snapshots,
  balances, lots and quarantine; replay identity holds with detectors
  live.

## Live campaign log

### Practice run 1 — score 77.52 (run_f1f7d5db2120)
posting 29.25/30 · checkpoints 24.13/40 · resilience 11.13/15 ·
liveness 10/10 · reconciliation 3.02/5.

**The planted systematic defect is D8: a second fill carrying a trade_id
an earlier fill already used.** Its payload is internally perfect — same
order, quantity, price, principal, and a broker that genuinely trades
that asset class — which is exactly the "internally well-formed and
wrong" the spec promises. Found by clustering the responses where we
posted legs and the reference wanted none (`missing: []` with every leg
`unexpected`). On that feed our predicate fired on exactly those two
events: zero false positives, zero misses. Armed.

Everything else in the 22-point gap was downstream of it: the two
customers wrong in EVERY checkpoint were exactly the two who received
duplicate fills; their inflated fee accruals made `firm_accounts` score
0.0 in all 12 checkpoints (all-or-nothing), and made three settlements
pay wrong amounts.

Three further fixes from the same evidence:
1. **Checkpoint re-answering.** A rewind re-delivers a checkpoint_request
   and we answered it again from our advanced state: `cp_postchaos`
   scored 0.687, then 0.314, then 0.099. A checkpoint describes the book
   AT ITS OFFSET, so we now cache the first reply and re-send it verbatim.
2. **Patient drain.** A burst of HTTP 502s made us abandon 17 postings
   after ~150 s; the ceiling is now ~25 minutes of retrying.
3. Confirmed the rest of the detectors must stay disarmed — on the real
   feed D4 fired 58 times (reference rejected 2), D10 6 times, and D6
   **106** times because OUR fx identity is inverted (usd = foreign ÷
   rate, not × rate). D2 never fired at all.

### Practice run 3 — score 99.71 (run_51271ec33678)
posting 29.92/30 · **checkpoints 40.00/40** · resilience 14.79/15 ·
**liveness 10/10** · **reconciliation 5/5**. 772 of 772 graded events
correct, and all seven checkpoints — including both as-of queries and
the post-rewind one — scored a clean 1.0000 on every part.

Progression: 77.52 → 96.93 → 99.71, and every step came from reading the
arena's own diagnostics rather than guessing.

### Practice run 2 — D8 armed
Postings 100% correct through 388 graded events (run 1 had failures long
before this point), and cp0 scored a clean **1.0000 on every part** —
including firm_accounts, which had been 0.0 in all twelve run-1
checkpoints, and cash_hold, which the D8 cascade had also been
corrupting (so the hold formula A15 is right as shipped).

## Defect hunt log

Measured over **56,198 events** of all four chaos generators (cash ×3,
market ×2, corporate ×2, finale ×2 seeds):

| Detector | Mode shipped | Firings on our own clean-ish feeds | Verdict |
|---|---|---|---|
| D1 broker/asset-class | **ARMED** (inline) | 0 | safe — table lookup |
| D3 interest share > gross | **ARMED** (inline) | 0 | safe — impossibility |
| D2 dividend net ≠ gross−tax | OBSERVE | (inline log) | needs one practice feed |
| D4 fill outside limit | OBSERVE | **4,002** | arming would have rejected 4,002 valid fills |
| D5 principal ≠ qty×price | OBSERVE | 141 | cent-convention risk |
| D6 fx usd vs rates | OBSERVE | **1,985** | quote-orientation risk, confirmed |
| D9 cumulative overfill | OBSERVE | 42 | needs placement; observe |
| D10 phantom dividend | OBSERVE | 290 | ordering FP, confirmed |
| D11 overdraw withdrawal | OBSERVE | 380 | highest FP risk, confirmed |

Caveat recorded honestly: some of those counts are artifacts of our own
generators (our fills price ±2% around the limit, which real limit orders
would not). That is exactly the point — we cannot tell a generator
artifact from a planted defect without a real feed, which is why nothing
beyond D1/D3 is armed. The practice bank + `tools/defect_cluster.py`
resolve it with evidence, and `tools/detector_ab.py` enforces the
deployment rule (zero attributable disagreements across ≥2 feeds).

The feed guarantees ≥1 systematic defect class (well-formed but wrong).
Detectors ship in observe-mode with a quarantine log; armed only after
zero false positives across ≥2 practice feeds. Findings recorded here.

*(empty — no live runs yet)*
