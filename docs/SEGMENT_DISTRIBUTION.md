# Segment distribution — design and build plan

Status: **design + prototype**. Nothing here is deployed. Written 2026-08-23.

## Why this and not something else

Block latency today is `chunk_work / N_cards + aggregate`, and the aggregate does not
divide. Measured on an L40S at po2 22, near-tip block 962000:

| term | measured |
|---|---|
| chunk proving, 16 chunks | ~13,434 card-seconds (mean 840 s/chunk, spread ±7%) |
| aggregate | ~1,070 s (estimate; being measured) |

That gives a floor of **~18 minutes at infinite cards**, because the aggregate is one
indivisible session on one device. Ten minutes is unreachable by adding cards.

Two levels of parallelism exist below a block, and neither is wired for distribution:

| level | artifacts | orchestration | ceiling |
|---|---|---|---|
| block range | yes | yes — coordinator `/api/claim` | throughput only, latency unchanged |
| chunk | yes — `chunk_i.bin` | **no** | ~32 min/block at N=16 |
| **segment** | primitives only | **no** | **~9 min at N=30** |

Chunk distribution is the cheap win and tops out around 32 min. Segment distribution is
the only route to 10 minutes, because it moves parallelism *below* the level recursion
charges for.

## What is already measured

Everything that would make this a gamble has been retired:

| constraint | measured | verdict |
|---|---|---|
| segment wire size | 0.04 MB (po2 18) – 0.28 MB (po2 20) | trivial |
| bandwidth to saturate a worker | 0.008 – 0.53 Mbit/s | not a constraint |
| serial execution floor | 2.2% (~21 s/chunk, ~46 s/block) | not a constraint |
| worker RAM, CPU po2 18 | 2.50 GB incl. recursion | **fits a 3.87 GB Ghost node** |
| worker VRAM, GPU po2 22 | 41.1 GB peak | fits 46 GB, excludes 24 GB cards |
| recursion cost, GPU | ~0.15–0.18 s/segment | ~20% of work |
| recursion cost, CPU po2 18 | 27.8 s/segment (lift 13.8 + join 14.0) | **45% of work** |

Detail and the runs behind each: `~/hazync-b200-results.txt` §29, §31, §34, §35, §37, §40–43.

## The pipeline, monolithic vs distributed

Monolithic, as `prove_session` does it today:

```
ExecutorImpl::run()          -> Session { segments: Vec<Box<dyn SegmentRef>> }
for each segment:
    segment_ref.resolve()    -> Segment
    prove_segment(ctx, seg)  -> SegmentReceipt
lift(SegmentReceipt)         -> SuccinctReceipt<ReceiptClaim>
join(a, b) up a tree         -> SuccinctReceipt<ReceiptClaim>
resolve(cond, assumption)    -> once per assumption
Receipt::new(Succinct, journal)
```

Distributed:

```
COORDINATOR
  execute                     serial, 2.2%, unavoidable
  bincode each Segment        0.04-0.28 MB per work item
  publish work items

WORKER  (one per segment, untrusted)
  fetch Segment
  prove_segment -> SegmentReceipt
  lift          -> SuccinctReceipt        <- per-segment, distributes perfectly
  return SuccinctReceipt (~200-300 KB)

COORDINATOR
  verify each returned receipt
  join pairwise up the tree                <- log N depth, each level distributes
  resolve assumptions
  assemble Receipt, verify against METHOD_ID
```

**No risc0 patch is required.** `Segment`, `SegmentReceipt` and `SuccinctReceipt` all
derive `Serialize + Deserialize`, and `prove_segment`, `lift`, `join` and `resolve` are
all on the public `ProverServer` trait via `get_prover_server(&opts)`. The existing
vendored risc0 (the preflight pipelining patch) is orthogonal and stays.

## Trust model — why untrusted workers are safe

This is the property that makes Ghost-node participation possible at all.

- A `SegmentReceipt` and a `SuccinctReceipt` are **self-verifying**. The coordinator
  verifies every returned receipt before using it.
- A worker cannot forge a valid receipt for work it did not do — that is what the STARK
  is. It can only fail to return one.
- A worker cannot return a valid receipt for the **wrong** segment: the claim binds the
  pre- and post-state digests, and `join` checks continuity (a's post == b's pre). A
  segment out of place fails the join. This was learned the hard way — `join(x, x)`
  fails an equality check on the state digest, which is the same mechanism.
- Therefore a malicious worker costs **latency, not soundness**. The mitigation is
  reassignment and a timeout, not consensus.

Failure handling follows: work items are idempotent, a timeout or a failed verify
reassigns the segment, and proving the same segment twice is harmless.

## Build phases

- **P0** — manual pipeline in one process: execute, prove each segment, lift, join,
  resolve, assemble. Must produce a receipt that verifies against `METHOD_ID`, and
  produce the same journal as the monolithic `prove()`. This proves the API decomposition
  is correct before any distribution exists.
- **P1** — split into file-based commands so the stages can run in separate processes:
  `seg-export`, `seg-prove`, `seg-lift`, `seg-join`, `seg-assemble`.
- **P2** — local orchestrator running N worker processes over the work directory, to
  measure overhead against monolithic and demonstrate real multi-worker execution.
- **P3** — network protocol and coordinator integration. **Out of scope for now.**

## Known open questions

- **Assumptions.** Chunk proving (guest mode 4) has none, so P0/P1 target it. The
  aggregate resolves 16 chunk receipts; `resolve` is on the same trait but the ordering
  against the join tree needs care.
- **A single card cannot demonstrate speedup.** With one GPU, P2 measures *overhead and
  correctness*, not wall-clock gain. Genuine parallelism can be shown using CPU workers
  alongside the GPU, at a much lower rate.
- **Join tree distribution** is not in P0–P2. Joins are done by the coordinator. At
  ~0.15 s/segment on GPU that is acceptable; on CPU nodes at 14 s/join it is not, and the
  tree would have to distribute too.
- **po2 is per-worker, not global.** Nodes are forced to po2 18 by RAM; cards want 21–22.
  A heterogeneous fleet cannot share one segment size, and a session's segments are fixed
  at execution time — so the coordinator must partition by worker class, or run separate
  sessions per class. **This is unresolved and is the biggest design risk.**

## Measured: a second machine added nothing, because of the transport

Two matched L40S, same datacentre, block 741000 chunk 0, po2 18, 1,684 segments:

| config | workers | segment prove | join tree |
|---|---|---|---|
| A | box1 x1 | 792.8 s | 399.3 s |
| B | box1 x2 | **743.9 s** | 397.8 s |
| C | box1 x1 + box2 x1 | 785.7 s | 401.7 s |

Digest correct in all three. **C is indistinguishable from A and worse than B.**

The work was genuinely shared — box 2 proved 1,059 of 1,684 segments (63%) — so claiming,
transport and assembly all work across machines. It just did not go faster:

```
box 1, local worker    0.47 s per segment
box 2, remote worker   ~1.0 s per segment    identical hardware
```

Every remote segment costs **three separate SSH connections** — `ssh mkdir` to claim, `scp`
down, `scp` up plus `ssh mv` — at roughly 150 ms setup each, against a segment that proves in
470 ms. The transport costs as much as the work.

**This is a property of fast segments, not of this network.** Any per-segment transport costing
~0.5 s erases the entire gain at po2 18.

### The fix: the coordinator should push

Pull was the wrong choice. The coordinator already holds the work list, so:

- **no claim round trip** — it assigns rather than waiting to be asked
- **transfer overlaps proving** — send segment N+1 while the worker proves N
- **one persistent connection** instead of three new ones per segment

At queue depth 2 the worker never waits on the network. Bandwidth was never the constraint
(0.06 MB per segment); it is connection setup and latency, and pushing removes both.

**This is P3 and it is not built.** Until it is, cross-machine segment distribution is correct
and gains nothing.

## Push transport: built, gated, and faster even on localhost

`seg-serve` / `seg-connect` replace the pull worker. Correctness gate, 2 workers over localhost:

    digest ce5e105094d8d307b81453b6e20821cb7b1643ba8969c5f9ba81bbe9b3839406  IDENTICAL
    execution 2.1 | worker wall 1991.0 | assembly 606.1 | TOTAL 2599.3

| path | total |
|---|---|
| monolithic | 3094 s |
| pull, file-based, 2 workers | 2873 s |
| **push, TCP, 2 workers** | **2599 s** |

**16% faster than monolithic, 10% faster than pull — on localhost, with no latency to save.**
That gap is the per-segment *file* work pull did (write, poll, read, rename) which push does not
do at all. The transport was costing something even with the network removed.

**The cross-machine win it was built for is still unmeasured** and needs two machines: one box,
~20 minutes, coordinator on the box and worker on the laptop.

## MEASURED: the work divides — 1.96x on two cards

Two matched L40S, push transport, block 741000 chunk 0, po2 18, 1,684 segments:

| | worker wall | assembly | total |
|---|---|---|---|
| 1 card | 804.6 s | 373.1 s | 1184.8 s |
| **2 cards** | **410.8 s** | 370.4 s | **788.2 s** |

**804.6 -> 410.8 s is 1.96x — 98% parallel efficiency.** Box 2 took 832 of 1,683 segments (49.4%)
with no balancing logic beyond taking work as it freed up. Both receipts verify.

**The same two boxes gained nothing under the pull transport.** That is the whole difference
between three SSH connections per segment and one held open with the next segment already in
flight.

### What it means for a block

Near-tip 962000, measured: chunk work 14,167 card-seconds, aggregate 1,466 s, total 15,633.
At 98% efficiency:

| cards | block |
|---|---|
| 16 | 17.1 min |
| **30** | **9.6 min** |
| 64 | 4.8 min |

### The remaining gap

Assembly did not divide here (373.1 vs 370.4 s) because `seg-serve` runs the join tree in-process.
That term *does* distribute — `seg-join` is built and gated — but **push and distributed joins are
not yet the same code path**. Wiring them together is the last step between this projection and a
measured sub-10-minute block.

## MEASURED: both phases scale — 2.03x on two cards

Two matched L40S, push transport, segments **and** joins distributed. Block 741000 chunk 0,
po2 18, 1,684 segments:

| term | 1 card | 2 cards | speedup |
|---|---|---|---|
| segment proving | 862.6 s | 410.9 s | 2.10x |
| **assembly** | **409.7 s** | **211.5 s** | **1.94x** |
| **total** | 1279.1 s | 629.3 s | **2.03x** |

Digest correct in both. **Assembly was flat at ~373 s in every previous run** — it was the last
undivided term, and with the join tree published as work over the same connections it now scales.

The tree behaves as designed: 11 levels for 1,684 segments, 1,683 joins, odd receipts carried in
position, `log2(N)` depth rather than `N`.

### The block

Near-tip 962000, measured: 14,167 card-seconds of chunk work plus a 1,466 s aggregate = **15,633
card-seconds**, against a ~46 s execution floor:

| cards | block |
|---|---|
| 16 | 17.0 min |
| **30** | **9.5 min** |
| 64 | 4.8 min |

**Every term is now measured to scale**, rather than one phase measured and the other assumed.
