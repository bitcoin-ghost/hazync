# Hazync roadmap

The state, the open work, and the order to do it in. Hazync proves Bitcoin Core's real consensus code
in a zkVM; the method is built and demonstrated on real mainnet data, and what remains is (1) closing
self-found soundness gaps, (2) presenting it credibly, (3) external review, and (4) actually producing
the full-chain proof. Status markers: `[ ]` open, `[~]` in progress, `[x]` done.

> **Goals live in [`GOALS.md`](GOALS.md).** That document states the six technical goals, where each
> actually stands (measured), and what would count as done. This file is the task inventory and the
> record of what has been completed — it answers "what has been done", not "what are we for".
>
> **Work distribution is designed in [`DESIGN_work_distribution.md`](DESIGN_work_distribution.md)**
> (#37 + #30): free-running provers, opportunistic tree folding, an incrementally-extended genesis
> spine, and the storage answer — retain every per-block receipt, discard bundles behind the frontier.
>
> **The finishing plan lives in [`RELEASE_PLAN.md`](RELEASE_PLAN.md).** Six workstreams to get the
> project shipped and ready to absorb outside compute, and the gate that must hold before money is
> spent on proving. It answers "what is left to build, and in what order".

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
- [x] **Resolved:** git history was squashed clean at publication; `PLAN.md` is absent from all refs
  (verified).
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
  `3f52baff…` (v0.10.0: libsecp `ECMULT_WINDOW_SIZE` 15→19 — a compile-time speed trade, no consensus
  change — see `reproduce/METHOD_ID`, authoritative). Superseded history:
  `d1fc4065…` (with k256) → `c029cee4…` (v0.5.0, k256 stripped) → `601d7ca2…` (round-8 leaf/anchor
  hardening; v0.6.0/v0.6.1) → `36a0415d…` (P2SH sigop guard; v0.7.x) → `cb114426…` (round-9 R-1 hardening) →
  `ffdc6095…` (real-Core `pow.cpp` retarget carve) → `7a8b29e0…` (chainparams-sourced constants; v0.8.0) →
  `68819a54…` (witness byte-packing + per-tx dedup; v0.9.0/v0.9.1) → `3f52baff…` (ecmult window 19; v0.10.0)
  → `85dc0b56…` (accumulator leaf/interior domain separation; unreleased).
  Each supersession changed only the guest source; the reproducible-build mechanism is unchanged.
  - [~] **Re-prove** the chain on the reproducible guest: through `36a0415d` → `cb114426` (R-1) the board
    carried over (robustness-only), and again through `ffdc6095` (pow.cpp carve) and `7a8b29e0`
    (chainparams-sourced constants) — both behaviourally identical (476-retarget + fuzz equivalence;
    chainparams-anchor green). The v0.9.0 change to `68819a54` altered the witness **wire format**
    (byte-packing + per-tx de-duplication), and v0.10.0's `3f52baff` changes the guest ELF again, so proofs
    are not interchangeable across either id. The board reached 3,897 blocks at `68819a54` before being
    **restarted from genesis at `3f52baff`** (the old ledger + receipts are archived and stay re-verifiable
    with the v0.9.1 binary). Two re-baselines in two days is the cost of shipping guest changes
    piecemeal: batch them.
  - [x] **Empirical era validation** (2026-07-25, re-confirmed 2026-07-26 at `68819a54`, **re-run in full
    2026-07-27 at `3f52baff`**): every representative era block validates on a real archive node with all
    consensus flags true — segwit (500000), taproot (750000), big-block (741000, ~6.4k inputs), and the
    pre-BIP34 coinbase-txid-collision case (130000). The pass surfaced one real defect — a host
    witness-builder bug (in-block spends keyed on txid, not the coin leaf) that would have stalled the
    bridge at the first colliding-txid block — fixed in `v0.7.2` (host-only, id unchanged; see
    `SECURITY.md`).

    v0.10.0 results, every fixture regenerated from an archive node via `prover/fetch_block_rpc.py` and
    each tip checked against the node's own `getblockhash`:

    | block | verdict | cycles | UTXO leaves | tip matches chain |
    |-------|---------|--------|-------------|-------------------|
    | 130000 (pre-BIP34) | VALID | 22,018,858 | 126 | ✓ |
    | 140000 | VALID | 406,574,397 | 329 | ✓ |
    | 500000 (segwit) | VALID | 12,864,697,913 | 5,128 | ✓ |
    | 741000 (big block) | VALID | 1,793,731,304 | 393 | ✓ |
    | 750000 (taproot) | VALID | 16,165,911,396 | 7,922 | ✓ |

    The leaf counts are one lower than previously recorded on 130000/140000/741000 because the old
    fixtures predated `coin_height` and so could not express an in-block spend; each of those blocks has
    exactly one. Tip hashes and `cum_work` are unchanged — see the leaf-count note in `../SECURITY.md`.

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
- [ ] SNARK-wrap the final chain proof for universal/on-chain verification. The Groth16 *proof* is
  ~200–300 B, but the shippable *receipt* measured **2033 B** on block 170 and grows with chain size
  (issues #21, #22); CUDA Groth16 is currently broken (#20).

## 4b. Consuming a proof in a node

The proving side answers "is this chain valid?". This section is about a node *acting* on the answer,
which is where the work actually pays off.

- [x] **Standalone verifier** — `verifier/`, 1.6 MB, no node, no peers, no chain data. Checks the
  SNARK, pins recursion to the canonical guest id, and asserts genesis anchoring. An ARM64 build is
  committed at `verifier/dist/`.
- [x] **C ABI** (`verifier-ffi/`) so ghostd calls this verifier rather than reimplementing it. A second
  implementation of the anchoring rules is a second place for them to be subtly wrong.
- [x] **ghostd startup adoption** — `-hazyncproof=<file>` verifies and reports. Reporting only: a proof
  alone changes nothing a node validates. (ghost#543)
- [x] **Proof-gated script skip** — `-hazyncskipvalidation` elides script and signature verification for
  blocks the proof covers. Demonstrated at height 1000: 1000 blocks elided, UTXO set byte-identical to
  full validation from genesis (#33). Eight adversarial cases — corrupt, truncated, empty, missing,
  and non-genesis-anchored proofs, plus each flag without the other — all elided nothing and still
  reached the correct chainstate (#34).
- [ ] Adversarial cases still open: a proof for a *competing* chain, and a reorg below the proven
  height. Both need a regtest proof, so both need a fixture generator (#34).
- [ ] Measure the speed benefit. Blocks 1..1000 hold ~1020 transactions, so #33 could establish
  equivalence but not saving. Needs a proof over a range with real signature load (#30).
- [ ] Start *from* a proof rather than validating-with-elision: begin at height N+1 from the committed
  UTXO set, without downloading blocks 1..N at all. Much larger change; not started.

> **The framing that matters.** Bitcoin Core already ships this exact elision. Its `assumevalid` hash
> sits near the tip, so a default node today skips signature verification for almost the whole chain on
> the authority of a hash the developers chose. Hazync does not introduce trusting-something-other-than-
> verification; it replaces a developer-asserted anchor with a proven one. That is a strictly smaller
> trust assumption than the status quo.
>
> It is also a live testing hazard: any benchmark of script-skipping must pass `-assumevalid=0` to
> **both** arms, or the control skips too and the comparison measures nothing.

## 5. Parking lot

- **Acceleration** (`ACCELERATION.md`): the naive "route the multiply through the precompile" is
  disproven (byte-correct but ~10% *slower* — conversion overhead). Sound-and-fast needs a libsecp
  field-backend rework. Decision: **stay pure-Core** for soundness — the k256 accelerator was removed
  from the guest (2026-07-19). Revisit only if a full run's economics demand it; zk-ASICs / better
  hardware will lower cost over time.
- **Barebones validating node**: Hazync is the engine for a stateless full-security node — verify one
  proof, hold the accumulator, follow the tip — no archive, no re-execution. Natural downstream product.
