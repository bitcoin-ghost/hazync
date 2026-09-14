# experimental

> **Updated 2026-09-14.** The field-backend reopen condition below was met: the bigint2 coprocessor
> multiply measured **83 cycles** in native form against `sys_bigint`'s ~678 (`docs/FIELD_BIGINT2_BACKEND.md`
> §1), and a field backend shipped in CORE as `patches/0012` from v0.21.0. The code here is the rejected
> `sys_bigint` attempt, kept as the record.

Work that was built, measured, and **not adopted**. Kept because the measurement is worth more than the
code, and because the ideas here are the ones people reach for first.

Nothing in this directory is compiled into the guest, the host, or any release. It is not on any path
that produces a proof. Treat it as a lab notebook with working code attached.

## Why keep rejected work

The alternative is a sentence in a design doc saying "we tried that, it did not work", which nobody
believes strongly enough to skip trying it themselves. A complete implementation with a number attached
is a different kind of answer.

Each subdirectory should say what was measured, what the number was, and what would change the
conclusion.

---

## `field-backend/` — libsecp field arithmetic through the RISC0 bigint precompile

**Measured 1.67x. Rejected 2026-08-19.** Full write-up in "Step 3 measurement" below (formerly
`field-backend/STEP3-RESULT.md`), and in `docs/history/ACCELERATION.md` where the idea originates.
Issue: hazync#129.

EC signature verification is ~95% of proving cost, so the standing question is whether libsecp's
modular multiply can be accelerated without reimplementing the cryptography Hazync exists to prove.

`docs/history/ACCELERATION.md` had already disproved the cheap version — intercepting `secp256k1_fe_mul` while
keeping libsecp's 10x26 representation measured **+10%**, because converting between representations
per operation costs about as much as the multiply it replaces. Its recommendation was a full field
*backend*: hold elements in precompile-native `[u32; 8]` permanently so nothing converts.

That is what this is. A third libsecp field backend alongside `field_5x52` and `field_10x26`, selected
with `USE_HZFE_FIELD`.

**It works and it is correct.** Block 962,000, 8,006 inputs: identical `tip_hash`, all consensus flags
true. 1.4M differential comparisons against the stock backend, mutation-checked eleven ways. libsecp's
EC layer — wNAF, GLV, precomputed tables, ECDSA and Schnorr — runs fine on a backend with no magnitude
system at all.

```
baseline (10x26)     17,394,637,671 cycles   2,172,700 per input
hzfe + sys_bigint    10,388,552,466 cycles   1,297,596 per input
                              1.67x
```

**Why it was rejected.** `sys_bigint` costs about **678 cycles** per 256-bit modular multiply, not the
10-100 instruction-equivalents projected. That is only ~40% below the 1,141-instruction software
multiply it replaces, and `mul`+`sqr` are 94.8% of the field work, so 40% off the dominant term is the
entire result.

At 7x, swapping one field backend with 1.4M differential comparisons behind it is a trade worth
defending. At 1.67x it spends the project's strongest claim — that this runs Bitcoin Core's real code —
for a 40% cycle reduction. The weakening was real: arithmetic Core ships replaced by arithmetic written
here, the magnitude contract the EC layer was written against removed, and a Rust shim plus a direct
`risc0-zkvm-platform` dependency added to the guest.

**What would reopen it.** One number: whether `risc0-bigint2` is materially cheaper than 678 cycles per
modular multiply. `docs/history/ACCELERATION.md` specifies bigint2 and the ~6x precedent came from a k256 experiment
using it, while this used `sys_bigint` — the older 256-bit `OP_MULTIPLY` syscall that
`risc0-zkvm-platform` exposes directly. `sys_bigint2_*` takes a `blob_ptr` and invokes a compiled
program, needing the `risc0-bigint2` crate. Untested here.

Everything else is built: the backend, the differential harness, the libsecp integration and the
throwaway guest-build path. Only that measurement is missing.

### Step 3 measurement: 1.67x with `sys_bigint` (formerly `field-backend/STEP3-RESULT.md`)

Execute mode, same machine, same day, on the same block as above: baseline **838 s**, hzfe + `sys_bigint`
**334 s** wall. Identical `tip_hash 4403cf83deb7d52f04ed0c2a1d70aa9d48d435ef2a1201000000000000000000`.

**The likely reason, stated at the time as a hypothesis:** this used the wrong precompile.
`docs/history/ACCELERATION.md` specifies the RISC0 **bigint2** precompile throughout, and the ~7x
precedent came from the removed k256 experiment, which used `risc0-bigint2`. This implementation used
`sys_bigint`, the older 256-bit `OP_MULTIPLY` syscall, because that is what `risc0-zkvm-platform`
exposes directly:

| | |
|---|---|
| `sys_bigint(result, op, x, y, modulus)` | one 256-bit modmul, direct syscall, what was used here |
| `sys_bigint2_*(blob_ptr, a1..a6)` | invokes a compiled bigint2 *program*; needs the `risc0-bigint2` crate for blobs and wrappers |

`risc0-bigint2` was not vendored in the local registry at the time. *(The hypothesis held — see the note
at the top.)*

Settled then: the backend is **correct** end to end on real consensus data; 40% of the cycles genuinely
disappear and the EC layer runs without a magnitude system; `sys_bigint` alone does not justify a
re-baseline (a near-tip block from 9.72 GPU-hours to about 5.8). Not settled then: whether bigint2
reaches 7x, and whether that 7x is even comparable, since k256 substituted the whole EC implementation.

**Against the stop condition:** #129 said under ~4x, reconsider rather than proceed to Step 4. 1.67x is
under 4x, so Step 4 was not started.

## `field-op-profile/` — what libsecp's field operations cost, and how often they run

Built to answer whether the backend rework could pay for itself. **Reusable for any question of this
shape**, and the more durable half of this directory.

Counts every field operation one ECDSA or Schnorr verification performs, on the 10x26 backend
`riscv32im` actually selects, and measures what each costs in rv32im instructions.

The results that shaped everything above:

| finding | consequence |
|---|---|
| `mul`+`sqr` are 94.8% of field-op instructions | the only thing worth accelerating |
| a mul is 1,141 instructions, an add is 57 | 1.02:1 by count is 20:1 by cost |
| `scalar_mul` is 5 calls/verify vs 932 field muls | scalar side is 0.4% of the work — skipped |
| `inv_var` is <=1 call/verify, `inv` is 0 | delegate to libsecp's safegcd, do not reimplement |
| `is_square_var`, `sqrt` are 0 calls | not in the counted region — the harness parses no pubkey (it verifies against a key made before the counters are zeroed). In the guest, pubkey decompression (`ge_set_xo_var`, a sqrt) was **45%** of the post-bigint2 cycles on block 962,000 (`docs/history/GHOST_GAINS.md` §1) |

Two traps it has already caught, both preserved as guards:

- On an x86-64 host libsecp selects `field_5x52`, so counters injected into `field_10x26_impl.h` never
  run and the harness reports a table of zeros that looks like a clean measurement.
  `-DUSE_FORCE_WIDEMUL_INT64=1` selects the backend the guest uses, and it now aborts if every counter
  is zero.
- Piping `cc` through `head` truncated a compile error and left a stale binary, so a failed build
  presented as a hang.
