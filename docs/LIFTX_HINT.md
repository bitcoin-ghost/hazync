# hazync#205 — hint the pubkey Y, verify it, and stop computing square roots

**Status: COMPLETE BUT NEVER COMPILED, AND NOT MEASURED.** Nothing here may be quoted as a
speedup until the A/B in §5 has run. → `feedback_only_measured_numbers`

## 1. The measurement that motivates it

Whole block as one chunk, execute mode, control build `916cde9e`, `RISC0_PPROF_OUT`:

```
block 962,000   14,040,353,795 cycles   all_valid=1   binds=8006
```

| function (cumulative) | cycles | % of block |
|---|---|---|
| **`secp256k1_ge_set_xo_var`** | **1,415,786,221** | **9.83** |
| `secp256k1_ec_pubkey_parse` | 1,384,619,933 | 9.61 |
| `secp256k1_ecdsa_sig_verify` | 268,033,917 | 1.86 |

Reached via `ec_pubkey_parse` (compressed ECDSA keys) **and** `xonly_pubkey_load`/`lift_x` (taproot),
which is why the cumulative exceeds `pubkey_parse` alone. **`patches/0005` does not touch it** — it
is outside `secp256k1_ecmult`. After #139 it is **~45% of all remaining work**.

Two independent confirmations that the attribution is real rather than an inlining artefact:

- op count: sqrt = exponentiation by (p+1)/4 ≈ 255 squarings + ~10 muls ≈ **265 field ops**;
  ~8,000 pubkeys x 265 x ~670 cyc ≈ **1.42 G predicted vs 1.416 G measured**.
- libsecp `bench_internal`: `field_sqrt` **6.50 us** vs `field_mul` **0.0246 us** = **264x**.

Measured ratio and op count agreeing to one part in 265 says the cost is **algorithmic**, so it
carries from native x86 to the guest's rv32im.

## 2. Why it is nearly free to remove

`secp256k1_ge_set_xo_var` **already** computes `x3 = x^3 + 7`, and **already** normalises `y` and
flips its sign to the requested parity. The sqrt is the only expensive line. So the hint supplies
*a* root and the verification is **one extra squaring plus a compare**.

## 3. Soundness

`y^2 == x3` plus the existing parity fixup accepts exactly what the sqrt would have returned: for an
`x` on the curve there are exactly two roots `±y`, separated by parity. An `x` not on the curve
admits no `y` satisfying the check, so the code falls through to libsecp's own sqrt and fails there
as before. secp256k1 has prime order and no point of order 2, so `y == 0` cannot arise.

⇒ **Advice-and-verify, not substitution.** No group arithmetic is replaced and there is no
equivalence surface to argue — libsecp decides the result. The fidelity posture is strictly better
than #139's. A missing or hostile hint costs only the sqrt already being paid.

## 4. What is written, and what is NOT

| piece | state |
|---|---|
| `patches/0006-lift-x-via-witness-hint.patch` — the libsecp half | written |
| `prover/methods/guest/src/liftx_hint.rs` — table, binary search, `hazync_lift_x_hint` | written |
| `HAZYNC_LIFTX_HINT=1` -> `liftx-hint` feature (`build.rs`, `Cargo.toml`, `main.rs`) | written |
| witness carries the hints (`write_chunk_inputs`) | written |
| host extracts pubkeys and computes Y (`liftx_hints`) | written |
| guest reads the block and calls `install()` | written |
| hit/miss accounting logged from the guest | written |
| **anything compiled or run** | **NOT DONE** |

⚠ `build.rs` previously early-returned on the first flag it saw, so it could not have expressed
"bigint2 AND liftx" — it would have dropped the second silently. It now accumulates features. That
matters because **the interesting arm enables both.**

### The remaining host work, specified

1. Walk each input's spending conditions and collect candidate pubkey X coordinates: P2TR prevout
   `spk[2..34]`; taproot annex-stripped control block internal key; P2WPKH witness item 1; P2PKH
   scriptSig's second push; bare/`P2SH` multisig pushes. **Completeness is an optimisation, not a
   correctness condition** — an unhinted key falls back to the sqrt.
2. For each, compute `y = sqrt(x^3+7)` host-side (cheap natively) and emit `(x, y)` pairs.
3. Ship them in the chunk payload as a `PackedHashes`-shaped field — ~32 B per key, **~256 KB
   against a 7.2 MB witness**. The packed encoder on `feat/aggregate-witness-read-v2` already
   handles fields of this shape.
4. Guest calls `liftx_hint::install(pairs)` before script verification.
5. **Print `liftx_hint::stats()`.** A silently-empty table reinstates the sqrt while every gate still
   passes — a run with 0 hits is a FAILED experiment, not a null result.
   → `gotcha_checks_that_cannot_fail_and_logs_that_cannot_speak`

## 5. The run that settles it

Execute-mode A/B on block 962,000, hinted vs control. **No GPU required.**

```
RISC0_PPROF_OUT=prof.pb HAZYNC_BLOCK=block_962000.json HAZYNC_CHUNKS=1 \
  HAZYNC_PROFILE_EXEC=1 host chunk-profile
```

**The gate is the journal digest: byte-identical to control, `all_valid=1`, or the change is wrong.**
Confirm in the profile that `ge_set_xo_var` has actually collapsed, and that hits ~= pubkey count.

⚠ It moves `METHOD_ID`, so batch it with the other re-baselining changes.
⚠ `vb-stages` profiles the *aggregate's* `validate_block`, not chunk work — use pprof here.
