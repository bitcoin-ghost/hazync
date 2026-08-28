# hazync#139, middle path — trial harness

⛔ **EXPERIMENT BRANCH. Opt-in, moves `METHOD_ID`, and nothing here is a recommendation.** The point
is to make the middle path *runnable and measurable* so the decision to move away from Core's code
can be taken on numbers rather than on an argument.

## What #139 established, and what it did not

**MEASURED:** risc0-crypto's bigint2 verifies ECDSA at **138,643 cycles against libsecp's
1,909,913 — 13.78x**, same 16 signatures through both paths. EC is ~97% of a transaction-heavy
chunk, so that is **~9.96x on the chunk**.

**NOT established, and both matter:**

- **Execute-mode cycles are not proving cost.** bigint2 runs on a separate coprocessor circuit;
  cycles are the usual proxy but linearity should not be assumed for an accelerated operation.
  **One GPU prove of one chunk settles this.**
- **16 sequential-key low-S signatures is a speed measurement, not an equivalence argument.**

**And the wholesale option is not the only option.** Using risc0-crypto's `ecdsa` module substitutes
libsecp's verification entirely — the reimplementation-equivalence question this project exists to
avoid. #139 noted a middle path and nobody measured it. **This branch is that middle path.**

## What this branch replaces — exactly one line

`secp256k1_ecdsa_sig_verify` (`ecdsa_impl.h:212`):

```text
  zero checks on r and s            <- libsecp, unchanged
  scalar_inverse_var(sn, s)         <- libsecp, unchanged
  u1 = sn*m ; u2 = sn*r             <- libsecp, unchanged
  secp256k1_ecmult(pr, Q, u2, u1)   <- THE ONLY LINE REPLACED
  infinity check                    <- libsecp, unchanged
  r == x(pr) mod n                  <- libsecp, unchanged
```

DER parsing, low-S normalisation, pubkey parsing, Core's sighash and the final comparison are all
untouched. What moves is the elliptic-curve point arithmetic, which is where nearly all the cost is.

⚠ **It does NOT preserve libsecp's wNAF and GLV, contrary to how #139 described the middle path.**
`double_scalar_mul` is Shamir's trick over the accelerator, so the scalar-multiplication *strategy*
changes too. The equivalence surface is far smaller than substituting the whole `ecdsa` module, but
it is **not** "only the field arithmetic", and it should not be sold as that.

## Soundness posture

A precompile is **constrained, not trusted** — the circuit proves it computed correctly. That is the
identical argument already accepted for the SHA-256 accelerator in `patches/0002`, so this adds no
trust assumption beyond RISC0 itself. What it adds is a **plumbing-correctness obligation**: that the
big-endian limb↔byte conversion in `bigint2_ecmult.rs` is right. That is what a differential gate
tests, and it is a closed, mechanical property rather than an open-ended equivalence claim.

## The backfill asymmetry — why this may be decidable in halves

Backfill (`GOALS.md` G2) proves a **closed, enumerable** set: every signature it will ever see
already exists on chain and can be differential-tested exhaustively against libsecp. Tip-following
(G6) cannot be, because its inputs have not been written yet.

⇒ Since backfill holds **138-229 card-years** and tip-following ~29 cards, a trade that is
unacceptable at the tip may be entirely defensible for history. **This is the shape of the decision,
not a recommendation to take it.**

## How to run it

```bash
# 1. provision a box with the experiment enabled (or apply patch 0005 by hand)
HAZYNC_BIGINT2_ECDSA=1 ./provision-vps.sh

# 2. build the guest with the feature (this MOVES METHOD_ID)
cd prover && HAZYNC_BIGINT2_ECDSA=1 cargo build --release -p host

# 3. benchmark, execute mode, no GPU — against the same control the ECMULT sweep used
HAZYNC_BLOCK=block_140000.json HAZYNC_CHUNKS=1 HAZYNC_PROFILE_EXEC=1 \
  ./target/release/host chunk-profile
```

Control on that block is **376,662,184 cycles** at the shipped settings, journal digest
`607f4a7e259b5570e0acbd74ff649ed5991f1552fef270faf03b3883e8f15fea`, 212 inputs, all ECDSA.

⛔ **The journal digest MUST match the control.** A cycle win that changes the output is a bug
wearing a win's clothing. Block 140,000 is 2011-era and almost entirely P2PKH ECDSA, which is exactly
the path this patches — and it carries no Schnorr, so it measures the accelerated fraction cleanly.

⚠ **Patch 0005 edits the shared `$HAZYNC_BASE/secp256k1` tree.** Back up `src/ecdsa_impl.h` and
restore it afterwards, or the canonical build inputs are left carrying the experiment. The same
hazard as the ECMULT table — see `TOPOLOGY_AND_SETTINGS.md` §4.1.

## What would decide it

| question | how | status |
|---|---|---|
| does it verify correctly? | journal digest matches control on block 140,000 | ⏳ |
| how much on a real chunk, in cycles? | `chunk-profile`, execute mode, free | ⏳ |
| **does the cycle win survive proving?** | **one GPU prove of one chunk** | ⛔ **needs a card** |
| does it hold on Schnorr-bearing blocks? | it cannot — risc0-crypto has **no BIP340** | ✅ known: ECDSA only |
| exhaustive equivalence for backfill | differential-test every historical signature | not started |

⚠ **No Schnorr.** risc0-crypto exports `bigint`, `curve`, `curves`, `ecdsa`, `field`, `modexp` — no
BIP340, no x-only keys. Taproot keypath spends gain nothing, and near-tip blocks carry real taproot
volume, so any speedup applies to the ECDSA fraction only. This is also why hazync#190's type-aware
packer is a **prerequisite** rather than a follow-up: post-#139 ECDSA and Schnorr diverge and a
packer that cannot see the difference throws away more than half the win.
