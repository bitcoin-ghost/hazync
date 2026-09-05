# hazync#139, middle path — trial harness

> ⛔ **DEVELOPMENT RECORD — landed from an experiment branch `exp/139-bigint2-middle-path`, 2026-08-28.**
> Kept because the reasoning is worth having; **read the corrections below before quoting any number.**
>
> - **"wholesale is 15% faster" was NEVER PRODUCED BY A RUN.** `hazync_ecdsa_verify_full` existed
>   from `3615a8d` and nothing ever called it until `patches/0014` (see `9b767b5`). The middle
>   path's 8.00x at proving time IS measured; the wholesale comparison is not.



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

## ✅ RESULT — middle path measured 2026-08-28: **9.19x on the chunk**

Block 140,000, 212 inputs, 1 chunk, execute mode, no GPU. Same harness and control as the ECMULT
sweep.

| | guest cycles | |
|---|---|---|
| control (stock libsecp) | 376,662,184 | — |
| **middle path (bigint2 group arithmetic)** | **40,989,238** | **9.189x — 89.1% fewer cycles** |

✅ **The journal digest is IDENTICAL**:
`607f4a7e259b5570e0acbd74ff649ed5991f1552fef270faf03b3883e8f15fea`, `all_valid=1`, `binds=212`. All
212 signatures verified to the same result as libsecp. `hazync_ecmult_verify` is confirmed present in
the guest ELF, so the path was genuinely taken — as the 9x drop independently shows.

### ⇒ This reframes the #139 decision, and against the intuition

#139 predicted **~9.96x on the chunk for the WHOLESALE swap** — substituting risc0-crypto's entire
`ecdsa` module, which is the reimplementation-equivalence question this project exists to avoid.

**The middle path gets 9.19x of that while replacing one line.**

```
wholesale / middle  =  9.96 / 9.19  =  1.084x
```

⇒ **The wholesale substitution buys at most ~8% more than the middle path, and costs the entire
equivalence surface for it.** The expensive part was always the group arithmetic; libsecp's DER
parsing, low-S handling, r/s checks, inversion and final comparison are cheap by comparison and can
stay Core's literal code essentially for free.

⚠ **On this evidence "fastest" and "most faithful of the accelerated options" are the same choice.**
That is the opposite of what one would assume, and it is the single most decision-relevant thing this
experiment produced.

### Caveats, none of which are small

- ⛔ **This is execute-mode cycles, NOT proving cost.** bigint2 runs on a separate coprocessor
  circuit; linearity must not be assumed. **This remains the question that decides everything**, and
  it now clearly justifies a GPU: one prove of one chunk, both arms.
- ⚠ **Block 140,000 is ~100% ECDSA** (2011-era, no Schnorr, no taproot). risc0-crypto has **no
  BIP340**, so at tip-era blocks the chunk-level win is capped by the ECDSA fraction. This number is
  the *ceiling*, not the tip-era figure — and it is why hazync#190's type-aware packer is a
  prerequisite.
- ⚠ Untested: anything other than 212 sequential P2PKH-era signatures. Exhaustive differential
  testing over the historical set is still the load-bearing work.

### A side effect worth checking

With bigint2 doing the group arithmetic, libsecp's `pre_g` table is no longer on the ECDSA-verify
path. If verification is its only consumer in this guest, the table (~16 MB at window 19, ~64 MB at
21) becomes dead weight, the ECMULT window tuning becomes moot, and the guest shrinks. **Not
verified — check whether `secp256k1_ecmult` has other callers here before assuming it.**

## ✅ PROVED ON A GPU 2026-08-28 — the win survives, 8.0x

The blocking question was whether execute-mode cycles predict proving cost, since bigint2 runs on a
**separate coprocessor circuit**. Measured on an L40S (46,068 MiB, driver 595.58.03), block 140,000,
one chunk, 212 inputs:

| arm | po2 21 | po2 22 | vs stock |
|---|---|---|---|
| **stock libsecp** | 446, 446, 446 s (189 seg) | **396, 396 s** (93 seg) | — |
| **middle path** | 55, 56, 56, 56 s (21 seg) | ~50 s (inferred) | **8.0x** |
| **wholesale** | 48, 49, 48 s (18 seg) | **43, 44 s** (9 seg) | **9.1x** |

```
execute-mode cycles   9.19x
GPU proving wall      8.00x     <- the coprocessor takes ~13%, not the whole win
segment count 189->21 9.0x      <- independent corroboration
```

⇒ **#139 is real.** Per-verify ECDSA: **1,723,407 → 140,044 cycles (12.31x)**.

### ⛔ The wholesale arm is 15% faster, not the ~1% predicted

An earlier revision of this document inferred, from backing non-EC overhead out of the middle path
(140,044 vs #139's wholesale 138,643), that wholesale would buy ~1%. **Measured, it buys 15%**
(48.3 s vs 55.8 s at po2 21). The inference was wrong; the fidelity trade is a genuine decision, not a
free lunch. → `HELIX_DUAL_BACKEND.md`

### Guards, because the first attempt was void

The first run of this experiment produced two confident wall times that were **the same binary twice**
— `cargo` is not on a non-interactive SSH `PATH`, so both builds silently failed and both arms re-timed
stock. It was caught only by printing `METHOD_ID` per arm. Every arm here therefore asserts: build
rc=0, `METHOD_ID` **distinct from every other arm**, the expected shim symbol **present in the guest
ELF**, and the binary linked against `libcuda`. Any failure aborts and restores the shared secp tree.

⚠ `cc::Build` emits no `rerun-if-changed` for **headers**, so editing `ecdsa_impl.h` without an env
change reuses the cached guest. The wholesale arm hit exactly this and was caught by the id guard.
Force with `rm -rf target/riscv-guest`.

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
