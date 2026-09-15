# Status — 2026-09-15

One page for where the project stands. It replaces the status blocks scattered through other docs, and
it is rewritten at each release ([`RELEASE_PROCESS.md`](RELEASE_PROCESS.md)). Goals are in
[`GOALS.md`](GOALS.md); every release is in [`../CHANGELOG.md`](../CHANGELOG.md). Board figures move by the
minute, so use the live API; the snapshot below is dated.

## Release

- **Latest: [v0.21.5](https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.5)**, published
  2026-09-15 ([notes](history/releases/RELEASE_NOTES_v0.21.5.md)). Assets: `hazync-host-x86_64-linux-gnu`,
  `hazync-host-x86_64-linux-gnu-cuda`, `hazync-worker`, `hazync-run-workers.sh`, `hazync-coordinator.py`,
  `hazync-verify-x86_64-linux-gnu`, `hazync-verify-aarch64`, `hazync-verify.wasm`, `SHA256SUMS.txt`,
  `SHA256SUMS.txt.asc`. Check them as in [`SECURITY.md`](../SECURITY.md#verifying-releases).

## Guest

- **Canonical `METHOD_ID`: `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`**
  ([`reproduce/METHOD_ID`](../reproduce/METHOD_ID)), canonical since 2026-09-07. v0.21.1-v0.21.5 did not
  re-baseline.
- Inputs: Bitcoin Core v28.0, secp256k1 v0.5.1, risc0 `=3.0.5` (rzup `cargo-risczero` 3.0.5, rust 1.94.1,
  cpp 2024.1.5).
- The CORE channel ships: Core patches `0001`/`0002` and libsecp patches `0012` (field backend) and `0013`
  (`lift_x` hint), applied by `provision-vps.sh`. See [`BUILDS.md`](BUILDS.md).
- Lineage: 17 canonical ids in [`reproduce/LINEAGE.tsv`](../reproduce/LINEAGE.tsv), gated by
  `scripts/lineage.sh --check`.

## Board

Live: [`/api/state?slim=1`](https://bitcoinghost.org/hazync/api/state?slim=1) ·
[`/api/meta`](https://bitcoinghost.org/hazync/api/meta) ·
[`/api/spine`](https://bitcoinghost.org/hazync/api/spine) ·
[`/api/spine/proof`](https://bitcoinghost.org/hazync/api/spine/proof) (check with `hazync-verify`). Every
route: [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md).

Snapshot at **2026-09-15 03:36 UTC**:

| | |
|---|---|
| proven | 69,162 blocks |
| folded | 36,256 blocks, 30,368 folds |
| frontier (genesis-anchored, contiguous) | 68,825; next block 68,826 unclaimed for 16,316 s (`needs_attention`) |
| spine | `[1..37,096]` |
| chain tip / `pct` | 967,070 / 7.1 |
| contributors | 7 |
| `/api/meta` `method_id` | canonical |
| `/api/meta` `source_sha256` | equals `coordinator/server.py` at `604d4ef` |
| sponsorship (`/api/sponsor`) | `open: false`, `payments: false`, `priced: true`, `btc_usd: null` |

## Shipped in v0.21

| release | date | what |
|---|---|---|
| v0.21.0 | 2026-09-07 | CORE becomes the shipped guest (`0012`/`0013`); re-baseline to `37987b85`, board reset |
| v0.21.1 | 2026-09-11 | #119 fixed in a vendored `risc0-circuit-rv32im-sys`; receipts no longer fail their own `verify()` |
| v0.21.2 | 2026-09-11 | workers fail loudly (#261, #256, #268); spine absorbs the widest chunk (#272); API does not jam (#265) |
| v0.21.3 | 2026-09-12 | refuse ranges that cannot join genesis and publish the blocker (#281/#283/#284); assembly is not a stall (#286); proved/folded/anchored attribution (#244); `.hzk` names (#278) |
| v0.21.4 | 2026-09-13 | workers report their release (#293); a checkout cannot write to the public board; only single blocks introduce coverage (#281) |
| v0.21.5 | 2026-09-15 | signed claims (#323) with claim grace, per-key cap and re-take wait (#297/#319/#321); worker push alerts (#326); one spelling per range id (#320); block 0 refused (#313); R2 off-site copies (#328) |

## Open issues

From GitHub on 2026-09-15.

| # | title |
|---|---|
| [#209](https://github.com/bitcoin-ghost/hazync/issues/209) | Ghost's next build: the four levers reopened by "fastest wins" |
| [#244](https://github.com/bitcoin-ghost/hazync/issues/244) | Proof durability: layer 1 (lineage) shipped; layer 2 (accepted set of method ids) needs a decision |
| [#252](https://github.com/bitcoin-ghost/hazync/issues/252) | Aggregate assembly is latency, not work; two of three levers shipped, the measurement has not run |
| [#253](https://github.com/bitcoin-ghost/hazync/issues/253) | Measure #236 (streaming `seg-serve` execute), which shipped unmeasured in v0.21.1 |
| [#277](https://github.com/bitcoin-ghost/hazync/issues/277) | Anchor warp: prove backwards from the anchor in the tip cluster's idle time |
| [#310](https://github.com/bitcoin-ghost/hazync/issues/310) | `/api/claim` is unsigned, so anyone can hold blocks under any public key (workers sign from v0.21.5; closes with `CLAIM_REQUIRE_SIG=1`) |
| [#311](https://github.com/bitcoin-ghost/hazync/issues/311) | Key rotation cannot be revoked, so a stolen key can take a contributor's attribution for good |
| [#312](https://github.com/bitcoin-ghost/hazync/issues/312) | `_find_host()` will run a prover binary it finds in the current directory |

## Decisions for the operator

Each verified on 2026-09-14; detail in [`THREAT_MODEL.md`](THREAT_MODEL.md#open-items).

- **When to set `CLAIM_REQUIRE_SIG=1`**: once contributors run v0.21.5 (the leaderboard's release column
  shows it).
- **#244 layer 2**: which method ids a verifier accepts.
- **Field-backend gate 4**: run the corrupt-signature negative control on the CORE guest
  ([`FIELD_BIGINT2_BACKEND.md`](FIELD_BIGINT2_BACKEND.md) §5b records it not run).
- **The accumulator reference fuzz control**: rerun it (`audit-fuzz/FINDINGS.md` marks it "NEEDS A RERUN"
  since #63).
- **Whether to publish CORE card-years**: [`GOALS.md`](GOALS.md) G2's 44–73 L40S card-years are INFERRED
  from one near-tip block.

## Also open, tracked

- The coordinator and worker weaknesses in #311 and #312, and #310 until signatures are required.
- The worker reads coordinator responses without a bound (`get()` in `coordinator/hazync`).
- Sponsorship payments are not built ([`SPONSORSHIP.md`](SPONSORSHIP.md)).
- Off-site copies: a second provider (B2) and the spine are not mirrored yet (#328).

## Resolved on 2026-09-14 and 2026-09-15

- **Reporting route**: security and conduct reports go through GitHub private vulnerability reporting
  ([`SECURITY.md`](../SECURITY.md#reporting-a-vulnerability), [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)).
- **Proof receipts have an off-site copy**: hourly append-only mirror to Cloudflare R2, and Litestream for
  the ledger, both alerting (#328).
- **`docs/COORDINATOR_REFERENCE.md` drift** stays a failing CI check.
