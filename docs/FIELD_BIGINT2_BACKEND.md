# A coprocessor field backend for libsecp — design

**Goal:** get Core mode from ~24 cards to ~9, **without conceding a single algorithm.** libsecp keeps
its wNAF, its GLV, its ECDSA logic and every check; only the field *backend* changes — which libsecp
already parameterises (`field_5x52`, `field_10x26`, asm variants). Adding `field_bigint2` uses its own
extension point.

⚠ **This is the same fidelity posture as `patches/0002`** (SHA-256 → risc0 accelerator), already
shipped in **both** models at 3.4% of guest compute. It is arguably a *smaller* concession than #139,
which replaces the scalar-multiplication **strategy**.

## 1. The measurements this rests on

MEASURED in-guest, libsecp's own functions, same run (`host field-bench`, mode 15):

| | cycles |
|---|---|
| software `fe_mul` (10x26) | **1,167** |
| software `fe_sqr` (10x26) | **737** |
| coprocessor multiply, **native form** | **83** |
| coprocessor multiply, **+ 10x26 conversion** | **854** |

⇒ **Conversion costs 771 cycles — 9.3x the operation itself.**

## 2. ⛔ Why the obvious cheap version is dead

Patching `fe_mul` to convert → call → convert back, keeping the 10x26 representation, gives **1.37x**
on multiply and is **SLOWER than software on squaring** (854 vs 737). Block-level: **1.10x, 26 cards**.
**Do not build this.** It was measured, not assumed.

## 3. The design that works, and the trap in it

**The representation must BE the coprocessor's** — 8 canonical 32-bit limbs, always `< p`, always
normalised — so conversion never happens.

⛔ **The trap:** `secp256k1_fe_impl_add` in 10x26 is **ten bare limb additions with no reduction** —
magnitude grows instead, and it costs ~10 cycles. A canonical representation cannot do that; every add
must reduce. **Adds get more expensive, and there are more of them than multiplies.**

⏰ **2026-08-30: the trap is smaller than this section assumed — see §3b.** Adds do not have to reduce,
and the reduction was never the expensive part anyway.

| design | mul | add | block | cards |
|---|---|---|---|---|
| (a) canonical, adds via coprocessor | 83 | 83 | 3.18x | 10 |
| **(b) canonical, adds in SOFTWARE** | **83** | **~50** | **3.67x** | **9** |
| (c) lazy limbs, convert per mul | 854 | 10 | 1.13x | 25 |
| reference: all software | 1,167 | 10 | 1.00x | 24 |

⇒ **(b).** Multiply, square, inverse and sqrt go to the coprocessor; add, sub, negate and half stay in
C as add-with-carry plus a conditional subtract of `p`. No coprocessor call and no conversion for the
cheap operations.

⚠ **My first estimate said 7 cards. It counted only mul/sqr getting cheaper and ignored that adds get
worse.** ~1.5 adds per multiply at +40 cycles each is ~750 M cycles back. **9, not 7.**

## 3b. ⏰ Two corrections to §3, found 2026-08-30

The first implementation took §3 literally: canonical after every operation, and constant-time
throughout. Both were wrong, and the second was the expensive one.

**(i) The reduction in `fe_add` is redundant.** `Fq::reduce_from_bigint` accepts any 256-bit value,
and because `p` has its MSB set it takes the `msb_set()` fast path — a *single* conditional subtract.
The coprocessor therefore reduces on load whether or not `fe_add` did. So elements are now **lazy**:
any value in `[0, 2^256)` congruent to the element. Adds fold the `2^256` carry with
`2^256 = 2^32 + 977` and leave it there; only `normalize` canonicalises. This is what libsecp's own
backends do, for the same reason.

**(ii) ⛔ Constant-time buys nothing inside a zkVM, and it cost more than the arithmetic.** The
branchless conditional subtract was 8 subtract-with-borrow plus 8 three-instruction selects — **24 of
the 72 instructions in `fe_add` were the select alone.** There is no timing side channel in a proven
execution trace, and the guest only ever *verifies* public data; it holds no secret scalar. libsecp's
own `_var` functions already branch throughout the verification path this backend serves. `fe_cmov`
and `fe_storage_cmov` stay branchless anyway — they are table-lookup primitives and cheap either way.

MEASURED, static rv32im instruction counts (`riscv32-unknown-elf-gcc -O3 -march=rv32im`, per function):

| function | canonical + constant-time | lazy + branching | |
|---|---|---|---|
| `fe_add` | 155 | **74** | **2.09x** |
| `negate` | 61 | **28** | **2.18x** |
| `normalize` | 1 | 21 | now real work — the cost of laziness |
| `mul_int` | 50 *(a loop)* | 56 *(straight line)* | see below |

⚠ **`mul_int`'s static count is misleading and I nearly quoted it as a regression.** The old one was a
double-and-add **loop**; libsecp calls it with 2, 3, 4, 5 and `SECP256K1_B`, so it ran 3–5 inlined
`addmod`s per call. The new one is a single 8-limb multiply-accumulate pass with an overflow fold —
one straight line, executed once. Static size says 0.89x; dynamic work is several times better.

⛔ **These are static instruction counts, not cycles, and not a block-level number.** They justify the
change; they do not size it. The block figure comes from the run in §5.

## 4. What has to be written

27 functions (`grep -oE 'secp256k1_fe_impl_[a-z_0-9]+' field.h`). Since the representation is always
canonical and magnitude is always 1, a good many collapse to almost nothing:

| group | functions | notes |
|---|---|---|
| **no-ops** | `normalize`, `normalize_weak`, `normalize_var`, `get_bounds` | already canonical |
| **trivial C** | `set_int`, `clear`, `is_zero`, `is_odd`, `cmp_var`, `cmov`, `to_storage`, `from_storage`, `normalizes_to_zero{,_var}` | limb-wise |
| **software mod-p** | `add`, `add_int`, `negate_unchecked`, `mul_int_unchecked`, `half` | add-with-carry + conditional subtract |
| **byte I/O** | `set_b32_mod`, `set_b32_limit`, `get_b32` | endianness only; the representation is already canonical |
| **coprocessor** | `mul`, `sqr`, `inv`, `inv_var`, `is_square_var` | via `Fq` |

⚠ `half` needs care: halving mod `p` is `x/2` if even, `(x+p)/2` if odd. Both stay correct for a lazy
input, since `2·((x+p)/2) = x+p = x (mod p)` and `x+p < 2^257`.
⚠ `cmov` stays branch-free — it is a table-lookup primitive and cheap either way.

⛔ **"27 functions" was wrong: there are 30.** The three the count missed are not optional, and the
link failed without them — `fe_storage_cmov`, `fe_to_signed30`, `fe_from_signed30`. The last two
convert to and from `modinv32`'s nine signed 30-bit limbs; neither is on the hot path, because
`fe_inv` and `fe_inv_var` both go to the coprocessor, but `modinv32_impl.h` is still compiled in and
libsecp's tests call them directly. **`grep`ping `field.h` for `secp256k1_fe_impl_*` does not
enumerate a backend's obligations — the compiler does.**

## 5. How it gets validated

⛔ **Compiling is not evidence.** The gates, in order:
1. **libsecp's own test suite** against the new backend. It exists precisely to validate a backend and
   is far more thorough than anything written for this.
2. **`METHOD_ID` moves** — expected; the guest changes.
3. ⏰ **THE gate: the chunk journal digest must be BYTE-IDENTICAL to the control**, `all_valid=1`, on
   block 962,000. That is what proves no consensus output moved. Every accepted lever this session
   passed it; `4fb3e3c5…` is the current value.
4. A negative control: corrupt one signature and confirm the block is **rejected**.

⚠ **A backend that passes 1 and 2 and fails 3 is a silent consensus break.** This session produced six
levers that compiled green and did nothing; a field backend that compiles green and is *wrong* is the
same failure with far worse consequences.

## 5b. ⏰ What has actually passed, 2026-08-30

Gates 0 and 1 both run on a workstation with no GPU and no guest build: `scripts/field-backend-tests.sh`.

| gate | result |
|---|---|
| **0. mod-p harness vs arbitrary precision** | ✅ **2,992 checks, 0 failures** |
| **1. libsecp256k1's own suite, `-DVERIFY`, counts 2 / 8 / 32** | ✅ **`no problems found`**, with a stock `field_10x26` control passing on the same command line |
| **2. mutation controls** | ✅ every mutant caught by at least one gate |
| **3. journal digest on block 962,000** | ✅ **PASS** — `4fb3e3c5…4656d`, byte-identical to control AND to the recorded value, `all_valid=1`, `binds=8006` |
| 4. corrupt-signature negative control | ⛔ **NOT RUN** |

⏰ **Gate 3 needed no GPU.** `RISC0_PPROF_ENABLE_INLINE_FUNCTIONS=1 HAZYNC_CHUNKS=1
HAZYNC_PROFILE_EXEC=1 host chunk-profile` executes the block on CPU in ~4 min (backend) / ~9 min
(control) on an 8 GB laptop, peak RSS 526 MB, and prints the digest and the cycle count. **A GPU is
only needed for wall-clock → cards.** ⚠ `chunk-profile` executes the block TWICE (count-packed and
cost-packed), so budget double.

### Gate 1 found two real bugs

Both in `fe_get_bounds`, and neither would have been found by reasoning about the backend in
isolation: **magnitude 0 means the bound is zero**, not the maximum; and **the low limb must be even**,
because `run_field_half` decrements it to build a worst-case odd input. `fe_get_bounds` is called from
`tests.c` and nowhere else, so both fixes are free.

### ⛔ Gate 1 is necessary and NOT sufficient

Two deliberately broken backends **pass libsecp's full suite at count 32** and are caught only by
gate 0:

- `hz_neg` skipping `hz_canon` — wrong for every input `>= p`
- `fe_to_signed30` skipping `hz_canon` — hands `modinv32` a value outside its contract

The reason is structural: **the lazy invariant creates states libsecp cannot express.** An element
`>= p` does not exist under `field_5x52` or `field_10x26`, so no test generator in `tests.c` ever
produces one. Any future lazy backend inherits this gap. Keep both gates.

### ⚠ The first mutation run was void

Three "controls" passed and I nearly recorded that as coverage. The quoted `#include` in
`field_impl.h` resolves relative to the **including file's** directory first, so it read the good copy
already sitting in `secp/src/` and no mutant ever reached the compiler. Each mutant now gets its own
tree and is `cmp`-checked against the source before its result counts. → `gotcha_checks_that_cannot_fail`

## 6. Status

⏰ **WRITTEN, and validated as far as a workstation can take it.** `patches/0012` plus
`field_bigint2{,_impl}.h`, `field_bigint2.rs`, and `testsupport/`. Gates 0-2 pass.

⏰ **2026-08-31 — MEASURED: 3.836x** (13,748,003,793 → 3,583,757,161 cycles), past the 3.67x
projected. Cards land at **~8-10** depending on the straggler, which is unmeasured for this arm and is
now the only figure here that needs a GPU.

✅ **§3's central worry did not materialise.** Canonical adds were projected to cost ~750 M and to be
the reason for 9 cards rather than 7. The lazy + branching rewrite put `hz_add` at **197 M**, and all
four C helpers together at **428 M (6.8%)**.

⛔ **What actually cost the block was the FFI wrapper: three redundant 32-byte copies per call, worth
2,191 M cycles — 38% of the block.** A first measurement of 2.381x was entirely this. Per-call cost
fell 296→138 cy (mul) and 208→123 (sqr) against an 83 cy operation. ⚠ The flat profile attributed only
663 M to `memcpy` and under-reported the true cost 3.3x, because the rest was inlined into the
wrappers. → `CORE_VS_GHOST.md` §8

⛔ **The earlier block number was a projection.** No guest build, no `METHOD_ID`, no digest. The host
reference stands in for the coprocessor, so what is proven is the *glue* — representation, lazy
invariant, libsecp's contracts — not the coprocessor and not the block. **~9 cards holds only if arm
C2 reproduces the control's journal digest byte for byte on block 962,000.**

⚠ **Arm C2 would have measured nothing until today.** `patches/0012` was applied inside the
`want_bigint2 = 1` branch of `scripts/gpu-stack-ab.sh`, and arm C2 passes `0` — Core mode has no #139.
It exported the macro, never applied the patch, and would have built stock libsecp while reporting a
moved `METHOD_ID`. That is the seventh silent no-op of this exact shape, and the first inside the code
written to catch them. Fixed, with a two-sided binary assertion on `hazync_fq_mul_limbs`.

## 7. ⛔ Two levers investigated and REJECTED, 2026-08-31

Both were sized optimistically from the profile and both died on inspection of what they'd replace.
Neither was built.

### `fe_sqrt` → the coprocessor: 1.85x WORSE

`secp256k1_fe_sqrt` is a hand-tuned addition chain, ~255 squarings + ~15 multiplies = **~270 ops**.
The obvious move is to route it to `hazync_fq_sqrt_limbs`, which this backend already provides and
which already passed libsecp's suite. But risc0-crypto's `Fq::sqrt()` is `pow((p+1)/4)` by plain
**square-and-multiply with no windowing**, and `(p+1)/4` is 254 bits with **247 set**:

```
risc0 Fq::sqrt()   253 squarings + 246 muls  =  499 ops
libsecp fe_sqrt    ~255 squarings + ~15 muls =  ~270 ops        -> 1.85x worse
```

⛔ **There is no hardware sqrt.** `sqrt` and `inverse` in risc0-crypto are software exponentiation
over the *same* coprocessor multiplies libsecp is already issuing. My ~224 M estimate assumed a
one-call primitive that does not exist.

⚠ **This makes `secp256k1_fe_impl_is_square_var` a real (if currently free) defect of this backend**:
it routes to that 499-op path where stock computes a **Jacobi symbol** via `modinv32`. It is absent
from the profile, so it costs nothing measurable today, but it is strictly worse than the code it
replaced.

### Canonical representation to skip `reduce_from_bigint`: incompatible with libsecp's `fe`

`load` pays a modulus comparison **19.6 M times** per block (two per multiply, one per square), while
the C producers that can create a non-canonical value run only ~6 M times. Reducing at the *producer*
would let `load` use `from_bigint_unchecked` with no comparison at all -- worth an estimated 5-8%.

⛔ **It fails libsecp's own test suite, and for a reason no patch fixes.** `fe_cmov_test` constructs
an **all-ones bit pattern** (`2^256-1`) to check that `fe_cmov` copies every bit. That is legal: a
libsecp `fe` is by design a *magnitude-carrying, possibly-unnormalised* type, and both `field_5x52`
and `field_10x26` can represent values ≥ p -- that is what magnitude means. **A backend that requires
canonical-at-all-times is fighting libsecp's design, not implementing it.**

⚠ The escape hatch does not work either. `UnverifiedFp::from_bigint` is a free `const` constructor
with no check, and `sys_mul` takes the modulus, so lazy limbs could in principle go straight to the
coprocessor. But feeding a non-canonical value to the bigint2 precompile is **outside what
risc0-crypto's own types permit** -- `Fp`'s invariant, and the `assert_canonical!` in `check()`, exist
precisely to stop that. Trading a documented reduction path for an unvalidated one, to save ~10-15
cycles a call, is the wrong trade.

✅ `reduce_from_bigint` stays. For secp256k1's `p` it takes the `msb_set()` single-subtract fast path,
which is the supported way to accept `[0, 2^256)`, and the comparison usually resolves on the first
limb.

### What this leaves

The residual ~55 cy/call beyond the 83 cy operation is mostly the C↔Rust boundary itself: 16 limb
loads and 8 stores per multiply, plus call overhead. **Removing that means doing the point arithmetic
in Rust rather than C -- which is #139, i.e. Ghost.** Core + this backend is close to its practical
ceiling at 3.836x; what is left that is genuinely Core-legal is configuration: `ECMULT_GEN_KB`,
an `ECMULT_WINDOW` re-sweep, and worker processes.
