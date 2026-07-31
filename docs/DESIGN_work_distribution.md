# Design — free-running provers, opportunistic folding, and an incremental spine

Resolves #37 (work distribution) and #30 (incremental fold). They are one design: #30 is the spine
half of #37, and specifying them separately would produce two things that have to agree.

**Land this with the re-baseline, not after.** A `METHOD_ID` change resets the board to genesis, so
this is the one moment where changing how work is distributed costs nothing. Retrofitting it to a
running board means changing allocation under load — and allocation is where the outages have been.

---

## The change in one line

The coordinator stops **allocating work** and starts **tracking what exists and advancing the spine**.

| role | who | concurrency |
|---|---|---|
| prove any block | anyone, unallocated | unbounded |
| fold any two *adjacent* receipts | anyone, opportunistic | unbounded |
| extend the spine `[1..N] + chunk → [1..M]` | one | serial by nature |

Proving is embarrassingly parallel: proving block 500,000 needs its witness and its stated boundaries,
not block 1. Only **anchoring** is sequential, because anchoring means `lo == 1`.

## Why the current scheme has to go

Three live consequences, all documented in #37 and none speculative:

1. **One bad block halts everyone.** `frontier_blocker` is a real field in `/api/state`; the retry and
   park machinery of #26 exists solely to cope with it.
2. **Width is a reliability bet, not a tuning knob.** #28: width 1000 meant a ~67-minute commitment
   where any failure discarded the range, and the board fell from 2,220 blocks/hr to one block per
   40 minutes *with the GPUs still busy*.
3. **Allocation is coordination.** Claims, overlap across two id grids, expiry, retry counters,
   capacity failures — a large share of `coordinator/server.py`, and the part that breaks.

## Folding is a tree

Any two **adjacent** ranges fold with no reference to genesis: `[100..199] + [200..299] → [100..299]`.
Width-100 workers already do this ~13,000 blocks from genesis.

This is load-bearing. "One worker folds everything sequentially" is one fold per block — **~660 h at
tip** at 2.48 s/fold. The tree does the same N−1 folds in parallel, and the spine then absorbs
*chunks* rather than blocks: ~9,580 folds at tip, ~6.6 h, with a shippable genesis-anchored proof
existing continuously rather than only at the end.

---

## The five open questions, answered

### 1. Do provers self-direct, or get hinted? — **Hinted, advisory.**

Fully free-running risks the whole fleet proving the same block. Pure allocation is what we are
removing. So: the coordinator serves a *suggestion* (least-recently-attempted, unproven, witness
available) and **accepts a submission for any height regardless**. No lease, no expiry, no claim
record. A worker that ignores the hint is not an error case; it is the normal case for anyone
backfilling or re-proving.

Duplicate work is possible and harmless — wasteful, not incorrect.

### 2. Who folds? — **Anyone, opportunistically.**

Same trade as duplicate proving. A worker with idle capacity looks for an adjacent pair where both
sides exist and the folded result does not, and folds it. Two workers may fold the same pair; the
outputs are identical receipts of the same range, so the second is discarded on submission.

Folding is far cheaper than proving (~1,025 GPU-hours for the whole chain against ~1.56M to prove it —
see #30), so wasted folds are noise.

### 3. Witness availability at arbitrary heights? — **Yes, verified.**

`/api/witness/<h>` returns a bundle for any height whose file exists, with no frontier or window logic
— the handler is a direct path lookup (`coordinator/server.py`, `bundle_{blk}.json`). Confirmed live
against heights 1, 5,000, 100,000 and 195,000: all HTTP 200.

This was #37's biggest unknown and it is a non-issue. Since the bridge fix (185×, #40) it produces
witnesses at ~77,600 blocks/hr, so witness supply now runs arbitrarily far ahead of proving.

### 4. Spine trust — **a liveness SPOF, not a soundness one.**

The spine holds the only genesis-anchored artifact, so whoever advances it can stall the headline
claim. They cannot corrupt it: every spine extension is a fold of receipts anyone can re-verify, and
because per-block receipts are retained, **anyone can rebuild the spine from scratch** without
re-proving anything. A stalled spine costs time; it cannot produce a wrong anchored proof, because a
wrong fold fails verification.

Mitigation is therefore operational, not cryptographic: more than one machine may run the spine, and
duplicate spine work is harmless for the same reason duplicate folding is.

### 5. Storage — **retain receipts, discard bundles.**

This answer changed today and is worth stating precisely, because the two artifacts have opposite
economics:

| | size at tip | regenerable? |
|---|---|---|
| per-block receipts | ~206 GB (measured: 8.3 GB / 38,507 ≈ 215 KB each) | only by re-proving — the expensive thing |
| bridge bundles | **0.8 TB floor, realistically 2–6 TB** (measured by era) | yes, at ~77,600 blocks/hr |

So bundles, not receipts, dominate storage — and they are the ones that are cheap to recreate.
**Retain every per-block receipt permanently** (G1, and an operator requirement: a sceptic must be
able to check one block in isolation). **Discard bundles once their block is proven and the receipt
is retained**; regenerate on demand for re-proving or backfill.

Before the bridge fix this was not a real option — regenerating a bundle meant a 291 blocks/hr walk.
At 77,600 blocks/hr it is minutes.

---

## Incremental fold (#30)

The spine must **extend**, never re-fold from scratch. Re-folding `[1..N]` each time the board grows
makes the cost recur *and* grow with the board, which is what made #30 feel urgent when it was framed
as a one-off job.

```
spine [1..N]  +  chunk [N+1..M]   ->   spine [1..M]
```

- One fold per absorbed chunk, not per block. Fold cost is **flat in range length** — a fold verifies
  two receipts and checks the seam, regardless of span (measured: 2.00 s/fold on one L40S at K=3,
  1.16 s with two).
- The spine is therefore always shippable: after every absorption there is a genesis-anchored proof of
  everything up to the current spine head.
- Absorption is the only serial step in the system, and it is ~9,580 folds across the whole chain.

**Use K=2, not K=3, for unattended runs** — K=3 peaks within ~50 MiB of OOM, fine for a 7-second
benchmark and reckless for a multi-hour one. `risc0` binds one GPU unless `CUDA_VISIBLE_DEVICES` says
otherwise, so multi-card boxes need explicit pinning or the extra cards idle.

---

## What the coordinator becomes

Removed: `/api/claim`, `/api/release`, `/api/heartbeat`, claim expiry, overlap detection across id
grids, retry and park counters, `frontier_blocker`.

Kept and extended:

- `/api/submit` — verify (`VERIFY_MODE=real`), store, index by range. Idempotent: a duplicate
  submission for an already-held range is accepted and discarded.
- `/api/hint` — advisory next-block suggestion. Statelessly derived; no record is written when it is
  served, so a crashed worker leaves nothing to clean up.
- `/api/witness/<h>` — unchanged, already serves arbitrary heights.
- `/api/proof/<h>` — unchanged, per-block receipts.
- spine head — the current genesis-anchored proof, served as the headline artifact.

## Invariants a reviewer should be able to check

1. Every height the board calls proven has its own retained receipt — gated by
   `coordinator/check-retention.py` (G1).
2. The spine head verifies as genesis-anchored under the canonical `METHOD_ID`, with the standalone
   verifier and no other input.
3. Any two adjacent stored ranges fold to a receipt whose boundaries match both — so the board is
   re-derivable from retained receipts alone by anyone.

## What this deliberately does not solve

Provers can still waste effort on duplicate work, and nothing prevents a worker submitting only easy
blocks. Both are accepted: the alternative is allocation, which is what we are removing, and the cost
of duplication is bounded by how much compute people volunteer.
