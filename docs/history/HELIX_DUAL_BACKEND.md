# Helix — one guest, two verification backends, gated by height

> ⛔ **DEVELOPMENT RECORD — landed from an experiment branch `hazync_helix_experiment`, 2026-08-28.**
> Kept because the reasoning is worth having; **read the corrections below before quoting any number.**
>
> - **9.10x wholesale was never measured** — see `9b767b5`; nothing called the wholesale entry point.
> - **the aggregate is not 1,575 s.** Measured 2026-09-02: **405.6 s** on two workers, 473.1 s with
>   the coordinator also working (1.63x, free). Any "impossible at any N" conclusion built on 1,575 s
>   does not survive it.
> - **"7 cards"** measured **10** (Core) and **5** (Ghost) — `BUILDS.md` §1.
> - Its own verdict — Helix is *probably not needed* — still stands; the operator chose the middle
>   path everywhere.



> ## ⚖ STATUS 2026-08-28 (end of day): **PROBABLY NOT NEEDED — read this first**
>
> Helix exists to run the **wholesale** backend for backfill and **Core's own code** at the tip. Both
> arms have now been measured, and the operator has chosen **the middle path everywhere**:
>
> | arm | proving wall | vs stock |
> |---|---|---|
> | middle path (one line swapped) | 55.75 s | **8.00x** |
> | wholesale (whole predicate) | 48.33 s | **9.10x** |
>
> ⇒ **The 15% premium buys at most ONE card** (6 vs 5), and only in the pessimistic aggregate case.
> One card is a cheap price for keeping Core's DER parsing, low-S handling, r/s checks, inversion and
> final `r == x(R) mod n` as literal libsecp code.
>
> ⇒ **If the middle path runs EVERYWHERE, there is no second backend** — and therefore no height gate,
> no committed cutover constant, no doubled audit surface, and none of the silent-divergence risk this
> document spends its longest section warning about.
>
> **Kept, not deleted**, because the reasoning is still the right reasoning if a future backend is
> fast enough to reopen the trade — and because the S1 analysis (why two guests cannot work) is
> load-bearing for any such proposal. → `TOPOLOGY_AND_SETTINGS.md`

**Status: DESIGN SKETCH, 2026-08-28.** Written down so the idea is not lost; it is not a
recommendation.

## The idea

The project has two workloads with **opposite** constraints (`GOALS.md`, "Two operating modes"):

| | **Backfill** (G2) | **Tip-following** (G6) |
|---|---|---|
| the work | **138-229 card-years** | ~29 L40S continuously |
| what it needs | throughput per pound | keeping pace |
| **inputs** | a **closed, enumerable** set — every signature already exists on chain | not yet written |

⇒ Backfill can be differential-tested against libsecp on **every input it will ever see**.
Tip-following cannot. So an acceleration that is indefensible at the tip may be entirely defensible
for history — and history is where ~99% of the compute is.

**Helix: compile BOTH verification backends into ONE guest and choose by block height.**

- below `HELIX_CUTOVER_HEIGHT` → the bigint2 backend (hazync#139, up to 13.78x per ECDSA verify)
- at or above it → Core's and libsecp's literal code, unchanged

## Why one guest rather than two

The obvious version — a fast backfill guest and a faithful tip guest — **does not work**, and the
thing that stops it is deliberate. `prover/methods/guest/src/main.rs`:

```rust
// S1: chain the image-id constraint down — the prev proof must have recursed against the SAME id.
// With the verifier asserting the FINAL self_id == METHOD_ID, this forces every level to METHOD_ID.
assert!(prev.self_id == self_id, "recursion image-id mismatch");
```

Every level of the chain must share one `METHOD_ID`, so two guests cannot fold into one chain proof.
Relaxing that to an allowed-*set* of image ids would weaken S1 — precisely the property that stops
someone splicing in a proof from a different, possibly weaker guest.

✅ **One guest with both backends avoids the question entirely.** The image id is the same whichever
path executes, so S1 is untouched, the verifier is unchanged, and **switching backends never needs a
re-baseline**. It also means the ~8% codegen batch and #139 can ship in a single re-baseline, after
which mode selection is free forever.

⚠ An untaken branch costs essentially nothing at runtime — RISC0 pages in the code that executes —
so the price is a larger ELF, not slower proving.

## ⛔ The constraint that makes it sound: the switch must not be a knob

**If the host chooses the backend, the system's soundness collapses to whatever the weakest enabled
path proves.** Whoever proves a tip block would simply select the cheaper one. A "knob" in the
ordinary sense is exactly the wrong shape.

The selector must be **derived and guest-enforced**:

```rust
// derived from a value the guest already constrains — NOT host-supplied
let use_bigint2 = height < HELIX_CUTOVER_HEIGHT;
```

This is enforceable today because the guest already asserts `w.height == prev.height + 1` against the
chain state, so a prover cannot choose the height. The backend actually used must also be
**committed into the journal**, so a verifier can see which path attested each block rather than
trusting that the right one ran.

✅ **Precedent: height-conditional verification is already how this guest works.**
`script_flags::block_script_flags(height, block_hash)` selects consensus rules by height, because
that is what Bitcoin does at every soft fork. A height-gated verification *backend* is structurally
the same move.

## What it costs — stated plainly

- **The equivalence obligation moves; it does not disappear.** Both paths live in the
  consensus-critical binary. A bug in the bigint2 path reachable below the cutover still produces a
  false proof for historical blocks. ⇒ **Exhaustive differential testing over the historical
  signature set is load-bearing, not nice-to-have.**
- **The audit surface doubles.** Reviewers read two verification paths.
- **The ELF grows** by risc0-crypto's ECDSA and curve code.

⚠ **The risk worth naming precisely.** This is a height-gated **implementation** switch, not a
height-gated **rule** switch. Core's height gates change *what is valid*; this changes only *how the
same rule is checked*. So if the two paths ever disagree, the result is a proof that **verifies
correctly while attesting to something Core would reject**, with the cutover deciding which. That
failure is **silent**. It is the whole reason the differential gate matters, and the reason
`HELIX_CUTOVER_HEIGHT` should be a committed, auditable constant rather than a build flag anyone can
flip.

## Sketch of the dispatch

Three pieces. None are built.

**1. The constant and the decision (guest, Rust).** A committed constant, and a decision derived from
the already-constrained height:

```rust
pub const HELIX_CUTOVER_HEIGHT: u32 = /* TBD — a committed consensus parameter */;

#[inline]
pub fn backend_for(height: u32) -> Backend {
    if height < HELIX_CUTOVER_HEIGHT { Backend::Bigint2 } else { Backend::Core }
}
```

**2. Runtime dispatch in libsecp (C).** Today's patch 0005 is `#ifdef`'d, i.e. compile-time. Helix
needs it to consult a value the guest sets **once per block**, before any input is validated:

```c
if (hazync_use_bigint2()) {
    /* patch 0005's bigint2 path */
} else {
    secp256k1_ecmult(&pr, &pubkeyj, &u2, &u1);   /* stock libsecp */
}
```

⚠ The flag must be set once per block from the derived height and be immutable for that block's
validation. If it could change mid-block a prover could mix backends across inputs — probably
harmless, and unnecessary surface regardless.

**3. Commit it.** The backend (or the cutover constant, plus the height, which is already committed)
goes into `ChunkOut` / the block journal, so "this block was attested by backend X" is a claim the
proof makes rather than an assumption the reader brings. Add a domain-tagged field, per H8.

## What would have to be true before this is worth building

| | status |
|---|---|
| bigint2 is materially faster on a **real chunk**, in cycles | ✅ **9.19x** (2026-08-28) |
| **the cycle win survives proving** | ✅ **8.00x measured on an L40S** — the gate PASSES |
| is the wholesale arm meaningfully faster than the middle path? | ✅ **yes, 15%** (48.3 s vs 55.8 s) — so the trade is real |
| the two backends agree on every historical signature | ⛔ not started — **the load-bearing test** |
| a cutover height is chosen and justified | not started |
| #190's type-aware packer has landed | ready, unmerged — a #139 prerequisite |

## ✅ The gate passes — Helix is justified, not speculative

The measurement this design was conditional on has been taken. On an L40S, block 140,000, one chunk:

| arm | wall (po2 21) | vs stock |
|---|---|---|
| stock libsecp | 446, 446, 446 s | — |
| **middle path** (one line swapped) | 55, 56, 56, 56 s | **8.00x** |
| **wholesale** (whole predicate) | 48, 49, 48 s | **9.10x** |

⇒ **And the 15% gap between the two arms is precisely what the height gate is for.** Had wholesale
been indistinguishable from the middle path, Helix would have been pointless — you would simply keep
Core's logic everywhere. It is not indistinguishable, so there is something to trade: **wholesale for
backfill, where the input set is closed and exhaustively testable; Core's own logic at the tip, where
it is not.**

⚠ **This does not lower the differential-testing bar, it raises it.** The wholesale arm replaces the
entire verification predicate, so "these two agree on every signature in chain history" becomes the
thing the whole design rests on. It is still not started.

## ⛔ Helix does not finish the job — it hands the bottleneck to the aggregate

**This is the part most likely to be missed by someone reading only the chunk numbers.** #139
accelerates ECDSA verification, which lives entirely in the **chunks**. It does not touch the
**aggregate** at all — no signature verification happens there.

So succeeding at Helix *changes which half is the problem*:

| | before #139 | after #139 |
|---|---|---|
| chunk side | 14,926 card-s (**90%**) | 2,426 (**61%**) |
| aggregate | 1,575 s (**10%**) | 1,575 s (**39%**) |

⇒ **A near-tip block goes to ~7 cards, and then stops.** Stacking the wholesale arm (+15%) and the
Tier 0 codegen batch (+2.2%) on top still reads **7 cards** — the chunk side has been optimised past
the point where it decides anything.

### The lever that does move it is already identified and unbuilt

`TEN_MINUTE_BLOCK.md` §7.5, MEASURED: **78.2% of block-validation cycles are deserialising the
witness**, and **#136's `read_slice` fix went to CHUNKS only** — the aggregate still does
`b.write(&w)`.

| aggregate | cards (a) | cards if the aggregate is SERIAL |
|---|---|---|
| 1,575 s (today) | 7 | **impossible at any N** |
| 767 s (read cost cut to 25%) | **6** | impossible |
| 497 s (read cost cut to 0%) | **5** | **24 — viable** |

⇒ **It is worth more than everything on the chunk side combined**, it moves `METHOD_ID` so it rides
the *same* re-baseline as Helix, and it is the difference between a serial aggregate being fatal and
merely expensive.

⚠ The board prices this as "1.06x now, ~1.34x after #139". That is the **block-level** factor and it
badly understates the aggregate-level effect, which is ~3x.

⚠ Resolution (**196 s**) is untouched by that fix and becomes the floor underneath it. It does not
bind at these numbers; it is the third thing to attack, not the second.

⇒ **Sequencing consequence: land the aggregate's witness read in the same re-baseline as Helix.**
Shipping Helix alone buys a fleet of 7; shipping both buys 5-6, and insures against the aggregate
turning out not to distribute.

## Naming

"Helix" — two strands, one backbone. The two backends run against one chain and one image id.
