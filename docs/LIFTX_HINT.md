# hazync#205 — hint the pubkey Y, verify it, and stop computing square roots

**Status: SHIPPED.** Merged in `42417d2` (#208); `patches/0013` is part of the canonical CORE guest
since v0.21.0 (`c12ad67`), applied by `provision-vps.sh` phase 5a with `HAZYNC_LIFTX_HINT=1` exported
beside it. **MEASURED: +6.31%** in execute mode, 6,897 hits / 134 misses (98.1%), journal digest gate
PASS (`docs/RELEASE_NOTES_v0.20.0.md`); a live `seg-serve` run logs `liftx: hits=430` (`9d860d7`).

⛔ **It must be set at RUN time as well as build time**, or chunked proving dies — §6.

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

## 4. What was built

| piece | state |
|---|---|
| `patches/0013-lift-x-via-witness-hint.patch` — the libsecp half | shipped |
| `prover/methods/guest/src/liftx_hint.rs` — table, binary search, `hazync_lift_x_hint` | shipped |
| `HAZYNC_LIFTX_HINT=1` -> `liftx-hint` feature (`build.rs`, `Cargo.toml`, `main.rs`) | shipped |
| witness carries the hints (`write_chunk_inputs`) | shipped |
| host extracts pubkeys and computes Y (`liftx_hints`, `b0c19dc`) | shipped |
| guest reads the block and calls `install()` (`chunk_prove`) | shipped |
| hit/miss accounting logged from the guest | shipped |
| compiled, digest-gated and measured | ✅ `42417d2` |

⚠ `build.rs` previously early-returned on the first flag it saw, so it could not have expressed
"bigint2 AND liftx" — it would have dropped the second silently. It now accumulates features. That
matters because **the interesting arm enables both.**

### The host half

`liftx_hints()` walks each input's witness items and scriptSig, and the scripts inside them, for 33-byte
compressed and 32-byte x-only keys, decompresses them with the `bitcoin` crate, and ships `(x, y)` pairs
sorted by x — the order the guest's binary search wants. Only the even root is stored: `ge_set_xo_var`
flips the sign itself to match the requested parity. **Completeness is an optimisation, not a
correctness condition** — a key not found pays the sqrt, and a key found wrong fails `y^2 == x3` and
falls back too.

⚠ **Read the hit count.** A silently empty table reinstates the sqrt while every gate still passes, so
a run with 0 hits is a failed run, not a null result.

## 5. The run that settled it

Execute-mode A/B on block 962,000, hinted vs control. **No GPU required.**

```
RISC0_PPROF_OUT=prof.pb HAZYNC_LIFTX_HINT=1 HAZYNC_BLOCK=block_962000.json HAZYNC_CHUNKS=1 \
  HAZYNC_PROFILE_EXEC=1 host chunk-profile
```

**The gate is the journal digest: byte-identical to control, `all_valid=1`.** It passed, at +6.31% and
98.1% hits (Status). The change moved `METHOD_ID` and rode the v0.21.0 re-baseline with `patches/0012`.

⚠ `vb-stages` profiles the *aggregate's* `validate_block`, not chunk work — use pprof here.

## 6. ⛔ Read at build time AND at run time

`HAZYNC_LIFTX_HINT` is read twice: by `prover/methods/build.rs`, which enables the guest's `liftx-hint`
feature, and by the host's `write_chunk_inputs` (`prover/host/src/main.rs`), which writes the hint block
into the chunk payload. The canonical guest is built with the feature, so its `chunk_prove` mode always
reads that block. **A host run without `HAZYNC_LIFTX_HINT=1` omits it, the input stream desynchronises,
and the guest dies with `DeserializeUnexpectedEnd`** — which reads as a corrupt fixture
(`docs/history/BENCH_8xL40S_2026-09-08.md`, Setup).

It applies to every command that builds a chunk payload — `prove-chunk`, `prove-seg`, `seg-serve`, and
the chunk profiling and `seg-*` measurement commands. The worker CLI (`coordinator/hazync`) proves
through `prove-range-bridge` and `fold-range`, which do not, so board contributors are unaffected.
`provision-vps.sh` exports it in `.bashrc`; a release binary run from any other shell does not inherit
it.
