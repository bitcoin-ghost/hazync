# Hazync — soundness & completeness statement

The document a reviewer should read first. States exactly what a Hazync proof guarantees, under what
trust assumptions, which consensus rules are enforced vs open, and the argument for each subtle point.
Written to pre-empt critique, not to hide it.

## 1. The claim
A verified Hazync **chain-tip proof** attests: *every block from the anchor to the tip is valid under
Bitcoin Core consensus, the UTXO set is exactly the committed root, and cumulative proof-of-work is as
committed* — checkable by verifying one succinct proof, without re-executing history or trusting peers.

## 2. Trust base (what the soundness rests on)
1. **Real Bitcoin Core v28 code.** The script interpreter, sighash, and libsecp256k1 are the *actual*
   Core sources compiled into the zkVM — not a reimplementation. Only two portability shims
   (`serialize.h` 32-bit int overload; SHA256 routed to the RISC0 accelerator, byte-identical) and
   libc/unwinder glue. **No consensus-logic changes.** This is the property that removes the
   reimplementation-soundness gap every prior effort carries. Auditable: `patches/000{1,2}` + the TU
   list in `prover/methods/guest/build.rs`.
2. **SHA-256 collision resistance** — the accumulator (Utreexo) and merkle/commitment checks are
   SHA-256; that's their entire security. The accumulator is *our* code but it's a commitment ABOVE
   consensus, not a Core reimpl — exhaustively tested (`accumulator/src/lib.rs`).
3. **RISC0 zkVM soundness** — the STARK/SNARK proving system. Standard cryptographic assumption.
4. **The anchor checkpoint** — the chain proof starts from a trusted state (genesis, or a trusted
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
- **Hardening (spec, to implement + prove-verify on a box):** commit `self_id` into the journal of
  every recursive step and have the top-level verifier assert `committed_self_id == METHOD_ID` at every
  level (or, equivalently, pin the id: the guest hardcodes the expected `METHOD_ID` digest once it's
  known and rejects any other). Either makes the self-reference explicit and checkable. **Until this is
  in, treat recursive proofs as "sound under an honest prover"; single-block proofs are unconditional.**
- This is the standard IVC concern; the fix is small and localized to `chain_step`/`aggregate` + the
  verifier. Tracked as the #1 pre-production item.

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
- No inflation: Σin ≥ Σout per tx; coinbase ≤ subsidy(height)+Σfees (subsidy = exact halving formula).
- PoW (`CheckProofOfWorkImpl`, real arith_uint256) + difficulty retarget rule.
- Merkle root; **BIP141 witness commitment** (new).
- Block weight ≤ 4M; full sigop cost ≤ 80k (legacy + P2SH + witness).
- Absolute locktime (`IsFinalTx`); BIP68 relative locktime (height + time) — *logic present*.

- **Coinbase maturity + BIP68 (height)** — CLOSED (S2, 2026-07-15). The fetcher/bridge now sources each
  spent coin's real `coin_height` + `coin_is_coinbase` and threads them into the witness (`build_full`,
  shared by prove-full/seg/chunk/agg). Both checks fire on real blocks; validated on block 741000
  (`all_ok=true` with real metadata). `coin_mtp` (BIP68-time) is a **placeholder (0)** in the current
  fetcher harness — and this is a genuine **soundness gap for the BIP68-time rule**, not a conservative
  one: with `coin_mtp = 0` the required-elapsed test collapses to `spend_mtp ≥ (relative seconds ≤ ~388d)`,
  which any real block MTP satisfies, so a block that spends a coin *too early* under a time-based
  relative lock would prove valid even though Core rejects it. Latent (no block tested contains a
  time-based relative lock, which are rare), but real until the archive-node bridge threads the real
  creation-MTP (see §6). Height-based BIP68 + maturity + absolute locktime are live now.
- **BIP34** (coinbase scriptSig encodes height) — CLOSED (S4a). `check_bip34` parses coinbase vin[0]
  scriptSig and compares the pushed height to the block height. Validated on 741000 (`bip34_ok=true`).
- **BIP30** (no duplicate txid overwriting an unspent output) — CLOSED (S4b). Explicit sorted-txid
  duplicate check in the guest. Validated on 741000 (`bip30_ok=true`).

OPEN (each with fix; none architectural):
- **BIP68 time-based** needs real `coin_mtp` — a **soundness gap** (false-accept) for that rule, not a
  conservative placeholder: see the §5-ENFORCED note above. **MTP** in standalone runs also uses
  placeholder timestamps. Both are closed for free by the archive-node bridge (real creation-MTP + real
  `recent_times` per block during IBD). Until then, a block with a premature time-based relative-locked
  spend is not caught.

## 6. What "solid" requires from here (ordered)
1. ✅ **S1 recursion hardening** — self_id committed + verifier asserts `==METHOD_ID` at every level;
   positive chain VERIFIED, adversarial wrong-id chain REJECTED (proven on a box).
2. ✅ **S2 real coin height/coinbase** threaded from the bridge — maturity + BIP68-height live on real
   blocks (validated on 741000). (BIP68-time/`coin_mtp` completes with the archive-node bridge.)
3. ✅ **BIP34 + BIP30** checks added and validated on 741000.
4. ✅ **Execute-mode regression** (`regress`, `check-full`) — block 170 chain_step tip-match + adversarial
   inflation; runs in seconds, no proving. `check-full` is the standard pre-flight before any GPU prove.
5. ✅ **Full segwit+taproot block** (741000, 670 inputs) proven end-to-end on GPU: 16 chunk STARK
   receipts → aggregate, receipt VERIFIED, tip hash byte-matches mainnet. Exercised the BIP141 witness
   commitment, BIP34/BIP30, and real maturity/BIP68 for real. Also fixed a latent header bug: the
   header builder hardcoded version 1 (invisible on the version-1-era test vectors 100000/130000/140000);
   now threads the real versionbits value, so PoW is correct on modern blocks.
6. **Next → SCALING.md**, reprioritised by the 741000 run: (a) **succinct chunk receipts** — the 1645s
   aggregate was dominated by verifying 16 *composite* receipts in-guest; succinct receipts make that
   cheap and fixed-cost (biggest single win). (b) **Archive-node bridge** (hazync-during-IBD) — replaces
   the explorer fetcher, gives real coin metadata + MTP for free, and closes S3 (real-UTXO binding).
   (c) **Parallel backfill** across a GPU fleet → tree fold.

The §2 trust base is untouched — the hard part (real Core code proving) is done and unimpeachable.
Items 1-5 are complete; item 6 is the scaling roadmap.

## 7. Known open issues (security)
This is a self-review, not an external audit. The living security status — including three findings
(SEC-1/2/3) found and fixed in a 2026-07-16 adversarial pass, plus the remaining open items (negative
regression tests, BIP68-time metadata, and the standing request for independent audit) — is tracked in
**`SECURITY.md`**. Read it before relying on any "undeniable" framing.
