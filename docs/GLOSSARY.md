# Glossary

Short definitions of the terms this repository uses, with the code that defines each. Where a doc and
the code disagree, the code is authoritative; known disagreements are flagged inline.

Related: [`SPEC.md`](SPEC.md) (format), [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md) (routes
and settings), [`THREAT_MODEL.md`](THREAT_MODEL.md) (who is trusted for what).

**Index:** [aggregate](#aggregate-join-tree-lift-resolve) ·
[anchor / absorb](#anchor-genesis-anchored-absorb) · [audit / round](#audit-vs-round) ·
[beat](#beat-heartbeat) · [bigint2](#bigint2) · [block proof](#block-proof-range-leaf) ·
[board](#proof-party-board-contributor-handle-sponsor) · [bridge / bundle](#witness-bundle-bridge) ·
[channels](#channels-core-ghost-stock) · [chunk](#chunk) · [claim](#claim) ·
[coinbase SMT](#coinbase-smt-bip30) · [fold](#fold) · [frontier](#frontier) · [Groth16](#stark-receipt-succinct-groth16-snark-wrap) ·
[journal](#rangestate-journal) · [lineage](#method-id) · [maximal-Core](#maximal-core) ·
[METHOD_ID](#method-id) · [po2](#segment) · [segment](#segment) ·
[seg-serve / seg-connect](#seg-serve-seg-connect) · [spine](#spine) · [straggler](#straggler) ·
[Tier 0](#tier-0) · [vrange](#vrange)

---

### aggregate, join tree, lift, resolve

- **aggregate** — guest mode 5 (`aggregate()`, `prover/methods/guest/src/main.rs`), which combines a
  block's chunk receipts into one block proof, re-checking each chunk's per-input binding digest against
  the block's own inputs. Host side: `prove-seg`, `agg-chunks` (`prover/host/src/main.rs`); see
  `docs/PROVING.md`.
- **lift** — risc0's recursion step that turns one `SegmentReceipt` into a `SuccinctReceipt`
  (`server.lift(..)` in `prover/host/src/main.rs`).
- **join** / **join tree** — risc0's recursion step that merges two adjacent succinct receipts. The
  vendored risc0 (`vendor/risc0-zkvm`) replaces the linear chain of joins with a balanced tree of depth
  `log2(N)` whose levels are published as work (`docs/history/HAZYNC_ARCHITECTURE.md`, "balanced join tree").
- **resolve** — discharging an assumption (e.g. a chunk receipt the aggregate `env::verify`s) against a
  conditional receipt; `seg-serve` sends resolves to workers one at a time under `RESOLVE_TAG`, or runs
  them itself with `HAZYNC_RESOLVE_LOCAL=1` (`docs/FLEET_OPERATIONS.md`).
  ⚠ The same word also names risc0's `SegmentRef::resolve()`, which merely loads a segment
  (`session.segments[..].resolve()` in `prover/host/src/main.rs`).

### anchor, genesis-anchored, absorb

- **genesis-anchored** — a range proof with `lo == 1` whose in-boundary is genesis in full (tip hash,
  empty accumulator, `nBits`, time, epoch start, recent times); `docs/SPEC.md` §9,
  `RangeState::is_genesis_anchored` (`rangestate/src/lib.rs`). The coordinator's label uses
  `is_genesis_anchored(in_tip, lo)` (`coordinator/server.py`) and relies on `verify-any` having pinned
  the rest of the boundary.
- **absorb** / **absorption** — folding the next range onto the spine: `spine [1..N] + [N+1..M] -> [1..M]`
  (`docs/SPEC.md` §10.1; host `extend-spine`, `extend_spine_cmd`; worker `hazync spine`). The block map's
  status 5 "anchored" means absorbed into the spine (`block_status`, `coordinator/server.py`).
- ⚠ **anchor** is overloaded. `docs/SOUNDNESS.md` §2 (trust base, item 4) uses it for the trusted starting checkpoint of a
  chain proof (genesis or a block-hash checkpoint); issue #277 ("anchor warp") uses it for a non-genesis
  checkpoint the tip cluster proves backwards from.

### audit vs round

Review **rounds** (1–9 internal, 10–11 external AI-assisted) are recorded in
`docs/history/SECURITY_AUDIT_LOG.md`, indexed from `SECURITY.md`. "**audit #N**"
(#1–#5, counted in `docs/EXTERNAL_REVIEW.md`) is a separate series: audit #3 is the 2026-08-03 BIP30 F-1
pass, audit #5 the 2026-08-04 pass. `docs/history/AUDIT_2026-07.md` is round 8. `SECURITY.md` states that the two
numberings do not line up. `reproduce/METHOD_ID` and `prover/methods/guest/build.rs` also use "an audit
round" loosely, meaning any review pass. No commissioned professional audit has happened (`SECURITY.md`).

### beat (heartbeat)

`POST /api/beat` → `beat()` (`coordinator/server.py`): refreshes `ranges.last_beat` on a claim the caller
holds. Signed ed25519 over `"<range>:<ts>"` with integer `ts`; a `ts` more than `BEAT_SKEW` (default
`120` s) from server time is refused, which bounds replay. The worker beats only when a segment has
finished since the last beat (`_tick` in `coordinator/hazync`), so a hung prover's claim lapses.
⚠ Stale text says there are no heartbeats: the comment above `CLAIM_TTL` in `coordinator/server.py`, the
`do_POST` comment ("no claim, no heartbeat"), and `cmd_run` in `coordinator/hazync`.

<a id="bigint2"></a>

### bigint2, field backend, lift_x hint

- **bigint2** — risc0's 256-bit big-integer coprocessor (precompile) available to the guest.
- **field backend** (`field_bigint2`) — `patches/0012-select-field-bigint2-backend.patch`: libsecp256k1's
  field arithmetic routed to bigint2 at the backend interface libsecp already parameterises
  (`field_5x52`, `field_10x26`); wNAF, GLV and the ECDSA logic are unchanged. `docs/FIELD_BIGINT2_BACKEND.md`.
- **lift_x hint** — `patches/0013-lift-x-via-witness-hint.patch`: the host supplies the pubkey Y
  coordinate (`liftx_hints`, `prover/host/src/main.rs`) and the guest checks `y² = x³ + 7` with libsecp's
  own arithmetic instead of computing a square root. `docs/LIFTX_HINT.md`.
- Both are applied unconditionally by `provision-vps.sh` since `METHOD_ID 37987b85` (`docs/PROVING.md`,
  "Releases"). The broader bigint2 substitutions (`0005` ECDSA, `0006` Schnorr, `0007` lift_x,
  `0008` scalar inverse, `0014` wholesale ECDSA) are not in the canonical build: `provision-vps.sh`
  applies `0005` only when `HAZYNC_BIGINT2_ECDSA=1` and warns that the id then differs.

### block proof, range, leaf

- **range** — an inclusive block span `[lo..hi]`, written `lo-hi` or `N` (`parse_any_range`,
  `coordinator/server.py`; `parse_range`, `coordinator/hazync`). A range proof commits a `RangeState`.
- **block proof** / **leaf** — a range proof of width 1, produced by `prove-range-bridge` (or the replay
  path `prove-range`). A leaf covers fresh territory; a fold re-expresses verified leaves
  (`_tiled_by_verified`, `coordinator/server.py`). The worker submits leaves as it proves them
  (`submit_leaves`, `cmd_prove` in `coordinator/hazync`).
- ⚠ "leaves" also means the Utreexo leaf count (`in_leaves` / `out_leaves` in `RangeState`, `leaves` in
  `/api/state` `frontier_proof`). Context decides.

### channels: CORE, Ghost, stock

Three guest builds, one canonical id (`docs/BUILDS.md`):

- **stock** — the digest oracle every acceleration is checked against; never contributes to the board.
- **CORE** — canonical since v0.21.0: what the release binary builds and the board accepts. Core patches
  `0001`/`0002` plus libsecp patches `0012`/`0013` (`provision-vps.sh`).
- **GHOST** — experimental; different `METHOD_ID`, so its proofs are rejected by the board.

⚠ Naming varies: `docs/BUILDS.md` says CORE/GHOST channels, `docs/history/CORE_VS_GHOST.md` says Core mode/Ghost
mode, `docs/history/MODELS.md` says models. `docs/history/CORE_VS_GHOST.md` §2 (dated 2026-08-30) also lists
patches `0009`/`0010` under CORE; `provision-vps.sh` does not apply them.

### chunk

A contiguous slice of one block's inputs, proved separately (guest mode 4, `chunk_prove()`) and combined
by the aggregate. `HAZYNC_CHUNKS` requests the count (default `2`, `nchunks_env()`); the packer balances
predicted cost rather than input count (`prover/host/src/main.rs`, "Chunk packing"; `docs/PROVING.md`).
⚠ `docs/SPEC.md` §10.1 and `hazync spine` also call the range being absorbed into the spine a "chunk".

<a id="claim"></a>

### claim, CLAIM_TTL, CLAIM_GRACE, CLAIM_MAX

- **claim** — `POST /api/claim` → `claim()` (`coordinator/server.py`): hands out the earliest block
  that is neither proven nor live-claimed, one block wide. Advisory: `submit` accepts any height
  regardless of who claimed it. A per-claim `nonce` makes a retried request return the same block (#268).
- A claim is live while `COALESCE(last_beat, claimed_at) > now - CLAIM_TTL` and
  `claimed_at > now - CLAIM_MAX` and (it has beaten, or `claimed_at > now - CLAIM_GRACE`)
  (`coverage_and_held`, `coordinator/server.py`).
- **`CLAIM_TTL`** — default `3600` s: liveness window measured from the last beat, or from the claim if
  never beaten.
- **`CLAIM_GRACE`** — default `600` s: a claim that has never beaten is released after this (#296).
- **`CLAIM_MAX`** — default `86400` s: hard cap regardless of beats.
- Releasing a claim cancels nothing; a late submission still lands.

### coinbase SMT, BIP30

BIP30 forbids creating an outpoint that duplicates an unspent one; a Utreexo accumulator proves
membership, never non-membership. The **coinbase SMT** is a sparse Merkle tree over coinbase txids,
storing counts, which closes BIP30 without the height bound of the BIP34 argument (#54;
`coinbase-smt/src/lib.rs` module docs). Its root is committed as `in_smt_root` / `out_smt_root` in
`RangeState`. The guest `#[path]`-includes `coinbase-smt/src/roots.rs` and `bip30.rs`, so editing them is
a re-baseline (`reproduce/METHOD_ID`; `scripts/check-guest-inputs.sh`).

### fold

Composing two adjacent range proofs `[a..b] + [b+1..c] -> [a..c]` when the left out-boundary equals the
right in-boundary in full; `range_work` sums (`docs/SPEC.md` §10). Guest mode 7 `fold_range()`; host
`fold-range`; worker `hazync fold`. The coordinator offers only aligned sibling pairs of equal width
whose parent is missing, so folds build a tree (`foldable()`, `GET /api/foldable`).

### frontier

The highest block `hi` of the most-work chain of verified ranges that starts at genesis and is
seam-continuous: tip linkage, full-boundary digest `in_bhash == out_bhash` and `lo == hi + 1`
(`_frontier_chain`, `frontier_hi`, `coordinator/server.py`). Published as `frontier` in `/api/meta` and
`/api/state`. It moves with any contiguous verified chain from genesis, so it runs ahead of the spine,
which needs absorptions.

### maximal-Core

As much of the result decided by Bitcoin Core's own code as possible, while allowing narrow
substitutions at interfaces Core or libsecp already parameterise. `patches/0002` (SHA-256 through the
risc0 accelerator) is the precedent (`docs/history/CORE_VS_GHOST.md` §1). Earlier use in `reproduce/METHOD_ID`
(2026-07-26) means sourcing all consensus constants from Core's `chainparams.cpp`.

<a id="method-id"></a>

### METHOD_ID, guest id, re-baseline, lineage

- **METHOD_ID** / **guest id** / **image id** — the risc0 image id of the guest ELF; every receipt is
  verified against it and the guest commits it as `self_id`. Canonical value: the bare hex line in
  `reproduce/METHOD_ID`; printed by `host method-id`; served as `method_id` by `GET /api/meta`
  (`expected_method_id`, `coordinator/server.py`).
- **re-baseline** — any change that moves the id. This includes line-moving comment edits in guest
  source or `#[path]`-included files, because panic metadata embeds line numbers (`reproduce/METHOD_ID`).
  Every existing proof stops verifying against the new id. `scripts/rebaseline-id.sh <id>` re-points
  embedded copies; `scripts/check-guest-inputs.sh` lists what counts as guest input.
- **lineage** — every id that has been canonical, in order, with its risc0/rzup versions:
  `reproduce/LINEAGE.tsv`. It is derived from git history by `scripts/lineage.sh` (`--check` is the CI gate)
  and needs a full clone.

### proof party, board, contributor, handle, sponsor

- **proof party** — the public effort to prove the chain from genesis (`CONTRIBUTING.md`,
  `coordinator/README.md`).
- **board** — the public coordinator at https://bitcoinghost.org/hazync and its dashboard; also the
  `board` list of ranges in `/api/state` (`state()`, `coordinator/server.py`).
- **contributor** — an ed25519 public key (`contributors` table). `hazync id` creates it under
  `$HAZYNC_HOME` (default `~/.hazync`, `key.hex`). `POST /api/rotate` (`rotate()`) moves attribution to
  a new key; both keys must sign, and history is not rewritten (#113). The operator can revoke a rotation,
  recorded in `rotation_revocations` (`coordinator/revoke-rotation.py`, #311).
- **handle** — the display label for a key, capped at `MAX_HANDLE` (default `48`) and HTML-stripped
  (`clean_handle`). The default is `ghost:<first 6 hex of pubkey>` (`identity()`, `coordinator/hazync`).
- **sponsor** — someone paying for a span to be proven (`docs/SPONSORSHIP.md`; `/api/sponsor*` routes).
  Handles folding to `sponsor…` are refused unless the key is registered in `sponsor_keys`; the sponsor
  bot (`coordinator/sponsor_bot.py`, `docs/SPONSOR_BOT.md`) submits as `SPONSOR: <name>`
  (`SPONSOR_HANDLE_PREFIX`, `handle_refused`). ⚠ `docs/SPONSORSHIP.md`'s status line says the proving bot
  does not exist; `coordinator/sponsor_bot.py` does, and `docs/SPONSOR_BOT.md` says it has never run live.

### RangeState, journal

The **journal** is the public output a receipt commits. For every range proof it is a `RangeState`:
`kind` (`KIND_RANGE = 0xC4A10006`), `lo`, `hi`, the in-boundary, the out-boundary, `range_work` and
`self_id`. It decodes positionally, so field order is part of the format (`rangestate/src/lib.rs`;
`docs/SPEC.md` §8). `scripts/check-rangestate.sh` keeps the guest, host and verifier copies in step.

### seg-serve, seg-connect

Distributed proving of one block across machines. `host seg-serve` (`seg_serve_cmd`) is the ephemeral
**segment coordinator**: it executes the guest, publishes segments, joins and resolves as work, and
assembles the receipt. `host seg-connect <host:port>` (`seg_connect_cmd`) is a worker that proves what
it is sent and holds no state. `HAZYNC_AGG=1` makes `seg-serve` build the aggregate (mode 5)
environment. ⚠ This is not the board coordinator (`CONTRIBUTING.md`; `docs/FLEET_OPERATIONS.md`).

<a id="segment"></a>

### segment, po2, seg_po2

- **segment** — risc0 splits one execution into segments of at most `2^po2` cycles, each proved
  independently and then lifted and joined.
- **po2** / **`seg_po2`** — that exponent. `seg_po2()` (`prover/host/src/main.rs`) returns `HAZYNC_SEG_PO2`
  if set, else `21` for a CUDA build and `20` otherwise; `host seg-po2` prints it. On failure the worker
  retries with smaller segments down to `18` (`_seg_ladder`, `_SEG_MIN`, `coordinator/hazync`).
- ⚠ `spine_segments()` / `GET /api/spine/segments` uses "segment" for a run of spine blocks absorbed by
  one contributor, which is unrelated.

### spine

The single genesis-anchored head `[1..N]` with the largest `N`, advanced only by absorption
(`docs/SPEC.md` §10.1). The coordinator verifies and stores it but does not build it
(`submit_spine`, `verify_spine`, `spine_head`, `coordinator/server.py`). It is served at `GET /api/spine`
(metadata) and `GET /api/spine/proof` (receipt). Absorption is the only serial step, so
`run-workers.sh` warns to run one spine worker fleet-wide.

### STARK receipt, succinct, Groth16 SNARK wrap

- **STARK receipt** — risc0's native proof. A *composite* receipt is a list of segment receipts; a
  **succinct** receipt (`ProverOpts::succinct()`) is one recursion-compressed STARK of fixed size.
  Chunk, aggregate and fold proofs are produced succinct (`prover/host/src/main.rs`).
- **Groth16 SNARK wrap** — compressing a succinct receipt to a Groth16 proof over BN254
  (`ProverOpts::groth16()`; host `snark-wrap`, `snark_wrap_cmd`; checked by `verify-snark`, which
  re-applies the genesis pin). Verification becomes three pairings. Figures:
  `prover/evidence/groth16_snark_wrap.txt`, `docs/PROVING.md`.

### straggler

A block proves in the time of its slowest chunk, so the straggler ratio is max chunk cost ÷ mean chunk
cost across a block's chunks. The packer prints it as `straggler (...): max ... vs mean ... = ...x`
(`prover/host/src/main.rs`, chunk packing report). It caps how much adding cards helps
(`docs/BUILDS.md` §1 and §3.1).

### Tier 0

The guest-codegen speed axis that costs no fidelity: C `-O3` for libsecp, Rust `lto = "fat"` +
`codegen-units = 1`, and `ECMULT_WINDOW_SIZE` tuning (`docs/history/TIER0_RESULTS_2026-08-26.md`). The
settings are in `prover/methods/guest/build.rs` ("TIER 0") and `prover/methods/guest/Cargo.toml`
`[profile.release]`.

### vrange

A verified range: a row in the coordinator's `vranges` table written when a submission verifies, holding
`lo`, `hi`, `in_tip`, `out_tip`, boundary digests, `range_work`, pubkey and handle (`init_db`, `submit`,
`coordinator/server.py`). The frontier is computed over vranges; `GET /api/vranges` serves them.

### witness, bundle, bridge

- **witness** — the host-supplied, untrusted per-block input: header, transactions, prevouts,
  per-input accumulator proofs and boundaries (`docs/SPEC.md` §6). The guest derives what it can instead
  of reading it.
- **bridge** — an archive node that replays the chain through the accumulator and emits a bundle per
  block. Host `bridge` (`cmd_bridge`) writes to `HAZYNC_BRIDGE_OUT` (default `/root/bridge_bundles`) up
  to tip minus `HAZYNC_BRIDGE_FINALITY` (default `100`) (`docs/history/HAZYNC_ARCHITECTURE.md`, "Archive-node
  bridge").
- **bundle** — `bundle_<n>.json`: a block's in-boundary (`in_roots`, …) plus its `witness`. That is
  enough to prove the block with no replay (`prove-range-bridge`). Served by `GET /api/witness/<n>` (in
  bulk by `GET /api/witnesses`), which falls back to a legacy `block_<n>.json` witness (`bundle_path`,
  `coordinator/server.py`; `fetch_bundles`, `coordinator/hazync`).
