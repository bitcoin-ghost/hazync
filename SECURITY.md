# Hazync — security review & status

**This is an internal self-review, not an external audit.** No third party has audited Hazync yet.
The findings below were surfaced by our own adversarial passes over the guest/host code; we fix them,
re-run the regression to identical results, and record them here in the open. **Independent review is
explicitly invited** — the open items at the bottom are the starting bounty list. If you find a way to
make an invalid input prove valid, that is the finding that matters most.

The property that makes this worth reviewing: the prover runs **real Bitcoin Core v28 consensus code**
(unmodified `interpreter.cpp`, `SignatureHash`, `libsecp256k1`) inside a RISC0 zkVM, plus a Utreexo
accumulator. There is no consensus reimplementation to diverge from Core — see `SOUNDNESS.md` for the
full trust base and the two portability shims.

## Status at a glance

| ID | Area | Severity | Status |
|----|------|----------|--------|
| SEC-1 | Witness-commitment bypass (`has_witness` host-controlled) | med-high | **fixed** (6c63565) |
| SEC-2 | Accumulator `delete` trusted an unverified position | high-crit location | **fixed** (6c63565) |
| SEC-3 | Prevouts vector length unchecked (OOB read) | low | **fixed** (6c63565) |
| S1 | Recursion `self_id` self-reference argument | soundness | **fixed** (committed + verifier-asserted; adversarial wrong-id chain rejected) |
| S2 | Coinbase maturity / BIP68-height fed placeholder metadata | soundness | **fixed** (real coin metadata; validated on 741000) |
| S4a/b | BIP34 (height in coinbase), BIP30 (duplicate txid) | completeness | **fixed** (validated on 741000) |
| C1 | No automated regression harness | quality | **fixed** (`check-full` / `regress` execute-mode) |
| SEC-neg | Negative regression tests for SEC-1/2 | quality | **done** — SEC-1 witness test (corrupted-witness block rejected on `witness_ok`) + SEC-2 position test (inconsistent-position spend rejected on `all_ok`/`root_matches`) |
| S3 | Standalone block proofs don't bind to the real UTXO set | inherent | **by design** — real binding comes from the chain recursion; closed operationally by the archive-node bridge |
| BIP68-time | Block-proving path now commits real `MTP(coinHeight−1)` | **soundness** | **fixed** (validated; check also proven on a real mainnet tx) |
| COV-1 | `time-too-old`: block timestamp must exceed MTP(prev 11) — was unchecked | **soundness** | **fixed + negative-tested** (asserted in chain_step/aggregate/prove_range) |
| COV-2 | Merkle CVE-2012-2459 mutation flag was discarded (`nullptr`) | **soundness** | **fixed + negative-tested** (capture Core's `mutated` and reject) |
| — | External audit | — | **open / wanted** |

## Fixed 2026-07-16 — adversarial pass over the guest (SEC-1/2/3)

All three were validated by rebuilding the guest and re-running the full regression (block 170, block
741000, `check-ibd` genesis→550) to **byte-identical** tip hashes — i.e. the fixes change nothing on
valid data, they only reject the malicious cases they close.

### SEC-1 (med-high) — `has_witness` was host-controlled ⇒ BIP141 witness-commitment bypass
The guest derived `has_witness` (and the wtxids) from host-supplied input. A malicious prover could
claim "no witness" for a segwit block with a missing or invalid witness commitment and have it prove
valid, even though Core rejects it — and it opened a witness-malleability divergence.
**Fix:** recompute `has_witness` *and* every wtxid in-guest from the raw transaction bytes, using
Core's own `HasWitness()` / `GetWitnessHash()`. The host can no longer influence the witness-commitment
decision. Block 741000 (segwit+taproot) still proves valid with an identical tip hash and 394 UTXO
leaves.

### SEC-2 (high-criticality location) — accumulator `delete` trusted an unverified position
`delete(i, proof_i, proof_last)` verified *membership* of the proven leaves but never checked that the
global index `i` actually matched them, nor that `proof_last` was the current rightmost coin. Fed
inconsistent values, a prover could corrupt the accumulator — the worst case being a spent coin
surviving (a double-spend). No working exploit was built, but the assumption was untested.
**Fix:** pin `i` to the proven leaf — its tree height must equal the proof's, and its local offset
(`i − tree_offset`) must equal `proof_i.position` (the *local* in-tree index) — and likewise pin
`proof_last` to `last`. (Subtlety: `Proof.position` is the local index, not the global one; a first
attempt that compared against the global `i` broke honest deletes at block 170 and was corrected.)

### SEC-3 (low, robustness) — prevouts vector length unchecked
`verify_input` / `check_tx` / `tx_full_sigops` indexed `spent[...]` without asserting
`spent.size() == tx.vin.size()`; a short blob is an out-of-bounds read (the zkVM has no memory
protection). Failed closed in practice.
**Fix:** explicit length asserts on the prevouts vector in all three entry points.

## Earlier findings (2026-07-15 self-audit) — status

- **S1 — recursion `self_id` is host-supplied.** The chain/aggregation guests call
  `env::verify(self_id, prev_journal)` with a host-controlled `self_id`; the concern is the IVC
  self-reference trap (a nested `self_id ≠ METHOD_ID` smuggling a malicious guest's receipt). **Fixed:**
  `self_id` is committed and the verifier asserts `== METHOD_ID` at every level; the positive chain
  verifies and an adversarial wrong-id chain is rejected. Argument written up in `SOUNDNESS.md §3`.
  Single-block proofs were always unconditional here; this hardened the recursive case.
- **S2 — maturity / BIP68-height fed placeholder metadata.** The harness set every spent coin's
  `coin_height`/`coin_is_coinbase`/`coin_mtp` benign, so maturity + BIP68 never fired on real blocks.
  **Fixed:** the fetcher/bridge sources each spent coin's real height + coinbase flag and threads them
  into the witness; both checks fire on real blocks (validated on 741000). While closing this we also
  found and fixed a latent header bug — the header builder hardcoded version 1 (masked by the
  version-1-era test vectors), so PoW was wrong on modern blocks; it now uses the real versionbits.
- **S3 — standalone block proofs don't bind to the real UTXO set.** `prove_full` fabricates `root_prev`
  from the block's own prevouts + filler, so a standalone proof attests *internal validity +
  accumulator consistency*, not "these coins were in mainnet's UTXO set at height N". **Not a bug —
  inherent to standalone testing.** Real-UTXO binding comes from the chain recursion carrying
  `root_next(N−1) == root_prev(N)` from a trusted anchor; operationally the archive-node bridge drives
  the accumulator from the real coin set. Stated plainly so results aren't over-read.
- **S4 — BIP34 / BIP30.** Both **added and validated on 741000** (`bip34_ok` / `bip30_ok`). BIP68-time
  remains the one open **soundness** item (the placeholder `coin_mtp = 0` is a false-accept for that
  rule, not conservative — needs real `coin_mtp`, free with the bridge).
- **C1 — regression harness.** **Added:** `check-full` / `regress` run known blocks + the adversarial
  inflation case in execute mode (seconds, no proving) and assert the flags; standard pre-flight before
  any GPU prove.
- **C2 — duplication** between `build_block`/`build_full` and `chain_step`/`aggregate` (the witness_ok
  conjunction drifted once). Low priority; a shared helper would prevent re-drift. **Open, cosmetic.**
- **C3 — fabricated anchor timestamps/nbits** in standalone runs — resolved by real recursion (tied to
  S2/S3). **Open only for near-retarget standalone runs.**
- **H1** — a committed `.pyc`; **H2** — `Cargo.lock` gitignored (consider committing for reproducible
  builds); **H3** — README refresh. Housekeeping.

## Coverage audit (2026-07-16)

A pass over Bitcoin Core's block-validation surface for rules we might not enforce found two — both
real consensus rules Core checks, both now fixed (see the COV rows above):
- **COV-1 `time-too-old`:** a block whose timestamp is ≤ the median-time-past of the previous 11 blocks
  is invalid; `chain_step`/`aggregate`/`prove_range` now assert `block_time > prev_mtp`.
- **COV-2 merkle mutation (CVE-2012-2459):** the `ComputeMerkleRoot` call discarded Core's `mutated`
  flag (duplicate-txid tree malleability); it is now captured and rejected.

Deliberately **not** enforced (documented trust boundaries, not gaps): the 2-hour future-time limit
(node-local wall-clock, unprovable); standardness/policy (not consensus). "Only the coinbase is a
coinbase" is covered by Core's `CheckTransaction` (null-prevout rejection for non-coinbase inputs).
The COV fixes were validated for no-regression (check-ibd 550 + 741000 + demo all still VALID) **and by
adversarial negative tests** (`prover/test_cov_negatives.sh`, evidence `prover/evidence/cov_negatives.txt`):
- COV-2: an honest `[A,B,C]` tx list and a malleated `[A,B,C,C]` (last tx duplicated) produce the
  *identical* merkle root — the CVE-2012-2459 collision — but the malleated one is flagged `mutated=1`
  and rejected on `merkle_ok`.
- COV-1: with the previous-11 median-time-past forced to equal the block's own timestamp, `check-full`
  rejects with `time_ok=false` and every other flag true (isolated to the timestamp check); the same
  block without the knob is VALID.

## Open items (the review bounty list)

1. **SEC-neg — DONE.** Negative regression tests proving the fixes *reject* the malicious cases (not
   just that valid blocks still pass). Both halves below now demonstrate rejection.
   - **SEC-1 (witness) — done.** `prover/make_negative_tests.py` produces `block_741000_badwit.json`
     (one byte flipped inside a transaction's witness → wtxid changes, txid does not). `check-full`
     reports `merkle_ok=true, witness_ok=false, all_ok=false` — the block is rejected specifically on
     the BIP141 witness commitment, confirming the check is enforced and unskippable.
   - **SEC-2 (position) — done.** A test-only host knob (`HAZYNC_SEC2_BADPOS=1`) corrupts the first
     spend's `global_pos` to a different in-range index while leaving its inclusion proof honest — the
     exact inconsistency the normal path can't express (both fields derive from the same accumulator
     lookup). `check-full` on block 170 then reports `all_ok=false, root_matches=false` with every
     other flag true, isolating the rejection to the hardened `delete`'s position check. Without the
     knob the same block is VALID. The knob is inert unless the env var is set. See
     `prover/make_negative_tests.py`.
2. **BIP68 time-based (soundness) — FIXED.** The correct value is Core's `GetMedianTimePast(coinHeight−1)`.
   The block-proving path previously committed the creating block's raw timestamp (or `0`), skewing the
   required-elapsed test so a premature time-based relative-locked spend could prove valid.
   - **The check is proven correct on REAL mainnet data.** `prover/test_bip68_real.sh`
     (evidence `prover/evidence/bip68_real_mainnet.txt`) runs the real `check_input_locks` on a real
     mainnet tx — `3fa669af…` in block 958250, a 90-day Taproot CSV lock — with the real
     `coin_mtp = MTP(945408)` and `spend_mtp = MTP(958250)`. The coin is 90.2 days old, mainnet accepted
     it, and the check returns VALID; a coin ~0.3 days younger is REJECTED (`-42`).
   - **The proving path now commits the real value.** A coordinated host+guest change: the guest's
     `validate_block` commits `mtp` (= `median(prev.recent_times)` = `MTP(h−1)`) on created-output
     leaves, and every host builder derives the same value from the chain it has processed (the
     `block_mtp` window in the IBD path, mirroring what an archive node holds for free) — so the
     creation-side and spend-side leaves match. Validated: `check-ibd` genesis→550, `check-full` 741000,
     and the 170→172 chain demo all remain VALID with **identical** tip hashes (those are header-derived;
     only the internal leaf MTP is now correct). No fetcher dependency for the IBD path — the host derives
     the MTP itself.
3. **External audit** — especially of the accumulator (the one non-Core component) and the recursion
   binding. Wanted.

## TL;DR
The hard part — proving the *real* Core consensus code, not a reimplementation — is done and is the
thing that removes the soundness gap every prior effort carried. The findings so far are around the
edges (a host-controllable witness flag, an under-constrained accumulator index, placeholder metadata,
a couple of missing rules) and are all fixed and regression-checked. What remains is negative tests,
one time-lock input from the bridge, and — the real ask — independent adversarial review.
