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

⚠ `half` needs care: halving mod `p` is `x/2` if even, `(x+p)/2` if odd. Must stay constant-time.
⚠ `cmov` must stay branch-free — it is used in scalar-multiplication ladders.

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

## 6. Status

⛔ **NOT WRITTEN.** This is the design and the cost case. The measurements in §1 are real; everything
in §3's table is derived from them. **~9 cards is a projection until a build passes §5's digest gate.**
