# Helix — one guest, two verification backends, gated by height

**Status: DESIGN SKETCH, 2026-08-28. Nothing here is built or measured.** Written down so the idea
is not lost; it is not a recommendation, and the numbers that would justify it (hazync#139's two
arms) are still being measured.

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
| bigint2 is materially faster on a **real chunk**, in cycles | ⏳ hazync#139 arms measuring now |
| **the cycle win survives proving** (bigint2 is a separate coprocessor circuit) | ⛔ needs one GPU prove |
| the two backends agree on every historical signature | not started — the load-bearing test |
| a cutover height is chosen and justified | not started |
| #190's type-aware packer has landed | ready, unmerged — it is a #139 prerequisite |

⇒ **If the cycle win does not survive proving, Helix is moot.** That measurement comes first.

## Naming

"Helix" — two strands, one backbone. The two backends run against one chain and one image id.
