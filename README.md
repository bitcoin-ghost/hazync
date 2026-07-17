# Hazync — Bitcoin validity proofs from Bitcoin Core's *own* consensus code

> New here and want to prove a block? See **[CONTRIBUTING.md](CONTRIBUTING.md)** for a simple step-by-step. Live party: https://bitcoinghost.org/hazync

Hazync proves that Bitcoin blocks are valid under Bitcoin Core's **real, unmodified** consensus code,
inside a zero-knowledge VM (RISC0). A verifier checks one small proof instead of re-executing every
script from genesis. The proofs are **recursive/foldable**, so a whole chain collapses into a single
succinct receipt.

> **New to zero-knowledge proofs?** Start with **[`EXPLAINER.md`](EXPLAINER.md)** — a plain-English,
> no-jargon walkthrough of what this is, why it matters, and how to help. For the full technical
> narrative (novelty, coverage matrix, trust model, "try to break it"), see **[`WRITEUP.md`](WRITEUP.md)**.
> This README is the quick technical overview.

The distinguishing property: **it is not a reimplementation.** Bitcoin Core's actual
`interpreter.cpp` (script evaluation), `SignatureHash`, and `libsecp256k1` are compiled to `riscv32im`
and proven *as-is*. Every prior validity-proof effort reimplements consensus logic and inherits the
question "does your reimplementation match Core in every edge case, forever?" — Hazync doesn't have that
question, because it runs Core.

> Status: the **method** is built, sound, and demonstrated end-to-end on real mainnet data (single
> blocks, IBD chains, tip operation, parallel backfill). It has **not** yet proven the entire ~900k-block
> chain — that is a compute campaign, not new capability (see [Scope](#scope-what-is-and-isnt-proven)).
> It has been through four rounds of internal adversarial self-audit (`SECURITY.md`), with a replayable
> `host adversarial` suite; it has **not** yet been externally reproduced or independently audited.

## What a Hazync proof attests

A verified chain/range proof attests: *every block from the anchor to the tip is valid under Bitcoin
Core consensus, the UTXO set is exactly the committed accumulator root, and cumulative proof-of-work is
as committed* — with no re-execution and no trust in peers. From the **genesis anchor** this binding is
unconditional; from a mid-chain checkpoint the anchor is a trust input.

### Consensus surface enforced (real Core unless noted)
- **Scripts, all types** under Core's exact `GetBlockScriptFlags` — always-on P2SH/WITNESS/TAPROOT,
  the buried DERSIG/CLTV/CSV/NULLDUMMY deployments at their real heights, and the two historical
  script-flag exception blocks — via real `VerifyScript` + `SignatureHash` + `libsecp256k1`. Exercised
  on real P2PKH, P2SH, P2WPKH, P2WSH, P2TR key-path and script-path spends.
- **`CheckTransaction`** (structure, duplicate inputs, value bounds).
- **No inflation**: Σin ≥ Σout per tx; coinbase ≤ subsidy(height) + Σfees (exact halving formula).
- **Proof-of-work** (`CheckProofOfWorkImpl`, real `arith_uint256`) + **difficulty retarget**.
- **Merkle root**; **BIP141 witness commitment**.
- **Block weight** ≤ 4M; **full sigop cost** ≤ 80k (legacy + P2SH + witness).
- **Coinbase maturity**; **absolute locktime** (`IsFinalTx`); **BIP68** relative locktime (height + time).
- **BIP34** (coinbase height); **BIP30** (duplicate-txid): within-block distinctness, plus Core's outpoint
  *overwrite* for the two grandfathered pre-BIP34 duplicate-coinbase blocks (91842/91880) — the superseded
  coinbase leaf is deleted to match Core (tested via `host check-bip30`; BIP34 makes coinbases unique after).
- **UTXO accumulator transition** (Utreexo): in-block-spend cancellation, unspendable-output skipping
  (`IsUnspendable`), so the committed root equals Core's UTXO set exactly. The guest **recomputes** the
  block's output set from the tx bytes (bound to the merkle root) — it does not trust the prover's list.

## Architecture

```
  per-input script proof ── block proof ── chain fold ── tip / range proof
   (real VerifyScript)     (all rules +     (recursive     (one succinct
                            accumulator)     IVC)            receipt)
```

- **Guest** (`prover/methods/guest/`): the zkVM program. `verify_input.cpp` wraps real Core; `main.rs`
  drives block validation, the accumulator, recursion, and range-folding.
- **Accumulator** (`accumulator/`): a Utreexo hash-forest UTXO commitment (our code — the one non-Core
  component; exhaustively unit-tested). Leaves commit `(txid, vout, value, scriptPubKey, height,
  is_coinbase, block_time)`.
- **Host** (`prover/host/`): builds witnesses, drives proving, verifies receipts.
- **Two folding modes:** a **sequential** recursive chain (`chain_step`), and a **parallel range-fold**
  (independent per-block proofs + a log-depth pairwise tree — the backfill path; see `SCALING.md`).
- **Witness sources:** the **archive-node bridge** (a full node emits a witness per block *during IBD*
  from its own UTXO view, via a guarded `-hazyncwitness=<dir>`-style hook — the production path; see
  `HAZYNC_ENGINE.md`), or the explorer fetcher `prover/fetch_block.py` (a scaffold for testing without
  a node).

## Trust model

The proof rests on exactly four things, stated plainly:
1. **Real Bitcoin Core v28 code** (two portability shims only — `serialize.h` 32-bit int overload,
   SHA-256 routed to the RISC0 accelerator byte-identically; **no consensus-logic changes**; ECDSA and
   Schnorr both run through the compiled, unmodified `libsecp256k1`). The `k256` substitution in
   `patches/0003` is an opt-in speed option that is **not** applied in the sound build and would
   reintroduce the reimplementation question — see `ACCELERATION.md`. See also `patches/` and `SOUNDNESS.md`.
2. **RISC0 zkVM soundness** (standard STARK/SNARK assumption).
3. **SHA-256** collision resistance (the accumulator + merkle/commitment checks).
4. **The anchor** (genesis is unconditional; a checkpoint is a documented trust input).

The binding between Core's code and the *specific block* being proven — the part most likely to hide a
bug — has been hardened across four adversarial rounds and is checked by the replayable `host adversarial`
suite: a mining-capable prover cannot downgrade the block height (soft-forks off + inflated subsidy),
inflate fees via unbound prevouts, forge the difficulty across a seam, or launder a proof across
recursion levels. Findings and fixes are in `SECURITY.md` — and breaking it is the most useful contribution.

## Reproduce

Turnkey on a fresh Ubuntu box with an NVIDIA GPU (validated on 2× L40S; CUDA proving needs CUDA **12.6**
— the script installs it):

```bash
# 1. provision: RISC0 toolchain + CUDA 12.6 + Bitcoin Core v28 source + patches + build the prover
GPU=1 REPO_DIR=$PWD ./provision-vps.sh
cd prover && cargo build --release --features cuda      # GPU build

# 2. get witnesses (explorer scaffold; or run an archive node's witness hook for the real bridge)
for h in $(seq 1 550); do python3 fetch_block.py $h /w/block_$h.json; done

# 3. validate fast (execute mode, no proving) — full consensus + accumulator, seconds/block
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=550 ./target/release/host check-ibd

# 4. PROVE the recursive chain from genesis (real STARK receipts), + tip extension
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=170 HAZYNC_TIP=3 ./target/release/host prove-ibd

# 5. PARALLEL range-fold across N GPUs -> one genesis-anchored receipt -> verify
NGPU=2 LO=1 HI=550 HAZYNC_WITNESS_DIR=/w bash rangecluster.sh
```

Single-block proofs: `check-full` (execute-mode validation) / `prove-full` (STARK) with `HAZYNC_BLOCK`.
Regression: `./target/release/host regress`.

## Demonstrated (see `prover/evidence/`)

- **Block 741000** — a real modern full block (670 inputs, segwit + taproot, witness commitment):
  proven end-to-end on GPU, receipt verified, tip hash byte-matching mainnet.
- **Genesis→170** recursive chain proof + **tip extensions** at ~1.2s/block (IBD and tip operation).
- **Parallel range-fold genesis→550** — one genesis-anchored succinct receipt, folding 550 independent
  block proofs through a 10-level tree, including a normal spend (170) and an **in-block spend** (546).
- **BIP30** duplicate-coinbase blocks 91812/91842 verified collision-free.

All tip hashes, cumulative work, and UTXO-leaf counts match mainnet exactly.

## Scope: what is and isn't proven

**Proven:** the method is sound (real Core code), complete over the consensus surface enumerated above
(with the explicit boundaries below — the 2-hour future-time limit is node-local and out of scope, as is
policy/standardness), and demonstrated on real mainnet data at single-block, chain, tip, and
parallel-backfill levels, with the real-UTXO binding from the genesis anchor.

**Not yet done (compute + review, not capability):**
- The **full genesis→tip backfill** (all ~900k blocks) — a parallelizable GPU-compute campaign.
- **External reproduction and independent audit** (internally it has had four adversarial self-audit
  rounds — `SECURITY.md` — but no outside review), including a formal audit of the accumulator (the one
  non-Core component).
- **SNARK-wrapping the final chain proof** to ~200–300 bytes for trivial universal verification — the
  capability is validated (Groth16, block 170) but not yet applied to the chain/range output.

## Repo map

| File | What |
|------|------|
| `SOUNDNESS.md` | Formal soundness & completeness statement; the recursion self-reference argument. |
| `HAZYNC_ARCHITECTURE.md` | The engine design (leaf → block → chain), section by section. |
| `HAZYNC_ENGINE.md` | Tip-operation protocol + the archive-node bridge (hazync-during-IBD). |
| `SCALING.md` | Succinct receipts, tree fold, the parallel-backfill range-fold. |
| `HARDENING.md` | In-block spends, output-recompute soundness, unspendable outputs, BIP30. |
| `SECURITY.md` | The adversarial audit record — four self-audit rounds, every finding and its fix (H1–H8, the script-flag activation set, the coordinator seam), and the `host adversarial` suite. |
| `CONTRIBUTING.md` | Join the live proof party in a few steps (build → identity → prove a range). |
| `ACCELERATION.md` | **Open task:** a sound EC speed-up is an open field-backend rework — the naive bigint2 modmul intercept was prototyped and *disproven* (~10% slower); k256 (`patches/0003`) is a measured but opt-in reimplementation, not applied in the sound build. Pure-Core is the baseline. Contributors welcome. |
| `PROVING.md` | The operator's guide to the real proving commands (single block, chain, range-fold, tip, SNARK-wrap). |
| `patches/` | The portability shims (0001 serialize, 0002 SHA-256 accel; auditable, no consensus-logic changes). 0003 k256 is opt-in and not applied. |
| `accumulator/` | The Utreexo UTXO accumulator crate + its exhaustive tests. |
| `prover/` | Guest (real Core + engine), host (driver), `fetch_block.py`, `rangecluster.sh`. |
| `provision-vps.sh` | Turnkey box setup. |

---

*Research repository. Bitcoin Core is BSD/MIT; the patches are portability-only and change no consensus
logic. This is early-stage research shared for review and reproduction — see [Scope](#scope-what-is-and-isnt-proven)
for what is and isn't proven.*
