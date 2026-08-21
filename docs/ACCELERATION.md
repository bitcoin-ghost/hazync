# Task: accelerate libsecp256k1 modular multiplication via the RISC0 bigint2 precompile

> **Update 2026-07-26 — profiled + measured the maximal-Core-safe alternatives.** A cycle profile of a
> signature-bearing block (execute mode, `RISC0_PPROF_OUT`) confirms the shape this doc assumes:
> **~69% of a small block is `secp256k1_fe_mul_inner` (47%) + `fe_sqr_inner` (22%)** — the field
> arithmetic only the (rejected) bigint2 backend below would touch. SHA is 3.4% (already accelerated),
> paging just 3.8%. So there is no *big* maximal-Core guest-compute win — the cost is the field math
> we've chosen not to reimplement.
>
> **Correction (2026-07-27): the `ECMULT_WINDOW_SIZE` sweep was not dead — it had only been run
> downward.** The first sweep tested 4→15, found every step down worse, and wrongly concluded window 15
> (libsecp's default) was optimal. It never tested *above* 15, because the checked-in
> `precomputed_ecmult.c` hard-errors there (`#if ECMULT_WINDOW_SIZE > 15 → #error`). Regenerating the
> table with libsecp's own `src/precompute_ecmult.c` generator and re-measuring shows **window 19 is the
> optimum**, and the saving holds across eras:
>
> | block | W=15 cycles | W=19 cycles | gain |
> |-------|------------|------------|------|
> | 130000 (pre-segwit, 10 inputs) | 22,421,345 | 22,000,202 | **−1.88%** |
> | 140000 | 416,197,506 | 406,635,106 | **−2.30%** |
> | 741000 (post-taproot, 670 inputs) | 1,826,507,658 | 1,793,753,595 | **−1.79%** |
>
> Above 19 it reverses (W=20: −1.65%) — the resident `pre_g` table doubles each step (~16 MB → 32 MB) and
> the paging cost finally overtakes the fewer wNAF additions. Adopted in **v0.10.0**. It is a pure
> libsecp *compile-time* config with zero soundness cost — no consensus code changes — but it does change
> the guest ELF, so it forced a `METHOD_ID` re-baseline. The generated table is deterministic EC maths
> (byte-identical on any machine), so the build stays reproducible; `build.rs` regenerates it only when
> the on-disk table isn't already ours.
>
> Modest, but the lesson is the transferable part: **"not worth it" was an unmeasured assertion, and the
> sweep that "proved" it only covered half the range.**
>
> **What we DID ship instead (v0.9.0, maximal-Core-safe, wire-format only):**
> 1. **Witness byte-packing** — risc0 serde encodes `Vec<u8>` as one 32-bit word *per byte* (4× bloat);
>    routing `raw_tx`/`prevouts` through `serialize_bytes` (4 bytes/word) turns the per-byte deserialise
>    loop into a bulk copy. **−40% guest cycles on big blocks** (block 741000: 3.06B → 1.84B).
> 2. **Per-tx `raw_tx`/`prevouts` dedup** — each input carried the full spending tx + prevouts blob, so a
>    multi-input tx repeated them per input (741000: 670 inputs, 146 unique txs). Sharing one blob per tx
>    (`tx_idx` reference) cuts the **witness 37×** (33.9MB → 0.92MB; `raw_tx` 56×, `prevouts` 67×). Its
>    payoff is bandwidth/storage (the coordinator ships 37× less to every worker), not proving cycles.
>
> Both are validated against the full adversarial suite (the #5 phantom-prevouts hole is now caught by the
> accumulator binding rather than a per-input blob-equality check).
>
> **Also shipped in v0.10.0 (host-side, no guest change): segment `po2` 20 → 21.** Bigger proving segments
> mean fewer of them and so less recursion/fold overhead. Measured on GPU (L40S, block 130000):
> po2 18 → 32.6s, 19 → 27.5s, 20 → 24.5s, **21 → 23.0s**, 22 → 23.3s — **~6% faster than the old default**,
> and flat beyond 21, so 21 is the sweet spot (same speed as 22, less GPU memory). The CLI still retries
> *downward* (21→20→19→18), so a smaller GPU that OOMs at 21 completes anyway. Receipts are identical
> either way — this is executor configuration, not guest logic.
>
> **Update 2026-07-30 — GPU concurrency measured and REJECTED, and this doc's po2 figure does not
> transfer to modern blocks.** Both measured on one 46 GB L40S, block 741000 (670 inputs), 16 chunks:
>
> | config | wall | vs best | peak VRAM |
> |---|---|---|---|
> | **po2=21, CONC=1** | **3,784 s** | — | 22,925 MiB |
> | po2=20, CONC=1 | 4,651 s | +22.9% | 13,835 MiB |
> | po2=20, CONC=2 | 4,443 s | +17.4% | 27,538 MiB |
> | po2=21, CONC=2 | — | **OOM** | 42,942 MiB |
>
> **1. There is no free throughput on this card.** A single prove at po2=21 peaks at 22.9 GB — half the
> card — so two do not fit (OOM in `risc0-zkp` `cuda.rs:246`, on a 128 MB allocation). Dropping to po2=20
> halves the segment and makes room, but concurrency then returns only **1.047x** while po2=20 itself
> costs 22.9%: it needed **1.229x** to break even and delivered a fifth of that. `po2=21, CONC=1` is
> optimal here. (`HAZYNC_GPU_CONC` was written on the branch this measurement came from, and that branch
> was deleted before it landed — see `c9e6bca`. **The knob does not exist**; this line claimed it did
> until 2026-08-18. Concurrency is fixed at 1 because nothing implements anything else.)
>
> The experiment was motivated by `nvidia-smi` showing 29-81% utilisation mid-proof, read as headroom.
> It is not: that figure means "a kernel was resident during the sample", not "the card is saturated".
> It reads 100% at CONC=1 and CONC=2 alike. **Do not tune concurrency from utilisation — only wall time
> settles it.**
>
> **2. The po2 sweep above (~6%) was measured on block 130000, a 10-INPUT block. On block 741000 the
> po2 20 -> 21 gap is 22.9%.** Smaller segments mean more segments, and per-segment recursion overhead
> scales with block size, so the penalty grows with the work. The sweep is not wrong; it is narrow.
>
> **3. The pattern, stated because it recurred three times in one day.** `ECMULT_WINDOW_SIZE` was swept
> only downward and declared optimal. The cycle profile was taken on a small block and missed that a
> tenth of a modern block is SHA marshalling. The po2 sweep was taken on a 10-input block and understates
> the penalty by 4x. **Parameters tuned on early blocks do not transfer to modern ones** — early blocks
> are nearly empty (block 20000 holds 19,023 UTXOs in total), so they exercise none of the cost that
> dominates at scale. Sweep on a signature-heavy block, or the result is about the wrong workload.
>
> **Update 2026-08-18 — the po2 sweep re-run on a near-tip block, and concurrency re-confirmed as a
> loss.** Measured on the same 46 GB L40S, block **962,000** (8,006 inputs), chunk 11 of 64.
>
> Point 2 above says the po2 sweep was taken on a 10-input block and warns it does not transfer. It did
> not. "Flat beyond 21" is a small-block result:
>
> | po2 | segments | wall | peak VRAM |
> |---|---|---|---|
> | 20 | 1,717 | 2,461 s | 13,835 MiB |
> | 21 | 841 | ~1,931 s | 22,905 MiB |
> | **22** | **415** | **1,779 s** | **41,089 MiB** |
>
> po2 22 is **~8% faster than 21** here, not flat. Segment count also halves cleanly with each step,
> which plausibly matters for hazync#119: if that fault is per-segment, fewer segments is less exposure.
>
> **No rate is quoted here, deliberately.** An earlier revision of this file claimed the fault was
> "per-segment at roughly 8e-5" and derived a 3.3%-against-12.8% chunk-failure comparison from it. That
> number had no provenance anywhere in the repo or in #119, and two things rule it out as evidence.
> #119 rests on **3 runs**, which bounds the chunk-failure rate only to **9%-99%** (Clopper-Pearson on
> 2 of 3) — a per-segment rate anywhere across a 48x spread, of which 8e-5 is the optimistic edge. And
> #119 measured a **different split**: block 962,000 chunk 2 of **16** (501 inputs), where the sweep
> above is chunk 11 of **64**, so 415 and 1,717 were never #119's segment counts. Direction only, until
> someone runs enough attempts to bound it.
>
> #119 also records that failure durations vary 8x on identical input and suspects a ceiling effect, so
> the independent-per-segment model may be wrong in kind and not merely in magnitude.
>
> Lower po2 loses on wall-clock regardless, and phase C below lost a job to #119.
>
> **This is NOT a recommendation to raise the default.** 41,089 MiB is 89% of the card, above the
> 42,942 MiB that OOMed at po2 21 CONC=2, and hazync#97 records unexplained *intermittent* po2-21 OOMs
> on this same box and driver (OOM with 39 GB free, proved with 24 GB free — non-monotonic, never root
> caused). One chunk of one block is not enough to justify a default that leaves 11% headroom on a card
> with a known intermittent allocation failure. It needs several chunks across several blocks, and
> ideally a second card, before anyone changes `seg_po2`.
>
> **Concurrency was re-measured and lost again**, with equal-sized jobs this time (same chunk index, so
> per-job cost is identical by construction):
>
> | config | wall | chunks/hr |
> |---|---|---|
> | po2 22, 1 job | 1,779 s | **2.02** |
> | po2 20, 2 jobs | 4,607 s | 1.56 |
> | po2 20, 3 jobs | 5,961 s | 1.21 (one job failed with hazync#119) |
>
> A caution for whoever measures this next, because it caught me: pairing a **big** job with a **small**
> one looks like 63% of the second job is absorbed free. It is not — small jobs slot into scheduling
> gaps. Equal jobs contend properly and the gain collapses to ~7%. Use identical workloads.
>
> **And read this file before measuring.** The 30 July result above already answered the concurrency
> question, including the `nvidia-smi` caveat, and it was re-derived from scratch on 2026-08-18 at a
> cost of several GPU-hours. That is the fourth instance of the pattern in point 3, in a different form:
> not a parameter tuned on the wrong workload, but an answer that already existed.
>
> **Update 2026-08-19 — the bigint field backend was BUILT and MEASURED at 1.67x, and rejected.**
> Not the naive multiply swap this file already disproved, but the full field-backend rework it
> recommends instead: a third libsecp backend holding field elements in precompile-native `[u32; 8]`
> permanently, so nothing converts per operation.
>
> Block 962,000, 8,006 inputs, execute mode, both sides same machine and day:
>
> | | cycles | per input | execute |
> |---|---|---|---|
> | baseline (10x26) | 17,394,637,671 | 2,172,700 | 838 s |
> | hzfe + `sys_bigint` | 10,388,552,466 | 1,297,596 | 334 s |
> | | **1.67x** | | |
>
> **It is correct.** Identical `tip_hash`, all consensus flags true, and libsecp's EC layer runs
> without a magnitude system at all. 1.4M differential comparisons against the stock backend, mutation
> checked. The approach works; it is not worth what it costs.
>
> **The number that killed it: `sys_bigint` costs about 678 cycles per 256-bit modular multiply.** The
> projection assumed 10-100 instruction-equivalents and predicted 6-8x. Every other input to that
> projection was measured; the estimated one decided the answer. 678 is only ~40% below the
> 1,141-instruction software multiply it replaces, and mul+sqr are 94.8% of the field work, so 40% off
> the dominant term is all you get.
>
> **Caveat, stated because it is unresolved rather than settled.** This file specifies **bigint2**, and
> the ~6x precedent came from the removed k256 experiment which used `risc0-bigint2`. The measurement
> above used `sys_bigint` — the older 256-bit `OP_MULTIPLY` syscall, which is what
> `risc0-zkvm-platform` exposes directly and what Step 0's "Precompile API — resolved" paragraph
> documents. `sys_bigint2_*` takes a `blob_ptr` and invokes a compiled bigint2 program, needing the
> `risc0-bigint2` crate. **Whether bigint2 is materially cheaper than 678 cycles is untested, and it is
> the single number that would reopen this.**
>
> **Why it was closed rather than retried.** At 7x, replacing one field backend with 1.4M differential
> comparisons behind it is a trade worth defending. At 1.67x it spends the project's strongest claim —
> that this runs Bitcoin Core's real code — for a 40% cycle reduction. The weakening was real: arithmetic
> Core ships replaced by arithmetic written here; the magnitude contract the EC layer was written
> against removed; a Rust shim and a direct platform dependency added to the guest. Operator decision,
> 2026-08-19: the cost is the point.
>
> The implementation, the differential harness and the integration path are preserved under
> `experimental/field-backend/`, and the profilers under `experimental/field-op-profile/`. Anyone reaching for this
> idea again will find it built, measured and answered.
>
> **4. What is left, given the above.** Scheduling is closed off. Guest compute is closed off (69.5% is
> Core's own field arithmetic; the SHA marshalling win was 3.8% and was rejected as not worth touching
> Core). That leaves exactly two levers, both procurement rather than engineering: **more cards**
> (linear, and chunks are independent so it is near-perfect scaling) and **a faster card**. The latter is
> unmeasured and worth an hour of cloud rental: proving is NTT-bandwidth-heavy and the L40S has ~864 GB/s
> against ~2,039 GB/s on an A100 and ~3,350 GB/s on an H100 SXM.

> Remaining maximal-Core levers: host-side tree-fold (log-depth aggregation, the rest of the ~14% fold
> overhead), and multi-GPU throughput (safe — every parallel proof is independently verified). The bigint2
> field backend below stays **out of scope** (reimplements Core's field layer → the exact equivalence
> question Hazync exists to delete).

> **Update 2026-07-19 — the k256 experiment has been REMOVED from the guest.** `k256_ecdsa_verify`, the
> `k256`/`crypto-bigint` dependencies, the `HAZYNC_ECDSA_BENCH` branch, and `patches/0003` are gone. The
> sound guest is now **pure Core + the accumulator, nothing else** — no alternative EC implementation
> linked in. This doc is kept as the acceleration *analysis*: why EC acceleration is hard, and why
> pure-Core is the sound baseline. Any future accelerator starts from here.

**Status:** PROTOTYPED end-to-end on WSL2 (no GPU needed — `sys_bigint` emulates in execute mode).
**Result: the naive field-mul intercept is byte-correct but ~10% SLOWER** — see "Prototype result"
below. The cheap version is disproven; the sound-and-fast path is a bigger job (a new libsecp field
backend). k256 was the measured 6× option but has been removed (it reintroduced the reimplementation
question); pure Core is the sound baseline.

## Step 0 — recon findings (2026-07-15)

**Field backend (unknown #1) — resolved.** The RISC0 rv32 toolchain has no `__int128`
(`'__int128' is not supported on this target`) and `SIZE_MAX == 0xffffffff`, so secp256k1 selects
`SECP256K1_WIDEMUL_INT64` → the **10×26 field backend** (`field_10x26_impl.h`) and **8×32 scalar
backend** (`scalar_8x32_impl.h`). These use emulated `uint64_t` on rv32 (32×32→64 via MUL+MULHU), i.e.
the expensive path we're replacing. Intercept at `secp256k1_fe_impl_mul` / `secp256k1_fe_impl_sqr`
(field_10x26_impl.h ~line 1005) and `secp256k1_scalar_mul` (scalar_8x32_impl.h ~line 644).

**Precompile API (unknown #2) — resolved.** `sys_bigint(result, OP_MULTIPLY, x, y, modulus)` computes
`(x*y) mod modulus` for **256-bit** operands (`[u32; 8]` little-endian) with an **arbitrary** modulus —
a direct fit for both `fe_mul` (mod p) and `scalar_mul` (mod n). `OP_MULTIPLY = 0`, `WIDTH_WORDS = 8`.
The now-removed k256 experiment used this same primitive at the field-arithmetic level for its ~5–6×,
which retires the worry that per-mul ecall overhead would negate field-level acceleration.

**Conversion (the plumbing).** Reuse libsecp's own helpers — no manual 26-bit repacking:
`secp256k1_fe_get_b32`/`set_b32_mod` and `secp256k1_scalar_get_b32`/`set_b32` convert to/from 32-byte
**big-endian**; the shim only byte-swaps BE↔LE-words. Field inputs to `fe_impl_mul` have magnitude ≤ 8,
so the C patch normalizes local copies (`secp256k1_fe_impl_normalize_var`) before `get_b32`. The output
is set via `set_b32_mod` (magnitude-1, value < p) — a valid drop-in for what `fe_mul` produces.

**The C patch (`patches/0004`, to apply + test on a box).**
```c
/* field_10x26_impl.h — secp256k1_fe_impl_mul / _impl_sqr */
extern void hazync_modmul_p(unsigned char* out, const unsigned char* a, const unsigned char* b);
SECP256K1_INLINE static void secp256k1_fe_impl_mul(secp256k1_fe *r, const secp256k1_fe *a, const secp256k1_fe * SECP256K1_RESTRICT b) {
    secp256k1_fe na = *a, nb = *b; secp256k1_fe_impl_normalize_var(&na); secp256k1_fe_impl_normalize_var(&nb);
    unsigned char ba[32], bb[32], bo[32];
    secp256k1_fe_impl_get_b32(ba, &na); secp256k1_fe_impl_get_b32(bb, &nb);
    hazync_modmul_p(bo, ba, bb); secp256k1_fe_impl_set_b32_mod(r, bo);
}
/* _impl_sqr: same with a single input, hazync_modmul_p(bo, ba, ba). */
/* scalar_8x32_impl.h — secp256k1_scalar_mul */
extern void hazync_modmul_n(unsigned char* out, const unsigned char* a, const unsigned char* b);
static void secp256k1_scalar_mul(secp256k1_scalar *r, const secp256k1_scalar *a, const secp256k1_scalar *b) {
    unsigned char ba[32], bb[32], bo[32]; int overflow;
    secp256k1_scalar_get_b32(ba, a); secp256k1_scalar_get_b32(bb, b);
    hazync_modmul_n(bo, ba, bb); secp256k1_scalar_set_b32(r, bo, &overflow);
}
```
The shim `bigint_accel.rs` (kept in git history, not present in the tree) provided `hazync_modmul_p` /
`_n` (`#[no_mangle] extern "C"`); adding `mod bigint_accel;` to the guest linked it against the patched C
(build already uses `--allow-multiple-definition`).

## Prototype result — field-mul-level intercept is a NET LOSS (2026-07-15, measured on WSL2, execute mode)

Built end-to-end without a GPU box (`sys_bigint` emulates in execute mode — confirmed: 256-bit `x*y mod p`
matches a num-bigint reference). Applied the intercept above to a real guest and measured **block 170**
(one real ECDSA verify):

| build | cycles | tip hash |
|-------|--------|----------|
| pure Core (baseline) | 2,299,144 | correct |
| libsecp modmul → `sys_bigint` (this patch) | **2,539,832 (+10%)** | **identical** |

**Byte-correct but ~10% slower.** Diagnosis: `sys_bigint` itself is cheap (the removed k256 experiment
did a whole verify in ~328K cycles *using it*). The loss is the **per-multiply conversion overhead** this approach requires —
each field mul does `normalize`×2 + `get_b32`×2 + BE↔LE swap + `set_b32_mod` (~80 net cycles × ~3000
muls/verify ≈ the +240K). The conversion costs about as much as the emulated 10×26 multiply it replaces.

**Conclusion:** you cannot get the speedup by swapping only the multiply while keeping libsecp's native
10×26 field representation — the per-op conversion eats it. The removed k256 experiment won because it kept
field elements in precompile-native `[u32;8]` form the *entire time* and never converted per-op. The sound-and-fast path is
therefore a **new libsecp field *backend*** (store `fe` as precompile-native, reimplement the field ops
— add/negate/normalize/sqr — around `sys_bigint`), keeping the EC algorithm / GLV / ECDSA real. That is
a real ~few-hundred-line reimplementation of the *field layer* (bigger than a mul swap, smaller and more
sound than a full-EC reimplementation), and whether it beats the removed experiment's measured 6× is itself unproven.

**So the acceleration options today, honestly:** (a) pure Core — fully sound, the current baseline,
~$1M full run; (b) the bigint2 field-backend rework above — sound, real work, uncertain it wins. (The
removed k256 EC-substitution experiment measured ~6× but reintroduced the reimplementation-equivalence
question Hazync exists to avoid, so it is not an option.) The naive "route the multiply" idea (this
file's original premise) is **disproven** as a cheap win.

Artifacts (staged, never committed to the guest, now in git history): the patch was applied to a *local*
secp256k1 clone; the shim was `prover/methods/guest/src/bigint_accel.rs`; the exact C intercept lives in
the git history of this measurement. The shim + intercept remain a correct reference for the field-backend approach.

## Why this matters

EC signature verification is ~95% of the proving cost (~2.1M cycles/input for pure real Core). Cutting
it ~5× takes a full-chain run from roughly **$1M → ~$200–400K** on well-chosen hardware. We have a
*measured* precedent that the arithmetic can be accelerated ~5–6× (the removed k256 experiment, which
routed ECDSA verify through the RISC0-accelerated `k256` crate). But that **substituted** libsecp's entire
EC + ECDSA implementation — reintroducing exactly the reimplementation-equivalence question Hazync exists
to avoid. This task gets the same speedup **without the substitution**.

## The idea

Keep **all** of libsecp256k1's real code — the EC algorithm (wNAF, GLV endomorphism, precomputed
tables), ECDSA logic, lax-DER parsing, low-S normalisation, the sighash — and replace **only the modular
multiplication primitive** (`secp256k1_fe_mul`/`_sqr` mod p, and `secp256k1_scalar_mul` mod n) with calls
to RISC0's **bigint2** precompile. Add a new libsecp *field backend* that, instead of the limb-based C
multiply, hands the operands to bigint2 and converts the result back.

## Why this is sound (and *more* sound than a full-EC reimplementation)

A zkVM precompile is **constrained, not trusted** — the circuit *proves* bigint2 computed `a·b mod p`
correctly. So using it adds **no new trust assumption beyond RISC0's zkVM soundness**, which we already
rely on. It is the *identical posture* to our existing SHA-256 accelerator (`patches/0002`), which we
already treat as sound and byte-identical.

The soundness surface shrinks from a full-EC substitution's "does an entire reimplementation match libsecp
forever?" to just **"the limb ↔ bigint2 plumbing is correct"** — a small, mechanically-checkable property (see the
differential gate, Step 2). Everything above the modmul stays literally libsecp's code.

## Task breakdown

### Step 0 — recon (determine the unknowns) — ~2 days
- **Which field backend is active on `riscv32im`?** libsecp picks its limb representation from
  `SECP256K1_WIDEMUL`. rv32im has 32-bit `MUL`/`MULH` but emulates 64-bit — determine whether the build
  uses the `5x52` (int128, emulated) or `10x26` backend. This decides where to intercept. Check the
  guest build (`prover/methods/guest/build.rs`) and the resulting `secp256k1` config.
- **bigint2 API surface.** Inspect `risc0-bigint2` (the crate the accelerated `k256` uses): does it
  expose a raw 256-bit modular-multiply (`modmul(a, b, modulus)`), or only higher-level EC ops? Confirm
  it takes an **arbitrary prime modulus** (we need both mod p *and* mod n). Determine the operand format
  (little-endian 256-bit words, alignment).
- **Invocation granularity.** Estimate the per-call precompile overhead. If single-field-mul calls are
  overhead-dominated, plan to batch several modular ops per bigint2 "blob" (as k256 likely does) — note
  the purity trade-off in Step 3.

### Step 1 — the field backend — ~1 week
- Write a Rust shim in the guest, `extern "C"`, e.g. `hazync_fe_mul_bigint2(r, a, b)` and
  `hazync_scalar_mul_bigint2(r, a, b)`: convert libsecp limbs → bigint2 operand format → `modmul` (mod p
  or mod n) → back to limbs.
- Patch libsecp to route `secp256k1_fe_mul_inner`/`secp256k1_fe_sqr_inner` (and the scalar equivalents)
  to the shim. Ship it as `patches/0004-field-mul-via-bigint2.patch` (same style as `0002`/`0003`).
- Get **one** ECDSA `verify` returning `1` on a known-good vector through the new backend.

### Step 2 — the soundness/correctness gate (differential fuzz) — ~3 days
- Native (host) test harness: run **stock libsecp** field/scalar mul and the **bigint2-backend** version
  (using `risc0-bigint2`'s host implementation) on the same random inputs; assert **byte-identical**
  outputs. ≥10M random field elements + ≥10M scalars, including edge cases (0, 1, p−1, n−1, values near
  the modulus). This validates the plumbing — the one real correctness risk.
- A smaller in-zkVM (execute-mode) test confirms the *precompile path itself* agrees with the host.

### Step 3 — measure — ~3 days
- Cycle count per input: stock pure-Core vs bigint2-backend, using the existing execute-mode path
  (`host check-full` reports cycles; compare on a real input-heavy block e.g. 741000, and an early
  ECDSA block).
- Report the achieved factor. If field-mul-level is < ~4×, prototype the blob-level variant (express
  point-add/double as bigint2 blobs) and re-measure — note that this moves the group law into blob form
  (still constrained/sound, but less literally libsecp's C; document the purity trade precisely).

### Step 4 — full-guest regression — ~2 days
- Rebuild the guest with the backend and re-run the Hazync regression set: block 170, block 741000,
  `check-ibd` genesis→550. **All tip hashes, cum_work, and UTXO-leaf counts must be identical** to the
  pure-Core results in `prover/evidence/`. This proves the acceleration changed *nothing* observable.

## Deliverables
1. `patches/0004-field-mul-via-bigint2.patch` + the guest Rust shim.
2. A native differential-fuzz test (byte-identical vs stock libsecp) — the soundness gate, reproducible.
3. A benchmark: measured cycles/input and the speedup factor, pure-Core vs accelerated.
4. A short writeup: the factor achieved, the constrained-accelerator soundness argument, and the
   field-mul-level vs blob-level purity note.

## Success criteria
- **Byte-identical** to stock libsecp across ≥10M random field + scalar muls (incl. edge cases).
- Every existing Hazync regression vector proves to the **identical** tip hash / cum_work / UTXO count.
- **≥3× per-input speedup measured** (5× = stretch goal). The measured number — not an estimate — is
  what a full-run budget is set against.

## Risks / open questions
- rv32 field backend representation (Step 0) — wrong assumption changes the whole intercept.
- bigint2 might expose only EC-level ops, not raw modmul — would push toward the blob-level variant.
- Per-call overhead could make field-mul-level < 4×, needing blob-level (more work, slight purity cost).
- Mod-n (scalar) support in bigint2 must be confirmed alongside mod-p.
- Proving-memory impact on smaller-VRAM GPUs (relevant to the cheap-hardware plan in `HAZYNC_ARCHITECTURE.md`).

## References in this repo
- `patches/0002-sha256-route-through-risc0-accelerator.patch` — precedent for a constrained accelerator
  swap (same soundness posture as this task).
- `prover/methods/guest/build.rs` — how the Core C++ + libsecp256k1 TUs are compiled into the guest.
- `HAZYNC_ARCHITECTURE.md` — the full-run cost model this speedup feeds into.
