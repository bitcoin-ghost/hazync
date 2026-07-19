# Hazync engine — architecture & integration plan

> **Historical working notes.** This is the original design/integration plan and reads as a changelog.
> Some of it has been overtaken: recursion (listed here as a future item) is fully implemented,
> hardened, and demonstrated; and the k256/`patches/0003` accelerator (described here as "done & the
> lever we needed") has been **removed from the guest** (2026-07-19) to keep it pure-Core. For the current truth see
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
- **~~STILL TODO~~ (now DONE — see `SOUNDNESS.md` §5 and `SECURITY.md`)** (§2.2): PoW *retarget*
  correctness, locktime/sequence (BIP68/112/113), sigops/weight — all implemented and validated; and
  larger/segwit real blocks (multi-tx, all prevouts from the bridge), demonstrated on 741000. These were
  open working notes at the time of writing; the carves followed the same pattern.

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

### The EC-acceleration lever — MEASURED, but the accelerator was since REMOVED

> **2026-07-19:** the k256 acceleration described below was **removed from the guest** to keep it
> pure-Core (`k256_ecdsa_verify`, the k256 deps, and `patches/0003` are gone — see `ACCELERATION.md`).
> The numbers are retained as the record of what EC acceleration would buy a future field-backend rework.

Swapped Core's `CPubKey::Verify` ECDSA path to RISC0's accelerated `k256` (guest exposed
`k256_ecdsa_verify`; `pubkey.cpp` kept Core's lax-DER parse + low-S normalize, then called it —
former patch `0003-pubkey-ecdsa-verify-via-k256-accel.patch`). **Head-to-head, same sig, in-guest:**

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


---

## Tip operation & node integration

*Merged from the former `HAZYNC_ARCHITECTURE.md`.*


How the proven validator (leaf→block→chain, `HAZYNC_ARCHITECTURE.md`) becomes a live protocol at the
chain tip. The prover/verifier separation is the whole point: **validate ≠ prove ≠ verify**. Everything
here is node-agnostic — it applies to any Bitcoin Core-derived full node.

### Three roles (not one)
- **Validator — every node, unchanged.** A new block is validated normally, in RAM, against the full
  data, and accepted on the spot. This stays. Acceptance does NOT wait for a proof — that would stall
  the chain. The proof is for *others*, not for the proposer.
- **Prover — specialised (miner/pool/anyone with the hardware), NOT every node.** Proves `script
  validity + Utreexo update + cumulative work` for a block and folds it into the running chain proof
  (`chain_step`, recursion increment mode). Expensive; permissionless but not universal.
- **Verifier — every node, cheap.** Verifies the one recursive proof (RISC0 receipt verify — no peers
  consulted, no re-execution). This is what replaces "re-validate from data."

### Three frontiers (they move at different speeds)
1. **Tip** `H_tip` — latest validated block; full data (incl. witnesses) in RAM/disk.
2. **Proof frontier** `H_proven` — latest block folded into the recursive chain proof.
3. **Pruned frontier** `H_pruned` — blocks that are *proven AND* past the re-org window; witnesses dropped.

Invariant: `H_pruned ≤ H_proven ≤ H_tip`, and `H_tip − H_pruned ≥ REORG_WINDOW` (≥100).

**Why this matters:** proving lags the tip (a full block is billions of cycles — see throughput). So
`H_proven < H_tip` in normal operation, and that's fine. The chain advances on validation; the proof
catches up behind it. **A coin's witness is dropped only once the proof for its block exists** — the
proof *is* the replacement for the witness.

### Block lifecycle
```
mined → validated (full data, RAM) → [accepted, at H_tip]
      → prover folds it into the chain proof         → now ≤ H_proven
      → every node verifies the new chain proof (cheap)
      → past REORG_WINDOW and proven                 → PRUNED: witness dropped, proof + UTXO root kept
      → full block kept for the re-org window, then discarded
```

### Graceful degradation (a property, not an accident)
If provers are slow or absent, `H_proven` stalls but `H_tip` keeps advancing (validation is
independent). The only consequence: you can't prune unproven blocks, so witness retention grows →
disk pressure, never a consensus break. Proving is a *liveness-optional* public good: the network is
always safe, just less storage-efficient when under-proven.

### The canary (monitoring, NOT security)
Security comes from the proof (self-verifiable). The canary is a tripwire: nodes gossip the
`ChainState.utxo_root` at their proof frontier; **any divergence at equal height ⇒ alarm + halt
pruning** (stay in full-validation mode, retain witnesses, page operators). Divergence means a prover
bug or a real split — either way you do NOT want to have dropped witnesses. Where a cohort of nodes
independently proves the same height, quorum agreement on the ChainState *is* the canary; disagreement
halts the pruned frontier.

### Fork choice & re-orgs
- `ChainState.cum_work` (real Core `GetBlockProof`, already committed) **is the fork-choice metric.**
  Competing chain proofs → follow the higher `cum_work`. Fork choice becomes a property of the proof.
- On a re-org: discard chain-proof segments above the fork point, re-fold the new branch (serial, but
  the folds are cheap; the per-block validity proofs for the new branch are the cost).
- `REORG_WINDOW` must exceed the deepest plausible re-org, because a re-org can never reach a pruned
  block — its witnesses are gone. ≥100 (coinbase-maturity convention); deeper re-orgs are catastrophic
  regardless.

### IBD / sync — the killer app
A new node: fetch headers → fetch the chain proof to `H_proven` → **verify it once (cheap)** ⇒ it now
holds the exact UTXO root at `H_proven` with full validity assurance → validate only the short
unproven suffix `H_proven..H_tip` normally. Sync collapses from "replay all history" to "verify one
proof + validate a bounded tail." The tail length = the proving lag.

### Throughput reality (the honest constraint)
To hold `H_proven` near `H_tip`, average proving throughput must beat block production (~1/600 s).
A busy block is ~thousands of inputs × ~2M cycles ≈ billions of cycles — too slow for one machine in
10 min. So a tip-prover is a **cluster**: prove inputs/txs in parallel (segment the block), aggregate
with the balanced-tree recursion, then a cheap serial fold into the chain proof. Within a block:
parallel. Across blocks: serial (but the fold is small). This is why proving is a role, not a
per-node duty. **EC acceleration is load-bearing here** — routing the libsecp verify through the
accelerated EC path (4.7–5.3×) is a large part of the difference between feasible and not at the tip;
further speedups are an open research line (see `ACCELERATION.md`, honest about what does and doesn't
work).

### Incentives (open design question)
Proving is compute the proposer isn't paid for, so a deployment needs an answer for who runs provers.
Options, none consensus-load-bearing (proving is defensive infrastructure, not a mining role):
- **Altruistic / self-interest** — large nodes, exchanges, and pools prove because fast, trustless sync
  and safe pruning benefit them directly.
- **A funded validator cohort** — a deployment that already runs a trusted quorum can have that quorum
  run the provers; "quorum-proved blocks" are canonical and permissionless external provers supplement.
- **Community proving** — a coordinator hands out block ranges + witnesses, verifies submitted proofs,
  and tree-folds them (the one-time genesis→tip backfill is embarrassingly parallel; see `HAZYNC_ARCHITECTURE.md`).

### Maps onto what's built
| Protocol step | Artifact |
|---|---|
| fold block into chain proof (increment) | `chain_step` (mode 2) — validates block + linkage + carry + work |
| the proof's public output | `ChainState {tip_hash, utxo_root, cum_work, height, retarget+MTP state}` (committed journal) |
| prover ties block N to N−1 | `env::verify(self_image_id, prev_ChainState)` + host `add_assumption(prev_receipt)` |
| node verification | RISC0 receipt verify of the ChainState proof |
| canary | gossip + compare `ChainState.utxo_root` |
| fork choice | `ChainState.cum_work` |
| pruning authorization | block ≤ `H_proven` and past `REORG_WINDOW` ⇒ drop witness |

### Node integration (concrete, node-agnostic)
Verify-only path first, then pruning, then the fast-IBD path:
- Keep normal validation untouched. Add a **proof-frontier tracker** and a chain-proof gossip message
  (the ChainState receipt); provers publish receipts, verifiers check them.
- Gate witness pruning on `H_proven` + `REORG_WINDOW`, not just block age.
- A **pruned-proof** node role is the natural light client: UTXO accumulator + re-org window + chain
  proof only, no archive, no re-execution.

### Archive-node bridge (hazync-during-IBD) — the production data path
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

### Build order to get to tip use
1. Real STARK proving of one block + the 2-step recursive fold (256 GB box / GPU).
2. SNARK-wrap the ChainState proof (STARK→Groth16, ~200–300 B) so verification is trivial everywhere.
3. Chain-proof gossip + proof-frontier tracker in the node (verify-only path first).
4. Gate pruning on the proof frontier; wire the divergence canary.
5. Fast-IBD path: verify proof → validate unproven tail.
6. The proving cluster (parallel block proving + tree aggregation) to hold the frontier near the tip.


---

## Scaling — fold cost & parallel backfill

*Merged from the former `HAZYNC_ARCHITECTURE.md`.*


Future-work record. Captures the scaling analysis and the two known fixes, surfaced *before* review,
plus the GPU ceiling. Empirical core is done: per-input script cost linear ~2.7s/GPU-input (pure
Core, L40S), fan-out ~2×/2 GPUs verified, fold cost measured (below).

### 1. The fold cost is a COMPOSITE-RECEIPT problem (not flat per-chunk)
Two data points don't fit "~10s/chunk":
- block 130000: 2 chunks, 5 inputs/chunk → aggregate ~6.7s → **~3.4s/chunk**
- block 140000: 8 chunks, ~26 inputs/chunk → aggregate ~81s → **~10.1s/chunk**

Per-chunk verify scales with chunk *fatness*, not count. Cause: `default_prover().prove()` returns a
**`CompositeReceipt`** = a list of ~1M-cycle segment receipts. A fat chunk has more segments, and the
aggregation guest's `env::verify` verifies the whole list → cost grows with segment count. Not O(1).
- **Diagnostic to confirm:** a 3rd data point at a different inputs-per-chunk ratio (e.g. rerun 140000
  with `HAZYNC_CHUNKS` high → ~5 inputs/chunk; per-chunk verify should drop toward ~3.4s).

### FIX A — succinct receipts (cheap, high value; probably beats the tree at current scale)  ✅ IMPLEMENTED
Compress each chunk to a **`SuccinctReceipt`** BEFORE folding — `prove_with_opts(env, ELF,
&ProverOpts::succinct())` (or `prover.compress(&ProverOpts::succinct(), &composite)`). RISC0's
lift+join recursion runs **on the GPU** (parallel side), collapsing the segment list to one succinct
STARK. Then every `env::verify` in the aggregation is genuinely **O(1)**, independent of chunk size.
~one-line change to the chunk prover. Sequential fold goes flat + minimal.

**Implemented 2026-07-15** (host `prove-chunk`, `prove-seg` loop, and the final `agg-chunks`/`prove-seg`
aggregate all use `prove_with_opts(..., &ProverOpts::succinct())`). Rationale confirmed by the 741000
run: the ~1645s aggregate was RISC0 lifting all 16 **composite** chunk receipts to succinct *sequentially*
inside the aggregate's resolve step. Proving each chunk to succinct up front moves that lift into the
parallel chunk phase (spread across the GPU fleet), so the aggregate only does a cheap resolve. The
aggregate is now succinct too → each block proof is one fixed-size STARK, directly composable in the
chain range-fold, and saved to `block_<h>.receipt`. Compiles clean (host-only build, cached guest ELF);
**timing win to be measured on the next GPU box** (expected: aggregate collapses from ~1645s to ~tens of
seconds; total wall-clock ~unchanged or slightly up in the chunk phase, but the sequential tail — the
part that blocks the chain fold — goes flat).

### 2. Tree aggregation — constant work, RESTORED parallelism, log tail
Correction to earlier framing: a tree does NOT reduce fold *work* (still N−1 folds). It converts the
fold from **sequential → parallelizable**: all nodes at one tree level fold concurrently across GPUs;
only *depth* is sequential. log₂(140) ≈ 8 levels × ~10–20s ≈ **~2–3 min critical path** vs ~25 min
flat. Combined with FIX A (each node verifies two O(1) receipts), the aggregation layer becomes a
rounding error. This is §2.4's "balanced binary tree over ranges" at the block level.

### 3. Chain-level fold — the 100-day sequential floor (load-bearing for the Bitcoin costing)
The block→chain fold is **~10s of irreducibly-sequential recursion per block**.
- **Tip-following:** nothing (10s / 600s interval).
- **Historical backfill (rolling composition):** 900k blocks × ~10s ≈ **>100 days sequential**, no
  matter how many GPUs chew the scripts. This is the caveat a murchandamus-calibre reviewer spots.
The fix is §2.4's tree-over-ranges applied **at chain level** (below). Surface it in the post as a
known design element, not a discovered flaw.

### 4. Parallel backfill architecture (per-block proofs, then fold)
Per-block proofs are **independent** — block N's proof attests it's internally valid AND transitions
`root_prev(N) → root_next(N)`, depending only on N's data + inclusion proofs, not on other blocks'
*proofs*. So all ~900k blocks prove concurrently across unlimited servers. Pipeline:

1. **Bridge pass** — sequential but CHEAP (hashing only, no proving): replay the whole chain through
   the Utreexo accumulator once (one machine, ~hours–1 day for all of Bitcoin), recording
   `root_prev(N)` for every height + emitting each input's inclusion proof. This is what a Utreexo
   bridge node already does. **The ONLY irreducibly-sequential step — and it's not the expensive one.**
2. **Per-block proofs** — fully parallel, unlimited servers, the GPU-years bulk. Each proves one block
   against its bridge-supplied `root_prev(N)`. Output: succinct per-block proof.
3. **Fold** — **tree over ranges**, pairwise, parallel, log-depth. NOT the rolling chain we have now.

Result: backfill is **parallel end-to-end**; sequential parts are only the bridge hashing pass + the
log-depth fold tail, both small. "Hundreds of GPU-years, fully parallel" — no 100-day floor.

### Code delta from what we have (modest)
- **Per-block proof commits BOTH roots:** `(block_hash, prevhash, root_prev, root_next, cumwork)`.
  Our `ChainState` already commits `root_next`; also commit `root_prev` so folds check adjacency.
- **Pairwise range-fold mode** (variant of the existing `aggregate`): verify two succinct range
  proofs + check boundary — left.`tip_hash` == right.`prevhash`, left.`root_next` == right.`root_prev`,
  `cumwork` sums → emit combined range proof. Recurse in a tree → one `[genesis, tip]` proof, log depth.
- A short chain history folds trivially either way; the Bitcoin-wide backfill needs exactly this.

### 5. GPU ceiling — how far we can push proof-generation speed
- **UpCloud max = 3× L40S** (144 GB VRAM, ~€4.86/hr) — only ~1.5× our current 2-GPU box.
- **8× H100 SXM node** (Lambda / CoreWeave / RunPod / AWS p5) is the practical single-node ceiling,
  and H100 is *much* better for this — RISC0 proving is FFT + Merkle-hash → **memory-bandwidth bound**;
  H100 SXM ~3.35 TB/s vs L40S ~864 GB/s (~4×). So **~3–4×/GPU × 8 ≈ ~25–30× one L40S ≈ ~14× current.**
  A full block: ~2h → single-digit minutes.
- **Beyond one node: no ceiling.** Chunks are independent; the fan-out already writes receipts to
  files → a multi-node version needs only shared receipt storage (S3/object store) + chunk
  distribution. Backfill = hundreds–thousands of GPUs across many nodes.
- **Single-block theoretical floor:** with up to one chunk per input + tree+succinct folding, critical
  path ≈ `(one chunk's prove) + log(chunks)·O(1) fold` → a block proves in ~minutes regardless of size.

### Status / next
- Empirical core done (per-input cost, fan-out, fold cost + scaling story).
- ✅ (a) Full **segwit+taproot** block done (741000, 670 inputs, proven end-to-end — the composite
  aggregate cost 1645s, which *confirmed* the FIX-A diagnosis directly).
- ✅ (c) **FIX A implemented** — succinct chunk + aggregate receipts (host, compiles; timing to measure
  on next box).
- ✅ (d)+(e) **Parallel range-fold DONE** (2026-07-15, commit `c3a5d2f`): guest mode 6 `prove_range`
  (independent per-block proof) + mode 7 `fold_range` (pairwise adjacency-checked merge) + `RangeState`
  committing both boundary contexts; host `bridge_pass`/`prove-range`/`fold-range`/`verify-range` +
  `rangecluster.sh`. Validated on 2×L40S: blocks 1..8 → 8 parallel proofs (~1.2s) → tree fold 4→2→1 →
  genesis-anchored [1..8] receipt VERIFIED in 13s, exact work/leaf counts, block-8 hash matches mainnet.
  This is the parallel backfill: per-block proofs independent (parallel across the fleet), folds
  log-depth. Wall-clock ≈ one block prove + log₂(N) folds, not N sequential steps.
- ✅ **Archive-node bridge DONE** (hazync-during-IBD): a full node emits witnesses during ConnectBlock
  via a guarded `-hazyncwitness`-style hook; validated genesis→199 on real mainnet, witnesses
  byte-identical to the fetcher's, closes S3. (Implemented and validated locally.)
- REMAINING: (b) measure the succinct monolithic aggregate on a box for a composite-vs-succinct number
  (the range-fold already supersedes it for backfill).
- Then the Delving post writes itself: architecture, receipts, honest economics, both scaling fixes
  surfaced pre-emptively.


---

## Backfill hardening

*Merged from the former `HAZYNC_ARCHITECTURE.md`.*


Making the engine correct for a full genesis→tip run (beyond the early blocks tested). Three items;
H1 is the one that BREAKS a run today, and closing it properly also closes a latent soundness gap.

### H1 — in-block spends + output recomputation (correctness + soundness)  ★ core

**The gap.** A block may contain a tx that spends an output created by an *earlier* tx in the **same
block** (chained / CPFP). Today `build_block_carried` deletes all external inputs then adds all outputs,
so an in-block spend panics on the host (the parent output isn't in the accumulator yet) and would fail
the guest's `stump.delete`.

**The latent soundness gap (found while scoping H1).** The guest adds the host-supplied
`w.new_outputs` **without recomputing them from the block's transactions** (`validate_block`, the
`for out_leaf in &w.new_outputs { stump.add }` loop). A malicious prover could therefore add fabricated
output leaves (fake coins, spendable later) or omit real ones. The honest host always supplies correct
outputs, so the demos are unaffected — but for "undeniable" the guest must derive the output set itself.

**The fix (one change closes both).** The guest recomputes every output leaf from the block's txs and
handles in-block coins by **ephemeral cancellation** (a coin created and spent in the same block never
enters the accumulator):

- Guest gathers all this-block txs: `coinbase_tx` + `{inp.raw_tx : inp.tx_first == 1}` (dedup).
- New C helper `tx_out_leaves(raw_tx, height, is_coinbase, block_time, out[])`: parse tx, for each
  vout emit `coin_leaf(txid,vout,value,spk,height,is_coinbase,block_time)`, **skipping provably-
  unspendable outputs** (Core `CTxOut::scriptPubKey.IsUnspendable()` — OP_RETURN or > MAX_SCRIPT_SIZE).
  This is also H3.
- Build the this-block output set keyed by `(txid, vout)` → leaf.
- Partition inputs: an input is **in-block** iff its `prevout.txid ∈ this-block txids`; else **external**.
  - external: verify inclusion proof + `stump.delete` (as today).
  - in-block: verify the referenced `(txid,vout)` is a real this-block output whose recomputed leaf
    matches; mark that output "spent-in-block". NO stump op. Script still runs (VerifyScript is
    independent of the accumulator).
- Add to the stump exactly the **surviving** outputs (this-block outputs not spent-in-block), recomputed
  — replacing the trusted `w.new_outputs`.

**Wire change.** `BlockInput` gains `in_block: u32`. For in-block inputs `global_pos`/`proof_i`/
`proof_last` are unused; `prevouts` still carries the parent output's (value, scriptPubKey) so the
script check runs. Host `build_block_carried` builds the partition + surviving-output set to match.

**Why sound.** The guest independently derives txids (bound by the merkle check), the output leaves
(from the real tx bytes), and the in-block/external partition. A prover cannot mark an external spend
in-block (the referenced txid wouldn't be in this block), cannot fake an output (leaves come from real
tx bytes), and cannot omit a surviving output (the guest adds all of them). Ephemeral cancellation gives
the identical net root as add-then-delete.

### H2 — BIP30 duplicate-coinbase exception blocks (91842, 91880) — overwrite implemented (F3)

Pre-BIP34, blocks 91842/91880 carried coinbases with the same txid as 91812/91722. Our leaves commit the
creation **height**, so a "duplicate" coinbase produces a DISTINCT leaf — there is no accumulator
*collision* (the collision-free property holds). BIP34 (enforced from 227931) makes coinbase txids unique
thereafter, so no later duplicate can occur.

But pre-enforcement Core does not merely tolerate the duplicate: it **overwrites** the old outpoint, so
block 91812's coinbase output becomes permanently unspendable. Keeping both leaves (the earlier reasoning
here — "an unspent bloat leaf, sound") would leave that superseded ~50 BTC coin spendable in the
accumulator, a divergence a from-genesis prove crossing height 91842 could exploit. **This was F3, now
fixed:** at exactly those two block hashes the guest deletes the superseded coinbase leaf, recomputed from
*this* block's coinbase at the (host-supplied) old height/mtp — the duplicate coinbase is byte-identical,
so the delete can only remove a genuine earlier duplicate of this coinbase's outpoint, and it is mandatory
(a prover cannot skip it).

**VERIFIED.** Blocks 91812 and 91842 carry the identical coinbase txid
`d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599`; our coin leaf for that (identical)
coinbase output at each height gives DIFFERENT leaves (the height commitment disambiguates). Test:
`host check-bip30` on real block 91842 — the honest overwrite is accepted with a matching accumulator
root (the 91812 leaf deleted, the 91842 leaf added, matching Core's UTXO set); skipping the overwrite, or
claiming the wrong old height, is rejected. Wired into CI.

### H3 — provably-unspendable outputs

Core excludes `IsUnspendable()` outputs (OP_RETURN etc.) from the UTXO set. Folded into H1's
`tx_out_leaves` (skip them). Makes the accumulator equal Core's UTXO set exactly, so the S3 claim
("bound to the real UTXO set") is precise, not a superset.

### Implementation status — WRITTEN, staged for box validation (2026-07-15)
All three items implemented; **not yet compiled/run** (the guest change invalidates the cached ELF and
needs a box). Files:
- `prover/methods/guest/verify_input.cpp` — new `tx_out_leaves` (recompute a tx's spendable output
  leaves; skips `IsUnspendable`).
- `prover/methods/guest/src/main.rs` — `validate_block` now recomputes the output set from the real tx
  bytes, **binds each tx's computed txid to the merkle-committed `w.txids`**, derives in-block spends by
  leaf-membership (no wire change), cancels them (ephemeral), and adds the surviving outputs — replacing
  the trusted `w.new_outputs`. (`extern tx_out_leaves`, `BTreeSet` import.)
- `prover/host/src/main.rs` — `out_spendable` helper; `build_block_carried` handles in-block inputs
  (dummy proof, no forest touch) + skips unspendable + excludes in-block-spent outputs; `build_block`
  and `build_full` skip unspendable too (so their `root_next` still matches the recomputing guest).

**Soundness note:** the guest no longer trusts `new_outputs`; it derives outputs from tx bytes bound to
the merkle root, so a prover cannot fake/omit coins. In-block detection is leaf-membership, which is
self-authenticating (a mislabelled spend fails either the output-set match or the inclusion proof).

### Test plan (extensive test, needs a GPU box) — validates the above
1. Build the guest hardening on the box (guest rebuild ~min).
2. Re-run the KNOWN-GOOD vectors first (regression): `check-ibd` genesis→199, block 170/173/176 tips,
   `prove-full` block 741000 — all must still verify with identical hashes (proves the recompute +
   unspendable change didn't regress).
3. `check-ibd` (execute) over a range with real **in-block spends** and block **91842** (BIP30 dup
   coinbase) — confirm they validate and UTXO-leaf/tip match mainnet.
4. `prove-ibd` a substantial contiguous range; `rangecluster.sh` across both GPUs.
5. Any guest bug surfaces in step 2/3 in seconds (execute mode) — fix + rebuild in minutes.
