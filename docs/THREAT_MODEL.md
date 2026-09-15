# Threat model

What each component is trusted for, what it is not, and what an attacker who controls it can and cannot
do, with the code that enforces each line. Derived from [`SPEC.md`](SPEC.md) §11-§14,
[`SOUNDNESS.md`](SOUNDNESS.md), [`../SECURITY.md`](../SECURITY.md) and the code cited; where a doc and the
code disagree, the code wins. `SECURITY.md` and
[`history/SECURITY_AUDIT_LOG.md`](history/SECURITY_AUDIT_LOG.md) are the history of findings; this page is
the current position.

**As of 2026-09-14**, code at `d5286b8` (v0.21.4 plus 13 commits), canonical guest `37987b85`. Terms:
[`GLOSSARY.md`](GLOSSARY.md). Routes and configuration: [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md).
Report a vulnerability privately: [`../SECURITY.md`](../SECURITY.md#reporting-a-vulnerability).

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
| worker CLI | no | local memory exhaustion, a stolen key, running a planted prover |
| websites | no, except the verifier module they serve | misinformation |

---

## 1. Guest program

`prover/methods/guest`. Proving modes `chain_step`, `prove_range`, `fold_range` and `aggregate`, block
validation in `validate_block` (all `src/main.rs`).

**Trust base** (SOUNDNESS §2, SPEC §12):

- **Bitcoin Core v28.0 consensus sources**, compiled into the guest; the translation-unit list is
  `prover/methods/guest/build.rs`. Two Core-tree patches, applied by `provision-vps.sh` phase 5:
  `patches/0001-serialize-ilp32-int-overload.patch` (portability) and
  `patches/0002-sha256-route-through-risc0-accelerator.patch` (SHA-256 through the risc0 accelerator — a
  substitution, and the precedent for the ones below; `history/CORE_VS_GHOST.md` §1).
- **libsecp256k1 v0.5.1**, with two patches applied unconditionally since v0.21.0 (`provision-vps.sh`
  phase 5a, defines `HAZYNC_FIELD_BIGINT2=1`, `HAZYNC_LIFTX_HINT=1`):
  - `patches/0012-select-field-bigint2-backend.patch` — the `field_bigint2` backend. Replaces the field
    arithmetic at the backend interface libsecp already parameterises; wNAF, GLV and the ECDSA and
    Schnorr logic are unchanged. Gates 0-3 passed (mod-p harness, libsecp's own suite under `-DVERIFY`,
    mutation controls, byte-identical journal digest on block 962,000). **Gate 4, the corrupt-signature
    negative control, has not run on a CORE build**; `FIELD_BIGINT2_BACKEND.md` calls it an open
    soundness item for shipped code.
  - `patches/0013-lift-x-via-witness-hint.patch` — the `lift_x` witness hint. `hazync_lift_x_hint`
    (`src/liftx_hint.rs`) offers a `y`; the patch accepts it only if `secp256k1_fe_sqr(y)` equals `x³ + 7`
    (`secp256k1_fe_equal`), and otherwise runs libsecp's own `secp256k1_fe_sqrt`. The hint is advice; a
    wrong or missing one cannot change the result.
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
  (`history/ROADMAP.md`).
- Anything below `lo` for a mid-chain range; a transaction count (SPEC §14); any network but mainnet.

**An attacker running the prover** chooses every witness byte. They cannot, and the guest enforces it:
lie about the height (H1 `w.height == prev.height + 1`; H9, height bound into `boundary_digest`); swap in
a different valid spend inside a chunk (H2 binding digest); double-spend or pre-spend an in-block coin
(H3); skip `CheckTransaction` on the coinbase (H4); feed phantom fee prevouts (H5); fabricate the genesis
in-boundary (H6, `assert_genesis_in_boundary` in `prover/host/src/main.rs`, also applied by `verify-any`
when a range claims genesis); launder another journal kind (H8, `KIND_*` tags); recurse against a
different guest (S1, `self_id` committed and asserted at every level); control `has_witness` (SEC-1).
What they can do is find a reject-valid bug and make a valid block unprovable — liveness, not soundness;
the audit record has several (G3, H-S2, #4, #8).

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
  before an add in two kernels (`history/releases/RELEASE_NOTES_v0.21.1.md`). #119 produced receipts that
  failed their own `verify()` — fail-closed, a completeness bug.

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
  `verify_integrity_with_context` before use (`prover/host/src/main.rs`; `FLEET_OPERATIONS.md`, "Trust
  model"). Cost: latency.
- **Whoever controls a rented host** can read `$HAZYNC_HOME/key.hex` (mode 600 guards it from other users,
  not from root). With that key they can submit and fold as the contributor, beat the contributor's claims,
  and, on a coordinator that turns rotation on, **rotate the key to one they hold**
  ([#311](https://github.com/bitcoin-ghost/hazync/issues/311)). `rotate()` requires signatures from the old
  and the new key over `rotate_message(old, new, ts)` within `ROTATE_MAX_SKEW`, and the thief can make both.
  It records one row per `old_pubkey`, so the real owner's later attempt is `409` "has already rotated", and
  nothing removes a rotation. So since #311 `/api/rotate` answers `410` unless the operator sets
  `ROTATE_ENABLED=1`; nobody had rotated on the public board. A stolen key can no longer move attribution. It
  can still submit and fold under the owner's name. Proofs and their verification are unaffected.
- **Sponsor bot pods** receive a per-sponsorship key (`SPONSOR_BOT.md`, "Identities"), and SSH to them runs
  with `StrictHostKeyChecking=no` because pods reuse addresses with new host keys. An on-path attacker could
  impersonate a pod. Blast radius: spend within the bot's caps, and attribution of sponsored blocks; every
  proof is still verified.

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

- **Can claim blocks under any pubkey, unsigned** ([#310](https://github.com/bitcoin-ghost/hazync/issues/310)).
  `claim()` accepts a claim signed by its key over `claim:<nonce>:<ts>` (within `BEAT_SKEW`), refuses one whose
  signature does not verify, and still accepts an unsigned one unless `CLAIM_REQUIRE_SIG=1`, because workers up
  to v0.21.4 sign nothing. `beat()`, `submit()` and `rotate()` are all signed. The bounds that apply:
  - one key holds at most `CLAIM_OPEN_MAX` live claims (4 by default, #319), and a key does not get back a block
    its own never-beaten claim let lapse for `CLAIM_RETAKE_WAIT` (#321). Both count **signed and unsigned claims
    apart**, so unsigned claims sent under a key cannot fill that key's signed slots or keep blocks from its
    signed claims; they can still crowd out that key's own unsigned claims, i.e. a worker older than the
    signing release;
  - a claim that is never beaten is released after `CLAIM_GRACE` (600 s by default, #296), and beats must be
    signed, so without the key a claim cannot be held longer;
  - `claim()` re-offers the frontier's own blocker at most once per `CLAIM_TTL` (`held`).

  The per-key cap does not stop an attacker using many fresh keys, and there is no per-address cap: public
  requests reach the coordinator from the web box, which is in `RATE_EXEMPT` (measured 2026-09-14), so the
  per-address `RATE_MAX` does not apply to them either; nginx's `10r/s` in front does. The effect on the board
  of many keys is not measured. Claims are advisory: `submit` accepts any height, and `hazync run <n>` never
  claims.
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
  `scripts/check-deployment.sh`. On 2026-09-14 at 08:31 and 12:02 UTC it equalled the sha256 of
  `coordinator/server.py` at `d5286b8`.

## 6. Worker CLI

`coordinator/hazync`, released as `hazync-worker`.

**Trusted for:** holding the contributor key and signing only what it produced — receipt bytes from the
local host, `<range>:<ts>` beats, `claim:<nonce>:<ts>` claims (#310), rotation messages.

**It trusts the coordinator for:** which block to prove, witness bundles, the pairs and receipts to fold
(`/api/foldable`, `/api/proof/`), `/api/vranges` for the spine, and the expected guest id.

**A hostile coordinator can:**

- waste GPU time with bad bundles or fold inputs; `host fold-range` rejects bad receipts, and a proof of
  the wrong thing fails at submit;
- stop the worker by reporting a different `method_id` (`_guest_mismatch_exit` returns `EX_CONFIG`);
- **exhaust the worker's memory**: `get()` returns `_open(...).read()` with no size bound, and `post()`
  parses the whole response. `EXTERNAL_REVIEW.md` §5 lists this for a second pass now that anyone can run
  a coordinator (#69).

**It cannot** steer local paths: range ids pass `parse_range` before becoming a receipt path
(`cmd_submit`), and `spine_index` only follows `/api/proof/` paths, fetched relative to `COORD_URL`.

**It runs the prover binary it finds, but only where the contributor or the release put it**
([#312](https://github.com/bitcoin-ghost/hazync/issues/312), fixed). When `HAZYNC_HOST` is unset, `_find_host()`
takes the first executable file named `hazync-host-x86_64-linux-gnu-cuda`, `hazync-host-cuda`,
`hazync-host-x86_64-linux-gnu` or `hazync-host` in, in order: the CLI's own directory, `$HAZYNC_HOME/bin`,
then `$HAZYNC_HOME` (default `~/.hazync`); the bare name `host` only in the CLI's own directory. After that
it searches `PATH`, but only for the `hazync-` names. It no longer searches the **current working
directory**: before the fix, a worker started from a directory someone else can write to, with no prover in
the first three places, ran whatever sat there under one of those names, including the bare `host`, and a
planted binary can print the canonical guest id, so no later check caught it.

- **Mitigates:** setting `HAZYNC_HOST`. `run-workers.sh` refuses to start without it (`${HAZYNC_HOST:?}`),
  so its worker loops never reach the search.
- **Does not mitigate:** the guest-id checks. `run-workers.sh` compares `$HAZYNC_HOST method-id` with
  `/api/meta` at startup, `selftest` does the same, and a plain `hazync run` notices a mismatch only after a
  rejected submission (`_guest_mismatch_exit`). All three ask the binary for its id, and a planted binary can
  print the canonical one.

A source checkout refuses to POST to the default public coordinator (`_guard_dev_writes`). `run-workers.sh`
also runs a GPU smoke prove before starting any loop.

**Alerts (`hazync notify`, off unless set up).** The worker and `run-workers.sh` POST alerts to the ntfy URL in
`$HAZYNC_HOME/ntfy` (mode 600) or `HAZYNC_NTFY`: a title with the handle and host name, and a body that can hold
a block id, the tail of a worker log, or the coordinator's error text (at most 3,500 bytes). Never the key.

- **The topic is the only secret.** Anyone who knows it can read the alerts and post fake ones to the prover's
  phone; `hazync notify new` generates an unguessable topic. The ntfy server, `ntfy.sh` by default, sees every
  alert.
- **A hostile coordinator can put its own text in a push**: a rejected proof or a refused claim quotes the
  coordinator's `error`. It cannot trigger more than one push per problem an hour (`HAZYNC_NTFY_REPEAT`).
- **A hostile or unreachable ntfy server cannot stop the work**: `notify()` gives up after 10 s, never raises,
  and a launcher never exits over an alert.

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

**The risk is what gets served, not what gets built.** A reader who clicks "verify" trusts the site serving
`hazync-verify.js` and `hazync-verify.wasm`. Two sites serve a copy: `bitcoinghost.org/hazync/verify/` and
`hazync.org/verify/` (both measured 200, 1,064,517 bytes, on 2026-09-14). On 2026-08-11 the bitcoinghost.org
module was two re-baselines behind and told readers the live spine was forged. A stale module has the same
size and exports and returns HTTP 200, so only calling it reveals the difference (header of
`scripts/check-deployed-verifier.sh`).

**Enforced by** `scripts/check-deployed-verifier.sh`, per site (`HAZYNC_SITE`, default bitcoinghost.org). It
requires the served loader to be byte-identical to `verifier-wasm/hazync-verify.js`, the served wasm size to
match the release asset and `verifier-wasm/README.md`, and `methodId()` to equal `reproduce/METHOD_ID`. It
also runs the live spine and a one-bit-flipped copy through the served module, and fails if the flipped copy
verifies. CI job `deployed-verifier` runs it **on schedule and `workflow_dispatch` only**, against the default
site. It catches drift after the fact and cannot stop a browser loading a bad module in between. The signed
CLI remains the stronger check. Deploying both copies is part of
[`RELEASE_PROCESS.md`](RELEASE_PROCESS.md).

## 9. Release signing

`.github/workflows/release-sign.yml`, triggered by `release: published` or `workflow_dispatch` with a tag.

- Release binaries are built outside CI and uploaded; the workflow does not build them (its header). The
  exception is the aarch64 verifier: built from the checked-out tag and asserted to embed the canonical id.
- It writes `SHA256SUMS.txt` over the `hazync-*` assets the release carries **when it runs**, failing if any
  `hazync-*` asset would be left out, signs it with the repo secret `GPG_PRIVATE_KEY`, and uploads
  `SHA256SUMS.txt` and `SHA256SUMS.txt.asc`. Key fingerprint `777FE81F 8CC077FD 3D08055E 852C2B31 90F5B928`,
  expiring 2028-04-25; second source `https://github.com/defenwycke.gpg` (`SECURITY.md`, "Verifying
  releases").

**Trusted for:** provenance — these are the bytes the maintainer key attested. **Not** for how a binary was
built. The guest id is reproducible; the host binary's build is not attested. Any proof verifies against
the id whoever built the host.

**An attacker with write access** can dispatch the workflow against any tag and get whatever assets are
attached at that moment signed. Tag injection into `run:` blocks, which exposed the signing key (H-1), is
fixed (#57) and banned repo-wide by `scripts/check-workflow-injection.sh`. **An attacker who swaps an asset
after signing** produces a checksum mismatch.

## 10. ghostd adoption

The consuming code is in `bitcoin-ghost/ghost` (PRs #543, #627, #630, #631 per `EXTERNAL_REVIEW.md` §6).
It is not in this repository and was not re-read for this page.

**Trusted for:** letting a node skip validation, or load a UTXO set, on a proof's authority. SPEC §11.2
sets the obligations: positions form a permutation, the coins are in bijection with the snapshot, and the
rebuilt forest's roots and leaf count equal the proof's — checked by the node that loads the set. It calls
`verifier-ffi` rather than reimplementing the anchoring rules (`history/ROADMAP.md`).

**Recorded:** `haze::HazyncAdoption` can only be obtained from `Authorise()`, which requires adoption armed,
the proof verified and the dump matched (`EXTERNAL_REVIEW.md` §6 names this "the claim to test"). Eight
adversarial inputs — corrupt, truncated, empty, missing and non-genesis-anchored proofs, and each flag
without the other — elided nothing (#34, `history/ROADMAP.md`). **Open:** a proof for a competing chain, and
a reorg below the proven height (`history/ROADMAP.md`); `m_chain_tx_count` is a substituted lower bound
(SPEC §14).

## 11. Websites

- `https://bitcoinghost.org/hazync` serves the board, a browser verifier (§8), and the API proxied by
  `coordinator/deploy/nginx-hazync.conf` (§5). `coordinator/web/index.html` is the coordinator's own static
  page (`COORD_WEB`), where handles pass `clean_handle` and every render sink escapes
  (`history/SECURITY_AUDIT_LOG.md`, round 6).
- `https://hazync.org` is built from `hazync/hazync-web`, a private repository. It serves its own
  copy of the browser verifier, pinned by sha256 in `deploy/deploy.sh` and `tools/check.py`, and proxies
  `/api` to the coordinator.

**Trusted for** nothing soundness-relevant except the verifier modules. **Whoever controls a site** can
publish false figures, false claims and wrong instructions, and serve a malicious verifier. Downloads stay
checkable against the signed manifest and a fingerprint from a second source.

---

## Open items

Each verified against the tree or GitHub on 2026-09-14.

1. **Field backend gate 4 has not run.** The corrupt-signature negative control is `NOT RUN` in
   `FIELD_BIGINT2_BACKEND.md` §5b, which calls it an open soundness item for shipped code: no run on a CORE
   build rejects a corrupted signature inside the guest.
2. **The accumulator reference fuzz control needs a rerun.** `audit-fuzz/FINDINGS.md` marks it "NEEDS A
   RERUN": #63 (`8e789a9`) removed the `tree_of` panic the control relied on, and whether
   `delete_soundness_reference` still detects its bug class is unverified. The in-crate L-2 tests have their
   own control, verified 2026-08-02 (comment in `accumulator/src/lib.rs`).
3. **CORE fleet figures rest on few measurements.** `GOALS.md` G2 gives CORE **44–73 L40S card-years**,
   INFERRED from one near-tip block (966,108, ~0.77 card-s per input). `BUILDS.md` computes 10 cards from
   block 962,000 proved serially, and its 8 × L40S fleet check on 966,108 puts a sub-10-minute block at ~13.
4. **#244 layer 2, the accepted set of method ids, needs a decision.** The issue's 2026-09-13 status: 10 of
   17 lineage ids predate at least one consensus rule, so "accept any historical id" re-admits proofs from
   narrower guests (§1).
5. **The worker reads coordinator responses without a bound** (`get()` in `coordinator/hazync`; §6).
6. **No commissioned external audit** (`SECURITY.md`). The accumulator, the recursion binding and ghostd
   adoption are the named priorities.
7. **Unsigned claims** — [#310](https://github.com/bitcoin-ghost/hazync/issues/310). Signed claims are verified
   and counted apart, so unsigned claims under a key cannot use up its signed cap or re-take wait; unsigned claims
   stay accepted until `CLAIM_REQUIRE_SIG=1`, which needs a worker release that signs. Many fresh keys are not
   limited, and there is no per-address cap behind the web box (§5). The effect on the board is not measured.
8. ✅ **Fixed: a stolen key could move attribution for good** — [#311](https://github.com/bitcoin-ghost/hazync/issues/311).
   Key rotation is off unless `ROTATE_ENABLED=1`, so a stolen `key.hex` cannot move anyone's blocks. Turning
   rotation on brings the risk back, since a rotation still cannot be undone (§4).
9. ✅ **Fixed: the worker searched the current directory for a prover** —
   [#312](https://github.com/bitcoin-ghost/hazync/issues/312). With `HAZYNC_HOST` unset it now looks only
   beside the CLI, in `$HAZYNC_HOME/bin` and `$HAZYNC_HOME`, and takes the bare name `host` only beside the
   CLI (§6). Kept in this list, numbered, so references to the items below stay valid.
10. **ghostd's competing-chain and reorg adversarial cases are open** (§10).
