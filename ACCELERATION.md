# Task: accelerate libsecp256k1 modular multiplication via the RISC0 bigint2 precompile

**Status:** PROTOTYPED end-to-end on WSL2 (no GPU needed — `sys_bigint` emulates in execute mode).
**Result: the naive field-mul intercept is byte-correct but ~10% SLOWER** — see "Prototype result"
below. The cheap version is disproven; the sound-and-fast path is a bigger job (a new libsecp field
backend). **Priority downgraded** accordingly — k256 (`patches/0003`, proven 6×) remains the pragmatic
accelerator, pure Core the sound baseline.

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
The accelerated `k256` crate uses this same primitive at the field-arithmetic level for its ~5–6×, which
retires the worry that per-mul ecall overhead would negate field-level acceleration.

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
The shim `bigint_accel.rs` provides `hazync_modmul_p` / `_n` (`#[no_mangle] extern "C"`); add
`mod bigint_accel;` to the guest and it links against the patched C (build already uses
`--allow-multiple-definition`).

## Prototype result — field-mul-level intercept is a NET LOSS (2026-07-15, measured on WSL2, execute mode)

Built end-to-end without a GPU box (`sys_bigint` emulates in execute mode — confirmed: 256-bit `x*y mod p`
matches a num-bigint reference). Applied the intercept above to a real guest and measured **block 170**
(one real ECDSA verify):

| build | cycles | tip hash |
|-------|--------|----------|
| pure Core (baseline) | 2,299,144 | correct |
| libsecp modmul → `sys_bigint` (this patch) | **2,539,832 (+10%)** | **identical** |

**Byte-correct but ~10% slower.** Diagnosis: `sys_bigint` itself is cheap (k256 does a whole verify in
~328K cycles *using it*). The loss is the **per-multiply conversion overhead** this approach requires —
each field mul does `normalize`×2 + `get_b32`×2 + BE↔LE swap + `set_b32_mod` (~80 net cycles × ~3000
muls/verify ≈ the +240K). The conversion costs about as much as the emulated 10×26 multiply it replaces.

**Conclusion:** you cannot get the speedup by swapping only the multiply while keeping libsecp's native
10×26 field representation — the per-op conversion eats it. k256 wins because it keeps field elements in
precompile-native `[u32;8]` form the *entire time* and never converts per-op. The sound-and-fast path is
therefore a **new libsecp field *backend*** (store `fe` as precompile-native, reimplement the field ops
— add/negate/normalize/sqr — around `sys_bigint`), keeping the EC algorithm / GLV / ECDSA real. That is
a real ~few-hundred-line reimplementation of the *field layer* (bigger than a mul swap, smaller and more
sound than k256's full-EC substitution), and whether it beats k256's 6× is itself unproven.

**So the acceleration options today, honestly:** (a) pure Core — fully sound, ~$1M full run; (b) k256
substitution (`patches/0003`) — proven 6×, soundness caveat, gated by a diff-fuzz test; (c) the field-
backend rework above — more sound than (b), real work, uncertain it wins. The naive "route the multiply"
idea (this file's original premise) is **disproven** as a cheap win.

Artifacts (staged, not committed to the guest): the patch is applied to a *local* secp256k1 clone; the
shim is `prover/methods/guest/src/bigint_accel.rs`; the exact C intercept is in the git history of this
measurement. The shim + intercept remain a correct reference for the field-backend approach.

## Why this matters

EC signature verification is ~95% of the proving cost (~2.1M cycles/input for pure real Core). Cutting
it ~5× takes a full-chain run from roughly **$1M → ~$200–400K** on well-chosen hardware. We have a
*measured* precedent that the arithmetic can be accelerated ~5–6× (`patches/0003`, which routes ECDSA
verify through the RISC0-accelerated `k256` crate). But `k256` **substitutes** libsecp's entire EC +
ECDSA implementation — reintroducing exactly the reimplementation-equivalence question Hazync exists to
avoid. This task gets the same speedup **without the substitution**.

## The idea

Keep **all** of libsecp256k1's real code — the EC algorithm (wNAF, GLV endomorphism, precomputed
tables), ECDSA logic, lax-DER parsing, low-S normalisation, the sighash — and replace **only the modular
multiplication primitive** (`secp256k1_fe_mul`/`_sqr` mod p, and `secp256k1_scalar_mul` mod n) with calls
to RISC0's **bigint2** precompile. Add a new libsecp *field backend* that, instead of the limb-based C
multiply, hands the operands to bigint2 and converts the result back.

## Why this is sound (and *more* sound than k256 substitution)

A zkVM precompile is **constrained, not trusted** — the circuit *proves* bigint2 computed `a·b mod p`
correctly. So using it adds **no new trust assumption beyond RISC0's zkVM soundness**, which we already
rely on. It is the *identical posture* to our existing SHA-256 accelerator (`patches/0002`), which we
already treat as sound and byte-identical.

The soundness surface shrinks from k256's "does an entire reimplementation match libsecp forever?" to
just **"the limb ↔ bigint2 plumbing is correct"** — a small, mechanically-checkable property (see the
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
- Proving-memory impact on smaller-VRAM GPUs (relevant to the cheap-hardware plan in `SCALING.md`).

## References in this repo
- `patches/0003-pubkey-ecdsa-verify-via-k256-accel.patch` — the k256 substitution (the thing we're
  *replacing* with a sound version; useful as a plumbing reference for hooking the guest to a Rust accel).
- `patches/0002-sha256-route-through-risc0-accelerator.patch` — precedent for a constrained accelerator
  swap (same soundness posture as this task).
- `prover/methods/guest/build.rs` — how the Core C++ + libsecp256k1 TUs are compiled into the guest.
- `SCALING.md` — the full-run cost model this speedup feeds into.
