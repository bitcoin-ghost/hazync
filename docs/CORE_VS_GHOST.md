# Core mode vs Ghost mode — makeup, cost, and the trade

**2026-08-30.** One block (962,000), one L40S, po2 22, 16 chunks. Every Ghost figure is MEASURED on
hardware. **Every Core figure is DERIVED from measured components — arm K has never been run.** §6
says exactly what one run would settle.

## 1. The line between them

Not "did we touch Core's source" — that misclassifies. The line is **who decides the result**.

| class | Core's decision procedure | in Core mode? | examples |
|---|---|---|---|
| **identical** | untouched | ✅ | Tier 0 codegen, witness encoder, join-tree pipelining, #190 packer, `read_slice`, SHA fast path, `TransformD64`, decompression memo |
| **advice-and-verify** | untouched — Core's own arithmetic still decides | ✅ | #205 liftx **hint** (`y² = x³+7` checked with libsecp's `fe_sqr`) |
| **substitution (narrow)** | one primitive replaced at a backend interface libsecp already parameterises; every algorithm above it is untouched | ⚖ **a choice** | `patches/0002` SHA-256 accelerator (already in both), **`patches/0012` field backend** |
| **substitution (broad)** | the *algorithm* is replaced; needs an equivalence argument | ❌ Ghost only | #139 bigint2, G1 liftx **accel**, G3 Schnorr lane, scalar inverse |

⚠ `patches/0002` (SHA-256 → risc0 accelerator) is substitution and is **already shipped in both**, at
**3.4%** of guest compute. So Core mode is *maximal-Core*, not *pure* Core, and that precedent is what
every later substitution has been argued from.

⏰ **2026-08-30: the narrow/broad split is new, and it is the whole decision.** `patches/0012` replaces
libsecp's field *backend* — the thing `field_5x52` and `field_10x26` already are — and leaves wNAF,
GLV, the ECDSA logic and every check exactly as they are. #139 replaces the scalar-multiplication
*strategy*. Both are substitutions; they are not the same size of claim, and §3 shows they are not
remotely the same size of win per unit of concession.

## 2. What each contains

**CORE** — Tier 0 · witness encoder · join-tree pipelining · #190 type-aware packer · #136
`read_slice` for the aggregate · SHA fast path (0009) · `TransformD64` via accelerator (0010) ·
decompression memo · **#205 liftx HINT** · optional bounded-lag framing.

**GHOST** — all of the above, with the hint replaced by the accelerated path, **plus**: #139 bigint2
middle path · G1 liftx via coprocessor · G3 Schnorr lane · scalar inverse via coprocessor.

## 3. The numbers

| | chunk work | straggler | aggregate | **cards @600 s** |
|---|---|---|---|---|
| stock `main` | 14,720 s | 1.054 | 1,131.8 s | **28** |
| **CORE, pure** *(derived)* | **~12,709 s** | 1.054 | 627 s | **~24** |
| **CORE + field backend** *(MEASURED cycles)* | **~3,758 s** | ⛔ unmeasured | 627 s | **~8-10** |
| **GHOST** *(measured)* | **1,683 s** | 1.312 | **627 s** | **5** |

⏰ **The "Core costs 5x the hardware" headline is an artefact of not having the field backend.** With
it the gap is ~1.8x, not ~5x, and with the levers still open on each side (§7) it is ~6–7 vs 5. That
changes the trade this document exists to frame. ⛔ **The Core rows remain derived — arm K and arm C2
have both never been run.**

**Chunk speedup vs stock: Core ~1.16x · Ghost 8.75x.** Ghost needs **7.55x fewer chunk-seconds**.

⚠ Both share the same **627 s aggregate**: every aggregate win this session — `read_slice` (1.46x),
`TransformD64`, the join-tree pipelining — is `identical`-class and belongs to BOTH modes. The
aggregate divides **1.881x on two workers**, measured, with only a **20.2 s** serial floor.

### Where Ghost's 8.75x comes from (each step measured)

| step | chunk total | step | cards |
|---|---|---|---|
| stock | 14,720 s | — | 28 |
| + bigint2 #139 | 3,580 s | **4.112x** | 15 |
| + armed packer #190 | 3,546 s | 1.010x | 11 |
| + G1 sqrt on coprocessor | 2,405 s | 1.474x | 8 |
| + memo + G3 Schnorr | 1,920 s | 1.253x | 7 |
| + scalar inverse | 1,775 s | 1.082x | 7 |
| + SHA fast path | 1,683 s | 1.021x | **5** |

⇒ **bigint2 alone is worth ~13 cards.** Everything after it is worth ~6 more combined. The single
substitution decision dominates; the rest is refinement.

## 4. Pros and cons

### CORE, pure — ~24 cards
✅ *"This is Bitcoin Core's validation logic, proven."* Defensible to anyone without qualification.
✅ No equivalence argument to maintain, review, or re-argue when libsecp changes.
✅ Every lever is plumbing or scheduling; a bug costs performance, never correctness.
✅ #205's hint is **advice-and-verify**: a wrong or hostile hint makes it slower, never wrong.
❌ **~24 cards.** Roughly 5x the hardware for the same block — but see §7: one narrow
substitution takes this to ~9, and the pure position's own floor is ~20.
❌ The zero-fidelity levers are nearly exhausted — Tier 0 is 1.02x, the SHA fast path 1.02x, and the
packer is worth **nothing** here because without bigint2 the block is already balanced (1.054).
⚠ Its one large untaken lever is the liftx **hint**, worth ~1.11x — written (PR #206) but never built.

### GHOST — 5 cards
✅ **8.75x on chunk work, measured end to end**, with byte-identical journal digests to stock.
✅ ~5x less hardware; the difference between a 5-card and a 24-card fleet.
✅ Every substitution is *constrained, not trusted* — the circuit proves the accelerator computed
correctly. Same posture as the SHA accelerator already shipped.
❌ **~78% of executed cycles no longer run Core's own code** (measured on block 962,000; the board's
"~85%" is from block 140,000, which has zero taproot).
❌ #139 does **not** preserve libsecp's wNAF/GLV — the scalar-multiplication *strategy* changes, so
the equivalence surface is smaller than a full module swap but is not nil.
❌ Four separate substitutions to defend, each moving `METHOD_ID`, each needing its own differential
gate.
⚠ Its straggler is **worse** (1.312 vs 1.054): accelerating ECDSA and not the rest makes chunk costs
uneven, which is why #190 is worth 5 cards in Ghost and nothing in Core.

## 5. What was rejected, and why

⛔ **Batch verification via MSM.** Sized at 9.24x over a whole block, but signatures are verified
**inside chunks** (~438 each), where Pippenger's fixed bucket cost does not amortise: **5.72x**, and
**4.3x** after the checks it cannot remove. **One card, 5 → 4**, in exchange for removing libsecp from
per-signature verification and a Fiat-Shamir soundness argument needing outside review. Wrong ratio.
The MSM primitive stays in-tree, tested, off by default. → `MSM_BATCH_VERIFY.md`

## 6. ⛔ What is NOT measured, and the one run that fixes it

**The entire Core column is derived.** It composes measured pieces — Tier 0 at 1.021x, the SHA fast
path at 1.021x, and the liftx hint at ~1.11x from the measured 1.416 G decompression cost — but the
combination has never been run.

⏰ **`ARM=K` is defined in `scripts/gpu-stack-ab.sh` and takes ~1.5 h.** It would settle:
1. whether Core's chunk work really lands near 12,700 s;
2. whether its straggler stays at 1.054 without the packer;
3. whether the liftx **hint** delivers its ~1.11x (PR #206 has never been compiled).

⚠ **This session composed measured parts wrongly four times** — a 7.53x projection that measured
4.48x, a txid lever off by 36x, an "obviously correct" Schnorr constant that cost a card, and an MSM
sized at the wrong scale. **Treat §3's Core row as a hypothesis, not a result.**

## 7. ⏰ The three positions, and each one's floor

Added 2026-08-30, when the field backend made the two-way comparison the wrong shape.

| position | what it concedes | levers still open | **floor** |
|---|---|---|---|
| **Core, pure** | nothing beyond `patches/0002` | liftx hint (#206, never compiled) · worker processes · paging | **~20** |
| **Core + field backend** | libsecp's field *representation* and five primitives | lazy adds ✅ · cheaper `mul_int` ✅ · scalar-inverse hint · liftx hint + memo · paging + `WINDOW_A` re-sweep · worker processes | **~6–7** |
| **Ghost** | the scalar-multiplication strategy, four ways | MSM ⛔ rejected on measured grounds (one card) | **5** *(measured today)* |

⛔ **Every floor above except Ghost's is modelled.** They compose measured components; the composition
has not been run. This document has been wrong that way before — §6.

### Why Core cannot go below ~6, whatever else is done

Both halves of this are measured:

```
Ghost:  double_scalar_mul, one fused coprocessor primitive =  79,971 cy/verify
Core:   ~1,780 field ops composed at 83 cy each            = 147,740 cy/verify
                                                     ratio    1.85x
```

Accelerating every field operation still leaves Core *composing* them under libsecp's wNAF/GLV. Closing
the last 1.85x means replacing the scalar-multiplication strategy — which is precisely what #139 does,
and precisely what Core mode is defined by not doing. **~6 cards is a structural floor, not a backlog.**

### What the field backend has actually passed, as of 2026-08-30

✅ **libsecp256k1's own test suite**, `-DVERIFY`, counts 2 / 8 / 32 — `no problems found`, with a stock
`field_10x26` control passing on the same command line. It found **two real bugs** first:
`fe_get_bounds(0)` must be zero, not the maximum; and its low limb must be **even**, because
`run_field_half` decrements it to build a worst-case odd input.
✅ **A standalone mod-p harness**: 2,880 checks against Python arbitrary precision across `0`, `1`,
`p−2`, `p−1`, **`p`, `p+1`, `p+2`** (the lazy invariant admits these), `2^255`, `2^256−1`,
`2^256−(2^32+977)` and 60 random values — 0 failures.
✅ **Mutation controls on both harnesses.** ⚠ The first attempt at the libsecp one was **void**: the
quoted `#include` resolved to the good copy in `secp/src/`, so no mutant ever reached the compiler and
all three "controls" passed. Each mutant now gets its own tree and is `cmp`-checked against the source
before it counts.

⛔ **Not yet passed: the journal-digest gate.** No guest build, no `METHOD_ID`, no block. The host
reference in `testsupport/field_bigint2_native.c` stands in for the coprocessor and is deliberately
the dumbest possible schoolbook code, so the *glue* is what these tests exercise. **Until arm C2 runs
block 962,000 to a byte-identical digest, the ~9 in §3 is a projection.**

## 8. ⏰ The profile, 2026-08-31 — the gap is the FFI boundary, not the design

`RISC0_PPROF_OUT`, execute mode, block 962,000, same run as §3's cycle count.

| function | cycles | share | |
|---|---|---|---|
| `hazync_fq_mul_limbs` | 1,938 M | 30.98% | **296 cy/call** — the coprocessor op is **83** |
| `hazync_fq_sqr_limbs` | 1,356 M | 21.68% | **208 cy/call** — likewise 83 |
| `memcpy` | 790 M | 12.62% | control: 178 M → **4.4x** |
| `secp256k1_ecmult_strauss_wnaf` | 440 M | 7.04% | point-arithmetic control flow |
| `[PageIn]` | 427 M | 6.83% | control: 441 M — unchanged |
| **`hz_add`** | **197 M** | **3.14%** | **modelled at ~937 M** |

**13.07 M coprocessor calls. At 83 cy the real work is 1,085 M; we spent 4,085 M. Overhead ~3,000 M —
48% of the block.**

### ✅ §3's trap was wrong, and in our favour

§3 predicted canonical adds would cost ~750 M and drop the result from 7 cards to 9. **The lazy +
branching rewrite made adds a non-issue: 197 M, and all four C helpers together are 428 M (6.8%).**
The concern that shaped the whole design was real in principle and small in practice.

### ⛔ What actually cost the block: three copies where zero are needed

`BigInt<N>` is `#[repr(transparent)]` over `[u32; N]`; secp256k1's `fe` is `uint32_t[8]`. They are the
same eight little-endian words. The first wrapper copied into a `[u8; 32]` staging buffer, then
`BigInt::from_le_bytes` copied **again** (a `bytemuck` `copy_from_slice` plus a length assert), and
`store` copied a third time — 13.07 M times each.

Now a raw `ptr::read`/`write` of `[u32; 8]`. ⚠ **Keep it that way.** Anything routed through a byte
slice reintroduces the staging copy, and it fails no test — it just costs a third of the block.

### ✅ MEASURED: the fix was worth 2,191 M cycles — 38% of the block

```
backend, copying wrapper   5,775,098,109   2.381x
backend, zero-copy         3,583,757,161   3.836x
per-call: mul 296 -> 138 cy,  sqr 208 -> 123 cy      (the operation itself is 83)
```

⚠ **The flat profile under-reported this 3.3x.** It attributed only 663 M to `memcpy`; the rest was
inlined into the wrappers and showed up as `hazync_fq_mul_limbs` costing 296 cy against an 83 cy
operation. **Reading a flat profile's `memcpy` line as the cost of copying will understate it** --
the per-call arithmetic (296 vs 83) was the honest signal, not the `memcpy` row.

### What is left, and why it is a harder squeeze

| function | cycles | share | |
|---|---|---|---|
| `hazync_fq_mul_limbs` | 903 M | 23.10% | 138 cy/call vs an 83 cy op |
| `hazync_fq_sqr_limbs` | 799 M | 20.44% | 123 cy/call |
| **`secp256k1_ecmult_strauss_wnaf`** | **441 M** | **11.27%** | **libsecp's own point arithmetic** |
| `[PageIn]` | 287 M | 7.35% | control: 599 M |
| `hz_add` | 197 M | 5.03% | |
| `memcpy` | 191 M | 4.89% | control: 127 M |

The residual ~50 cy/call is `reduce_from_bigint`'s modulus compare plus call overhead -- real, but
nothing like three redundant copies. **The next-largest single item is now `ecmult_strauss_wnaf`:
libsecp's wNAF/GLV control flow, which Core mode cannot touch by definition.** That is 7's structural
floor showing up in a profile.
