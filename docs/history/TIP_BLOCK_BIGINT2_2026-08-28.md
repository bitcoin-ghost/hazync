# bigint2 on block 962,000 — MEASURED, and it lands ~40% below the projection

Block 962,000, 8,006 inputs, `HAZYNC_CHUNKS=16`, both partitions, execute mode, no GPU.
Three arms, same block, same partitions, same machine, one sitting.

| arm | `METHOD_ID` | execute cycles (2 partitions) | per partition | vs control |
|---|---|---|---|---|
| control (`main`) | `916cde9e…` | 28,105,138,490 | **14.05 G** | — |
| Tier 0 only | `70fc6484…` | 27,515,818,284 | 13.76 G | **−2.10%** |
| Tier 0 + bigint2 | `3aa6a082…` | 6,276,323,320 | **3.14 G** | **4.48x** |

## ✅ The control cross-checks a prior session to 0.07%

An earlier session recorded block 962,000's chunk cost as **14.06 G cycles**. This run measures
**14.05 G** independently. Same block, same work — which is what makes the divergence below
attributable to the bigint2 arm rather than to a different baseline.

## ⛔ The projection said 7.53x; measurement says 4.48x

That session projected `14.06 G → 1.87 G (7.53x)`, and **the fleet arithmetic that produced
"7 cards" was built on it**:

> *per-verify, stock 1,723,407 cycles · middle path 140,044 → 12.31x on the ECDSA primitive*
> *block 962,000 chunk: 14.06 G → 1.87 G (7.53x) · chunk card-seconds 14,926 → 2,278*

It was computed from measured primitives, not modelled — but the composition step under-weighted the
**non-ECDSA residual**. Redone against this block's actual EC verify count:

```
EC verifies              7,015  (chunk-profile, 0.88 per input)
ECDSA cycles   7,015 x 1,723,407  = 12.09 G   of the 14.05 G
non-ECDSA residual                =  1.96 G   <- held ~constant by the accelerator
after bigint2  7,015 x   140,044  =  0.98 G
predicted total          0.98 + 1.96 =  2.94 G   => 4.78x
MEASURED                                3.14 G   => 4.48x
```

⇒ **A correct Amdahl calculation over the same primitives gives 4.78x, not 7.53x.** The remaining
gap to 4.48x (~7%) is plausibly the coprocessor's in-situ plumbing — the big-endian limb↔byte
conversion per verify, which a primitive-level benchmark does not charge.

## ⛔ This is NOT a taproot effect — an earlier draft of this document said it was, and was wrong

`BIGINT2_MIDDLE_PATH.md` records block 962,000 as **1.8% taproot by input** (hazync#190 says 2.7%
Schnorr). Either way ~98% of the block is ECDSA and DOES benefit. **Schnorr is not the explanation
here.** The 9.19x/8.00x figures come from block **140,000**, whose cycles are almost entirely ECDSA;
962,000 carries ~1.96 G of non-ECDSA work that the accelerator cannot touch, and that residual —
not taproot — is what caps the ratio.

⚠ Taproot sensitivity is real and separately documented (7 cards at 2.7% Schnorr → 16 at 35%), but
it is a different axis and must not be conflated with this one.

## What it does to the fleet arithmetic

The "7 cards" figure came from `chunk card-seconds 14,926 → 2,278`, which used the projected 7.53x.
At the MEASURED 4.48x the same division gives **3,333 card-seconds**.

⛔ **No card count is stated here.** Translating card-seconds into a fleet size needs a PROVING
measurement, and this run is execute mode. Execute cycles are not proving cost — bigint2 is a
separate coprocessor circuit and linearity must not be assumed.

⇒ **What is measured:** block 962,000 costs **4.48x less in execute-mode cycles** with the middle
path on, against a projection of 7.53x.
⇒ **What is NOT measured:** the proving ratio on this block, and therefore the fleet size. An
earlier draft of this document put a figure on both. It should not have.
→ [[feedback_only_measured_numbers]]

⏰ **The run that settles it: one GPU prove of a block 962,000 chunk with `HAZYNC_BIGINT2_ECDSA=1`,
against the stock control.** That is a direct measurement of the thing the fleet size depends on,
and it is the highest-value GPU minute available. Until it exists, the card count is unknown --
not "optimistic by roughly X", unknown.

## ✅ The correctness gate it passed on the way

All **32 journal digests byte-identical to control**, `all_valid=1` on all 32, three distinct
`METHOD_ID`s proving each arm genuinely rebuilt, and `hazync_ecmult_verify` confirmed in the guest
ELF (checked alongside a control string, so the check itself could fail).

⇒ **Substituting the ECDSA group arithmetic changes no committed output on 8,006 tip-era inputs** —
a far broader differential test than block 140,000's 212 signatures.
