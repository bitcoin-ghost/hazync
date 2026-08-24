# Segment distribution — final state

Complete. Design: `docs/SEGMENT_DISTRIBUTION.md`. Full measurement log: `~/hazync-b200-results.txt`
§29–§64.

## What was built

| piece | gate |
|---|---|
| segment distribution (`seg-coordinate` / `seg-work`) | ✅ 3 gates — 1 proc, 2 proc, GPU |
| balanced join tree, replacing risc0's **linear** fold | ✅ identical receipt, cost unchanged |
| distributed join levels (`seg-join`) | ✅ 4 processes, identical digest |
| worker-side lifts (`HAZYNC_WORKER_LIFTS`) | ✅ undivided work 58% → 2.1% |
| push transport (`seg-serve` / `seg-connect`) | ✅ identical digest, 10% faster than pull |
| distributed joins over push | ✅ both phases scale |

**Every path in that table produces an identical receipt**, and so does the monolithic prove they
are all checked against — nine in total, counting the baseline. The gates are listed row by row above
rather than summarised as a number, because a bare count is not checkable: an earlier tally in the
run log says "six execution paths", which was correct when it was written and predates the last three
rows.

The strongest of the nine is the cross-machine one: segments proved on a different machine, by a
different binary, against a guest with a different image id, still fold into the same journal
digest.

## The measurement

Two matched L40S, block 741000 chunk 0, po2 18, 1,684 segments:

| term | 1 card | 2 cards | speedup |
|---|---|---|---|
| segment proving | 862.6 s | 410.9 s | 2.10x |
| assembly | 409.7 s | 211.5 s | 1.94x |
| **total** | 1279.1 s | 629.3 s | **2.03x** |

For a near-tip block — 14,167 card-seconds of chunk work plus a 1,466 s aggregate = **15,633
card-seconds**, against a ~46 s execution floor:

| cards | block |
|---|---|
| 16 | 17.0 min |
| **30** | **9.5 min** |
| 64 | 4.8 min |

## Why it works

**The linear fold was the blocker.** risc0's `composite_to_succinct` folded strictly left — lift,
join into an accumulator, lift, join — so every join depended on the one before it and assembly
could not be parallelised by threads *or* machines at any cost. Rebalanced to a tree it is the same
`N-1` joins and the same claim, at `log2(N)` depth.

**Pull was the second blocker.** Three SSH connections per segment at ~150 ms of setup, against
0.47 s of proving, meant a second GPU added *nothing*. Push holds one connection open and sends
segment N+1 while the worker proves N.

**Workers are untrusted by construction.** A receipt is self-verifying, the coordinator verifies
each on arrival, and `join` asserts `a.post == b.pre` — so a bad worker costs latency, never
soundness. That is what allows a heterogeneous fleet.

## Known limits

- **The last segment cannot move to a worker.** The session journal and assumptions merge into its
  claim before lifting, and a worker has no session.
- **`assemble_from_joined` has no `CompositeReceipt`**, so its integrity and claim checks are gone.
  What remains: per-receipt verification, `join`'s continuity check, and the final `METHOD_ID`
  verify. Weaker against a *buggy prover*, not against a dishonest worker.
- **po2 is fixed per session.** Nodes are forced to po2 18 by RAM (2.50 GB); cards want 21–22. A
  heterogeneous fleet cannot share one session.
- **2.03x is noise-above-linear, not superlinear.** Do not quote it as such.

## Related

#148 (this work), #143, #151, #145, #119, #69. #152 remains: rescue the lax-DER tests before
`feat/pipeline-preflight` is dropped.
