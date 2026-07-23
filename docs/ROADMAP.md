# Hazync roadmap

The state, the open work, and the order to do it in. Hazync proves Bitcoin Core's real consensus code
in a zkVM; the method is built and demonstrated on real mainnet data, and what remains is (1) closing
self-found soundness gaps, (2) presenting it credibly, (3) external review, and (4) actually producing
the full-chain proof. Status markers: `[ ]` open, `[~]` in progress, `[x]` done.

## 1. Security fixes — from the self-audit (see `SECURITY.md`)

These are the findings from an adversarial pass over the guest. They must be fixed before any
"undeniable" claim carries weight. Each fix is validated by rebuilding the guest and re-running the
regression (block 170, block 741000, `check-ibd` genesis→550) to **identical** tip hashes.

- [x] **SEC-1 (medium-high): `has_witness` is host-controlled → BIP141 witness-commitment bypass.**
  FIXED (commit `6c63565`): the guest recomputes `has_witness` *and* the `wtxids` in-guest from the raw
  transactions (Core's `HasWitness()` / `GetWitnessHash()`); the host can no longer influence the
  witness-commitment decision. Block 741000 still proves valid with an identical tip hash.
- [x] **SEC-2 (high-criticality location): accumulator `delete` trusted an unverified position.**
  FIXED (`6c63565`): `delete` pins the global index `i` to the proven leaf (tree height matches the
  proof, and `i − tree_offset == proof_i.position`, the LOCAL index) and pins `proof_last` to `last`.
  (Subtlety: `Proof.position` is the local, not global, index — a first attempt using global broke
  honest deletes at block 170 and was corrected.)
- [x] **SEC-3 (low, robustness): prevouts vector length unchecked.** FIXED (`6c63565`): length asserts
  `spent.size() == tx.vin.size()` in `verify_input` / `check_tx` / `tx_full_sigops`.
- [x] **SEC-neg: negative regression tests.** Both fixes shown to REJECT the malicious cases.
  - [x] SEC-1 (witness): `prover/make_negative_tests.py` → `block_741000_badwit.json` (one witness byte
    flipped). `check-full` reports `merkle_ok=true, witness_ok=false, all_ok=false` — rejected on the
    BIP141 commitment, exactly the check SEC-1 makes unskippable.
  - [x] SEC-2 (position): test-only host knob `HAZYNC_SEC2_BADPOS=1` corrupts the first spend's
    `global_pos` (different in-range index) while leaving `proof_i` honest. `check-full` on block 170
    reports `all_ok=false, root_matches=false`, every other flag true — rejected by the hardened
    `delete`'s position check. Inert unless the env var is set; VALID without it.

> Validation: SEC-1/2/3 all verified by rebuilding the guest + re-running the regression (block 170,
> block 741000, `check-ibd` genesis→550) to **byte-identical** tip hashes — the fixes reject the
> malicious cases they close and change nothing on valid data.

## 2. Repo & presentation hygiene

The repo went public fast and reads like working notes. Make it a curated artifact.

- [x] Remove `PLAN.md` from the public repo (internal session log; leaked the private node project + a
  local filesystem path). Removed from the tree; kept locally. *(Note: it remains in git history —
  see the residual item below.)*
- [x] `AUDIT.md` → `SECURITY.md`: relabelled as a *self-review, no external audit yet*; SEC-1/2/3 +
  reconciled S1/S2/S4/C1 statuses added; open-items bounty list at the bottom.
- [x] `SOUNDNESS.md`: added §7 "Known open issues (security)" pointing at `SECURITY.md`.
- [x] `HAZYNC_ARCHITECTURE.md`: de-coupled from the private node — dropped the node-specific serving/quorum
  codenames, the private fast-sync internals, and the private source line-numbers; node integration is
  now framed generically (any Bitcoin Core-derived full node).
- [x] Scrub docs for local paths / internal codenames / memory `[[wikilinks]]` (ARCHITECTURE, README,
  SCALING, SOUNDNESS, and the anchor-checkpoint references all de-coupled).
- [ ] **Residual:** `PLAN.md` and the earlier docs are still in **git history** (the repo is already
  public). Decide whether a history rewrite is worth it (force-pushing a public repo others may have
  cloned is itself disruptive; the leak is a username + a project codename, no secrets/keys). Low
  severity — flag for the user.
- [x] Consolidate the docs (2026-07-19): 17 → 12 files. Lean value-first README; merged
  ENGINE/SCALING/HARDENING into `HAZYNC_ARCHITECTURE.md`; dropped the redundant `WRITEUP.md` and the
  internal `coordinator/ROADMAP.md`. Remaining voice/date-style normalisation is a later polish pass.
- [x] **Reproducible guest build / canonical `METHOD_ID`.** A proof verifies only against the guest
  image id it was made with, and that id is a hash of the *whole* build (Core source + riscv
  cross-toolchain + risc0 versions + **absolute build paths** baked into the ELF). So a from-source host
  got a different `METHOD_ID` and failed to verify genuine proofs — an onboarding trap (looked fake).
  DONE: pinned `risc0-*` `=3.0.5` + rzup toolchain + Core v28.0 + secp256k1 v0.5.1 + committed
  `Cargo.lock`; `host method-id` + legible mismatch error in `verify-any`/`verify-range`; and a hermetic
  container (`reproduce/Dockerfile`) that builds the guest at FIXED paths (stock `RISC0_USE_DOCKER` was
  insufficient — the guest embeds external Core C++ + a custom cross-toolchain). **Verified reproducible
  bit-for-bit across machines** (local WSL2 == GitHub CI == GPU box): the canonical id checked in at
  `reproduce/METHOD_ID` is asserted by the `reproducible-image-id` CI job. The current canonical id is
  `601d7ca2…` (round-8 guest; ships unchanged in v0.6.0 **and v0.6.1** — see `reproduce/METHOD_ID`, authoritative). Superseded history:
  `d1fc4065…` (with k256) → `c029cee4…` (v0.5.0, k256 stripped) → `601d7ca2…` (round-8 leaf/anchor
  hardening). Each supersession changed only the guest source; the reproducible-build mechanism is unchanged.
  - [x] **Re-prove** the chain on the reproducible guest: the old proofs were archived and the board is
    re-proven on the current `601d7ca2` guest (`provision-vps.sh GPU=1`).

## 3. External review + writeup

The bottleneck now is credibility, not compute. Get eyes on it. Two audiences, two registers:
experts (who verify) and everyone else (who spread the word, contribute compute, or donate).

- [~] **Plain-English explainer for non-experts (`EXPLAINER.md`).** No one helps or donates to what
  they don't understand. Explain, with zero jargon: what a Bitcoin node does today, why syncing is
  slow/heavy, what a "proof you can check without redoing the work" is (everyday analogies), what
  Hazync proves, why "real Core code, not a rewrite" is the whole point, and — concretely — how a
  reader can help (run a prover, donate compute to the proof party, review, share). **Drafted**
  (`EXPLAINER.md`, linked from README top); iterate for tone + add the visual/FAQ layer below.
- [ ] A clear **technical** writeup (Delving post / blog) for the experts: what it is, the trust model,
  reproduce in 25 min, honest scope + known open issues, "try to break it."
- [ ] A short **visual/FAQ** layer (diagram of the three frontiers; "is my money safe?", "do I have to
  run this?", "how is this different from a checkpoint?") — bridges the two writeups.
- [ ] Invite independent reproduction + adversarial review (the SEC findings above are the starting
  bounty list).
- [ ] Consider a formal audit of the accumulator (the one non-Core component).

## 4. Complete the proof

- [ ] **Hazync Proof Party**: a coordinator (VM + backup) that runs the one-time bridge pass, hands out
  block ranges + witnesses, verifies + tree-folds submitted proofs, stores results, serves a
  verification API, and shows an attribution leaderboard. Self-verifying (contributors can't cheat) and
  fault-tolerant. This produces the full genesis→tip proof as a community effort.
- [ ] Sponsored tip-proving cluster (small committed GPU set; ~5–30 L40S-equivalents to keep pace).
- [ ] SNARK-wrap the final chain proof (~200–300 B) for universal/on-chain verification.

## 5. Parking lot

- **Acceleration** (`ACCELERATION.md`): the naive "route the multiply through the precompile" is
  disproven (byte-correct but ~10% *slower* — conversion overhead). Sound-and-fast needs a libsecp
  field-backend rework. Decision: **stay pure-Core** for soundness — the k256 accelerator was removed
  from the guest (2026-07-19). Revisit only if a full run's economics demand it; zk-ASICs / better
  hardware will lower cost over time.
- **Barebones validating node**: Hazync is the engine for a stateless full-security node — verify one
  proof, hold the accumulator, follow the tip — no archive, no re-execution. Natural downstream product.
