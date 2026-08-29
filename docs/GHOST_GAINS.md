# Every remaining gain for Ghost — enumerated, priced, and ranked

**2026-08-29.** Ghost's goal is to be radically fast within what is *safe* — sound, and verifiable.
This enumerates what is left after the measured three-arm A/B, with the evidence for each.

⛔ Labels are load-bearing. **MEASURED** = run on hardware. **DERIVED** = arithmetic over measured
parts. **UNPRICED** = no number, and none should be quoted. → `feedback_only_measured_numbers`

## 0. The measured baseline

Block 962,000, one L40S, po2 22, 16 chunks per arm, all three arms on the SAME box.
`METHOD_ID` C `1d6c3792…` ≠ S/P `db79b63b…`; S and P share an id because #190 is host-only, which is
itself proof the packer changed the partition and not the computation.

| arm | build | total | straggler | ratio | cards @600 s |
|---|---|---|---|---|---|
| C | stock `main` | 14,720 s | 1.054 | — | ~29 |
| S | four-lever stack, bigint2 on | 3,580 s | 2.118 | **4.112x** | ~16 |
| **P** | **+ armed type-aware packer** | **3,546 s** | **1.390** | **4.151x** | **~11** |

✅ The aggregate **distributes** — 1.96x segments, 2.03x with joins, 98% parallel efficiency, and
confirmed end to end on this block. Card counts use that. → `SEGMENT_DISTRIBUTION.md`

## 1. Where the remaining work is

From the execute-mode pprof of the control build, minus what bigint2 removes (**DERIVED**, and it
reproduces the measured post-bigint2 total to 1%):

| term | cycles | share of the 3.14 G |
|---|---|---|
| **pubkey decompression (`ge_set_xo_var`)** | **1.42 G** | **45%** |
| ECDSA verify, accelerated | 0.96 G | 31% |
| paging (`PageIn`/`PageOut`) | 0.49 G | 16% |
| Schnorr verify | 0.28 G | 9% |
| SHA-256 family (already accelerated) | 0.18 G | 6% |
| memcpy / `__bswapsi2` / journal | 0.34 G | 11% |

## 2. G1 — decompress through the accelerator. **The big one.**

⏰ **risc0-crypto ALREADY EXPOSES THIS.** `curve::decompress(x, is_y_odd)` → `ys_from_x` → `sqrt`:

```rust
pub fn sqrt(&self) -> Option<Self> {
    let root = self.pow(&P::MODULUS_PLUS_ONE_DIV_FOUR);
    if (&root * &root).check_is_eq(self) { Some(root) } else { None }
}
```

Same algorithm libsecp uses — but `pow` runs on the **bigint2 coprocessor**, not the guest's software
`field_10x26`. ⇒ **Patch 0007 is a near-verbatim copy of 0005 pointed at `ge_set_xo_var`.** No witness
change, no host change, no hints — unlike #205's route.

**DERIVED:** a sqrt is ~265 field ops (**MEASURED**: libsecp `bench_internal` puts `field_sqrt` at
264x `field_mul`). bigint2 delivered **12.3x** on the ECDSA primitive, so ~1.42 G → ~0.12 G,
**saving ~1.30 G — 41% of everything left.**
⚠ The 12.3x is for a double-scalar-mult, a different op mix. **Not measured for `pow`.**

## 3. G2 — the ECMULT window is now ANTAGONISTIC. Free, one env var.

⛔ **Tier 0 sets `ECMULT_WINDOW_SIZE=21`, measured on block 140,000** — 212 inputs, **zero taproot**,
**stock ecmult** — for **−1.245%**. `guest/build.rs`: *"the pre_g table doubles per step (~16 MB at 19
→ ~64 MB at 21)"*.

**bigint2 then removes 7,015 of that table's ~7,160 callers.** With it on, `secp256k1_ecmult` is
reached only from `schnorrsig/main_impl.h:255` (**145** verifies) and `eckey_impl.h` taproot tweaks.

⇒ **A 64 MB table now serves ~145 uses. The −1.245% is gone; the paging is not.** Paging is
**MEASURED at 0.49 G**, 16% of what remains.
⇒ **Sweep `HAZYNC_ECMULT_WINDOW` with bigint2 ON.** Zero fidelity, zero code, one env var, already a
build knob. ⚠ **Re-measure — never reuse the pre-bigint2 optimum.**
✅ Compounds with G3: accelerate Schnorr too and the table is dead for verification entirely.

## 4. G3 — the Schnorr lane (`patches/0006`)

`secp256k1_schnorrsig_verify` computes `rj = s*G + (-e)*pkj` via
`secp256k1_ecmult(&rj, &pkj, &e, &s)` — the **identical call shape** patch 0005 replaces. Map
`u1←s`, `u2←e` (already negated), `Q←pk`. Post-check is *simpler* than ECDSA's.
**DERIVED:** 0.28 G → ~0.02 G, **~0.26 G (8%)**. Grows directly with taproot adoption, and matters far
more for a genesis-to-tip historic mode than for tip blocks today.
✅ **Second-order win:** with ECDSA and Schnorr both accelerated their costs converge, so the 13.8x
curve divergence that #190 exists to model **shrinks** — the straggler improves for free.

## 5. G4 — `ECMULT_GEN_KB` 22 → 2. Free.

`guest/build.rs`: *"INERT for this workload — 2, 22 and 86 all produce bit-identical guest cycles"*;
it sizes the **signing** table and Hazync only ever verifies. ⇒ Pure guest-image reduction, which is
paging. **UNPRICED**, free, take it in the same re-baselining.

## 6. G5 — give the packer its BYTE dimension too

**MEASURED:** chunk 4 got **3.424x at 0% taproot** — the block's heaviest payload at **26.9 MB**, so
its cost is marshalling, not verification. ⇒ **The packer is blind to two axes, not one.**
§7.1 records the defect: the encoder is per-GROUP, the cost model per-INPUT — **53.40 MB charged
where 1.53 MB is real, a 34.9x gap.** The fix is a second term, `COST_PER_GROUP_BYTE`, not a refit.
**DERIVED:** straggler beyond #190's 1.390, so more cards. Zero fidelity.

## 7. G6 — memoise the decompression. Compounds with G1.

**MEASURED:** 6,913 verifying inputs, **2,160 distinct keys — 3.2x redundancy.** Even accelerated,
69% of decompressions are recomputing a value already known. ~20 guest lines, keyed on `x`, cleared
per run. **Zero fidelity — memoising a pure function is not even advice-and-verify.**

## 8. G7 — the witness encoder's cycle effect

**MEASURED: 2.019x on wire bytes** (7,256,592 → 3,594,208). ⛔ **Cycle effect UNMEASURED.** It is
already in the stack, so arm S/P include whatever it gives. The aggregate is 78% deserialisation at
347 cyc/byte, so this is an aggregate-side lever and the aggregate distributes.

## 9. ⛔ Ruled out: batch verification

Turning n verifications into one multi-scalar multiplication is the standard ZK trick and would be
worth far more than anything above. **risc0-crypto exposes no MSM** — only `double_scalar_mul`,
`add_into`, `double`. Without Pippenger, batching n signatures costs the same n scalar
multiplications, so **there is no win to take today.** Revisit if risc0-crypto adds MSM; it is the
single largest theoretical lever left.

## 10. Ranked, and what it adds up to

| # | gain | saving | class | cost |
|---|---|---|---|---|
| **G1** | decompress via accelerator | **~1.30 G (41%)** | substitution | one patch |
| **G2** | ECMULT window with bigint2 | part of 0.49 G | **free** | one env var |
| **G3** | Schnorr lane | ~0.26 G (8%) | substitution | one patch |
| G6 | memoise decompression | compounds G1 | **free** | ~20 lines |
| G5 | packer byte dimension | straggler | **free** | one term |
| G4 | `ECMULT_GEN_KB` | paging | **free** | one constant |

**DERIVED end state:** 3.14 G → ~1.28 G ⇒ **~11x on chunk work**, ~1,338 chunk-seconds.
With the straggler improving as the curves converge, that is **~6 cards** — the figure the board
originally projected, but this time reached from measured components rather than a projection.

⛔ **Nothing in §10 is measured as a stack.** Each row needs its own arm. The order is G2 first (free,
one env var, no rebuild of anything else), then G1 (largest), then G3.

⏰ **And note what happens then:** at ~1,338 chunk-seconds against a 1,575 s aggregate, **the aggregate
becomes 54% of the block.** Every gain above makes the aggregate matter more. It distributes, so cards
still help — but the next frontier after this list is the aggregate, not the chunks.
