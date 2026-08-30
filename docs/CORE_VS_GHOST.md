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
| **substitution** | replaced; needs an equivalence argument | ❌ Ghost only | #139 bigint2, G1 liftx **accel**, G3 Schnorr lane, scalar inverse |

⚠ `patches/0002` (SHA-256 → risc0 accelerator) is substitution and is **already shipped in both**, at
**3.4%** of guest compute. So Core mode is *maximal-Core*, not *pure* Core, and that precedent is what
every later substitution has been argued from.

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
| **CORE** *(derived)* | **~12,709 s** | 1.054 | 627 s | **~24** |
| **GHOST** *(measured)* | **1,683 s** | 1.312 | **627 s** | **5** |

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

### CORE — ~24 cards
✅ *"This is Bitcoin Core's validation logic, proven."* Defensible to anyone without qualification.
✅ No equivalence argument to maintain, review, or re-argue when libsecp changes.
✅ Every lever is plumbing or scheduling; a bug costs performance, never correctness.
✅ #205's hint is **advice-and-verify**: a wrong or hostile hint makes it slower, never wrong.
❌ **~24 cards.** Roughly 5x the hardware for the same block.
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
