# Two models: Core and Ghost

**Proposed 2026-08-29.** Build two provers, push each to its own limit, benchmark them head to head,
and take the fidelity decision *once*, against numbers, at the end.

- **Core** — Bitcoin Core decides every consensus question. Defensible as *"this is Core's validation,
  proven"*.
- **Ghost** — free to substitute implementations where the equivalence argument is acceptable.

This replaces the current shape, in which every lever is argued individually and the fidelity question
is re-litigated each time. → `TEN_MINUTE_BLOCK.md` §2

## 1. The classification is THREE-WAY, not two

Splitting on *"does it touch Core's source"* puts the best Core-model lever in the wrong bucket.
The line that matters is **who decides the result**.

| class | Core's decision procedure | equivalence argument needed? | examples |
|---|---|---|---|
| **identical** | untouched | none | Tier 0 codegen, packing/encoder, join-tree scheduling, po2 and worker tuning, `ECMULT_WINDOW_SIZE`, wire format, bounded-lag framing, #190 packer |
| **advice-and-verify** | **untouched — Core's own arithmetic still decides** | **none** | **#205 liftx hint** |
| **substitution** | replaced | **yes** | #139 bigint2 (middle or wholesale), a Schnorr lane, the SHA-256 accelerator (`patches/0002`) |

**Advice-and-verify is the interesting class and it is nearly unexplored.** #205 edits
`group_impl.h`, so a source-touched test would exile it to Ghost — but it substitutes nothing: the
guest supplies `y`, and libsecp's own `fe_sqr`/`fe_equal` check `y^2 == x^3 + 7` and return the
verdict. A wrong or hostile hint can only make it slower. **It belongs in Core.**

⇒ The general question this opens: **which other expensive derivations can be hinted and checked?**
Anything cheaper to verify than to compute is a candidate. Nobody has enumerated them.

## 2. What each model contains

**Core** — identical + advice-and-verify:
Tier 0 · witness-read encoder · join-tree pipelining · #190 type-aware packer · worker/po2 tuning ·
**#205 liftx** · bounded-lag framing (§1.1).

**Ghost** — all of the above, plus substitution:
#139 bigint2 (**middle or wholesale**) · a Schnorr lane (`patches/0006` shape, ~7% on block 962,000) ·
any further precompile.

⚠ `patches/0002` (SHA-256) is already substitution, shipped, at **3.4%** of guest compute. So the
"Core" model is *maximal-Core*, not *pure* Core, and that precedent is the one every later
substitution has been argued from.

## 3. What "% of Core" means — it is the share of WORK, not of source

The fidelity column counts **the share of executed cycles no longer running Core's own code**:

| lever | displaced |
|---|---|
| SHA-256 accelerator (shipped) | 3.4% |
| #139 middle path | ~85% (board) · **~78% measured on block 962,000** |
| #139 wholesale | ~95% |

⛔ **"Middle path" names the SURFACE, not the concession.** It replaces one line — keeping DER
parsing, low-S normalisation, `scalar_inverse_var`, both `scalar_mul`s, the infinity check and the
final `r == x(pr) mod n` — but that line is where ~85% of the cycles live. It also does **not** keep
libsecp's wNAF/GLV. Do not read "middle" as "small".

✅ **Measured refinement (2026-08-29):** on block 962,000, control 14.04 G → post-#139 3.14 G, so
**10.90 G is displaced = 77.6%**, not 85%. The 85% derives from block 140,000, which has **zero
taproot** and where more of the field math sits inside `ecmult`. On a tip block part of it is the
pubkey-decompression sqrt, which #139 does not touch. **The middle path's real concession on a modern
block is smaller than the board states.**

## 4. Ballpark — INDICATIVE, and the caveats dominate

Measured inputs, box A: chunk card-seconds **14,667** (projected from 14 of 16 measured; the board
records 14,926), straggler **1.058**, aggregate **1,575 s** (board, measured).
Model: `N = (chunk_card_seconds x straggler / R + aggregate) / 600`, scenario (a).
✅ It reproduces the board's own "32 cards → 9.0 min" exactly, so it is the same arithmetic.

| model | R | cards | displaced |
|---|---|---|---|
| **Core** (Tier 0 + liftx + packing/scheduling) | ~1.13x | **~26** | 3.4% |
| Ghost, middle | 4.48x *(execute)* | ~9 | ~78% |
| Ghost, wholesale | ~5.9x | ~7 | ~95% |
| Ghost, middle + liftx | ~8.1x | ~6 | ~78% |
| **Ghost, wholesale + liftx** | ~14.8x | **~5** | ~95% |

⛔ **NOT MEASURED, and each is load-bearing:** the bigint2 *proving* ratio (execute cycles are not
proving cost — the running A/B settles this); the liftx saving (unbuilt); and above all **whether the
aggregate distributes at all.** If it does not, the aggregate alone is 1,575 s = 26 min and
**no model reaches 600 s at ANY card count.** That has never been exercised.
→ `feedback_only_measured_numbers`

⚠ **liftx's headline is conditional on #139.** It removes 1.42 G: ~45% of the 3.14 G that remains
*after* bigint2, but only ~10% of the 14.04 G before it. In the Core model it is worth **~1.11x, not
8x** — so the board's existing conclusion, that the zero-fidelity levers do not reach the bar,
**survives** this finding rather than being overturned by it.

## 5. Two findings that should shape the Ghost track

**Wholesale buys about one card.** 5.25x → 6.95x is 1.32x, but it applies to a *shrinking* term:
after liftx the split is chunks 1,049 s against aggregate 1,575 s. **The aggregate is then 60% of what
is left.** This agrees with the standing decision of 2026-08-28 (middle, not wholesale) but for a
sharper reason than fidelity taste. ⇒ If 85% is already accepted, the extra 10 points are cheap — just
do not expect them to move the fleet.

⏰ **The highest-value Ghost-only lever is the AGGREGATE, not more chunk substitution.** It never
received #136's `read_slice` fix, 78% of its guest run is deserialisation at 347 cyc/byte, and whether
it distributes is unexercised. That is where Ghost's freedom is actually worth something.

## 6. Running the benchmark

**Fix the decision rule BEFORE the numbers land.** *"How many cards is 78% of Core worth?"* is
answerable honestly now and rationalisable afterwards. And the board has never asked the right
question: the target needs **1.95x**; the smallest #139 variant delivers **5.25x**. ⇒ **Nobody has
asked what the SMALLEST concession that clears the bar looks like.** A Ghost model tuned to *just*
clear 600 s may spend far less of Core than either existing variant.

Mechanically this is a third arm on `scripts/gpu-stack-ab.sh` — same block, same box, same partitions,
so no cross-box variance. Each arm asserts its own `METHOD_ID` and they must differ, or an arm did not
rebuild and every number is void.
