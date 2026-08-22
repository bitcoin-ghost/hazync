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
