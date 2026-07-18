# Hazync protocol engine — tip operation

How the proven validator (leaf→block→chain, `HAZYNC_ARCHITECTURE.md`) becomes a live protocol at the
chain tip. The prover/verifier separation is the whole point: **validate ≠ prove ≠ verify**. Everything
here is node-agnostic — it applies to any Bitcoin Core-derived full node.

## Three roles (not one)
- **Validator — every node, unchanged.** A new block is validated normally, in RAM, against the full
  data, and accepted on the spot. This stays. Acceptance does NOT wait for a proof — that would stall
  the chain. The proof is for *others*, not for the proposer.
- **Prover — specialised (miner/pool/anyone with the hardware), NOT every node.** Proves `script
  validity + Utreexo update + cumulative work` for a block and folds it into the running chain proof
  (`chain_step`, recursion increment mode). Expensive; permissionless but not universal.
- **Verifier — every node, cheap.** Verifies the one recursive proof (RISC0 receipt verify — no peers
  consulted, no re-execution). This is what replaces "re-validate from data."

## Three frontiers (they move at different speeds)
1. **Tip** `H_tip` — latest validated block; full data (incl. witnesses) in RAM/disk.
2. **Proof frontier** `H_proven` — latest block folded into the recursive chain proof.
3. **Pruned frontier** `H_pruned` — blocks that are *proven AND* past the re-org window; witnesses dropped.

Invariant: `H_pruned ≤ H_proven ≤ H_tip`, and `H_tip − H_pruned ≥ REORG_WINDOW` (≥100).

**Why this matters:** proving lags the tip (a full block is billions of cycles — see throughput). So
`H_proven < H_tip` in normal operation, and that's fine. The chain advances on validation; the proof
catches up behind it. **A coin's witness is dropped only once the proof for its block exists** — the
proof *is* the replacement for the witness.

## Block lifecycle
```
mined → validated (full data, RAM) → [accepted, at H_tip]
      → prover folds it into the chain proof         → now ≤ H_proven
      → every node verifies the new chain proof (cheap)
      → past REORG_WINDOW and proven                 → PRUNED: witness dropped, proof + UTXO root kept
      → full block kept for the re-org window, then discarded
```

## Graceful degradation (a property, not an accident)
If provers are slow or absent, `H_proven` stalls but `H_tip` keeps advancing (validation is
independent). The only consequence: you can't prune unproven blocks, so witness retention grows →
disk pressure, never a consensus break. Proving is a *liveness-optional* public good: the network is
always safe, just less storage-efficient when under-proven.

## The canary (monitoring, NOT security)
Security comes from the proof (self-verifiable). The canary is a tripwire: nodes gossip the
`ChainState.utxo_root` at their proof frontier; **any divergence at equal height ⇒ alarm + halt
pruning** (stay in full-validation mode, retain witnesses, page operators). Divergence means a prover
bug or a real split — either way you do NOT want to have dropped witnesses. Where a cohort of nodes
independently proves the same height, quorum agreement on the ChainState *is* the canary; disagreement
halts the pruned frontier.

## Fork choice & re-orgs
- `ChainState.cum_work` (real Core `GetBlockProof`, already committed) **is the fork-choice metric.**
  Competing chain proofs → follow the higher `cum_work`. Fork choice becomes a property of the proof.
- On a re-org: discard chain-proof segments above the fork point, re-fold the new branch (serial, but
  the folds are cheap; the per-block validity proofs for the new branch are the cost).
- `REORG_WINDOW` must exceed the deepest plausible re-org, because a re-org can never reach a pruned
  block — its witnesses are gone. ≥100 (coinbase-maturity convention); deeper re-orgs are catastrophic
  regardless.

## IBD / sync — the killer app
A new node: fetch headers → fetch the chain proof to `H_proven` → **verify it once (cheap)** ⇒ it now
holds the exact UTXO root at `H_proven` with full validity assurance → validate only the short
unproven suffix `H_proven..H_tip` normally. Sync collapses from "replay all history" to "verify one
proof + validate a bounded tail." The tail length = the proving lag.

## Throughput reality (the honest constraint)
To hold `H_proven` near `H_tip`, average proving throughput must beat block production (~1/600 s).
A busy block is ~thousands of inputs × ~2M cycles ≈ billions of cycles — too slow for one machine in
10 min. So a tip-prover is a **cluster**: prove inputs/txs in parallel (segment the block), aggregate
with the balanced-tree recursion, then a cheap serial fold into the chain proof. Within a block:
parallel. Across blocks: serial (but the fold is small). This is why proving is a role, not a
per-node duty. **EC acceleration is load-bearing here** — routing the libsecp verify through the
accelerated EC path (4.7–5.3×) is a large part of the difference between feasible and not at the tip;
further speedups are an open research line (see `ACCELERATION.md`, honest about what does and doesn't
work).

## Incentives (open design question)
Proving is compute the proposer isn't paid for, so a deployment needs an answer for who runs provers.
Options, none consensus-load-bearing (proving is defensive infrastructure, not a mining role):
- **Altruistic / self-interest** — large nodes, exchanges, and pools prove because fast, trustless sync
  and safe pruning benefit them directly.
- **A funded validator cohort** — a deployment that already runs a trusted quorum can have that quorum
  run the provers; "quorum-proved blocks" are canonical and permissionless external provers supplement.
- **Community proving** — a coordinator hands out block ranges + witnesses, verifies submitted proofs,
  and tree-folds them (the one-time genesis→tip backfill is embarrassingly parallel; see `SCALING.md`).

## Maps onto what's built
| Protocol step | Artifact |
|---|---|
| fold block into chain proof (increment) | `chain_step` (mode 2) — validates block + linkage + carry + work |
| the proof's public output | `ChainState {tip_hash, utxo_root, cum_work, height, retarget+MTP state}` (committed journal) |
| prover ties block N to N−1 | `env::verify(self_image_id, prev_ChainState)` + host `add_assumption(prev_receipt)` |
| node verification | RISC0 receipt verify of the ChainState proof |
| canary | gossip + compare `ChainState.utxo_root` |
| fork choice | `ChainState.cum_work` |
| pruning authorization | block ≤ `H_proven` and past `REORG_WINDOW` ⇒ drop witness |

## Node integration (concrete, node-agnostic)
Verify-only path first, then pruning, then the fast-IBD path:
- Keep normal validation untouched. Add a **proof-frontier tracker** and a chain-proof gossip message
  (the ChainState receipt); provers publish receipts, verifiers check them.
- Gate witness pruning on `H_proven` + `REORG_WINDOW`, not just block age.
- A **pruned-proof** node role is the natural light client: UTXO accumulator + re-org window + chain
  proof only, no archive, no re-execution.

## Archive-node bridge (hazync-during-IBD) — the production data path
The explorer fetcher (`prover/fetch_block.py`) is a scaffold. In production the prover runs its own
**full-validation archive node** and emits the prover witness *as each block connects during IBD* — the
node already computes every spent coin's metadata, so the witness costs ~nothing and needs no network.
This also closes **S3** (the accumulator is driven by the real coin set, not a fabricated `root_prev`)
and **BIP68-time** (real `coin_mtp`, the last OPEN item in SOUNDNESS §5).

The mechanism, in standard Bitcoin Core terms:
- **Hook the connect-block spend loop.** As each block connects, `UpdateCoins` → `SpendCoin` hands
  back the full spent `Coin` for every non-coinbase input. Per spent coin you get, for free: value,
  scriptPubKey, creation height, and the coinbase flag (`Coin` packs `nHeight*2 + fCoinBase`).
- **Derive creation-MTP** as the median-time-past of the block at `coin.nHeight` — that ancestor is
  already connected during IBD, so it's a cheap lookup on the active chain. (Core stores no per-coin
  MTP.)
- Also capture the block header, coinbase, each tx's raw bytes, and wtxids for the witness/merkle
  checks. Emit one witness record per block (same shape as the fetcher's JSON, or a binary feed to the
  prover queue), gated behind a node option so it's **inert by default and consensus-inert**.
- **Clean no-consensus-edit alternative:** subscribe to the `BlockConnected` validation-interface
  signal and re-read spent coins from the block's **undo data** (`CBlockUndo` / `CTxUndo::vprevout` is
  a `std::vector<Coin>`). Works for normally-connected blocks; still needs the height→MTP derivation.

Net: a ~1-file, consensus-inert hook turns a full-validation archive node into a witness source —
genesis→tip, no explorer, real metadata, and it's the same node that would run the proving cluster.

> Note: any fast-sync mode that discards spent coins (ephemeral-UTXO schemes) is incompatible with
> witness generation — run the witness-producing archive node in full-validation mode, which is what you
> want when producing proofs anyway.

## Build order to get to tip use
1. Real STARK proving of one block + the 2-step recursive fold (256 GB box / GPU).
2. SNARK-wrap the ChainState proof (STARK→Groth16, ~200–300 B) so verification is trivial everywhere.
3. Chain-proof gossip + proof-frontier tracker in the node (verify-only path first).
4. Gate pruning on the proof frontier; wire the divergence canary.
5. Fast-IBD path: verify proof → validate unproven tail.
6. The proving cluster (parallel block proving + tree aggregation) to hold the frontier near the tip.
