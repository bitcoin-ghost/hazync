# Recommended topology and settings

**As of v0.21.4 (2026-09-14), for the canonical CORE guest `37987b85`.** One page for "what should we
actually run, and why" — fleet shape, card, per-box settings, guest build, provisioning. It states
conclusions. The CORE measurements behind them are `docs/history/BENCH_8xL40S_2026-09-08.md`,
`docs/history/MILESTONE_966256_2026-09-10.md` and `docs/history/MILESTONE_966256_RUN4_2026-09-10.md`;
the stock-guest investigations that preceded them are `docs/history/TEN_MINUTE_BLOCK.md`,
`docs/history/ACCELERATION.md` and `docs/history/TIER0_RESULTS_2026-08-26.md`. Earlier revisions of this
page priced the stock guest; their headline numbers are in §7 so they are not quoted again.

**Every row is labelled.** MEASURED means it exists in this repo's evidence — on the CORE guest unless it
says *stock*. INFERRED means arithmetic over measured inputs. UNKNOWN means nobody has measured it.

⚠ **Two rules this project keeps re-learning:**

1. **Never quote a fleet size without its framing, its card and its po2.**
2. **Measure before quoting.** Every number in §7 was, at the time, someone's confident summary of a
   real measurement.

---

## 0. Two workloads — decide which one you are sizing for

`GOALS.md` G2 and G6 are one goal and two workloads, and pricing them as the same thing is where most
fleet arguments on this board came from.

| | **Backfill** (G2) | **Tip-following** (G6) |
|---|---|---|
| sized by | budget and calendar | the block interval |
| per-block latency | **irrelevant** — the whole chain is queued | matters, once caught up |
| fidelity posture | inputs are a **closed set**, exhaustively testable | inputs do not exist yet |

⇒ **While backfilling, size for throughput (§1.2).** Tip latency (§1.1) is not a live question until
catch-up is in sight.

---

## 1. Fleet size

### 1.1 Latency — one block, tip to receipt, inside 600 s

**MEASURED — 8 × L40S, 8 chunks (one per card), po2 21, aggregate over 8 `seg-connect` workers**
(`BENCH_8xL40S_2026-09-08.md`):

| block | prevouts | chunk phase (slowest card) | straggler | aggregate | compute total |
|---|---|---|---|---|---|
| 966,108 | 8,562 | 709 s | 1.118 | 203.8 s | **912.8 s** |
| 966,107 | 7,961 | 602 s | 1.048 | 205.8 s | **807.8 s** |
| 966,106 | 6,644 | 522 s | 1.059 | 189.8 s | **711.8 s** |

**INFERRED — the card curve**, fitted to 966,108, the heaviest of the three:

```
block ≈ (5,075 / N) × straggler  +  19.2  +  (184.6 × 8 / N)
```

| cards | 8 | 10 | 12 | **13** | 16 |
|---|---|---|---|---|---|
| block | 15.2 min *(measured 15m13s)* | 12.3 min | 10.3 min | **~9.8 min** | 7.8 min |

⇒ **~13 L40S for a sub-10-minute CORE block.** Only N=8 is measured. Two caveats pull in opposite
directions: the straggler is likely to get worse above 8 chunks, and the ~19 s execute has since come
off the critical path (#236, merged after these runs).

**MEASURED — rented RTX 4090s, block 966,256** (4,741 txs, 9,079 inputs), po2 21, one chunk per card:

| run | cards | chunk phase | aggregate | end to end | GPU cost |
|---|---|---|---|---|---|
| 1 | 26 | 243.9 s | 139.4 s | **485.2 s** (8.09 min), attested from logs | $1.191 |
| 4 | 27 | 247.9 s | 241.2 s | **544.0 s** (9.07 min), receipt on disk | $1.387 |

⚠ At 26–27 cards the aggregate is as large as the chunk phase, but it is not a fixed floor. On the same
27 receipts the #235 N-sweep on A40s measured assembly **162.5 s at N=4 → 63.5 s at N=26**; run 4's
128.7 s assembly is most likely coordinator round trips across a geographically spread fleet —
**inferred, not measured** (`MILESTONE_966256_RUN4_2026-09-10.md`, correction; #252).

### 1.2 Throughput — bounded lag

Concurrency comes from proving *different* blocks at once: `prove-chunk` takes no previous receipt and
per-block aggregates are independent, so nothing has to distribute across cards to keep pace.

**INFERRED**, block 966,108:

```
per block:   5,075 chunk card-s  +  184.6 × 8 aggregate card-s  +  19.2 s  ≈  6,570 card-seconds
throughput:  6,570 / 600  ≈  11 L40S
```

⚠ One near-tip block, and its aggregate card-seconds come from the fit above rather than a meter. Blocks
vary: the three in §1.1 span 3,942–5,075 chunk card-seconds.

**The chain fold does not threaten it.** MEASURED on the stock guest: **3.76 s/fold** on an L40S, flat in
range length (`prover/evidence/fold_and_snark_wrap_1_1000.txt`), and 2.0–3.0 s/fold depending on
concurrency (`prover/evidence/fold_concurrency_2xL40S.txt`). ⚠ Both over early receipts with small UTXO
boundaries; per-fold VRAM at high-UTXO heights is UNKNOWN.

---

## 2. The card

| card | verdict | evidence |
|---|---|---|
| **L40S 46 GB** | ✅ the baseline | §1.1 — 8 cards measured |
| **RTX 4090 24 GB** | ✅ runs the CUDA default po2 21 | 26 and 27 cards on block 966,256; peak **22,478 MiB** of 24,564 (91%) |
| H100 80 GB | ✗ **0.95x** (stock) | more memory bandwidth bought less throughput — `docs/history/ACCELERATION.md` |
| B200 | ✗ 8.7% slower than an L40S even with native `sm_100` (stock) | `docs/history/ACCELERATION.md` |
| L4 | ✗ 37% more expensive per proof (stock) | `docs/history/TEN_MINUTE_BLOCK.md` |

⚠ **24 GB cards cannot run po2 22** — a stock chunk peaked at 40.6 GB there. At the default po2 21 they
fit, as measured above.

---

## 3. Per-box settings

| setting | value | label | why |
|---|---|---|---|
| `HAZYNC_LIFTX_HINT` | **`1`, at run time** | MEASURED | the CORE guest's chunk mode reads a pubkey-hint block, so a chunked command (`prove-chunk`, `prove-seg`, `seg-serve`, …) run without it dies with `DeserializeUnexpectedEnd`, which reads as a corrupt fixture. `provision-vps.sh` exports it in `.bashrc`; board workers (`coordinator/hazync`) do not take the chunk path. → `docs/LIFTX_HINT.md` §6 |
| `HAZYNC_CHUNKS` | **= card count** | MEASURED | 8 vs 16 chunks on 8 cards: mean **+0.2%** over three blocks, sign inconsistent, all six receipts digest-identical. A free operational choice, not a tuning lever |
| `HAZYNC_SEG_PO2` | **21** (CUDA default; 20 on CPU) | MEASURED | `seg_po2()` in `prover/host/src/main.rs`; every CORE fleet run above used it. po2 22 measured ~11–12% faster on the stock and #139 arms at ~40.6 GB peak — 46 GB cards only, and GPU work must then be serialised. Not measured on CORE |
| GPU concurrency | **1** | MEASURED (stock) | rejected three times at 0.95–1.03x. `hazync` serialises proves through a GPU lock; a direct `host prove-*` does not (#97) |
| disk per segment work dir | ~1.6 GB | MEASURED (stock) | watch `df` |
| po2 23 | **do not** | MEASURED (stock) | ~79 GB (B200-only) and two code changes — §6 |

⚠ **`nvidia-smi utilization.gpu` is kernel residency, not useful work.** Never tune from it.

hazync#119 — 5 faults in 80 chunk attempts on the 8-card fleet, each restarting a chunk from segment
zero — is fixed in v0.21.1 (#245) by the vendored `risc0-circuit-rv32im-sys` in `prover/Cargo.toml`.

---

## 4. Guest build settings — all shipped

Every row moves `METHOD_ID`, so none is an operator knob. The codegen and window rows shipped in v0.20.0
(`42417d2`); the libsecp patches became canonical in v0.21.0 (`c12ad67`).

| setting | shipped | measured gain |
|---|---|---|
| C/C++ opt level | `-O3` | −0.264% |
| Rust `lto` | `"fat"` | −0.486% |
| Rust `codegen-units` | `1` | −0.361% |
| `ECMULT_WINDOW_SIZE` | **21** | **−1.245%** — §4.1 |
| `ECMULT_GEN_KB` | 22, unchanged | 0% — inert for verification, see below |
| `NDEBUG` | absent | −0.0018%, not worth the fidelity question |
| field backend + `lift_x` hint | `patches/0012` + `0013` | **4.095x** fewer cycles on block 962,000, journal byte-identical to stock (`docs/BUILDS.md`) |

The codegen, window and `NDEBUG` figures are guest cycles on block 140,000 (212 inputs) with stock
libsecp (`docs/history/TIER0_RESULTS_2026-08-26.md`); the codegen arms were additive to within 0.001%.

**`ECMULT_GEN_KB` is inert**: 2, 22 and 86 give bit-identical cycles, because it sizes the `k·G` table
used for *signing* and Hazync only verifies. Setting it to 2 would reclaim memory; it moves
`METHOD_ID`, so it waits for a re-baseline that is happening anyway.

### 4.1 `ECMULT_WINDOW_SIZE` — 21, shipped

MEASURED on block 140,000, 212 inputs, stock libsecp: window 19 **376,662,184** cycles, 20
**375,914,975** (−0.198%), 21 **371,971,773** (−1.245%). The journal digest is identical across all
three and reproduces `docs/history/TIER0_RESULTS_2026-08-26.md` bit for bit. Window 21 is the guest
`build.rs` default and `provision-vps.sh` exports it.

⚠ **A small-workload sweep under-rates large windows.** On block 130,000 (10 inputs) window 20 measured
*worse* than 19; at 212 inputs the curve falls monotonically. Re-run any guest-codegen arm at a
realistic input count before believing it, in either direction.

⛔ **Windows ≤15 cannot be swept naively.** `build.rs` regenerates `precomputed_ecmult.c` only above
window 15; at ≤15 it reuses whatever table is on disk. **And any sweep mutates the shared source tree at
`$HAZYNC_BASE`** — back up `secp256k1/src/precomputed_ecmult.c` and restore it afterwards, or the
canonical build inputs carry the last arm's table.

---

## 5. Provisioning

| setting | value | why |
|---|---|---|
| CUDA | **12.8** | RISC0 3.0.5's kernels do not build against 13.x (`provision-vps.sh` phase 7; `HAZYNC_CUDA_VER` overrides) |
| `SKIP_GROTH16=1` | on proving and benchmark boxes | skips `risc0-groth16`; the box proves but cannot `snark-wrap` |
| split phases | `HAZYNC_PROVISION=deps` then `build` | |
| CUDA release binary | `sm_80 sm_86 sm_89 sm_90 sm_100` | `prover/build-release.sh` |

⚠ **Check the per-box evidence directory before commissioning a run.** A B200 was once provisioned to
re-test po2 23 before anyone read the evidence that already answered it.

---

## 6. What is NOT settled

1. **The card curve above 8 chunks on one fleet.** ~13 L40S is inferred from N=8; no L40S fleet between
   9 and 26 cards has run.
2. **po2 22 on CORE.** ~11–12% on the stock and #139 arms; unmeasured on CORE, and 46 GB cards only.
3. **Per-fold cost and VRAM at high-UTXO heights** (§1.2).
4. **po2 23.** Needs **two** changes: raise `DEFAULT_MAX_PO2` **and** recompute and ship
   `allowed_control_root("poseidon2", 23)` — raising the cap alone moves the computed root away from
   the baked `ALLOWED_CONTROL_ROOT` and every proof fails verification. B200-only (~79 GB).

**Closed — do not re-open without new evidence:** chunk count (§3); GPU concurrency (rejected 3x); the
card axis (§2); the CUDA kernel lever at the compiler level (5 arms, stock is best); coordinator egress;
`NDEBUG`; C/C++ LTO (`rust-lld` cannot read GCC LTO bytecode); a newer risc0.

---

## 7. Superseded numbers — do not quote these

| number | status |
|---|---|
| **~29 L40S** throughput, **32–48** latency | ⛔ **stock guest**, priced on block 962,000 before CORE. CORE: ~11 throughput, ~13 latency (§1) |
| **~7–9 cards** with #139 | ⛔ projection. Measured **10** CORE / **5** Ghost on block 962,000 (`docs/BUILDS.md` §1) |
| **9.10x** wholesale bigint2 | ⛔ **never produced by a run** — nothing called the wholesale entry point until `patches/0014` (`9b767b5`) |
| "aggregate scaling past 2 cards is UNMEASURED" | ⛔ 3 workers **2.78x** (stock, `2facde4`); 8 workers 203.8 s (§1.1); the #235 N-sweep to N=26 |
| "the chain fold's per-block cost is UNKNOWN" | ⛔ 3.76 s/fold (§1.2) |
| "24 GB cards must drop to po2 20" | ⛔ po2 21 fits, 22,478 MiB peak (§2) |
| aggregate 1,575 s / 627 s on block 962,000 | ⚠ superseded: 473.1 s with the coordinator also working, 405.6 s on two workers (`docs/BUILDS.md` §1) |
| "the aggregate saturates at N=2" / "is the binding constraint" | ⛔ false (§1.1) |
| aggregate ">3,300 s" | ⛔ stale |
| `N^1.79` aggregate scaling | ⛔ artefact — the block changed, not N |
| "the GPU is 65% idle" | ⛔ refuted: 91.5% busy |
| "worker processes are worth 1.20x" | ⚠ ceiling ≤1.09x (stock) |
