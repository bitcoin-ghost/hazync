# Hazync engine — architecture & integration plan

> **Historical working notes.** This is the original design/integration plan and reads as a changelog.
> Some of it has been overtaken: recursion (listed here as a future item) is fully implemented,
> hardened, and demonstrated; and k256/`patches/0003` (described here as "done & the lever we needed")
> is an *opt-in* accelerator that is **not** applied in the sound build. For the current truth see
> `SOUNDNESS.md`, `SECURITY.md`, `PROVING.md`, and `ACCELERATION.md`.

*How the "real Core VerifyScript in a zkVM" result becomes the Hazync validity-proof engine: block
proving, the UTXO accumulator, recursion, tip validation, node verification, IBD, and serving-layer
integration. Efficiency is the through-line.*

---

## 0. The one-line goal
A node verifies **one small proof** and knows the entire Bitcoin chain is consensus-valid —
instead of re-executing every script from genesis. The proof is over Bitcoin Core's **real**
consensus code (already demonstrated), so there is no reimplementation to get wrong.

## 1. What we have — the leaf
`VerifyScript(scriptSig, scriptPubKey, witness, flags, TransactionSignatureChecker)` — Core v28's
actual interpreter + sighash + libsecp256k1, compiled to riscv32im, proven in RISC0. Validated on
REAL mainnet inputs: legacy P2PKH, segwit P2WPKH, taproot **key-path**, taproot **script-path**
(tapscript inscription). ~2.1M cycles/input, dominated by EC signature math. This is the atom.

The layers below turn "prove one input" → "prove one block" → "prove the chain."

---

## 2. Leaf → Block → Chain

### 2.1 Input proof (DONE)
One input, one `VerifyScript`. Batchable (see §6.2).

### 2.2 Block proof — what it must attest
A block is valid iff ALL of the following, and Core has real code for each (carve the same way):
1. **Header / PoW** — `block_hash <= target`, target matches the difficulty-adjustment schedule,
   timestamp > median-time-past and < now+2h, version rules. (Cheap; `CheckProofOfWork`, the
   retarget calc.)
2. **Merkle root** — the tx list hashes to `header.hashMerkleRoot`. (`BlockMerkleRoot`.)
3. **Per-tx structural** — `CheckTransaction`: non-empty vin/vout, no negative/overflow values,
   no duplicate inputs, size/weight limits, coinbase rules.
4. **Per-input script** — `VerifyScript` (the leaf we have) for every non-coinbase input.
5. **Amount / no-inflation** — Σinputs ≥ Σoutputs per tx; coinbase ≤ subsidy(height) + Σfees;
   subsidy halving schedule. (`Consensus::CheckTxInputs`, `GetBlockSubsidy`.)
6. **Sequence/locktime** — BIP68/112/113 relative & absolute locktimes.
7. **Sigops / weight** — block-level limits.
8. **UTXO state transition** — see §3.

The expensive item by far is (4). Everything else is nearly free but MUST be included for soundness
(a block proof that skips inflation checks is worthless). Carve the relevant Core functions
(`CheckBlock`, `CheckTransaction`, `Consensus::CheckTxInputs`, `ContextualCheckBlock`) the same way
we carved the interpreter — they're pure functions of (block, chainparams, prev-state).

### 2.3 The UTXO accumulator — the piece that makes blocks stateless (the real missing link)
The prover can't carry the whole UTXO set (~10 GB, ~200M outputs). Commit it to a small root and
supply per-input **inclusion proofs**.

- **Choice: Utreexo** (hash-forest accumulator; ~1 KB roots; per-input proofs ~hundreds of bytes;
  well-studied; already the Bitcoin-community answer). A block proof takes `UTXO_root_prev`, and for
  each input verifies a Utreexo inclusion proof that the spent coin existed, deletes it, inserts the
  new outputs, and asserts the result is `UTXO_root_next`.
- **Who supplies proofs:** a **bridge** node holds the full accumulator/UTXO set and emits inclusion
  proofs. Archive nodes are natural bridges (they already hold the chainstate). This is the
  same "bridge node" pattern ZeroSync/utreexo use — but here we ALSO validate witnesses (they
  punted on that).
- **Why this is the linchpin:** with it, each block proof is a pure function
  `(block, UTXO_root_prev, inclusion_proofs) → UTXO_root_next`, so blocks compose with no shared
  state — which is exactly what recursion needs.

**STATUS — accumulator DONE (2026-07-14), native crate `accumulator/` (`hazync-utreexo`):**
- `Forest` = host/bridge oracle (full leaf set; generates inclusion proofs). `Stump` = roots-only,
  the guest-side half (`add` / `verify` inclusion / `delete`→next root). Height-indexed roots;
  `parent = SHA256(l‖r)` (routes through the RISC0 SHA accelerator once in-guest).
- Deletion is swap-and-shrink built on one clean primitive: removing the rightmost leaf is trivially
  correct because that leaf's proof siblings ARE the surviving left-subtrees. Three cases derived
  (delete-last / different-tree / same-smallest-tree).
- **Verified:** exhaustive single-delete == Forest oracle for every n≤40 and every index; double-spend
  rejected (stale proof fails post-delete); 120 block-simulations with proofs against running state;
  2000 interleaved add/delete ops. All pass.
- **Protocol:** a block's spends are applied in a fixed order, each inclusion proof against the state
  just before it — so the bridge (an archive node) can produce them deterministically and the
  guest needs no full set.
- **In-guest cost (estimate, grounded in the §11 SHA-accel measurement):** an inclusion proof at
  mainnet depth (~200M UTXOs → depth ~28) is ~28 accelerated SHA256 compressions ≈ 30–60K cycles per
  input — i.e. **~2–3% on top of the ~2M-cycle script verify.** Confirm for real when wired in-guest.

### 2.5 Block-proof guest interface — ATOM WORKING (2026-07-14)
**A first block proof runs end-to-end in the zkVM.** One execution over a synthetic block of the 3
real ECDSA mainnet spends: each input's script verified by real Core `VerifyScript`, each spent coin
proven present in the Utreexo accumulator (bound by the canonical leaf the guest recomputes from the
parsed tx), spent coins deleted + created coins inserted, and the resulting root asserted ==
committed `UTXO_root_next`. Journal: `{script_results:[1,1,1], all_ok:true, root_matches:true}`.
- **Cost: 10.34M cycles total; ~10.13M is the 3 script verifies → the whole accumulator layer
  (inclusion + 3 deletes + 10 inserts + root check) is ~3%.** Confirms EC verify dominates; the
  accumulator is nearly free (small forest here, depth ~4; mainnet depth ~28 ⇒ ~30–60K/input).
- **Soundness link established:** `verify_input.cpp::coin_leaf` = `SHA256(txid‖vout_le‖value_le‖spk)`
  computed inside the guest from the SAME tx+coin `VerifyScript` checked, and the guest commits it —
  the host bridge (`bitcoin` crate + `hazync-utreexo` Forest) computes byte-identical leaves
  (matched first try). So "script valid" and "coin in the UTXO set" are bound to one coin.
- Guest `mode==1` reads a `BlockWitness {root_prev, inputs[], new_outputs[], root_next}`; per input a
  `(proof_i, proof_last)` pair against the running state (bridge advances its Forest in lockstep).
- **CheckTransaction + no-inflation DONE (2026-07-14).** Carved real Core `consensus/tx_check.cpp`
  (`CheckTransaction`) + a `check_tx` wrapper adding the amount rules (all values in `MoneyRange`,
  non-coinbase Σin≥Σout ⇒ fee≥0). Wired per-tx into the block proof: honest block `tx_checks=[1,1,1]`,
  real fees computed (25,106 sat across the 3 txs), +~60K cycles (~0.6%). **Adversarial proof that it
  bites:** dropping a coin's value below its outputs → `check_tx` returns `-23` (inflation) AND the
  script independently returns `-3` (segwit sighash commits the amount) → block REJECTED.
- **REAL BLOCK PROOF DONE (2026-07-14): mainnet block 170** (coinbase + the first Bitcoin tx,
  Satoshi→Hal Finney) validated end-to-end in ONE zkVM run with every core consensus check:
  - real `VerifyScript` (P2PK spend) `[1]`; real `CheckTransaction` `[1]`; no-inflation amounts.
  - **PoW**: `check_pow` = Core's CheckProofOfWorkImpl (real `arith_uint256` SetCompact + compare,
    mainnet powLimit) on the real 80-byte header — verifies the actual nonce. Header hash
    reconstructs to `00000000d114…a2ee`.
  - **Merkle**: real Core `ComputeMerkleRoot([cb_txid, spend_txid])` == header.hashMerkleRoot.
  - **Subsidy**: coinbase 50 BTC ≤ `block_subsidy(170)` 50 BTC + fees 0 (no over-issuance).
    `block_subsidy` = exact Core halving formula (reimplemented; validation.cpp too heavy to carve).
  - **UTXO**: block-9 coinbase deleted from the accumulator, coinbase+spend outputs inserted, root
    advanced. **2.03M cycles total** (~1.9M = the one ECDSA verify; PoW+merkle+subsidy+UTXO ≈ 130K).
  - **Adversarial (clean isolation):** lower the spent coin below its outputs — legacy P2PK sighash
    ignores the amount so the SCRIPT still passes `[1]`, but the no-inflation rule fires (`-23`) →
    REJECTED. Proves the amount rule is load-bearing.
- **STILL TODO** (§2.2): PoW *retarget* correctness (nBits is the right difficulty — needs prev/epoch
  context, comes with recursion), locktime/sequence (BIP68/112/113), sigops/weight; and larger/segwit
  real blocks (multi-tx, all prevouts from the bridge). Carves are the same pattern.

### (original plan) Block-proof guest interface
One guest execution proves one whole block. Journal commits `(valid, block_hash, UTXO_root_prev,
UTXO_root_next, work)` so the recursion step (§2.4) can chain on them.

Private inputs (from the bridge, not committed): the block, and per-non-coinbase-input a Utreexo
inclusion proof of the coin it spends against `UTXO_root_prev`.

Guest steps, all pure functions of the inputs (carve real Core where noted):
1. **Header/PoW** (`CheckProofOfWork`) + timestamp/version. Retarget needs the epoch's first-block
   time → supply as a committed input from the previous chain proof (the "PoW-carve" note).
2. **Merkle root** — tx list hashes to `header.hashMerkleRoot` (`BlockMerkleRoot`).
3. Fold the accumulator at `UTXO_root_prev`. For each tx, in order:
   a. `CheckTransaction` (structure, no dup inputs, value bounds).
   b. For each input: verify its inclusion proof (§2.3), then `VerifyScript` (the leaf we have),
      then `Stump::delete` the spent coin.
   c. Insert the tx's outputs as new leaves; enforce Σin ≥ Σout (fees ≥ 0).
4. Coinbase: value ≤ `GetBlockSubsidy(height)` + Σfees.
5. Assert the resulting accumulator root == `UTXO_root_next`; commit the journal.

Order of build: (3b VerifyScript + inclusion) is the atom we have + the accumulator we just built —
wire those first on a real small block, then add the cheap-but-mandatory checks (1,2,3a,4).

### 2.4 Recursion — TRANSITION VALIDATED (2026-07-14)
**The IVC transition F(prev_state, block) → next_state runs over real consecutive mainnet blocks
170 → 171 → 172** (guest `mode==2`, `chain_step`). `ChainState = {tip_hash, utxo_roots+leaves,
cum_work[32], height}` is the committed journal. Each step: `validate_block` (all §2.2 checks) ∧
`header.hashPrevBlock == prev.tip_hash` ∧ `block.root_prev == prev.utxo_root` (UTXO carry) ∧
`cum_work += GetBlockProof(nBits)` (real Core 256-bit chainwork) ∧ `height+1`; a proof exists only
if all hold (panic ⇒ no proof). Result: tip hashes match the real chain (✓✓✓), heights 170/171/172,
cumulative work 1×/2×/3× the difficulty-1 block work (4,295,032,833 each), one linked UTXO root.
Coinbase-only blocks ~160K cycles; the spend block 2.08M.
- **RECURSION HOOK (the crypto binding, deferred to the big box):** when not the base step, the prev
  state must be a valid `ChainState` proof of THIS guest — `env::verify(self_image_id, prev_journal)`
  (RISC0 composition; host discharges it with the previous receipt via `add_assumption`). The
  transition LOGIC above is validated in execute here; cryptographic recursion *proving* is
  resource-heavy → run on the 256 GB box. The base step trusts the anchor checkpoint (genesis
  unconditionally, or a documented block-hash checkpoint — the deployment's trust-ladder choice).
- **PoW retarget** naturally lives here: the epoch's first-block time comes from the chain state, so
  once recursion carries it, `GetNextWorkRequired` can be checked (the "PoW-carve" resolves).

### 2.6 §2.2 tail checks — DONE (2026-07-14)
Added to `validate_block` / `chain_step`, all real Core code, all enforced over blocks 170→172
(negligible cost — coinbase-only stayed ~176K cyc):
- **Difficulty retarget** (`chain_step`): between epochs nBits must equal the tip's nBits; on a
  2016-boundary it must equal `calc_next_bits` (Core's CalculateNextWorkRequired math — real
  arith_uint256, 2-week timespan clamped 4×, capped at powLimit). `ChainState` now carries
  `prev_nbits`, `prev_time`, `epoch_start`. Non-boundary path validated on 170–172; boundary path is
  faithful-by-construction, exercised when a real 2016-boundary block is folded. **This closes the
  PoW-carve** — difficulty can no longer be forged.
- **Block weight** ≤ `MAX_BLOCK_WEIGHT` (4M) and **sigop cost** ≤ `MAX_BLOCK_SIGOPS_COST` (80k):
  `tx_wu_sigops` = real `GetSerializeSize(TX_NO_WITNESS/TX_WITH_WITNESS)` + `CScript::GetSigOpCount`,
  summed over coinbase + txs.
### 2.7 Leaf extension + timelocks/maturity — DONE (2026-07-14)
The UTXO leaf now commits the coin's **height + coinbase flag**:
`SHA256(txid‖vout‖value‖spk‖coin_height‖is_coinbase)` — so age-based rules can't be forged. Added,
all enforced over 170→172:
- **Coinbase maturity** (100 blocks): a coinbase output is unspendable for 100 blocks.
  **Exercised for real** — block 170 spends block-9's coinbase at height 170 (161 blocks mature →
  passes; <100 → `-40`).
- **Absolute locktime finality** (`is_final_tx` = exact Core `IsFinalTx`): nLockTime vs height/time
  + `SEQUENCE_FINAL`. Run on the coinbase and every tx.
- **BIP68 relative locktime** (`check_input_locks`, height-based): tx version ≥ 2 + sequence
  disable/type bits, `coin_height + (seq & mask)` vs spend height. Wired; a no-op for these pre-2016
  v1 txs. `BlockInput` now carries `coin_height` + `coin_is_coinbase` (leaf-committed).
### 2.8 Refinements — DONE (2026-07-14)
- **Per-height script flags** (`block_script_flags`): consensus `VerifyScript` flags by mainnet
  soft-fork activation height (P2SH 173805, DERSIG 363725, CLTV 388381, **CSV/BIP112 419328**, WITNESS
  +NULLDUMMY 481824, TAPROOT 709632). `validate_block` derives flags from height, so BIP66/65/CSV/
  segwit/taproot are all enforced through the real interpreter automatically.
- **Full sigop cost** (`tx_full_sigops` = real Core `GetTransactionSigOpCost`): legacy·4 + P2SH
  (`GetSigOpCount(scriptSig)`) + witness (real `CountWitnessSigOps`), using the spent coins + flags.
- **Multi-tx modern validation (mode 3):** real P2WPKH/P2SH/P2WSH-multisig/P2TR-keypath/P2TR-script
  spends all validate at height 800000 (flags `0x20e15`), sigop costs varying by type (1/1/7/4/4 —
  proves full witness/P2SH counting, not legacy fallback). Block-level PoW/merkle proven on block 170;
  a synthetic multi-spend block can't carry real PoW, so this exercises the validation path directly.
- **BIP68 time-based + BIP113 (MTP) — DONE.** `ChainState` carries `recent_times` (last 11 block
  timestamps); `median_time_past` = median; `chain_step` passes the previous block's MTP into
  `validate_block` and advances the window. **BIP113:** from CSV height (419328) absolute-locktime
  finality uses MTP not block time. **BIP68 time-based:** the leaf now also commits the coin's
  creation-MTP (`SHA256(txid‖vout‖value‖spk‖height‖is_coinbase‖coin_mtp)`), and `check_input_locks`
  enforces `coin_mtp + ((seq&mask)<<9) ≤ spend_mtp` for v≥2 time-type sequences (`-42`). MTP is
  carried+correct; unused at heights 170–172 (pre-BIP113, as consensus dictates), active ≥419328.

**CONSENSUS SURFACE COMPLETE.** Block/chain validation now enforces, all real Core: scripts (all
types, correct per-height flags) + CheckTransaction + no-inflation + PoW + retarget + merkle +
subsidy + weight + full sigops + coinbase maturity + absolute locktime (MTP) + BIP68 (height+time),
plus chain linkage + UTXO-root carry + cumulative work. Next = the big compute (STARK proving) on a VPS.

### (original) Recursion — block proofs → one chain-tip proof
`ChainProof_N` attests: `ChainProof_{N-1}` is valid ∧ `block_N` is valid (§2.2) ∧
`block_N.hashPrevBlock == block_{N-1}.hash` ∧ cumulative work ∧ `UTXO_root` advanced
`prev → next`. Each step verifies the previous chain proof *inside* the guest (RISC0 native
recursion) and folds in one block. The tip proof is a single receipt covering genesis→tip.

- **Aggregation shape:** either a linear fold (one block at a time, good for tip-following) or a
  balanced binary tree over ranges (good for parallel initial proving of history). Use both:
  tree for backfill, linear for the tip.
- **Final wrap:** RISC0 STARK → Groth16 SNARK → ~200–300 byte proof, verifiable cheaply anywhere
  (even on-chain / in a light client on a phone).

---

## 3. Tip validation (real-time proving)
As each block arrives, a prover generates `block_N`'s proof and folds it into `ChainProof_{N-1}` →
`ChainProof_N`. This must keep pace with the ~10-min interval. Feasibility rests entirely on §6
(efficiency): a busy block is a few thousand inputs; at today's un-accelerated ~2.1M cycles/input
that's ~billions of cycles — provable but heavy, so we accelerate and parallelise. Provers can be:
(a) the node itself if it has the hardware, (b) a small set of designated provers, or (c) an
open proof market. The tip proof always reflects the current best chain; reorgs re-fold from the
fork point.

## 4. Node verification
A verifying node holds only: the **chain-tip proof** (tiny) + the **UTXO accumulator root**.
- *Is the chain valid?* → verify the proof (milliseconds). Full-consensus assurance, zero
  re-execution.
- *Is this new block valid & does it extend the tip?* → verify the block proof links to the tip
  proof (or, if the node is also a full validator, it can validate directly and optionally prove).
- Trust model: **none** beyond the soundness of the proof system + that we proved the real Core
  code. No trusted checkpoints, no signed snapshots.

## 5. IBD — the killer application
Today: a new node downloads ~600 GB of blocks and re-validates every script (hours–days).
With Hazync: download the **chain-tip proof** + a **UTXO snapshot** (or just the Utreexo root and
fetch inclusion proofs on demand), **verify the proof**, and you are synced — trustlessly, in
seconds–minutes. The proof attests the UTXO root, so the snapshot is self-verifying (its hash must
match). This is "sync a full node in an instant" — but, unlike a signed UTXO snapshot (which needs
trust) or ZeroSync (which skipped witness validation), this proves the *complete* consensus rules
over the *real* code, including every signature.
- Utreexo synergy: a Utreexo node doesn't keep the full UTXO set at all — just the root — so IBD
  becomes "verify chain proof, keep root." Storage collapses from GBs to KBs.
- IBD ≠ needed for Hazync itself: the chain proof is built forward as blocks come; a fresh node
  just *verifies* it. So there is no "prove from genesis every time" — the recursion means the tip
  proof already encodes all history.

## 6. Efficiency — the priority (faster proofs, smaller compute)
Current cost is ~2.1M cycles/input, ~all in libsecp256k1 EC operations, with SHA256 second.

1. **SHA256 precompile (do first — low risk, keeps real code).** RISC0 ships a SHA256 accelerator.
   Route Core's `CSHA256` (used by every sighash, merkle root, txid, Utreexo hash) through it. This
   accelerates the *real Core hashing primitive* — SHA256 is SHA256, no consensus reimplementation
   risk — and sighash is a large chunk of the non-EC cost. High value, low risk.
2. **Batching (do first).** The per-proof fixed cost (guest init, C++ ctors, table loads) is
   amortised by validating many inputs per guest execution. A block proves in batches of, say,
   32–256 inputs. Cuts fixed overhead dramatically vs one-proof-per-input.
3. **secp256k1 acceleration (evaluate — biggest single win, small risk).** The EC math dominates.
   Two options, decreasing faithfulness / increasing ease:
   a. Patch Core's `pubkey.cpp` so libsecp256k1's field/group ops call RISC0's secp256k1 precompile
      — accelerate the *real* code's arithmetic (most faithful, hardest).
   b. Verify signatures with RISC0's accelerated secp256k1 (patched RustCrypto) *instead of*
      libsecp256k1 — reintroduces a tiny, well-tested reimpl surface (only the ECDSA/Schnorr verify
      math; sighash + script stay real Core). RISC0/SP1 report ~100× here (their bls12-381 sync
      committee: 6B→50M cycles). This is the pragmatic lever if (a) is too deep.
   Recommendation: measure the potential (instrument cycle counts for the EC portion), then decide.
   Even option (b) keeps the soundness-critical part — sighash construction and script semantics —
   as the real Core code; only the final "is this sig valid over this hash" swaps to an audited
   accelerated verifier.
4. **Trim guest fixed cost:** run `__libc_init_array` once per batch (not per input), keep the
   precomputed ecmult tables resident, avoid re-deserialising shared data.

Target: get per-input proving from ~2.1M cycles to the low hundreds of K, and prove a full block on
the 256 GB box within the block interval.

---

## 7. Coverage matrix (what still needs one real-spend test each)
All go through the SAME `VerifyScript`; each just needs one confirming real spend.
- [x] P2PKH (legacy ECDSA)        — real, proved
- [x] P2WPKH (segwit v0)          — REAL mainnet spend, VALID
- [x] P2TR key-path (BIP341)      — REAL mainnet, proved
- [x] P2TR script-path / tapscript (BIP342) — REAL mainnet inscription reveal: VALID, ~3.67M cycles
- [x] P2SH — REAL mainnet spend, VALID
- [x] P2WSH (native multisig) — REAL mainnet spend, VALID (~6M cycles, ~3 sigs)
- [ ] bare multisig, P2PK (rare; same VerifyScript)
- [ ] non-standard / edge scripts (OP_RETURN outputs, unusual sighash flags: SINGLE/NONE/ACP)
Beyond script: header/PoW, merkle, CheckTransaction, amount/inflation, locktime/sequence, sigop &
weight limits, coinbase maturity — carve the corresponding Core functions (§2.2) for the block
proof.

## 8. Serving-layer integration
- **Hazync = the proof engine; a serving/overlay layer distributes the receipts** (witness-free
  recursive validity-proof overlay + light-client backbone; node-side, not wallet; defensive
  infrastructure, a public good). Hazync produces the receipts the serving layer distributes.
- A **proven block** is one whose Hazync proof is available; nodes serve `ChainProof` + block/UTXO
  data over their existing P2P/serving endpoints. Light clients & syncing nodes request the tip proof
  and verify it.
- An operator UI shows proof-generation & serving status (is the prover keeping up with the tip?
  current UTXO root? proof size?).

## 9. Roadmap
- **A (done):** prove one real input, every major script type.
- **B (next):** efficiency — SHA256 precompile + batching, then evaluate secp256k1 accel; publish
  per-input & per-block cost curves. THIS unblocks everything else being practical.
- **C:** block proof — carve header/PoW + merkle + CheckTransaction + amount rules; prove a whole
  real block's script+consensus validity (on the 256 GB box).
- **D:** UTXO accumulator — integrate Utreexo; prove the `UTXO_root_prev → next` transition; stand
  up a bridge that emits inclusion proofs.
- **E:** recursion — fold block proofs into a chain-tip proof; SNARK-wrap the tip.
- **F:** productionise — tip-following prover, node-side verification, IBD flow, serving layer,
  light-client integration.

## 10. Open questions / risks
- **Prover throughput vs 10-min tip** — the whole thing is only "live" if a block proves within the
  interval. Efficiency (§6) + parallel/cluster proving decide this. Measure early.
- **Utreexo proof bandwidth** — inclusion proofs add per-input data; bridges must serve them at
  scale. Consider proof caching / aggregation.
- **Carving the non-script consensus** — `CheckBlock`/`ConnectBlock` have more OS/state tentacles
  than the interpreter; expect more shims. Time-box it like we did the interpreter.
- **Vendor independence** — RISC0 is the substrate today; keep the guest logic (real Core code) and
  the accumulator/recursion protocol backend-agnostic so the prover can be swapped (a different
  proving backend slotted in) later. The *value* is proving the real code, not the brand.
- **Reorgs** — the tip proof must cheaply re-fold from a fork point; design the linear-fold to keep
  recent block proofs cached.

---

## 11. Efficiency — MEASURED (overnight 2026-07-14)
Instrumented on the working spike. **The crypto dominates; batching and SHA do not.**

| Component | Cost | Notes |
|---|---|---|
| Fixed overhead / proof | **~55K cycles (2.5%)** | init, C++ ctors, secp256k1 selftest, tables. Measured via batch scaling (n=1/2/4). |
| SHA256 (sighash/hashing) | **~110K cycles (~5%)** | ROUTED through RISC0's `sys_sha_compress` accelerator (byte-swap state, raw block). All real spends STILL validate — correct. |
| **EC signature verify (secp256k1)** | **~1.9M cycles (~95%)** | the bottleneck. This is where efficiency must come from. |

Post-SHA-accel per-input: P2WPKH 2.19M→2.08M, taproot 2.19M→2.07M, tapscript 3.67M→3.55M
(all still `result=[1]`). So **batching (fixed cost tiny) and SHA (5%) are NOT the levers — EC is.**

### The EC-acceleration lever — DONE & MEASURED (2026-07-14, option B)
Swapped Core's `CPubKey::Verify` ECDSA path to RISC0's accelerated `k256` (guest exposes
`k256_ecdsa_verify`; `pubkey.cpp` keeps Core's lax-DER parse + low-S normalize, then calls it —
patch `0003-pubkey-ecdsa-verify-via-k256-accel.patch`). **Head-to-head, same sig, in-guest:**

| ECDSA verify | cycles/verify |
|---|---|
| real libsecp256k1 | **1,967,155** |
| RISC0-accelerated k256 | **327,966** |
| **speedup** | **6.0×** |

**End-to-end on real mainnet spends (all still `result=[1]` — correct):**

| spend | k256 cycles | libsecp256k1 | speedup |
|---|---|---|---|
| P2WPKH | 434,353 | ~2,030,000 | **4.7×** |
| P2SH | 452,968 | ~2,070,000 | **4.6×** |
| P2WSH 2-of-3 | 1,146,412 | ~6,030,000 | **5.3×** |
| P2TR key-path (Schnorr — control) | 2,068,813 | 2,070,000 | unchanged ✓ |
| P2TR script (Schnorr — control) | 3,545,806 | 3,550,000 | unchanged ✓ |

The two Schnorr controls being unchanged to <0.1% proves the swap is surgical: only the ECDSA
verify accelerated, sighash/script/taproot untouched. **Per-input for the common ECDSA types drops
~4.7–5.3×** — this is the block-at-the-tip lever we needed.

**Soundness posture (the one caveat):** k256 (RustCrypto) is a *different* ECDSA implementation than
libsecp256k1. We now prove "real Core interpreter + sighash + DER-parse + low-S normalize, and the
final r,s validity via k256" rather than 100% real Core crypto. ECDSA *verify* (unlike sign) is a
deterministic valid/invalid decision on already-canonicalised inputs, and k256's RISC0 fork is the
standard audited RustCrypto verifier — so consensus-agreement risk is low, but it IS a posture change
kept behind patch 0003 so it can be toggled. Non-accelerated (pure real Core) build = drop patch 0003.

**Still open — Schnorr/taproot:** k256 doesn't accelerate BIP340 here, so taproot key-path stays at
~2.07M (real libsecp256k1 schnorrsig). Next lever if taproot volume matters: accelerated BIP340
verify (k256 `schnorr` feature or a bigint2 blob) — same pattern, patch `XOnlyPubKey`'s verify.

### (superseded) The EC-acceleration decision — original analysis
RISC0's crypto accelerators: `sys_sha_compress` (SHA, done), `sys_bigint` (one 256-bit
`x·y mod m`), and `sys_bigint2` (programmable blob — the whole-scalar-mult EC precompile that
RISC0's `k256` crate uses). Options, decreasing faithfulness:
- **(A) Route libsecp256k1's field-mul through `sys_bigint`** — keeps the REAL crypto, but its
  software field-mul is already ~50 instructions and uses 5×52 limbs, so per-mul ecall + limb↔256
  conversion likely won't beat it. **Probably not a win.**
- **(B) Swap only the signature *verify* to RISC0's accelerated `k256`/bigint2** — expose a C
  `verify_ecdsa/verify_schnorr` from the guest, patch Core's `pubkey.cpp` to call it instead of
  libsecp256k1. Bounded, well-tested reimpl surface (only "is this sig valid over this hash");
  sighash + script semantics stay REAL Core. Expected ~40–100× on the EC portion (RISC0's published
  EC-precompile speedups) → per-input from ~2M to plausibly ~100–300K cycles. **The pragmatic lever.**
- **(C) Route ALL of libsecp256k1 through a bigint2 EC precompile** — most faithful + fast, but
  requires the EC blob programs wired to libsecp256k1's group ops. Hardest.

**Recommendation & why it's a decision, not a mechanical change:** (B) reintroduces a *small*
reimplementation — the ECDSA/Schnorr verify math — which is standardised and heavily audited (unlike
the interpreter/sighash where the reimpl gap bit us all week). Given ~95% of cost is here, (B) is
almost certainly the right trade: keep the soundness-critical, easy-to-get-wrong parts (sighash,
script, consensus) as real Core; accelerate only the one well-defined primitive. But it IS a
soundness posture choice, so flagging for a decision rather than doing it unilaterally overnight.
Target with (B): ~10× per-input, making block-at-the-tip proving realistic.

### Kept as-is (safe wins)
- SHA256 accelerator routing (validated correct, ~5%).
- Batch entry point (amortises the ~55K fixed cost across a block's inputs — small but free).

## 12. Coverage — CONFIRMED on real mainnet (overnight 2026-07-14)
Every major script type validates through the ONE unmodified Core `VerifyScript` in the zkVM
(all `result=[1]`), on genuine confirmed-on-chain transactions:
| Type | cycles | source |
|---|---|---|
| P2PKH (legacy ECDSA) | ~2.1M | real, +proved |
| P2WPKH (BIP143) | 2.03M | real |
| P2SH | 2.07M | real |
| P2WSH (multisig) | 6.03M | real (~3 sig-verifies) |
| P2TR key-path (BIP341) | 2.07M | real, +proved |
| P2TR script-path / tapscript (BIP342) | 3.55M | real inscription, +proved |
This is the complete script-validation surface that makes up ~all real mainnet inputs — done, on
real data, through the actual Core code. The remaining block-proof pieces are the cheap non-script
consensus (header/PoW, merkle, CheckTransaction, amounts) + the UTXO accumulator + recursion.
