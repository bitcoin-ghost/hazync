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
| H1 | Block height host-controlled (flag/subsidy downgrade) | **critical** | **fixed + negative-tested** (`w.height == prev.height+1`; `host adversarial` #1) |
| H2 | Segmented chunks bound neither flags nor spending witness | **critical** | **fixed + negative-tested** (per-input binding digest; `HAZYNC_H2_BADHEIGHT` prove-seg) |
| H3 | In-block coin double-spend / ordering (inflation) | **high** | **fixed + negative-tested** (ordered multiplicity guard; `host adversarial` #3) |
| H4 | Coinbase never run through `CheckTransaction` | med | **fixed + negative-tested** (`host adversarial` #4) |
| H5 | Multi-input tx: non-`input_idx` fee-prevouts unbound to the accumulator (inflation/theft) | **critical** | **fixed + negative-tested** (per-tx input-list pre-pass; `host adversarial` #5) |
| H6 | Range verifier under-pinned the genesis in-boundary (`in_epoch_start`/`in_roots`/`in_recent`) → forgeable first retarget / phantom UTXO seed | high | **fixed** (`assert_genesis_in_boundary` in `verify-range`; `verify-any` applies it when the range claims genesis) |
| H7 | Coordinator chained ranges by tip-hash only — no cross-range difficulty/MTP continuity | medium | **fixed** (`verify-any` now pins the genesis in-boundary + exposes nbits/epoch; coordinator `_frontier_chain` requires `out_nbits/out_epoch(k) == in_nbits/in_epoch(k+1)` across every seam) |
| H8 | Cross-mode journal laundering: `block_proof` (mode 1) commits a self_id-free journal that never aborts | speculative | **fixed** (domain tag `KIND_*` is the first committed field of every recursion-consumed journal — `ChainState`/`RangeState`/`ChunkOut` — and asserted on every decode) |
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

## Fixed 2026-07-17 — deeper adversarial pass (H1–H4: binding the proof to the block)

A second adversarial review looked specifically for ways a mining-capable prover could make an invalid
block prove valid, and found four under-constraints between the (real, correct) Core code and the block
being proven. All four are fixed; each has a negative test that must reject, alongside an honest
baseline that must still be accepted. The execute-mode cases run self-contained via `host adversarial`
(and in CI); the segmented one runs on a GPU box.

### H1 (critical) — block height was host-controlled
`chain_step`/`aggregate` committed `height = prev.height + 1` but validated the block using the
host-supplied `w.height` with no equality check. Height selects the script flags and the coinbase
subsidy, so `w.height = 1` turned every soft-fork flag off (segwit/taproot outputs become
anyone-can-spend) and set the subsidy to 50 BTC, while the journal still committed the true height.
**Fix:** assert `w.height == prev.height + 1`. **Test:** `host adversarial` (#1). The range-fold path
was already bound by its genesis pin + adjacency check.

### H2 (critical) — segmented chunks bound neither flags nor the spending witness
A chunk proof committed only the coin leaf, so the aggregation could accept a *different* valid spend of
the same coin, or the spend validated under attacker-chosen weaker flags. **Fix:** each chunk commits a
binding digest over `(raw_tx, input_idx, prevouts, coin metadata, flags)`; the aggregation recomputes it
under the block's real flags and requires equality. **Test:** `HAZYNC_H2_BADHEIGHT=1 host prove-seg`
(aggregate rejects).

### H3 (high, inflation) — in-block coins had no double-spend / ordering guard
A coin created earlier in the same block bypasses the accumulator; the set that tracked it did not count
multiplicity or check ordering, so it could be spent twice (minting its value) or before it was created.
**Fix:** enforce creation by a strictly earlier tx and spend-at-most-once. **Tests:** `host adversarial`
(#3 double-spend, #3 spend-before-create).

### H4 (medium) — the coinbase never ran through CheckTransaction
The coinbase reached only the subsidy/BIP34/witness-commitment checks, never `CheckTransaction`, so a
malformed coinbase (bad-cb-length, out-of-range or overflowing output sum) could pass. **Fix:** run the
coinbase through real Core `CheckTransaction` plus an `IsCoinBase` assertion. **Test:** `host
adversarial` (#4).

## Fixed 2026-07-17 (round 2) — re-audit of the patched code (H5–H8)

A second adversarial pass (three reviewers) attacked the H1–H4 fixes and swept the rest. H2/H3/H4 held
up; H1 held on the folded path. It found one more critical and a cluster of range/coordinator anchoring
gaps.

### H5 (critical) — multi-input fee-prevouts were not bound to the accumulator
Each `BlockInput` carries its own `prevouts` blob, but only the entry at its `input_idx` is authenticated
(folded into the leaf + `stump.delete`). `check_tx` runs once per tx on the first input's blob and sums
**every** entry into the fee. So for a ≥2-input tx a prover puts a phantom high-value coin at another
index of the first input's blob — never authenticated — inflating the fee to ~21M BTC and minting it via
the coinbase (a sibling variant omits a `BlockInput` to skip a script check + a deletion → theft /
double-spend). **Fix:** a pre-pass ties the flat input list to each tx's real `vin` — exactly
`tx_vin_count` consecutive `BlockInput`s, sequential `input_idx`, one shared `raw_tx` + `prevouts` blob —
so every entry `check_tx`/sigops read is an authenticated coin. **Test:** `host adversarial` (#5), with an
honest 2-input baseline that must still pass.

### H6 (high) — range verifier under-pinned the genesis in-boundary
`verify-range` pinned `in_tip`/`in_leaves`/`in_nbits` but not `in_epoch_start` (feeds the block-2016
retarget, propagates across fold seams → forgeable difficulty), `in_roots` (`in_leaves==0` alone permits
phantom roots), or `in_recent`/`in_time`. **Fix:** `assert_genesis_in_boundary` pins the full genesis
boundary; `verify-any` applies it whenever a range claims to connect to genesis.

### H7 (medium) — coordinator cross-range continuity — FIXED
`server.py` chained verified ranges by tip-hash only, so `in_nbits`/`in_epoch_start` of range k+1 weren't
checked against range k's `out_*` (a range could claim an easier `in_nbits` and be mined cheaper).
**Fix:** `verify-any` pins the genesis in-boundary and now prints `in_nbits/out_nbits/in_epoch/out_epoch`;
the coordinator's `_frontier_chain` walks ranges from genesis requiring `out_nbits/out_epoch(k) ==
in_nbits/in_epoch(k+1)` across every seam (deploy: `vranges` gains those columns + a redeploy).

### H8 (speculative) — cross-mode journal laundering — FIXED
`block_proof` (mode 1) commits a self_id-free `BlockOutput` and never aborts; in principle a mode-1
receipt could be laundered as a fake `prev` if its bytes decoded as a `ChainState` with trailing
`self_id == METHOD_ID`. No exploit was constructed (the type-mismatched decode makes it very hard).
**Fix:** every recursion-consumed journal (`ChainState`, `RangeState`, `ChunkOut`) now commits a distinct
domain tag (`KIND_CHAIN`/`KIND_RANGE`/`KIND_CHUNK`) as its first field, and every consumer asserts it —
so a journal of the wrong type can never be laundered across modes.

## Fixed 2026-07-17 (round 3) — re-audit of the H1–H8 code

A third pass (three reviewers: bypass H5–H8, consensus-flag surface, trust boundary). H5/H6/H8 and the
genesis-catch were attacked and held. New findings, all fixed:

### Script-flag / activation layer (guest — these were mostly *reject-valid*, i.e. the from-genesis prover would STALL on canonical blocks)
- **H-S1 (high):** `block_script_flags` ignored Core's `script_flag_exceptions`. Core forces P2SH|WITNESS|TAPROOT
  on for all blocks *except* two historical violating blocks (BIP16 → no flags; Taproot ~709632 → no TAPROOT).
  The guest enforced TAPROOT everywhere and would permanently stall on the Taproot-exception block. **Fix:**
  rewrote `block_script_flags` to match Core's `GetBlockScriptFlags` exactly — always-on base + a block-hash
  exception table (hash passed to `chunk_prove` too, bound via the H2 digest) + buried deployments.
- **H-S2 (high):** BIP34 enforced from 227836; Core's `BIP34Height` is 227931 → guest rejected valid blocks
  in that 95-block window. **Fix:** 227931.
- **H-S3 (medium):** BIP68 relative-locktime enforced with no CSV gate; Core only enforces it from 419328.
  **Fix:** gate the relative-lock branch on `spend_height >= 419328`.
- **H-S4 (low, accept-invalid):** the old height-gated P2SH/WITNESS/TAPROOT were *more lenient* than Core below
  the gates (a proof there didn't imply Core-validity). Closed by the same always-on rewrite (H-S1).

### Coordinator trust boundary (host + Python — no guest change)
- **S1 / F1 (high):** the coordinator chained ranges on a WEAKER seam check than the guest fold — it matched
  tip-hash (and, after H7, nbits/epoch) but **not the UTXO accumulator roots, `in_time`, or the MTP window**.
  A mid-chain range could fabricate its in-boundary UTXO set (spend non-existent coins / double-spend) or its
  `in_time` (forge a 4× easier retarget) with a valid STARK, and be spliced into the frontier. **Fix:**
  `verify-any` computes a **full boundary digest** (`boundary_digest`: tip + normalized UTXO roots + leaves +
  nBits + time + epoch + MTP window) — exactly what `fold_range` binds — and the coordinator's `_frontier_chain`
  requires `out_bhash(k) == in_bhash(k+1)` across every seam. Not live-exploitable before (frontier below the
  first retarget, single prover), now closed.
- **F2 (low):** `_frontier_chain` used an unordered SELECT with first-wins; added `ORDER BY lo, ts` and the
  full-boundary digest makes a preempting range have to match the real boundary anyway.
- **F3 (low):** rows with no boundary digest (pre-migration NULL) are no longer chainable.
- **S2 (medium foot-gun):** `VERIFY_MODE` defaulted to `mock` (accept-everything) when `HAZYNC_HOST` was unset.
  **Fix:** mock now fails closed unless `COORD_ALLOW_MOCK=1`.
- **S3 (low):** `verify-any` output was scraped from stdout+stderr; now only the single `RANGE-OK` stdout line.
- **Signature fail-closed:** `verify_sig` accepted everything when the ed25519 lib was missing; now fails closed
  unless `COORD_ALLOW_UNSIGNED=1`.

Verified sound (attacked, no action): genesis constants (`GENESIS_WORK` etc. checked against real block 0),
retarget/MTP/PoW math, weight/sigop formulas, taproot/annex path, test-only env hooks (guest reads no env),
`METHOD_ID` handling. `regress` + full `adversarial` suite + honest segmented composition pass on the round-3 guest.

## Round 4 (2026-07-17) — no soundness break found; hardening only

A fourth pass (three reviewers: C++/Core integration + patches, Utreexo accumulator + primitive math,
whole-chain no-inflation + UTXO carry) found **no new soundness hole**. Confirmed sound: the `VerifyScript`
invocation (correct precomputed data / amount / sigversion for legacy/segwit/taproot), ECDSA is real
libsecp256k1 (the k256 acceleration experiment has since been removed — the guest is pure Core), the SHA-256 accelerator is byte-identical, the
serialize shim is consensus-neutral, static-ctor tagged-hash init covers all paths, the accumulator
delete/proof handling and `num_leaves`/root recomputation, all primitive math (`check_pow`/`SetCompact`,
`add_work`, `calc_next_bits` clamping, subsidy halving, merkle root), global no-inflation, and UTXO carry
(no resurrection; `w.new_outputs`/`w.wtxids`/`inp.flags` confirmed dead/unused). Hardening applied:

- **Bench backdoor fenced.** `verify_input` short-circuited to a fixed test-vector ECDSA result for two
  magic flag values (`0xB0`/`0xB1`) — a "return valid" path, unreachable in consensus (`block_script_flags`
  never yields those) but now compiled out unless `HAZYNC_ECDSA_BENCH` is defined.
- **`MiniReader` fails closed.** `read`/`ignore` now trap on any read past the buffer end (was an unchecked
  `memcpy` — OOB-read only, never accept-invalid, but now a clean rejection).
- **Reference-spec doc corrected.** `accumulator/src/lib.rs` now states the guest `utreexo.rs` adds the
  SEC-2 pinning the reference oracle lacks (the proven guest is the authority).

Documented, not changed (non-exploitable, deliberately deferred to avoid churn/re-prove): a trailing-byte
`r.p==r.e` parity assert (trailing bytes are inert — txid is PoW-bound); explicit leaf/internal-node hash
domain tags (implicit separation already holds because every leaf preimage begins with an uncontrollable
txid); and removing the dead `new_outputs`/`wtxids`/`inp.flags` witness fields.

## Round 5 (2026-07-17) — no new hole; one trivial fix + one documented gap

Fifth pass (three reviewers: a regression-hunt inside the fixes, a fresh full-surface sweep, and a
docs/web-page currency check). The regression hunt traced every H1–H8 + flag/coordinator fix against
Core v28 and found **no regression** — the fixes are correct as written. Net: one trivial fix, one
documented gap, one false alarm, plus a large docs-currency pass.

- **F2 (low, accept-invalid) — FIXED.** Block weight omitted Core's `4*(80 + CompactSize(ntx))`
  header + tx-count term, so a block could sit up to ~324 WU over `MAX_BLOCK_WEIGHT`. No inflation;
  now matches Core's `GetBlockWeight`.
- **F1 (flag always-on "diverges from Core") — NOT A BUG.** Two independent reviewers and Core's own
  `GetBlockScriptFlags` (read on the box) confirm the base P2SH|WITNESS|TAPROOT is always-on with exactly
  the two exception blocks — the exact code a from-genesis IBD runs (Core itself would stall otherwise).
  Height-gating would be the accept-invalid behaviour already flagged as H-S4. No change.
- **F3 (low, pre-BIP34 BIP30 duplicate-coinbase overwrite) — FIXED + tested.** The two historical
  duplicate-coinbase blocks (91842 / 91880, below the BIP34 height) have *distinct* accumulator leaves
  (the leaf commits height, so no collision and the "collision-free" claim holds), but pre-enforcement Core
  **overwrites** the old outpoint whereas the guest kept both, leaving one extra leaf Core discards — which
  a from-genesis prove crossing height ~91842 could later spend. **Fix:** at exactly those two block hashes
  the guest now deletes the superseded coinbase leaf, recomputed from *this* block's coinbase at the
  host-supplied old height/mtp (the duplicate coinbase is byte-identical), so the delete can only remove a
  genuine earlier duplicate of this coinbase's outpoint, and it is *mandatory* at those hashes (a prover
  cannot skip it). BIP34 (enforced from 227931) makes coinbases unique thereafter, so no later duplicate can
  occur. **Test:** `host check-bip30` on real block 91842 — honest overwrite accepted with a matching root,
  skipping it rejected, wrong old-height rejected. In CI.
- **Docs currency:** rewrote the stale `PROVING.md` (it described recursion as unimplemented and handed
  out a pre-hardening `chain_step`), corrected the README `ACCELERATION` repo-map line + status/audit
  language, `HAZYNC_ARCHITECTURE.md`'s BIP34 height (227836→227931), annotated the stale 741000 evidence log
  (402→394), added a working-notes banner to `HAZYNC_ARCHITECTURE.md`, and updated the live page's
  self-audit copy to the four-round history.

## Round 6 (2026-07-18) — one MAJOR soundness finding (H9) + web/liveness + completeness

Sixth pass (three reviewers: guest consensus, host witness-binding, coordinator seam). The soundness
core held (H1–H8 / SEC / F1–F3 all survived concrete attack), but the host reviewer found one genuine
MAJOR hole in the coordinator seam.

- **H9 (MAJOR, over-issuance / weak-flags splice) — FIXED.** The coordinator chains independently-verified
  ranges on tip-hash + full `boundary_digest` continuity, but the digest bound the UTXO set / difficulty /
  MTP window and **not the block height**, and `prove_range` never tied `w.height` to the in-boundary's
  chain position. So a block mined onto the *real* tip (real prev-hash, real UTXO root, real current
  difficulty ⇒ real PoW) but **labelled a false low height** would have `block_subsidy(low)` = up to 50 BTC
  (over-issuance) and `block_script_flags(low)` omitting DERSIG/CLTV/CSV/NULLDUMMY (a script invalid at the
  true height validates) — a self-inconsistent `(height, boundary)` pair the guest never rejects standalone.
  The guest `fold_range` rejects it (`hi+1==lo` adjacency); the coordinator's non-folding chain did not, so
  it could splice into the genesis-anchored frontier composite. **Fix (defence-in-depth, two independent
  layers):** (1) coordinator `_frontier_chain` enforces `lo==1` at genesis and `lo==prev_hi+1` at every seam;
  (2) the host `boundary_digest` now binds height (out-boundary = `hi`, in-boundary = `lo-1`) from the
  in-circuit-committed `RangeState.lo/hi`, so `out_bhash(k)==in_bhash(k+1)` chaining *structurally* requires
  adjacency. Either layer alone closes it. Verified: a mislabeled-height range no longer advances the
  frontier while honest contiguous + out-of-order gap-fill still do.
- **Coordinator hardening (web/liveness).** Stored XSS via the contributor `handle` rendered into the public
  dashboard through `innerHTML` — fixed at both layers (`clean_handle` strips `< > & " '`; the dashboard
  escapes every render sink). Liveness DoS — the up-to-120 s `verify-any` ran inside the global write lock,
  stalling every claim/heartbeat/submit — moved out of the lock (re-check status on commit; unique temp
  paths). Stat double-count — `proven` summed overlapping ranges — replaced with an interval-merge.
- **Completeness / robustness (guest + host).** Added the **nVersion soft-fork rejection** (Core
  `ContextualCheckBlockHeader`: reject `v<2 @227931`, `v<3 @363725`, `v<4 @388381`) closing an accept-invalid
  gap; asserted `header.len()==80`; bounds-guarded `check_input_locks`' `input_idx` (fail-closed `-43`).

Validated in execute mode with **no regression**: `regress` (block 170 byte-exact), `adversarial` (all
holes reject), `check-full` 741000 (byte-exact tip, UTXO 394), `check-bip30`, and an nVersion negative that
rejects. `METHOD_ID` changed (guest changed) ⇒ prior proofs invalid, re-proven from genesis.

## Round 7 (2026-07-18) — re-audit of the round-6 fixes; no new soundness hole

Seventh pass (three reviewers over commit `a094ae5`: coordinator changes, guest/host changes, holistic
H9-closure sweep). All three **clean on soundness**. The sweep confirmed H9 is closed on all five proof
paths (`chain_step`, `aggregate`, `chunk_prove`, `prove_range`, `fold_range`) with the two fix layers each
independently sufficient. One genuine new-in-round-6 **minor** was fixed: the lock-free `submit()` had no cap
on concurrent `verify-any` subprocesses (fan-out DoS on the small box) — bounded with a `Semaphore(cpu_count)`.
Two other observations (the `0-999` seed range is unprovable by design so blocks 1–999 are proven as
single-block ranges; `by_in` first-wins can understate the frontier) are pre-existing and non-soundness.

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
- **H1** — a committed `.pyc`; **H2 — resolved:** all three `Cargo.lock` files (`accumulator/`,
  `prover/`, `prover/methods/guest/`) are now committed for reproducible builds; **H3** — README
  refresh. Housekeeping.

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
