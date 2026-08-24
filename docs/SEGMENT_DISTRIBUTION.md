# Segment distribution — design and build plan

> **Two different things are called "coordinator" in this project.** This document is about the
> **segment coordinator** — the ephemeral `seg-serve` process that executes one guest run, pushes its
> segments to workers and drives assembly. It lives for the duration of one prove and needs a GPU.
>
> The **board coordinator** (`coordinator/server.py`, [`docs/RUN_YOUR_OWN_COORDINATOR.md`](RUN_YOUR_OWN_COORDINATOR.md))
> is the long-lived public service that hands out block ranges, verifies submitted proofs and runs
> the scoreboard. It never proves anything and needs no GPU. The two share no code.


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

## Running it

Everything below is one binary. The segment coordinator executes and hands out work; workers connect and
prove. Workers hold no work list and make no decisions.

### A chunk

```sh
# coordinator: executes, serves segments, drives the join tree
HAZYNC_BLOCK=block_962000.json HAZYNC_CHUNKS=16 HAZYNC_CHUNK=0 \
  HAZYNC_SEG_PO2=22 HAZYNC_PORT=9110 ./host seg-serve

# each worker, on any machine that can reach the segment coordinator
HAZYNC_WORKER_ID=w1 ./host seg-connect <coordinator-host>:9110
```

### The aggregate (#153)

Same commands, plus `HAZYNC_AGG=1`, and the chunk receipts must already exist as `chunk_0.bin` …
`chunk_N.bin` in the segment coordinator's working directory. They are verified against `METHOD_ID` on the
way in, so a receipt from a different guest is refused by name rather than failing later inside the
prover.

```sh
HAZYNC_BLOCK=block_962000.json HAZYNC_CHUNKS=16 \
  HAZYNC_AGG=1 HAZYNC_SEG_PO2=22 HAZYNC_PORT=9110 ./host seg-serve
```

Workers are identical — they take segments, joins and resolves off the same connection and do not
need to know which phase is running.

### Knobs that matter

| variable | what it does |
|---|---|
| `HAZYNC_SEG_PO2` | segment size. **22 is the default on CUDA and peaks at ~40.6 GB of VRAM** |
| `HAZYNC_PORT` | coordinator listen port (default 9110) |
| `HAZYNC_PUSH_DEPTH` | segments in flight per worker (default 4) |
| `HAZYNC_AGG` | serve the mode-5 aggregate instead of a mode-4 chunk. **Presence is what counts** |
| `HAZYNC_WORKER_ID` | worker name in logs; `seg-connect` defaults to `push1`, so set it per worker |

⚠ **`HAZYNC_AGG=0` still turns the aggregate ON.** It is tested with `env::var(...).is_ok()`, so any
value enables it and only *unsetting* it disables it. That is the house style here — twelve flags in
`main.rs` work the same way — but it reads like a boolean and is not one.

⚠ **One prove per card at po2 22.** A full chunk peaks at 40,609 MiB of a 46 GB card — 88%, about
5.5 GB spare. Two concurrent proves will OOM it. This is not a loss: concurrency was measured twice
and buys ~3%, inside noise.

⚠ **The segment coordinator needs a card too**, but only briefly. It proves the last segment itself, because
the session journal and assumption set merge into that segment's claim and a worker has no session.
That happens *after* the distributed segments come back, so the segment coordinator can share a card with a
worker without colliding.

### If a worker dies

Its in-flight segments go back on the queue and another worker takes them. That is the same
reassignment the pull design got from expiring a claim, without needing claims. A worker returning a
bad receipt is caught by `verify_integrity_with_context` before the receipt is stored, and the
segment is requeued.

## Trust model — why untrusted workers are safe

This is the property that makes Ghost-node participation possible at all.

- A `SegmentReceipt` and a `SuccinctReceipt` are **self-verifying**. The segment coordinator
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

- ~~**Assumptions.** Chunk proving (guest mode 4) has none, so P0/P1 target it. The
  aggregate resolves 16 chunk receipts; `resolve` is on the same trait but the ordering
  against the join tree needs care.~~ **RESOLVED 2026-08-24 (#153).** `seg-serve` takes
  `HAZYNC_AGG=1` and builds the mode-5 environment; resolves go out under a third job tag.
  The ordering concern was real: resolves are a chain, each consuming the conditional the
  previous one produced, so `session_assumptions_succinct` returns them in SESSION order and
  the segment coordinator publishes one job at a time rather than queueing them together. Gated on
  digest equality at 2- and 4-chunk partitions, with the worker's own log asserting exactly
  N resolves discharged remotely. See the section at the end.
- **A single card cannot demonstrate speedup.** With one GPU, P2 measures *overhead and
  correctness*, not wall-clock gain. Genuine parallelism can be shown using CPU workers
  alongside the GPU, at a much lower rate.
- **Join tree distribution** is not in P0–P2. Joins are done by the segment coordinator. At
  ~0.15 s/segment on GPU that is acceptable; on CPU nodes at 14 s/join it is not, and the
  tree would have to distribute too.
- **po2 is per-worker, not global.** Nodes are forced to po2 18 by RAM; cards want 21–22.
  A heterogeneous fleet cannot share one segment size, and a session's segments are fixed
  at execution time — so the segment coordinator must partition by worker class, or run separate
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

### The fix: the segment coordinator should push

Pull was the wrong choice. The segment coordinator already holds the work list, so:

- **no claim round trip** — it assigns rather than waiting to be asked
- **transfer overlaps proving** — send segment N+1 while the worker proves N
- **one persistent connection** instead of three new ones per segment

At queue depth 2 the worker never waits on the network. Bandwidth was never the constraint
(0.06 MB per segment); it is connection setup and latency, and pushing removes both.

**This was P3.** It is built — see the next section — and it is what turned "correct but gains
nothing" into a measured 2.03x.

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

### The remaining gap (closed by the next section)

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

### The block — MEASURED end to end (2026-08-24)

Near-tip 962000, 16 chunks, po2 22, on one box with 3x L40S. Every term below was measured on the
same rig on the same day, so the ratios are internally consistent even though this rig runs ~12%
slower per chunk than the earlier §52 reference.

    chunk work    15,835 card-seconds  (16 chunks, mean 990 s)
    aggregate      1,541 s in-process  (the undistributed baseline)

**The aggregate now divides**, which is the whole point. Same block, same 16 chunk receipts, workers
added one at a time:

| | in-process | 1 worker | 2 workers | 3 workers |
|---|---|---|---|---|
| segment proving | — | 1448.2 s | 760.1 s | **506.6 s** |
| assembly (join tree + resolves) | — | 96.1 s | 51.7 s | **36.0 s** |
| of which resolves | — | 4.7 s | 4.6 s | 4.2 s |
| **total** | 1541.3 s | 1565.5 s | 833.3 s | **563.6 s** |
| speedup vs 1 worker | — | 1.00x | 1.88x | **2.78x** |
| journal digest | reference | OK | OK | **OK** |

Both halves scale. Assembly falling 96.1 -> 36.0 is the join tree distributing, not just the
segments. Every run produced a byte-identical journal digest, so nothing is bought by dropping work.

One worker costs **1.6% more** than proving in-process (1565.5 vs 1541.3). That is the whole price of
putting the work on a wire.

### What that does to a whole block

    1 card:   16 x 998 s + 1541 s = 17,509 s = 292 min
    3 cards:  15,835/3 + 563.6 s  =  5,999 s = 100 min

| | 1 card | 3 cards | speedup |
|---|---|---|---|
| before the aggregate divided | 292 min | 116 min | 2.51x |
| **after** | 292 min | **100 min** | **2.92x** |

Against a theoretical ceiling of 3.0x. What is left undivided is the ~21 s execution floor and a
per-segment contention cost that grows slowly with worker count (3.8 s solo, 4.0 s at two, 4.26 s at
three).

⚠ **The ~45-cards figure and the ~175 s resolve estimate were both wrong.** #153 projected the
sixteen assumption resolutions at ~175 s by converting 11.35 M cycles each at segment-proving rates.
Measured: **4.7 s for all sixteen**, about 37x out. Resolves are recursion proofs, and recursion is
far cheaper per cycle on GPU than segment proving -- the same reason a join costs 0.23 s against a
segment's 3.8 s. So the undivided term was never ~221 s, and distributing `resolve` was never where
the win was. The win is that a mode-5 aggregate can be **served at all**: 1,448 s of segment proving
inside it that previously could not leave one machine.

## Cards in one box barely contend

Three cards in one chassis cost each other about **2%**, which is inside run-to-run noise:

    solo (other two cards idle)   998 s
    three-up                    1,019 s
    §52 reference (2026-08-23)    892 s

So sharing a chassis is close to free, and a 3-card box is not meaningfully worse than three 1-card
boxes on contention grounds.

⚠ **This control does NOT explain the 12% gap against §52.** It compared one active card against
three active cards *on the same three-card rig*. It cannot separate "a card in a dense rig" from "a
card in a single-card rig", and the §52 reference was almost certainly measured on the latter.
Candidates for the remaining 12%: host resources per card (16 cores across 3 here, 8 for 1 there),
PCIe topology, or sustained boost residency. Unresolved, and it does not affect the ratios above
because every configuration ran on the same rig.

## The aggregate distributes (2026-08-24, #153)

Until this, the aggregate could not be distributed at all. `seg_serve_cmd` wrote `4u32`
unconditionally and never called `add_assumption`, so a mode-5 session could not even be
constructed; and `resolve` ran inside `assemble_from_joined`, on whichever machine called it.

- **`HAZYNC_AGG=1`** makes `seg-serve` build the mode-5 environment. Downstream is unchanged, because
  an aggregate session produces ordinary segments like any other.
- **`RESOLVE_TAG`** (bit 30, disjoint from `JOIN_TAG`'s bit 31 and above any segment index) carries a
  resolve job: the conditional receipt and one assumption, in the same pair body a join uses. The
  worker tests resolve *before* join, because both bodies are pairs and the tag order is the only
  discriminator.

The segment coordinator's job path needed no change — it already routed results by returned tag.

**Ordering is load-bearing.** Resolves are a chain: each consumes the conditional the previous one
produced. `session_assumptions_succinct` returns assumptions in session order, and the segment coordinator
publishes one job at a time rather than queueing them, because the next job's input is the previous
job's output.

Also measured on GPU (see the block section above): the aggregate scales **2.78x on three cards**,
where before it was a fixed cost no matter how much hardware you owned.

Gated against the in-process aggregate, block 130000, on CPU:

| partition | in-process | pushed | resolves on worker |
|---|---|---|---|
| 2 chunks | `09f4e49c…` | `09f4e49c…` | 2/2 |
| 4 chunks | `09f4e49c…` | `09f4e49c…` | 4/4 |

The worker counts come from the worker's own log, not the segment coordinator's, and the 4-chunk gate asserts
exactly 4 rather than non-zero — so it cannot pass by resolving some locally.

Both partitions produce the same digest, so **a block's proof does not depend on how the block was
chunked**. That had not been demonstrated before.
