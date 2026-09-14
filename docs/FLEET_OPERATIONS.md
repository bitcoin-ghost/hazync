# Fleet operations — one block across many GPUs

How to prove a single block on many cards with the shipped CORE build (v0.21.x, canonical `METHOD_ID` in
`reproduce/METHOD_ID`). Everything here is one binary, `hazync-host-x86_64-linux-gnu-cuda` (called `./host`
below). Commands, defaults and constants are read from `prover/host/src/main.rs`; numbers cite the run that
measured them.

> **Two different things are called "coordinator".** This page is about the **segment coordinator** —
> the `seg-serve` process that executes one guest run, pushes work to `seg-connect` workers and assembles
> the result. It lives for one prove. The **board coordinator** (`coordinator/server.py`,
> [`RUN_YOUR_OWN_COORDINATOR.md`](RUN_YOUR_OWN_COORDINATOR.md)) hands out blocks and verifies submitted
> proofs; it never proves and shares no code with this.

## The shape of a run

A block is split into `HAZYNC_CHUNKS` chunks; each chunk is one guest session (mode 4) producing a chunk
receipt; the **aggregate** (mode 5) folds the chunk receipts into the block proof. Two levels of parallelism:

1. **Chunks across cards** — `prove-chunk <i>` on card `i`. **One chunk per card.** Chunk count is not a
   lever: 8 vs 16 chunks on the same 8-card fleet measured **+0.2% mean over three blocks**, with no
   consistent sign ([`history/BENCH_8xL40S_2026-09-08.md`](history/BENCH_8xL40S_2026-09-08.md)).
2. **Segments across cards** — `seg-serve` executes one session and pushes its segments, joins, resolves
   and the final lift to `seg-connect` workers. This is how the aggregate, which is one session, stops
   being a single-card cost.

That is the pattern of the 544.0 s run on 27 RTX 4090s (block 966,256): `prove-chunk` per card, then the
aggregate through `seg-serve` with workers on the other cards
([`history/MILESTONE_966256_RUN4_2026-09-10.md`](history/MILESTONE_966256_RUN4_2026-09-10.md)). On L40S, a
formula that reproduces the measured 8-card block sizes a sub-ten-minute block at ~13 cards (BENCH record
above).

## ⛔ `HAZYNC_LIFTX_HINT=1` at run time

The canonical guest is built with the lift_x hint (`patches/0013`; `provision-vps.sh` exports
`HAZYNC_LIFTX_HINT=1` for the canonical build). The guest then **reads a hint table before its first
`VerifyScript`**, and only a host run with `HAZYNC_LIFTX_HINT=1` writes one (`write_chunk_inputs`). Leaving it
unset does not lose an optimisation — it desynchronises the guest's input stream.

Set it for every command that writes chunk inputs: `prove-chunk`, `prove-seg`, `seg-serve` (chunk mode), and
the chunk measurement commands `segment-size`, `exec-time`, `segment-mem`, `seg-distribute`,
`seg-coordinate`, `seg-coordinate-tree` and `chunk-profile` (when it executes). It is **not** read by
`prove-range-bridge` or `fold-range` (the board's per-block path), by the aggregate (`agg-chunks`,
`seg-serve` with `HAZYNC_AGG`), or by `seg-connect` workers, which receive segments rather than inputs.
`HAZYNC_FIELD_BIGINT2` and `HAZYNC_ECMULT_WINDOW` are build-time only; the host never reads them.

## Chunks

```sh
# card i, for i in 0..N-1 — every card with the SAME block file and HAZYNC_CHUNKS
HAZYNC_LIFTX_HINT=1 HAZYNC_BLOCK=block_966256.json HAZYNC_CHUNKS=27 \
  HAZYNC_OUT=chunk_$i.hzk ./host prove-chunk $i
```

`HAZYNC_CHUNKS` defaults to **2**, so set it everywhere, identically: the partition (`chunk_bounds`) is
recomputed by each command, and the aggregate reads as many receipts as the partition actually yields.
Output defaults to `chunk_<i>.hzk`.

## The aggregate

```sh
# segment coordinator — receipts chunk_0.hzk … in $HAZYNC_RECEIPTS (default: the working directory)
HAZYNC_BLOCK=block_966256.json HAZYNC_CHUNKS=27 HAZYNC_AGG=1 HAZYNC_PORT=9110 ./host seg-serve

# every worker, on any machine that can reach it
HAZYNC_WORKER_ID=w1 ./host seg-connect <coordinator-host>:9110
```

- Chunk receipts are read as `chunk_<i>.hzk`, falling back to `chunk_<i>.bin`, and each is **verified against
  `METHOD_ID` on the way in**, so a receipt from another guest is refused by name.
- The block receipt is verified against `METHOD_ID` and written to `$HAZYNC_OUT` (default
  `aggregate_receipt.bin`); a verified receipt that cannot be saved exits 3.
- `seg-serve` in chunk mode (no `HAZYNC_AGG`, `HAZYNC_CHUNK=<i>`) distributes one chunk the same way, and
  writes to the same `$HAZYNC_OUT` default — set it.
- **Put a worker on the coordinator's own card.** `seg-serve` distributes but does not prove: with one remote
  worker the aggregate took 772.4 s with the coordinator's card idle and 473.1 s with a worker on it
  ([`BUILDS.md`](BUILDS.md) §1).
- One box can do the whole block in-process instead: `agg-chunks` with the same `HAZYNC_CHUNKS` and
  `HAZYNC_RECEIPTS` (see `scripts/gpu-benchmark.sh`).

### Who does what

| step | where |
|---|---|
| execute | segment coordinator; since #236 it streams segments as they are produced, so workers start at segment 0 |
| prove + lift each segment | workers |
| prove the last segment | a worker (`NOLIFT_TAG`, #158 — it used to be the coordinator) |
| merge the session journal and assumptions into the last claim | segment coordinator |
| lift the merged last segment | a worker (`LIFT_TAG`) |
| join tree | workers (`JOIN_TAG`), `log2(N)` levels |
| resolve assumptions (aggregate only) | workers (`RESOLVE_TAG`), one at a time, because each consumes the previous one's output; `HAZYNC_RESOLVE_LOCAL=1` runs them on the coordinator instead (#252, opt-in) |
| verify every returned receipt, assemble, verify against `METHOD_ID` | segment coordinator |

Wire tags are bits of the job index: `JOIN_TAG` bit 31, `RESOLVE_TAG` bit 30, `NOLIFT_TAG` bit 29,
`LIFT_TAG` bit 28. A worker tests resolve before join, because both bodies are pairs.

## Knobs

| variable | default | what it does |
|---|---|---|
| `HAZYNC_SEG_PO2` | **21** on CUDA, 20 otherwise | segment size. po2 21 peaks near **22 GB**; po2 22 peaks ~40.6 GB and measured ~11.5% faster on an L40S ([`TOPOLOGY_AND_SETTINGS.md`](TOPOLOGY_AND_SETTINGS.md) §3.1), so only on ≥46 GB cards |
| `HAZYNC_CHUNKS` | 2 | chunk count; identical on every command |
| `HAZYNC_CHUNK` | 0 | chunk index for `seg-serve` in chunk mode (`prove-chunk` takes it as an argument) |
| `HAZYNC_BLOCK` | `block_full.json` | the block file |
| `HAZYNC_AGG` | unset | serve the mode-5 aggregate. **Presence-tested** |
| `HAZYNC_RECEIPTS` | `.` | directory holding the chunk receipts |
| `HAZYNC_OUT` | `chunk_<i>.hzk` / `aggregate_receipt.bin` | receipt output path |
| `HAZYNC_PORT` | 9110 | `seg-serve` listen port |
| `HAZYNC_PUSH_DEPTH` | 4 | jobs in flight per worker |
| `HAZYNC_PUSH_BYTES` | 64 MiB | in-flight byte budget; clamps the depth. Raise this, not the depth |
| `HAZYNC_RESOLVE_LOCAL` | unset | `=1` resolves on the coordinator (#252) |
| `HAZYNC_WORKER_ID` | `push1` | worker name in logs; set it per worker |
| `HAZYNC_SEG_QUIET` | unset | worker prints the old sparse, undated lines instead of one timestamped line per task (#254). Presence-tested |

⚠ **Presence-tested means `HAZYNC_AGG=0` still turns the aggregate ON.** These flags are checked with
`env::var(..).is_ok()`, so only unsetting them disables them. Every presence-tested flag in `main.rs` behaves
this way.

⚠ **One prove per card.** Two proves do not fit on a 46 GB card (#97); concurrency was measured and rejected.

## When things fail

- **#119 retry is in the binary.** `prove-chunk`, `agg-chunks` (#240, first released in v0.21.1) and every
  `seg-connect` worker prove each segment through `prove_segment_resilient`: up to 3 attempts, retrying **only**
  the known transient verify failure (anything else panics at once), printing a `[#119]` line per retry and the
  total at exit. #119 itself was root-caused and fixed in v0.21.1 (#245); the retry stays as defence in depth,
  and a retry now is new information worth reporting on the issue.
- **A worker dies or returns garbage.** Every returned receipt is checked with `verify_integrity_with_context`
  before it is stored; a failed check or a dropped connection puts the job back on the queue for another worker.
- **Trust model.** Receipts are self-verifying, a segment receipt binds its pre- and post-state digests, and
  `join` checks continuity, so a worker cannot substitute work for the wrong segment. A malicious worker costs
  latency, not soundness.

## Where the evidence and tooling are

- [`../tools/milestone/`](../tools/milestone/) — the orchestration that produced the 544.0 s run, including the
  failure modes it defends against. Gather per-card evidence before tearing a fleet down.
- [`history/MILESTONE_966256_RUN4_2026-09-10.md`](history/MILESTONE_966256_RUN4_2026-09-10.md) — the four
  block-966,256 runs.
- [`history/BENCH_8xL40S_2026-09-08.md`](history/BENCH_8xL40S_2026-09-08.md) — 8×L40S CORE fleet, chunk-count
  verdict, sizing.
- [`BUILDS.md`](BUILDS.md) — what the CORE and GHOST builds contain and their measured card counts.
- [`history/SEGMENT_DISTRIBUTION.md`](history/SEGMENT_DISTRIBUTION.md) — the original design and its
  two-card and three-card measurements.
