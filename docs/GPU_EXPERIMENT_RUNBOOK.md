# GPU experiment runbook

Everything from `PERF_INVESTIGATION_2026-08-26.md` that could not be settled without a card, in an
order that respects what produces what. Written to be executed top to bottom on a rented box.

**Everything free has already been done.** Tier 0 (E1–E4) is measured, E5's chunk half is measured,
E10 is closed, and E11's segment half is measured. What is left genuinely needs a GPU.

## 0. Before anything — the two guards that make results trustworthy

⚠ **Record `METHOD_ID` for every arm.** A build that silently did not happen returns identical numbers
and reads as "this change had no effect" — a false negative indistinguishable from a real result. Read
it with `host method-id`, never by scraping the binary: `strings | grep -oE '[0-9a-f]{64}'` returns
`000000000019d668…`, the **Bitcoin genesis hash**, which is 64 hex characters and entirely plausible.

⚠ **Never quote a local build id in a doc.** `scripts/check-versions.sh` rejects it, correctly. A local
build never matches the canonical id anyway (hazync#88 — the build path is baked into panic metadata).
Run that script only AFTER `git add`; it enumerates with `git ls-files`, so it cannot see an unstaged
file and will pass by not looking.

⚠ **Confirm the card.** L40S is the reference. H100 returns 0.95x, B200 is 8.7% slower at ~3x the
power, L4 is 37% more expensive per proof. If the rented card is not an L40S, record which it is on
every line — cycles/sec is the portable metric, wall-clock is not.

## 1. FIRST: produce chunk receipts — three later experiments need them

    HAZYNC_BLOCK=block_962000.json HAZYNC_CHUNKS=16 HAZYNC_SEG_PO2=22 \
      ./target/release/host prove-chunk <i>      # for i in 0..15, writes chunk_<i>.bin

`read_chunk_receipts()` verifies each against `METHOD_ID` on the way in, so a bad one names its own
file. **Keep these** — §4, §5 and §6 all consume them, and regenerating is the expensive part.

Record: wall per chunk, peak VRAM, segments per chunk. That alone confirms or corrects the
`~4.5 GPU-hours per block` figure in `FLEET_SIZING.md` §2, which is currently derived from a MODELLED
14.34 G cycle count.

## 2. E6 — worker processes x po2. **Run this before E9; it decides whether E9 is worth doing at all**

The gating experiment. It is 2-D because processes and po2 trade against each other through VRAM:
**L40S at po2 22 peaks at ~41 GB of 46 GB — 89%** — so a second process may not fit at po2 22 at all.

| | po2 20 | po2 21 | po2 22 |
|---|---|---|---|
| 1 process | | | ← current production |
| 2 processes | | | |
| 3 processes | | | |
| 4 processes | | | |

For every cell record **throughput (cycles/sec), peak VRAM, and mean GPU utilisation** — and record
cells that **OOM as OOM**, because the failures are the data that separates contention from memory.

Prior results that must be reconciled, not ignored:

- **1.20x on one card** from worker processes — but with **no recorded po2 or card**. If it was taken
  at po2 20 it is not comparable to a po2 22 baseline at all.
- **GPU concurrency rejected 3x at 0.95–1.03x** — but those were concurrent *chunk proves*, each
  peaking ~22 GB. `seg-connect` *segment* workers are lighter. Whether that distinction is real is
  exactly what this table settles.
- Dropping the in-process pipeline **"also unblocked po2 22, worth 1.15x on chunks and 1.42x on the
  aggregate"**. po2 22 is worth more than either scheduling fix, so any arm that buys parallelism by
  spending po2 22 is probably a bad trade.

**Decision rule.** If the optimum is several processes at po2 20/21, processes already fill the 65%
idle and **E9 is dead**. If it is 1 process at po2 22 — VRAM-bound, GPU still 65% idle — then only
pipelining can fill that idle, and E9 becomes worth its risk.

## 3. E7 / E8 — the B200 queue, and #182

- **E7**: po2 23 with #182's vendored risc0#3781 fix. po2 23 currently produces **invalid proofs**;
  #3781 is still **open upstream** and will not be merged for us, so #182 is permanent, not a stopgap.
- **E8**: po2 22 on a newer driver, to test whether the B200's 8.7% deficit is software maturity
  rather than silicon.

⚠ Take a **po2 22 control on the same binary first**. A cap-23 binary at po2 22 previously produced an
identical digest and a 1.4% wall difference, which is what proved the constant inert until the larger
po2 is actually requested. Without that control a po2 23 result means nothing.

## 4. E5 (remainder) — the aggregate's wire size

Chunks are measured: **202–218 segments, 55.3–62.0 MB per chunk at po2 22 (273–284 KB/segment)**, so
16 chunks ≈ **949 MB**. The aggregate is estimated at ~390 MB from a single po2 23 line
(`186 segments, 261.9 MB` = 1.41 MB/segment) scaled by the measured po2 slope.

With receipts from §1:

    HAZYNC_BLOCK=block_962000.json HAZYNC_CHUNKS=16 HAZYNC_AGG=1 HAZYNC_SEG_PO2=22 \
      ./target/release/host seg-serve      # prints "N segments, X MB" before it listens

Cheap, and it converts the one remaining estimate in `FLEET_SIZING.md` §6 into a measurement. Egress is
~18 Mbps either way; this is for completeness, not because the answer is in doubt.

## 5. E11 — is `HAZYNC_CHUNKS = 16` optimal?

**Free half, already measured.** The cost-packed packer holds the straggler at **1.00x from 4 to 64
chunks** (1.01x at 96 and 128), and the slowest chunk halves on every doubling. Total work is *exactly*
invariant: 4 x 3,585,326,976 = 128 x 112,041,468 = 14,341,307,904 cycles. So chunking is free at the
execute level, and 16 is a default nobody revisited — the code says so: *"HAZYNC_CHUNKS has been
treated as free."*

**GPU half.** More chunks means more **assumption resolutions** in the aggregate, and resolution is
recursion, so that cost lands in proving and is invisible to execute mode.

    for N in 8 16 32 64; do
      prove N chunks, then aggregate them, recording aggregate wall and resolve time
    done

**The question**: does resolution cost grow *sublinearly* in N? If so, more chunks is close to free
throughput and 16 is leaving parallelism unused. If linear or worse, 16 may already be right.

⚠ This needs receipts **per N**, so it is the most expensive item here — §1's sixteen only serve N=16.
Consider N ∈ {8, 32} first: two extra prove passes, and enough to establish the slope's sign.

## 6. E9 — preflight overlap with a GPU lock. **Only if §2 says there is idle left**

The GPU is **65% idle** waiting on one host thread (`vmstat`: 0.86 and 0.82 busy cores on H100 and
B200 — doubling cores changed nothing). Upstream solved this: **risc0#3201**, merged 2025-06-02, runs
preflight and `prove_core` in parallel in the actor worker, `800 ms -> 400 ms` at po2 20, and records
that **GPU locking adjustments were required** — the lock our own attempt lacked.

⛔ **Our attempt deadlocked CUDA**: hazync#147, chunk 11 of block 962000 at po2 22, hung **76 min and
3h38m** with the GPU at 0% and the consumer parked in `rx.recv()`. #148 removed it.

⛔ **There is no upgrade route.** Upstream is dormant — five commits in three months, all housekeeping;
#3781 open; our #3798 and #3799 unanswered. #3201 lives in the actor worker, which we do not use. So
this is a **fork** of `vendor/risc0-zkvm`, not a version bump, and the cost is correspondingly higher.

If attempted: set a **hard timeout** on every prove. The failure mode is a silent hang with the GPU at
0%, which is indistinguishable from a slow run until hours have gone.

## 7. Also worth a card, and arguably ahead of §6

**#139's packer refit.** After #139, ECDSA costs ~141,612 cycles/verify and Schnorr 1,950,000 —
**13.8x apart** — and `predicted_ec_ops` does not distinguish them. Simulated on block 962,000 (only
2.7% Schnorr):

| | slowest chunk | block speedup |
|---|---|---|
| after #139, packer unchanged | 305 M | **2.95x** |
| after #139, packer type-aware | 130 M | **6.95x** |

**The refit is worth 2.36x, is host-only, costs no `METHOD_ID` and spends no fidelity.** It is a
prerequisite for #139 rather than a follow-up: without it the win is 2.95x and the fidelity is spent
either way. Reproduce the simulation with
`python3 prover/tools/pack_after_139.py prover/block_962000.json`.

## 8. What NOT to spend the card on

| | why |
|---|---|
| A faster/bigger card | closed — 3.9x bandwidth returned 0.95x; three architectures agree |
| Cheap-card tiers | closed — L4 is 37% more expensive per proof |
| Wire compression | closed — egress is ~18 Mbps and does not bind |
| `NDEBUG` | closed — 0.0018% |
| `ECMULT_WINDOW` | closed — 19 is at the knee |
| C/C++ LTO | closed — `rust-lld` cannot read GCC LTO bytecode |
| A newer risc0 | closed — 3.0.6 is a Rust 1.97 fix we do not use; 5.0.0-rc.1 predates our pin |

## 9. The Tier 0 result is waiting on a re-baseline, not on a card

`-O3` + rust `lto="fat"` + `codegen-units=1` + `ECMULT_WINDOW=20` is **−1.160%**, gate-validated
(byte-identical journal digest, `ChunkOut` unchanged, both arms confirmed rebuilt by differing ids).

It is four lines and it moves `METHOD_ID`. It should ride the next guest change already paying for a
re-baseline rather than resetting the board on its own — the board currently carries the project's
first external contribution, and one of those two ranges *is* the frontier.
