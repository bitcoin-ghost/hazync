# Threat model

What each component is trusted for, what it is not, and what an attacker who controls it can and cannot
do, with the code that enforces each line. Derived from [`SPEC.md`](SPEC.md) §11-§14,
[`SOUNDNESS.md`](SOUNDNESS.md), [`../SECURITY.md`](../SECURITY.md) and the code cited; where a doc and the
code disagree, the code wins. `SECURITY.md` is the history of findings; this page is the current position.

**As of 2026-09-14**, `main` at `951a08a` (v0.21.4 plus 8 commits), canonical guest `37987b85`. Terms:
[`GLOSSARY.md`](GLOSSARY.md). Routes and configuration: [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md).

## The claim being protected

A receipt that passes SPEC §11.1 — verifies against the canonical `METHOD_ID`, `journal.self_id` equals
it, `journal.kind == KIND_RANGE`, and the genesis pin of §9 holds — attests that blocks `1..hi` are valid
under Bitcoin Core's consensus rules and that the UTXO set is the committed out-boundary. Everything
below is either part of that statement's trust base or outside it: liveness, attribution, availability,
cost.

| component | inside the soundness trust base? | worst case if hostile |
|---|---|---|
| guest program and its inputs | **yes** | an invalid chain proves valid |
| risc0 zkVM, recursion, Groth16 | **yes** | a forged receipt |
| verifier CLI and C ABI | **yes** — it is the check | accepts what it should not |
| browser verifier, as served | **yes, for whoever loads it** | reports "verified" for anything |
| release signing, reproducible build | **yes, for anyone who does not rebuild** | a wrong verifier or host is trusted |
| ghostd adoption | **yes, for that node** | the node loads an unproven UTXO set |
| host / prover | no | wasted work; no false proof |
| GPU workers, rented hosts | no | latency, cost, a stolen contributor key |
| coordinator | no for proofs; yes for attribution and board figures | censorship, misattribution, stalls, false figures |
| worker CLI | no | local memory exhaustion, a stolen key |
| websites | no, except the verifier module they serve | misinformation |

---

## 1. Guest program

`prover/methods/guest`. Proving modes `chain_step`, `prove_range`, `fold_range` and `aggregate`, block
validation in `validate_block` (all `src/main.rs`).

**Trust base** (SOUNDNESS §2, SPEC §12):

- **Bitcoin Core v28.0 consensus sources**, compiled into the guest; the translation-unit list is
  `prover/methods/guest/build.rs`. Two Core-tree patches, applied by `provision-vps.sh` phase 5:
  `patches/0001` (a `serialize.h` 32-bit int overload, portability) and `patches/0002` (SHA-256 routed
  to the risc0 accelerator — a substitution, and the precedent for the ones below; `CORE_VS_GHOST.md` §1).
- **libsecp256k1 v0.5.1**, with two patches applied unconditionally since v0.21.0 (`provision-vps.sh`
  phase 5a, defines `HAZYNC_FIELD_BIGINT2=1`, `HAZYNC_LIFTX_HINT=1`):
  - `patches/0012` — the `field_bigint2` backend. Replaces the field arithmetic at the backend interface
    libsecp already parameterises; wNAF, GLV and the ECDSA and Schnorr logic are unchanged. Gates 0-3
    passed (`FIELD_BIGINT2_BACKEND.md` §5b: mod-p harness, libsecp's own suite under `-DVERIFY`,
    mutation controls, byte-identical journal digest on block 962,000). **Gate 4, the corrupt-signature
    negative control, is recorded as not run.**
  - `patches/0013` — the `lift_x` witness hint. `hazync_lift_x_hint` (`src/liftx_hint.rs`) offers a `y`;
    the patch accepts it only if `secp256k1_fe_sqr(y)` equals `x³ + 7` (`secp256k1_fe_equal`), and
    otherwise runs libsecp's own `secp256k1_fe_sqrt`. The hint is advice; a wrong or missing one cannot
    change the result.
- **The Utreexo accumulator**, `prover/methods/guest/src/utreexo.rs`. Project code, SEC-2 hardened,
  differentially fuzzed (`audit-fuzz/`), not externally audited — "the single most likely location of any
  remaining soundness bug" (`SECURITY.md`).
- **The coinbase SMT for BIP30**, `coinbase-smt/src/roots.rs` and `bip30.rs`, `#[path]`-included into the
  guest (`reproduce/METHOD_ID`).
- **`sha2`** from `risc0/RustCrypto-hashes` tag `sha2-v0.10.8-risczero.0` (`[patch.crates-io]` in
  `prover/methods/guest/Cargo.toml`).
- **SHA-256 collision resistance** and the risc0 proof system (§2).

**Trusted for:** the per-block assertions of SPEC §7 and the ENFORCED list of SOUNDNESS §5.

**Not attested:**

- The 2-hour future-time limit (G4): verifier-local wall clock, unprovable. Policy and standardness.
- That the proven chain is the **most-work** chain. A proof commits `range_work` and attests validity;
  `_frontier_chain` in `coordinator/server.py` picks the most-work chain, but the verifier CLI compares
  nothing against the network, and a proof for a competing chain is an open adversarial case for ghostd
  (`ROADMAP.md`).
- Anything below `lo` for a mid-chain range; a transaction count (SPEC §14); any network but mainnet.

**An attacker running the prover** chooses every witness byte. They cannot, and the guest enforces it:
lie about the height (H1 `w.height == prev.height + 1`; H9, height bound into `boundary_digest`); swap in
a different valid spend inside a chunk (H2 binding digest); double-spend or pre-spend an in-block coin
(H3); skip `CheckTransaction` on the coinbase (H4); feed phantom fee prevouts (H5); fabricate the genesis
in-boundary (H6, `assert_genesis_in_boundary` in `prover/host/src/main.rs`, also applied by `verify-any`
when a range claims genesis); launder another journal kind (H8, `KIND_*` tags); recurse against a
different guest (S1, `self_id` committed and asserted at every level); control `has_witness` (SEC-1).
What they can do is find a reject-valid bug and make a valid block unprovable — liveness, not soundness;
`SECURITY.md` records several (G3, H-S2, #4, #8).

**An attacker who changes guest source** changes `METHOD_ID`: any edit that moves a line does, comments
and `#[path]`-included crates included (`reproduce/METHOD_ID`, `scripts/check-guest-inputs.sh`). Proofs
from the changed guest do not verify against the canonical id. The attack therefore has to be a
re-baseline, which is public: a new row in `reproduce/LINEAGE.tsv`, derived from git history and gated by
`scripts/lineage.sh --check`, and an id anyone can rebuild (`reproduce/Dockerfile`, CI job
`reproducible-image-id`). That protects a verifier that pins the id. It does nothing for one that accepts
whatever id it is handed — which is what the undecided accepted-set layer of #244 would have to get right.

## 2. risc0 zkVM, recursion and the vendored crates

**Trusted for:** receipt soundness — the STARK, recursion, and for the wrapped form Groth16 including its
trusted setup (SPEC §12). Pinned `=3.0.5`.

**Vendored.** `prover/Cargo.toml` patches two crates for the prover:

- `risc0-zkvm` → `vendor/risc0-zkvm`: upstream 3.0.5 except two changed files, the recursion fold
  rebalanced into a tree and assembly from externally proved segment receipts (comment in
  `scripts/check-test-surfaces.sh`).
- `risc0-circuit-rv32im-sys` → `vendor/risc0-circuit-rv32im-sys`: the #119 fix, zeroing a LogUp cell
  before an add in two kernels (`RELEASE_NOTES_v0.21.1.md`). #119 produced receipts that failed their own
  `verify()` — fail-closed, a completeness bug.

The host links the vendored crates, so the coordinator's `verify-any` runs through them. `verifier/` and
`verifier-ffi/` depend on crates.io `risc0-zkvm =3.0.5` with no patch, and `verifier-wasm/` calls the same
`hazync_verify::verify`. A fault in the vendored code changes what gets proved; it does not change what
the standalone verifiers accept.

**An attacker who breaks the proof system** forges anything. That is the standard assumption, and out of
scope.

## 3. Host / prover

`prover/host`. Builds witnesses, proves, folds, extends the spine.

**Not trusted for soundness** (SPEC §12: the prover, the witness and the bridge are not trusted).

**Trusted, when used as a verifier.** The coordinator runs `host verify-any` on every submission and
`host verify-range` then `verify-any` on every spine (`verify_receipt`, `verify_spine` in
`coordinator/server.py`). The id the host embeds is the id the coordinator accepts: `/api/meta` reports
`HAZYNC_HOST method-id`.

**An attacker controlling a proving host** can prove a block against the wrong predecessor state — a fork,
or a stale bundle after a reorg — at the right height. That receipt verifies and covers the block, but
cannot seam into the frontier, so `claim()` re-offers frontier+1 (#281, #284). They cannot make an invalid
block verify.

## 4. GPU workers, rented hosts, distributed segments

All untrusted.

- **Segment workers** (`seg-serve` / `seg-connect`) cannot forge a segment receipt, and a correct receipt
  for the wrong segment fails the join's state-digest continuity. Returned receipts are checked with
  `verify_integrity_with_context` before use (`prover/host/src/main.rs`; `SEGMENT_DISTRIBUTION.md`, "Trust
  model"). Cost: latency.
- **Whoever controls a rented host** can read `$HAZYNC_HOME/key.hex` (mode 600 guards it from other users,
  not from root). With that key they can submit and fold as the contributor, beat the contributor's claims,
  and **rotate the key to one they hold**: `rotate()` requires signatures from the old and the new key, and
  the thief can make both; afterwards the old key cannot rotate again (`409`, "already rotated"), and
  `rotate()` has no revocation path. The effect is permanent attribution theft short of an operator editing
  the database. It touches no proof.
- **Sponsor bot pods** receive a per-sponsorship key (`docs/SPONSOR_BOT.md`, "Identities"), and SSH to
  them runs with `StrictHostKeyChecking=no` because pods reuse addresses with new host keys. An on-path
  attacker could impersonate a pod. Blast radius: spend within the bot's caps, and attribution of sponsored
  blocks; every proof is still verified.

## 5. Coordinator

`coordinator/server.py`, public at `https://bitcoinghost.org/hazync/api/`.

**Trusted for:** liveness and allocation hints; attribution (who proved, folded, anchored); the figures on
the board; retaining receipts; serving witness bundles.

**Not trusted for validity.** Every accepted receipt can be downloaded (`/api/proof/<id>`,
`/api/spine/proof`) and checked by anyone. The board's numbers are claims; the spine file is evidence.

**Enforced at its boundary:**

- `verify_receipt`: runs `host verify-any`, reads only the one `RANGE-OK` stdout line, and requires the
  proven `[lo..hi]` to equal the claimed id. `VERIFY_MODE=mock` fails closed without `COORD_ALLOW_MOCK`,
  and `__main__` refuses a non-loopback bind in any insecure mode unless `COORD_ALLOW_PUBLIC_INSECURE` is
  set.
- `verify_sig`: ed25519 over the receipt bytes; fails closed without the library unless
  `COORD_ALLOW_UNSIGNED`.
- `_frontier_chain`: the most-work genesis-anchored chain, each seam requiring `in_tip == out_tip`,
  `in_bhash == out_bhash` and `lo == prev.hi + 1` (H7, S1/F1, H9). Fuzzed by `coordinator/seam_fuzz.py`,
  with its `--control` run in CI.
- `submit`: a range wider than one block must be tiled by verified ranges (#281); a sponsor-held block is
  accepted only from its registered key (`_hold_refusal`).
- Input handling: `parse_any_range` shape-checks every id before it reaches a path; `clean_handle` strips
  `< > & " '`; `HANDLE_DENY`; `MOD_BLOCK_FILE`; `MAX_BODY`; per-IP rate limits, with `X-Forwarded-For`
  honoured only from `TRUSTED_PROXIES`; `_verify_sem` bounds concurrent verification; static files are
  contained to `COORD_WEB`.
- `sync_from_peers` re-verifies every peer receipt with `verify_receipt`, shape-checks peer ids, and bounds
  reads (64 MiB for an index, `MAX_BODY` for a receipt). The handle a peer reports is recorded as given.

**An outside attacker** (no access to the box):

- **Can claim blocks under any pubkey.** `POST /api/claim` checks no signature. A claim that never beats
  holds its block for `CLAIM_GRACE` (600 s by default), and only the key holder can beat it. Claims are
  bounded by `RATE_MAX` per IP per `RATE_WINDOW` and nginx's `10r/s`, and they are advisory: `submit`
  accepts any height, and `hazync run <n>` never claims. How far this can starve workers that rely on
  `claim` is **not measured**.
- Can submit a valid proof against the wrong predecessor (§3); can take any unreserved handle first; can
  read every pubkey, which is why beats are signed (audit #5, L-2).
- **Cannot** add an unverified range, advance the frontier with a range that does not seam, replace the
  spine with a shorter one (`submit_spine` compares under `_lock`), prove a sponsor-held block without its
  key, or rotate a key it does not hold.

**The operator, or anyone with the box:**

- Can misattribute or hide contributors; edit, withhold or delete receipts; refuse submissions; publish any
  frontier or progress figure; serve wrong or stale bundles (costing provers GPU time); serve a stale spine;
  report a different `method_id` in `/api/meta`, which stops every worker that compares ids (exit `78`);
  and set sponsorship statuses by hand, since payments are not connected (`SPONSORSHIP.md`).
- Cannot make a receipt verify for anyone else.
- Deployment drift is visible: `/api/meta` publishes `source_sha256` of the running `server.py`, compared by
  `scripts/check-deployment.sh`. On 2026-09-14 08:00 UTC it equalled the sha256 of `coordinator/server.py`
  at `951a08a`.

## 6. Worker CLI

`coordinator/hazync`, released as `hazync-worker`.

**Trusted for:** holding the contributor key and signing only what it produced — receipt bytes from the
local host, `<range>:<ts>` beats, rotation messages.

**It trusts the coordinator for:** which block to prove, witness bundles, the pairs and receipts to fold
(`/api/foldable`, `/api/proof/`), `/api/vranges` for the spine, and the expected guest id.

**A hostile coordinator can:**

- waste GPU time with bad bundles or fold inputs; `host fold-range` rejects bad receipts, and a proof of
  the wrong thing fails at submit;
- stop the worker by reporting a different `method_id` (`_guest_mismatch_exit` returns `EX_CONFIG`);
- **exhaust the worker's memory**: `get()` returns `_open(...).read()` with no size bound, and `post()`
  parses the whole response. `EXTERNAL_REVIEW.md` §4 flags this for a second pass if untrusted coordinators
  are ever supported (#69).

**It cannot** steer local paths: range ids pass `parse_range` before becoming a receipt path
(`cmd_submit`), and `spine_index` only follows `/api/proof/` paths, fetched relative to `COORD_URL`.

**Local trust.** Without `HAZYNC_HOST`, `_find_host()` runs the first prover binary found beside the CLI,
in `$HAZYNC_HOME/bin`, in `$HAZYNC_HOME`, in the **current directory**, then `hazync-*` names on `PATH`.
Whoever can write those directories chooses the binary. A source checkout refuses to POST to the default
public coordinator (`_guard_dev_writes`). `run-workers.sh` refuses to start on a guest id that differs from
`/api/meta` and runs a GPU smoke prove first.

## 7. Verifier CLI and C ABI

`verifier/`, `verifier-ffi/`.

**Trusted for** exactly SPEC §11.1. `verify()` in `verifier/src/lib.rs` verifies the receipt against the
embedded `METHOD_ID_HEX`, checks `self_id` and `KIND_RANGE`, and applies the genesis pin, returning
`Invalid` or `NotAnchored` (exit 1 or 2). No node, no network. `verifier-ffi` exposes the same function as
`hazync_verify_proof` (`include/hazync_verify.h`).

**Not trusted for:** most-work (§1), or anything above `hi`. A checkpoint-anchored proof is `NotAnchored`
by design.

**Whoever supplies the proof file** cannot get a mid-chain or foreign-guest proof accepted. **Whoever
supplies the verifier binary** controls the answer, which is what §9 is for. The embedded id is held to the
canonical id by `scripts/check-versions.sh` (both `lib.rs` files), and `release-sign.yml` asserts that the
aarch64 verifier it builds embeds it.

## 8. Browser verifier, and the served-module risk

`verifier-wasm/`: the same `hazync_verify::verify` compiled to wasm, returning `verified`, `invalid` or
`not_anchored`.

**The risk is what gets served, not what gets built.** A reader who clicks "verify" trusts the site
serving `hazync-verify.js` and `hazync-verify.wasm` under `/hazync/verify/`. On 2026-08-11 that module was
two re-baselines behind and told readers the live spine was forged. A stale module has the same size and
exports and returns HTTP 200, so only calling it reveals the difference (header of
`scripts/check-deployed-verifier.sh`).

**Enforced by** `scripts/check-deployed-verifier.sh`. It requires the served loader to be byte-identical to
`verifier-wasm/hazync-verify.js`, the served wasm size to match the release asset and the README, and
`methodId()` to equal `reproduce/METHOD_ID`. It also runs the live spine and a one-bit-flipped copy through
the served module. CI job `deployed-verifier` runs it **on schedule and `workflow_dispatch` only**, not per
push. It catches drift after the fact and cannot stop a browser loading a bad module in between. The signed
CLI remains the stronger check.

## 9. Release signing

`.github/workflows/release-sign.yml`, triggered by `release: published` or `workflow_dispatch` with a tag.

- Release binaries are built outside CI and uploaded; the workflow does not build them (its header). The
  exception is the aarch64 verifier: built from the checked-out tag and asserted to embed the canonical id.
- It writes `SHA256SUMS.txt` over the assets the release carries **when it runs**, signs it with the repo
  secret `GPG_PRIVATE_KEY`, and uploads `SHA256SUMS.txt` and `SHA256SUMS.txt.asc`. Key fingerprint
  `777FE81F 8CC077FD 3D08055E 852C2B31 90F5B928`, expiring 2028-04-25; second source
  `https://github.com/defenwycke.gpg` (`SECURITY.md`, "Verifying releases").

**Trusted for:** provenance — these are the bytes the maintainer key attested. **Not** for how a binary was
built. The guest id is reproducible; the host binary's build is not attested. Any proof verifies against
the id whoever built the host.

**An attacker with write access** can dispatch the workflow against any tag and get whatever assets are
attached at that moment signed. Tag injection into `run:` blocks, which exposed the signing key (H-1), is
fixed (#57) and banned repo-wide by `scripts/check-workflow-injection.sh`. **An attacker who swaps an asset
after signing** produces a checksum mismatch.

## 10. ghostd adoption

The consuming code is in `bitcoin-ghost/ghost` (PRs #543, #627, #630, #631 per `EXTERNAL_REVIEW.md` §5).
It is not in this repository and was not re-read for this page.

**Trusted for:** letting a node skip validation, or load a UTXO set, on a proof's authority. SPEC §11.2
sets the obligations: positions form a permutation, the coins are in bijection with the snapshot, and the
rebuilt forest's roots and leaf count equal the proof's — checked by the node that loads the set. It calls
`verifier-ffi` rather than reimplementing the anchoring rules (`ROADMAP.md`).

**Recorded:** `haze::HazyncAdoption` can only be obtained from `Authorise()`, which requires adoption armed,
the proof verified and the dump matched (`EXTERNAL_REVIEW.md` §5 names this "the claim to test"). Eight
adversarial inputs — corrupt, truncated, empty, missing and non-genesis-anchored proofs, and each flag
without the other — elided nothing (#34, `ROADMAP.md`). **Open:** a proof for a competing chain, and a reorg
below the proven height (`ROADMAP.md`); `m_chain_tx_count` is a substituted lower bound (SPEC §14).

## 11. Websites

- `https://bitcoinghost.org/hazync` serves the board, the browser verifier (§8), and the API proxied by
  `coordinator/deploy/nginx-hazync.conf` (§5). The board front end is not in this repository;
  `coordinator/web/index.html` is the coordinator's own static page (`COORD_WEB`), where handles pass
  `clean_handle` and every render sink escapes (`SECURITY.md`, round 6).
- `https://hazync.org` is built from `bitcoin-ghost/hazync-web`, a private repository.

**Trusted for** nothing soundness-relevant except the verifier module. **Whoever controls a site** can
publish false figures, false claims and wrong instructions, and serve a malicious verifier. Downloads stay
checkable against the signed manifest and a fingerprint from a second source.

---

## Open items

Each verified against the tree or GitHub on 2026-09-14.

1. **Field backend gate 4 has not run.** The corrupt-signature negative control is recorded `NOT RUN` in
   `FIELD_BIGINT2_BACKEND.md` §5b (2026-08-30). Nothing later in the tree records it run on the CORE guest
   that shipped in v0.21.0.
2. **The accumulator reference fuzz control needs a rerun.** `audit-fuzz/FINDINGS.md` describes
   `delete_soundness_reference` crashing on a `tree_of` panic in `accumulator/src/lib.rs`. #63 (`8e789a9`,
   2026-08-02) made `tree_of` return `None` and `delete` refuse, and `run_reference` uses that crate
   (`hazync_utreexo::Stump`). No rerun is recorded since, so whether the control still detects its bug class
   is not measured. The in-crate L-2 tests have their own control, verified 2026-08-02 (comment in
   `accumulator/src/lib.rs`).
3. **CORE fleet figures rest on one block.** `BUILDS.md` §1 measures CORE at 10 cards on block 962,000;
   `GOALS.md` scales the stock backfill card-years by "Core's 2.9x"; `CORE_VS_GHOST.md` is one block, one
   L40S. CORE card-years are an inference from one block.
4. **#244 layer 2, the accepted set of method ids, needs a decision.** The issue's 2026-09-13 status: 10 of
   17 lineage ids predate at least one consensus rule, so "accept any historical id" re-admits proofs from
   narrower guests (§1).
5. **The worker reads coordinator responses without a bound** (`get()` in `coordinator/hazync`; §6).
6. **No commissioned external audit** (`SECURITY.md`). The accumulator, the recursion binding and ghostd
   adoption are the named priorities.
7. **Unsigned claims.** How far `/api/claim` under forged pubkeys can starve claim-driven workers is not
   measured (§5).
8. **Key rotation cannot be revoked.** A stolen contributor key can move that contributor's attribution
   permanently (§4).
9. **ghostd's competing-chain and reorg adversarial cases are open** (§10).
