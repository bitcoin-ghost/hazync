# Hazync — soundness & completeness statement

The document a reviewer should read first. States exactly what a Hazync proof guarantees, under what
trust assumptions, which consensus rules are enforced vs open, and the argument for each subtle point.
Written to pre-empt critique, not to hide it.

## 1. The claim
A verified Hazync **chain-tip proof** attests: *every block from the anchor to the tip is valid under
Bitcoin Core consensus, the UTXO set is exactly the committed root, and cumulative proof-of-work is as
committed* — checkable by verifying one succinct proof, without re-executing history or trusting peers.

## 2. Trust base (what the soundness rests on)
1. **Real Bitcoin Core v28 code.** The script interpreter, sighash, `CheckTransaction`, the
   transaction/weight/sigop machinery, `ComputeMerkleRoot`, the difficulty retarget (`pow.cpp`'s
   `CalculateNextWorkRequired` / `CheckProofOfWorkImpl`, driven through the real `CBlockIndex` /
   `chain.cpp` with consensus constants from `kernel/chainparams.cpp` and `primitives/block.cpp`), and
   libsecp256k1 are the *actual* Core sources compiled into the zkVM — not a reimplementation. The shims
   under Core's own sources are portability-only (`serialize.h` 32-bit int overload; SHA256 routed to the
   RISC0 accelerator, byte-identical; single-thread no-op `coreshim/{sync,threadsafety,logging}.h`) plus
   libc/unwinder glue. **No consensus logic is changed.** This is the property that removes the
   reimplementation-soundness gap every prior effort carries. Auditable: `patches/000{1,2}` + the TU list
   in `prover/methods/guest/build.rs`.
2. **Non-Core code beneath libsecp256k1 (since v0.21.0).** The canonical guest also applies
   `patches/0012` and `patches/0013` (`provision-vps.sh` phase 5a). Every signature check runs through
   them, so they are consensus-relevant, and they are ours rather than Core's:
   - **`0012` — the `field_bigint2` backend** (`prover/methods/guest/field_bigint2.h`,
     `field_bigint2_impl.h`, `src/field_bigint2.rs`). A narrow substitution at the field interface libsecp
     already parameterises: lazy 8×32-bit limbs, with add/sub/negate/half in C and multiply/square/inversion
     through the RISC0 bigint2 coprocessor. wNAF, GLV, the ECDSA and Schnorr logic and every check above
     the field stay libsecp's. Evidence: a mod-p harness against arbitrary precision, libsecp's own suite
     at `-DVERIFY`, mutation controls — two broken lazy backends pass libsecp's suite and are caught only
     by the harness — and a block-962,000 journal digest byte-identical to stock
     (`scripts/field-backend-tests.sh`, `docs/FIELD_BIGINT2_BACKEND.md` §5b). ⚠ No corrupt-signature
     negative control has run on a CORE build (§7).
   - **`0013` — the `lift_x` witness hint** (`prover/methods/guest/src/liftx_hint.rs`). Replaces the
     square root in `secp256k1_ge_set_xo_var` with a host-supplied Y, accepted only if `y² == x³ + 7`
     under libsecp's own `fe_sqr`/`fe_equal`; the existing parity fix-up then determines y uniquely, and a
     missing or wrong hint falls back to libsecp's square root. Advice-and-verify: a hint can skip work,
     not change a result (`docs/LIFTX_HINT.md` §3) — but that check is itself computed on the `0012`
     backend.
3. **SHA-256 collision resistance** — the accumulator (Utreexo), the coinbase SMT and the
   merkle/commitment checks are SHA-256; that's their entire security. Both accumulators are *our* code,
   but commitments ABOVE consensus rather than Core reimplementations. The proven Utreexo is the guest's
   `prover/methods/guest/src/utreexo.rs`, differentially fuzzed against a reference model
   (`accumulator/src/lib.rs`); the coinbase SMT that carries BIP30 is `coinbase-smt/src/{roots,bip30}.rs`,
   compiled into the guest by path and checked in CI against a naive oracle that shares no code. With the
   `0012` backend they are the non-Core code most likely to hold any remaining soundness bug, and none of
   it has been externally audited.
4. **RISC0 zkVM soundness** — the STARK/SNARK proving system. Standard cryptographic assumption.
5. **The anchor checkpoint** — the chain proof starts from a trusted state (genesis, or a trusted
   block-hash checkpoint). Everything after the anchor is proven; the anchor itself is a trust input
   (documented; which anchor to trust is the deployment's trust-ladder choice).

## 3. Recursion soundness (audit S1 — the self-reference argument)
The chain/aggregation guests call `env::verify(self_id, prev_journal)` where `self_id` is read from
host input (`prover/methods/guest/src/main.rs`). The concern: a malicious prover controls `self_id`.

**Argument that this is sound as used, plus the hardening applied:**
- RISC0 composition binds an *assumption* `(image_id, journal)` that the prover must discharge with a
  matching receipt. The honest **verifier** checks the *final* receipt against the true `METHOD_ID`.
- The load-bearing question is whether a nested `self_id ≠ METHOD_ID` can smuggle a malicious guest's
  receipt into the chain. It cannot survive top-level verification **iff every nested composition used
  the true image id**. Relying on that implicitly is the trap.
- **Hardening (implemented).** Every recursive step commits `self_id` into its journal and asserts the
  previous step recursed against the same id (`prev.self_id == self_id` in `chain_step`/`aggregate`);
  the host verifier asserts the *final* `self_id == METHOD_ID` after `receipt.verify(METHOD_ID)`.
  Together these force every nested composition to the true image id. The adversarial `prove-chain-bad`
  test folds a block against a corrupted `self_id` and confirms it is rejected. Recursive proofs are
  therefore sound on the same footing as single-block proofs — no honest-prover assumption — given the
  verifier obligation below.
- **Verifier obligation.** The ultimate external verifier must replicate the `self_id == METHOD_ID`
  check and pin the leftmost in-boundary to the genesis anchor. A verifier that skips these has not
  checked the chain.

## 3b. Binding each proof to the block it claims (2026-07 adversarial audit)

An external adversarial soundness review found four places where the guest under-constrained the
witness relative to the block being proven. All four are fixed, and each has a negative test — the
execute-mode `adversarial` suite (self-contained, in CI) or a GPU-box proving test — alongside an
honest baseline that must still be accepted, so a rejection can't pass for the wrong reason.

- **H1 — block height.** `chain_step`/`aggregate` now assert `w.height == prev.height + 1`. The height
  selects the script flags and the coinbase subsidy, so an unbound, host-supplied height let a
  mining-capable prover set `w.height = 1`, turning every soft-fork flag off (segwit/taproot outputs
  become anyone-can-spend) and inflating the subsidy to 50 BTC while the journal still committed the
  true height. Test: `host adversarial` (#1). (The range-fold path was already bound by its genesis
  pin + adjacency check.)
- **H2 — segmented flags and witness.** Each chunk proof commits a per-input binding digest over the
  exact `(raw_tx, input_idx, prevouts, coin metadata, flags)` it verified; the aggregation recomputes
  it from the block's own input under the block's real flags and requires equality. Previously a chunk
  committed only the coin leaf, so it could substitute a *different* valid spend of the same coin, or
  validate the spend under attacker-chosen weaker flags. Test: `HAZYNC_H2_BADHEIGHT=1 host prove-seg`
  (aggregate rejects on the GPU box).
- **H3 — in-block coins.** A coin created earlier in the same block bypasses the accumulator, so the
  guest now enforces that it was created by a strictly earlier transaction and is spent at most once
  (ordered, multiplicity-checked). Previously an in-block output could be spent twice (inflation) or
  before it was created. Tests: `host adversarial` (#3 double-spend, #3 spend-before-create).
- **H4 — coinbase.** The coinbase now runs through real Core `CheckTransaction` plus an `IsCoinBase`
  assertion (bad-cb-length, per-output `MoneyRange`, value-sum range), which it previously skipped.
  Test: `host adversarial` (#4).

A second pass (re-auditing the fixes above) found one more critical and a set of range/coordinator
anchoring gaps:

- **H5 — multi-input fee-prevouts (critical, inflation/theft).** Only each input's own `input_idx`
  coin was authenticated, but `check_tx` sums the whole first-input prevouts blob into the fee, so a
  phantom coin at another index inflated the fee (~21M BTC) or an omitted `BlockInput` skipped a script
  + a deletion. Fixed by a pre-pass tying the flat input list to each tx's real `vin` (one shared blob,
  sequential `input_idx`, exactly `tx_vin_count` inputs). Test: `host adversarial` (#5) + honest 2-input
  baseline.
- **H6 — genesis in-boundary (high).** `verify-range` now pins the FULL genesis boundary
  (`assert_genesis_in_boundary`: `in_epoch_start`, `in_roots`, `in_recent`, `in_time`, not just
  `in_tip`/`in_leaves`/`in_nbits`); `verify-any` applies the same pin whenever a range claims genesis.
  Closes a forgeable first-retarget difficulty and a phantom-root UTXO seed. **`in_smt_root` joined the
  pin later.** #54 added the coinbase-SMT root to the boundary and to `RangeState::is_genesis_anchored`,
  but neither the standalone verifier (audit #3 F-2) nor this host gate (F-3) got it, so a range starting
  from a fabricated BIP30 history verified as genesis-anchored. Both now require it to equal the empty
  tree's root — the verifier by calling the shared predicate.
- **H7 (fixed).** Coordinator now chains ranges on the full boundary, not tip-hash alone: `verify-any`
  emits a boundary digest over height, tip, roots, leaves, nBits, time, epoch start, recent times and the
  SMT root, and `_frontier_chain` (`coordinator/server.py`) requires, across each seam, tip linkage,
  digest equality (`in_bhash == out_bhash`) and **height contiguity** `b.lo == a.hi + 1` (H9, round 6).
  Among every chain meeting that, it selects the one with the **most cumulative work** from genesis, not
  the tallest — so a longer low-difficulty fork cannot shadow the real chain.
- **H8 (fixed).** Every recursion-consumed journal (`ChainState`/`RangeState`/`ChunkOut`) commits a
  domain tag as its first field, asserted on decode — no cross-mode journal can be laundered in.

## 3c. Anchor identity + completeness (2026-07-22 audit, round 8)

A five-reviewer completeness+verifier audit (full write-up: `AUDIT_2026-07.md`). The soundness core
held — accumulator, FFI/VerifyScript boundary, and enforcement gating all came back sound. One
verifier-hole and five completeness deviations were found; all fixed except the one unprovable rule.

- **A1 / S5 — anchor identity (verifier-hole, FIXED).** A bare `ChainState` receipt committed no record
  of the anchor it started from, so an `is_base=1` receipt built on a *fabricated* anchor was
  journal-indistinguishable from a genesis-anchored one — not reachable through any shipped verifier
  (`verify-range`/`verify-any` take only `RangeState` and pin genesis) but a foot-gun for a raw chain
  receipt. **Fix:** the guest commits `anchor_id = dsha256(base-anchor journal)` (set at `is_base==1`,
  carried forward), and the new **`verify-chain`** command pins `anchor_id == dsha256(genesis_anchor)`.
  This makes `ChainState` self-authenticating to its anchor, exactly as `RangeState` is (H6). The
  **verifier obligation in §3 now has a chain-track form:** a chain receipt must be checked with
  `verify-chain` (or the anchor pinned equivalently); a checkpoint-anchored proof correctly fails it.
- **G3 — witness commitment now activation-gated** at segwit height 481824 (was unconditional → a
  reject-valid stall on canonical pre-activation blocks); below activation, `unexpected-witness` forbids
  any witness data (now including the coinbase — G2).
- **G5 — block-level `MoneyRange(nFees)`** now asserted explicitly (+ i128-safe `subsidy+fees`), not left
  to anchor-integrity induction.
- **G1 — BIP30** was a utreexo non-membership limitation carried by a bounded structural argument
  (sound to ~1,983,702). **CLOSED in #54** by a second accumulator — a coinbase-only sparse Merkle tree
  that proves non-membership directly. See the matrix below.
- **N1/N2/N3 — hardening:** removed the dead host-flags field, length-prefixed the leaf `scriptPubKey`,
  and made `tx_full_sigops` fail closed on a short blob.
- **G4 — 2h future-time** stays deliberately unenforced (verifier-local wall-clock, unprovable).

Validated in execute mode with no regression (170→172 byte-exact; 130000/741000 VALID with byte-exact
tips; 741000_badwit rejected; check-bip30 PASS). `METHOD_ID` + leaf format changed ⇒ re-prove from genesis.

## 4. Scope of each proof type (audit S3 — be explicit)
- **Single-block / segmented block proof** (`prove-full`, `prove-seg`): attests the block is
  *internally valid* (scripts, structure, no-inflation, PoW, retarget, merkle, subsidy, weight, sigops,
  witness commitment) AND that a *self-consistent* accumulator transitions `root_prev → root_next`.
  It does **NOT** by itself attest that `root_prev` is the real mainnet UTXO set at height N — the
  standalone harness constructs `root_prev` from the block's own prevouts + filler.
- **Chain proof** (recursion / range-fold): this is where real-UTXO binding comes from — each step
  enforces `root_next(N-1) == root_prev(N)` back to the anchor, so the tip proof's UTXO root is the
  real set *given the anchor*. **Real soundness for "these coins existed" lives in the chain, not the
  standalone block.** The 100000/130000/140000 runs are internal-validity + accumulator-consistency
  demonstrations; they are not, and were never claimed to be, UTXO-membership proofs.

## 5. Consensus-rule completeness matrix
ENFORCED (real Core unless noted):
- Script validity, all types, per-height soft-fork flags (P2SH/DERSIG/CLTV/CSV/segwit/taproot).
- `CheckTransaction` (structure, dup inputs, value bounds).
- No inflation: Σin ≥ Σout per tx; coinbase ≤ subsidy(height)+Σfees (subsidy = exact halving formula —
  **reimplemented, differentially fuzzed vs Core**).
- PoW (`CheckProofOfWorkImpl`, real arith_uint256) + difficulty retarget (`pow.cpp`'s
  `CalculateNextWorkRequired`, **real compiled Core**, cross-checked against the actual on-chain nBits
  at all 476 mainnet retargets).
- Merkle root, **including the CVE-2012-2459 mutation check** (duplicate-txid malleability — Core's
  `mutated` flag is captured and rejected); **BIP141 witness commitment** (activation-gated at segwit
  height 481824, round 8 / G3) + **`unexpected-witness`** below activation (incl. the coinbase).
- **Block-level `MoneyRange(nFees)`** and an overflow-safe `coinbase ≤ subsidy(height) + Σfees` bound
  (round 8 / G5), on top of the per-tx `CheckTransaction` value ranges.
- Block weight ≤ 4M; full sigop cost ≤ 80k (legacy + P2SH + witness).
- **`time-too-old`**: a block's timestamp must exceed the median-time-past of the previous 11 blocks.
  (The 2-hour future-time limit is node-local wall-clock — not a provable consensus rule; see §6.)
- Absolute locktime (`IsFinalTx`); BIP68 relative locktime (height + time, real `MTP(coinHeight−1)`).

- **Coinbase maturity + BIP68 (height)** — CLOSED (S2, 2026-07-15). The fetcher/bridge now sources each
  spent coin's real `coin_height` + `coin_is_coinbase` and threads them into the witness (`build_full`,
  shared by prove-full/seg/chunk/agg). Both checks fire on real blocks; validated on block 741000
  (`all_ok=true` with real metadata). **BIP68-time `coin_mtp` — FIXED.** The block-proving path now
  commits the real Core value `GetMedianTimePast(coinHeight−1)`: the guest commits `mtp`
  (= `median(prev.recent_times)`) on created-output leaves, and every host builder derives the same
  value from the chain it processed (the `block_mtp` window in the IBD path), so creation and spend
  leaves match. Validated — `check-ibd` genesis→550, `check-full` 741000, and the 170→172 demo stay
  VALID with identical tip hashes. The check itself is also proven on a real mainnet 90-day-CSV tx
  (`prover/test_bip68_real.sh`). Height-based BIP68 + maturity + absolute locktime are live too.
- **BIP34** (coinbase scriptSig encodes height) — CLOSED (S4a). `check_bip34` parses coinbase vin[0]
  scriptSig and compares the pushed height to the block height. Validated on 741000 (`bip34_ok=true`).
- **BIP30** (no duplicate txid overwriting an unspent output) — **CLOSED (#54)**. In-block distinctness
  is still an explicit sorted-txid check. The cross-block rule is no longer a bounded invariant but a
  **proof**: a coinbase-only sparse Merkle tree (key = coinbase txid, value = unspent output count),
  journalled as `in_smt_root`/`out_smt_root` and continuity-checked at the fold seam. Per block the guest
  proves the new coinbase absent-or-zero against the INCOMING root, inserts it, then decrements each
  spent coinbase — checking before the updates, so a block cannot spend its own duplicate to zero and
  then claim the slot was free.
  What the guest DERIVES rather than reads is the security argument: the coinbase txid and its
  spendable-output count from the coinbase transaction, and each spent coinbase's txid from the same
  Core-deserialised transaction the scripts ran against. Only the proofs come from the witness.
  `is_genesis_anchored` pins the tree to empty at the anchor, so a prover cannot start from a fabricated
  history. The ~1,983,702 ceiling is gone: nothing in the check depends on scriptSig encoding.
  The F3 grandfathered overwrite REMAINS — it deletes a superseded coin from the accumulator, which is
  still real work. Only the BIP30 check moved; under the tree those two blocks are ordinary inserts.

- **BIP68 time-based** — CLOSED. The IBD/chain proving path commits real `MTP(coinHeight−1)` (host
  derives it from the chain, mirroring an archive node; see the §5-ENFORCED note). The one residual is
  the **standalone `build_full` harness**, whose fabricated anchor uses placeholder `recent_times` — to
  prove a *real* time-locked block standalone it needs the real last-11 timestamps + real per-coin MTP
  (which the fetcher/bridge can supply). The IBD path has no such dependency.

OPEN (none architectural):
- Standalone `build_full` real-MTP anchor (above) — only matters for proving an isolated real
  time-locked block via `check-full`; the IBD path is complete.

## 6. What "solid" requires from here (ordered)
1. ✅ **S1 recursion hardening** — self_id committed + verifier asserts `==METHOD_ID` at every level;
   positive chain VERIFIED, adversarial wrong-id chain REJECTED (proven on a box).
2. ✅ **S2 real coin height/coinbase** threaded from the bridge — maturity + BIP68-height live on real
   blocks (validated on 741000). (BIP68-time `coin_mtp` was fixed separately, without the bridge — §5.)
3. ✅ **BIP34 + BIP30** checks added and validated on 741000.
4. ✅ **Execute-mode regression** (`regress`, `check-full`) — block 170 chain_step tip-match + adversarial
   inflation; runs in seconds, no proving. `check-full` is the standard pre-flight before any GPU prove.
5. ✅ **Full segwit+taproot block** (741000, 670 inputs) proven end-to-end on GPU: 16 chunk STARK
   receipts → aggregate, receipt VERIFIED, tip hash byte-matches mainnet. Exercised the BIP141 witness
   commitment, BIP34/BIP30, and real maturity/BIP68 for real. Also fixed a latent header bug: the
   header builder hardcoded version 1 (invisible on the version-1-era test vectors 100000/130000/140000);
   now threads the real versionbits value, so PoW is correct on modern blocks.
6. **Scaling → HAZYNC_ARCHITECTURE.md**, reprioritised by the 741000 run: (a) ✅ **succinct chunk
   receipts — implemented 2026-07-15.** The 1645 s aggregate was dominated by lifting 16 *composite*
   receipts inside its resolve step; each chunk is now proved to a `SuccinctReceipt` up front, so that
   lift moves into the parallel chunk phase and the aggregate's in-guest verifies are fixed-cost. (b) **Archive-node bridge** (hazync-during-IBD) — **BUILT +
   running as of 2026-07-23**: it drives one resident Utreexo forest over the real chain and emits each
   block's witness with the real `root_prev` + inclusion proofs, replacing the explorer fetcher, giving
   real coin metadata + MTP for free, and closing S3 operationally (real-UTXO binding) — the receipt is
   byte-identical to the replay path, so the trust base is unchanged. (c) **Parallel backfill** across a
   GPU fleet → tree fold.

Items 1–6 did not change the §2 trust base. It grew in v0.21.0, when the two libsecp patches in §2 item 2
became part of the canonical guest. Items 1–5 are complete; of item 6, (a) and (b) are done.

## 7. Known open issues (security)
There has been no commissioned audit. `SECURITY.md` records nine rounds of self-audit and two AI-assisted
full-source reviews by people outside the project (rounds 10 and 11); neither found a soundness break in
the guest, the verifier or the coordinator frontier, and neither is a professional audit. The two items
this section used to list as open — negative regression tests and BIP68-time metadata — are done
(`SECURITY.md`, open items 1 and 2). What remains:

- **Independent audit**, especially of the non-Core code in §2 — the accumulator, the coinbase SMT, the
  `field_bigint2` backend and the `lift_x` hint — and of the recursion binding. Rounds 10 and 11 predate
  both libsecp patches.
- **A corrupt-signature negative control on the shipped CORE guest** (`docs/FIELD_BIGINT2_BACKEND.md`
  §5b, gate 4). The byte-flip control in `fuzz-native/realvector.cpp` runs natively on libsecp's stock
  field backend, and the negative corpus has no signature case.
- The standalone `build_full` real-MTP anchor (§5), which matters only for proving an isolated real
  time-locked block.

Read `SECURITY.md` before relying on any "undeniable" framing.
