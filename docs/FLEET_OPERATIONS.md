# Fleet operations — one block across many GPUs

How to prove a single block on many cards with the shipped CORE build (v0.21.x, canonical `METHOD_ID` in
`reproduce/METHOD_ID`).

> **Proving a block from the board?** Go to
> [A board block across many cards (mode 6)](#a-board-block-across-many-cards-mode-6). The chunk and
> aggregate sections below prove a *fixture*, and their receipt is one the coordinator will not accept. Everything here is one binary, `hazync-host-x86_64-linux-gnu-cuda` (called `./host`
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
| resolve assumptions (aggregate only) | **the coordinator, by default** since #446 — the chain is serial (each step consumes the previous one's output), so distributing it bought no parallelism and paid a round trip per step. `HAZYNC_RESOLVE_LOCAL=0` sends them to workers under `RESOLVE_TAG` instead (#252) |
| verify every returned receipt, assemble, verify against `METHOD_ID` | segment coordinator |

Wire tags are bits of the job index: `JOIN_TAG` bit 31, `RESOLVE_TAG` bit 30, `NOLIFT_TAG` bit 29,
`LIFT_TAG` bit 28. A worker tests resolve before join, because both bodies are pairs.

## Renting the fleet: `coordinator/tip_smoke.py`

Everything above assumes cards that already exist. `tip_smoke.py` is the driver that rents them,
stages the prover and the block, runs the gates, proves, harvests the evidence and releases the pods.

```sh
python3 coordinator/tip_smoke.py --cards 3 --spares 3 \
    --block 741000 --block-path prover/block_741000.json \
    --rundir /tmp/run1 --repo tools/milestone --live-rig tools/live --key ~/.ssh/hz_smoke
```

| flag | default | meaning |
|---|---|---|
| `--cards` | 2 | cards the run needs. Fewer surviving the gates and the run fails rather than proving on a smaller fleet than asked for |
| `--spares` | 1 | extra pods rented so one bad pod does not end the run. Unused spares are released immediately |
| `--gpu-type` | `NVIDIA GeForce RTX 4090` | comma-separated, in preference order. **4090 only by default** — see below |
| `--block` / `--block-path` | 130000 | the block, and the **local** path to its fixture. ⚠ `--block-path` is pushed from this machine; it is not a path on the card |
| `--claim` | off | claim a board block and prove it from its bundle (mode 6) instead of a fixture |
| `--session HOURS` / `--budget-usd` | off | keep claiming and proving until the time or money runs out |
| `--cleanup` | — | release whatever `rented.json` records and exit, for a driver that died hard |

### ⭐ Card type is the largest single lever on wall-clock

Measured 2026-09-21, 9 runs on block 741000 (hazync#448):

| fleet (3 cards) | times | mean | spread |
|---|---|---|---|
| **all 4090** | 267.9 / 271.7 / 276.9 s | **272.2 s** | **9.0 s** |
| contains an A40 | 357.9 / 380.2 / 410.4 s | 382.8 s | 52.5 s |

⛔ **And the slower fleet cost MORE**: the all-4090 run billed **$0.243**, the 4090+2×A40 run
**$0.262**. The A40 is $0.49/hr against the 4090's $0.74/hr and is slow enough that the cheaper card
loses on **price per proof**. Choosing on price per hour is the wrong objective.

`deploy_listening` walks `--gpu-type` in order and takes the first RunPod will sell, so a list means
**silent fallback**. That is how mixed fleets appeared. Pass
`--gpu-type "NVIDIA GeForce RTX 4090,NVIDIA A40"` only when completing a run matters more than its
wall-clock — and expect it to be slower and dearer when the fallback fires.

⚠ **Check the `FLEET:` line before comparing any two runs.** A mixed fleet is flagged:

```
FLEET: 3x NVIDIA GeForce RTX 4090
FLEET: 1x NVIDIA GeForce RTX 4090, 2x NVIDIA A40   ⚠ MIXED CARD TYPES — timings are NOT comparable
```

Until #449 nothing stated the composition, so a mixed fleet and a uniform one were identical in every
log. 142 s of spread was published as a geography effect when it was card type all along.

### The fleet ledger

Every finished run appends one line to `docs/history/fleet-economics.jsonl` — fleet composition,
wall-clock and cost. `python3 coordinator/tip_economics.py` reports it:

```
fleet                   n   mean_s     min     max  spread   mean_$
3x RTX 4090             3    272.2   267.9   276.9     9.0    0.255
1x A40 + 2x RTX 4090    1    357.9   357.9   357.9     0.0    0.262
2x A40 + 1x RTX 4090    2    395.3   380.2   410.4    30.2    0.261
```

⛔ The 4090-only default exists because of those rows, not the other way round. It rests on nine
runs on one block on one night, which justifies a default and does not settle a question — so the
ledger accumulates and the claim can be overturned by evidence rather than argued from memory. A run
that produced no proof is **not** recorded: it says nothing about cost per proof.

⚠ This ranks card types for **selection**. It does not rank rented cards for **keeping** — once a
card is paid for, the run's wall-clock is set by the fleet, so `tip_lifecycle.rank` keeps the
*fastest* cards and is right to ignore price.

### Passing levers to the cards

Any `HAZYNC_*` variable set in the driver's environment is forwarded to the aggregate **and every
worker** (`tip_lifecycle.lever_env`, #445). Keys the driver computes per run — `HAZYNC_BLOCK`,
`HAZYNC_PORT`, `HAZYNC_CHUNKS`, `HAZYNC_RANGE` and friends — are **reserved** and never taken from
your shell, so a stray `HAZYNC_BLOCK` cannot retarget a run while the logs still name the block you
asked for.

```sh
HAZYNC_JOIN_LOCAL_MAX=2 python3 coordinator/tip_smoke.py --cards 3 ...
```

⛔ Before #445 `prove_env` was a hardcoded five-key dict and the worker launch line carried only
`HAZYNC_WORKER_ID`, so **no lever reached anything**. An A/B run against an unreachable lever does
not fail loudly: both arms run identically and the result reads "no measurable difference", which is
indistinguishable from a lever that does nothing.

⚠ `HAZYNC_LIFTX_HINT`, `HAZYNC_FIELD_BIGINT2` and `HAZYNC_ECMULT_WINDOW` are read only in
`methods/build.rs` and `methods/guest/build.rs`. They are **build-time guest flags** baked into the
released binary; setting them at runtime does nothing.

## A board block across many cards (mode 6)

Everything above proves a **fixture**: `HAZYNC_BLOCK=block_<n>.json`, chunks, then the mode-5 aggregate.
That path cannot be submitted to the board. The coordinator verifies a **`KIND_RANGE`** receipt, and the
fixture path emits mode 4 (`KIND_CHUNK`) or the mode-5 aggregate — so before #361/#364 the fast path was
unsubmittable and the submittable path (`prove-range-bridge`) was one card, however large the block.

Mode 6 closes that: `seg-serve` serves a **bridge range**, so a board block gets N cards and still produces
the receipt the coordinator accepts.

```sh
# segment coordinator — ONE block, from its bridge bundle. No HAZYNC_BLOCK, no chunks.
# HAZYNC_BIND=0.0.0.0 is REQUIRED for workers on other machines — see below.
HAZYNC_RANGE=74928 HAZYNC_BRIDGE_OUT=/workspace HAZYNC_PORT=9110 HAZYNC_BIND=0.0.0.0 ./host seg-serve

# every worker, one per card — unchanged from the aggregate
CUDA_VISIBLE_DEVICES=0 HAZYNC_WORKER_ID=w0 ./host seg-connect <coordinator-host>:9110
```

⛔ **Without `HAZYNC_BIND`, a worker on another machine cannot attach.** `seg-serve` binds `127.0.0.1` by
default (#365) because the wire is **unauthenticated**, so a remote worker gets `Connection refused` against a
coordinator that is running perfectly. Since #401 the shell says so as it binds, rather than leaving you to
infer it from a refused connection. `HAZYNC_SEG_REMOTE=1` implies `0.0.0.0`.

### One command instead of the two above (#367)

The recipe above claims nothing and submits nothing — it proves a block you already chose. `hazync` can drive
the whole thing under your own key:

```sh
# claim the earliest free block, serve it, wait for cards, submit it
hazync run --distributed

# or a specific block, also starting 4 local workers (one per card)
HAZYNC_BIND=0.0.0.0 hazync run 74928 --distributed --workers=4
```

- **`--workers` defaults to 0**, so nothing competes with board workers already running on this box. Cards come
  from outside: start `host seg-connect <this-box>:9110` wherever you like, and since #402 a worker that arrives
  **before** the server waits and retries rather than dying, so they may join in any order and at any time.
- The receipt is the same `range_<n>.hzk` one card would have produced, so submission is unchanged.
- ⛔ **One block.** Mode 6 serves a single bridge range; `hazync run 100-200 --distributed` is refused up front.
- The claim is beaten from a progress-gated ticker throughout, so a run far longer than `CLAIM_GRACE` (600 s)
  keeps its block — and a **wedged** fleet still loses it, which is the behaviour you want.

⚠ **This costs throughput on the board.** Measured 2026-09-18 on block 230,000: four cards pooled on one block
is **96.0 s/block**, while the same four cards proving a block each is **82.9 s/block**. Pooling buys **latency**
on one block (3.45x), not more blocks per hour. Use it for the block holding the frontier, for a large block, or
for the tip — not to make the party go faster.

⚠ Binding `0.0.0.0` exposes the port to anyone who can route to it, and **anyone who can reach it can feed work
in**. Expose it only on a network you trust, or forward it over SSH (`ssh -N -L 9110:127.0.0.1:9110 …`) and
point the worker at `127.0.0.1:9110`. A rented pod usually publishes only the ports declared when it was
created, so the tunnel is often the only thing that works anyway.

- The bundle is read from **`$HAZYNC_BRIDGE_OUT/bundle_<n>.json`** (default `/root/bridge_bundles`). There is
  no `HAZYNC_BLOCK` fixture for a board block — that is precisely why `build_full()` cannot serve this path.
- `HAZYNC_CHUNKS` is **not used**. A range is one session; the parallelism is segments across cards, not
  chunks across cards.
- ⛔ **`HAZYNC_LIFTX_HINT` is NOT read here**, exactly as it is not read by `prove-range-bridge`. The hint
  table is written by `write_chunk_inputs`; mode 6 goes through `write_range_env`, which mode 6 and
  `prove-range-bridge` share so their receipts stay interchangeable. Setting it changes nothing on this path.
- **Put a worker on the coordinator's own card**, as with the aggregate — `seg-serve` distributes but does
  not prove.

### ⛔ A distributed prove must beat its claim

A board block is claimed before it is proved, and a claim that has never reported progress is released
after `CLAIM_GRACE` (600 s). A block worth distributing takes an hour, so this matters: the worker watches
`seg-serve`'s output, and mode 6 prints a **different shape** from a single-card prove —

```
     91/2352 segments  124s elapsed, ~3071s left     <- number FIRST, noun PLURAL
     joins 25/2352
```

against a single-card prove's `segment 91/2352` (number last, singular). `hazync` understands both since
#368. An older worker does not, so on a distributed run its progress never rises, the claim is never
beaten, and the coordinator hands the block to someone else ten minutes in. **Use a worker from v0.21.7 or
later for mode 6.**

### Measured

The first mode-6 run, block **74,928** on 3 × RTX 4090 with the canonical guest verified on all three
([`history/BENCH_MODE6_3xRTX4090_2026-09-17.md`](history/BENCH_MODE6_3xRTX4090_2026-09-17.md)):

```
execution         123.6 s
worker wall      2622.7 s   <- 2,352 segments pushed over the network
assembly          948.1 s   <- last segment + join tree + resolves
TOTAL            3694.4 s
```

Both tips matched what the board recorded for that range, and the receipt was **229,754 bytes — the same
size as the one submitted for the same block from a single card**. The two paths are interchangeable by
construction: one helper writes the input stream for both.

⚠ Wait dominated: pod 2 waited 1,855 s of its 3,694 s. Three cards on a 2,352-segment block is not the
shape to optimise for — see the per-pod table in the record.

## Knobs

| variable | default | what it does |
|---|---|---|
| `HAZYNC_SEG_PO2` | **21** on CUDA, 20 otherwise | segment size. po2 21 peaks near **22 GB**; po2 22 peaks ~40.6 GB and measured ~11.5% faster on an L40S ([`TOPOLOGY_AND_SETTINGS.md`](TOPOLOGY_AND_SETTINGS.md) §3.1), so only on ≥46 GB cards |
| `HAZYNC_CHUNKS` | 2 | chunk count; identical on every command |
| `HAZYNC_CHUNK` | 0 | chunk index for `seg-serve` in chunk mode (`prove-chunk` takes it as an argument) |
| `HAZYNC_BLOCK` | `block_full.json` | the block file (fixture path only; mode 6 does not use it) |
| `HAZYNC_AGG` | unset | serve the mode-5 aggregate. **Presence-tested** |
| `HAZYNC_RANGE` | unset | `=<n>` serves the **mode-6 bridge range** for board block `n`, from `$HAZYNC_BRIDGE_OUT/bundle_<n>.json`. Parsed, not presence-tested |
| `HAZYNC_BRIDGE_OUT` | `/root/bridge_bundles` | where mode 6 reads `bundle_<n>.json` |
| `HAZYNC_RECEIPTS` | `.` | directory holding the chunk receipts |
| `HAZYNC_OUT` | `chunk_<i>.hzk` / `aggregate_receipt.bin` | receipt output path |
| `HAZYNC_PORT` | 9110 | `seg-serve` listen port |
| `HAZYNC_BIND` | `127.0.0.1` | `seg-serve` listen address. **Remote workers cannot attach on the default** (#365) — set `0.0.0.0`, and only on a network you trust |
| `HAZYNC_SEG_REMOTE` | unset | `=1` implies `HAZYNC_BIND=0.0.0.0`. An explicit `HAZYNC_BIND` still wins |
| `HAZYNC_RECONNECT_MAX_S` | **600** | how long a worker keeps trying to reconnect after a dropped link before giving up, measured from the **current** outage. `0` restores the old behaviour (exit on drop) |
| `HAZYNC_PUSH_DEPTH` | 4 | jobs in flight per worker |
| `HAZYNC_PUSH_BYTES` | 64 MiB | in-flight byte budget; clamps the depth. Raise this, not the depth |
| `HAZYNC_RESOLVE_LOCAL` | **on** | resolves on the coordinator. `=0` pushes them to workers. ⚠ Value-tested, not presence-tested: only an exact `0` disables it, and **unset means ON** (#252/#446) |
| `HAZYNC_JOIN_LOCAL_MAX` | `0` (off) | proves join levels of `<= N` pairs on the aggregate rather than distributing them. The narrow top of the tree is serial, so those joins buy no parallelism and still pay a round trip. Measured worth ~0.4–1.6 s; off by default pending a fleet run (#252/#444) |
| `HAZYNC_WORKER_ID` | `push1` | worker name in logs; set it per worker |
| `HAZYNC_SEG_QUIET` | unset | worker prints the old sparse, undated lines instead of one timestamped line per task (#254). Presence-tested |

⚠ **`HAZYNC_RESOLVE_LOCAL` is the exception to the rule below**: it is value-tested, defaults to ON,
and only an exact `0` turns it off. An unset variable means the feature is ACTIVE.

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
- [`history/BENCH_MODE6_3xRTX4090_2026-09-17.md`](history/BENCH_MODE6_3xRTX4090_2026-09-17.md) — the first
  mode-6 run: a BOARD block (74,928) distributed across 3 cards and verified against the canonical guest.
  Read it for the transport finding — a join is ~0.4 s of GPU and ~47 s of round trip.
- [`BUILDS.md`](BUILDS.md) — what the CORE and GHOST builds contain and their measured card counts.
- [`history/SEGMENT_DISTRIBUTION.md`](history/SEGMENT_DISTRIBUTION.md) — the original design and its
  two-card and three-card measurements.
