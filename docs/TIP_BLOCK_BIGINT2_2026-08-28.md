# bigint2 on a TIP block — the first measurement, and it halves the headline

Block 962,000, 8,006 inputs, `HAZYNC_CHUNKS=16`, both partitions, execute mode, no GPU.
Three arms, same block, same partitions, same machine, in one sitting.

| arm | `METHOD_ID` | total execute cycles | vs control |
|---|---|---|---|
| control (`main`) | `916cde9e…` | 28,105,138,490 | — |
| Tier 0 only | `70fc6484…` | 27,515,818,284 | **−2.10%** |
| Tier 0 + bigint2 | `3aa6a082…` | 6,276,323,320 | **4.478x** |

⇒ **bigint2 alone = 4.384x** (against the Tier 0 arm).

## ✅ Tier 0's additivity holds on a different block

Predicted from `TIER0_RESULTS_2026-08-26`: `-O3` 0.264% + rust LTO/CGU 0.697% + window 21 1.245%
= **2.206%**. Measured here: **2.10%**.

That prediction was fitted on block **140,000** (212 inputs, 2011, ~100% ECDSA) and holds to within
0.1 percentage points on block **962,000** (8,006 inputs, tip-era). The three axes really are
independent and really are additive.

## ⛔ bigint2's tip figure is roughly HALF its ceiling

| block | character | execute-mode ratio |
|---|---|---|
| 140,000 | 2011, ~100% ECDSA | **9.19x** |
| **962,000** | **tip-era, taproot present** | **4.384x** |

⇒ tip / ceiling = **0.477**.

This is exactly the effect the project anticipated but had never quantified: **risc0-crypto has no
BIP340**, so Schnorr/taproot inputs get none of the win. Block 962,000 predicts **7,015 EC verifies
across 8,006 inputs — 0.88 per input**, against block 140,000's ~1.0 of a much more ECDSA-dense mix.

## ⛔⛔ What this does to the six-card arithmetic

**The stack table's "+ bigint2 middle path: 32 -> 8 cards" step uses 8.00x, and that 8.00x is a
block-140,000 GPU proving figure** (`BIGINT2_MIDDLE_PATH.md`: *"55, 56, 56, 56 s (21 seg) … 8.00x"*,
*"journal digest identical to stock on all 212 signatures of block 140,000"*).

On that same block, execute 9.19x realised **8.00x** at proving time -- proving captured ~87% of the
execute ratio, because the coprocessor itself takes ~13%.

⇒ If that relationship carries to tip, a tip-block proving win lands near **3.8-4.0x, not 8.00x**,
and the 32 -> 8 card step becomes roughly **32 -> 15**.

⚠ **INFERRED, NOT MEASURED, and do not promote it.** Two reasons to be careful:

- **Execute-mode cycles are NOT proving cost.** bigint2 is a separate coprocessor circuit and
  linearity must not be assumed -- the 87% capture rate is one data point on one block.
- The 4.384x IS measured, on the real tip block, with an identical journal digest (below). It is the
  *proving* consequence that is inferred.

⇒ **The six-card conclusion rests on a figure measured on the most favourable block in the set.**
A GPU prove of one tip-block chunk with bigint2 on would settle it, and it is the single highest-value
GPU minute available. Until then, treat 6 cards as resting on an untested extrapolation.

⇒ This also raises the stakes on **hazync#190** (type-aware packer), already a prerequisite: if
Schnorr inputs get no bigint2 win, packing chunks by EC-op *type* matters more, not less.

## ✅ The correctness gate it passed on the way

All **32 journal digests byte-identical to control**, `all_valid=1` on all 32, with three distinct
`METHOD_ID`s proving all three arms genuinely rebuilt. `hazync_ecmult_verify` was confirmed present
in the guest ELF (with a control string, so the check itself could fail).

⇒ **Substituting the ECDSA group arithmetic changes no committed output on 8,006 tip-era inputs.**
That is a far broader differential test than the 212 signatures of block 140,000.
