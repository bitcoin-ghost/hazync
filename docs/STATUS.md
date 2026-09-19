# Status — 2026-09-15

One page for where the project stands. It replaces the status blocks scattered through other docs, and
it is rewritten at each release ([`RELEASE_PROCESS.md`](RELEASE_PROCESS.md)). Goals are in
[`GOALS.md`](GOALS.md); every release is in [`../CHANGELOG.md`](../CHANGELOG.md). Board figures move by the
minute, so use the live API; the snapshot below is dated.

## Release

- **In preparation: v0.21.7** ([notes](history/releases/RELEASE_NOTES_v0.21.7.md)) — a board block across many
  cards (mode 6, #361/#364) driven by one command, `hazync run --distributed` (#367/#408); the bridge's height
  cap removed (#398); any bundle in the gap rebuildable on demand (#374, #377, #378); sponsorship payments
  through BTCPay (#371, #372). Not a re-baseline: `METHOD_ID 37987b85` is unchanged, reproduced twice on
  2026-09-18 from separate commits.
- **Latest: [v0.21.6](https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.6)**, published
  2026-09-15 ([notes](history/releases/RELEASE_NOTES_v0.21.6.md)). Assets: `hazync-host-x86_64-linux-gnu`,
  `hazync-host-x86_64-linux-gnu-cuda`, `hazync-worker`, `hazync-run-workers.sh`, `hazync-coordinator.py`,
  `hazync-verify-x86_64-linux-gnu`, `hazync-verify-aarch64`, `hazync-verify.wasm`, `SHA256SUMS.txt`,
  `SHA256SUMS.txt.asc`. Check them as in [`SECURITY.md`](../SECURITY.md#verifying-releases).

## Guest

- **Canonical `METHOD_ID`: `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`**
  ([`reproduce/METHOD_ID`](../reproduce/METHOD_ID)), canonical since 2026-09-07. v0.21.1-v0.21.6 did not
  re-baseline.
- Inputs: Bitcoin Core v28.0, secp256k1 v0.5.1, risc0 `=3.0.5` (rzup `cargo-risczero` 3.0.5, rust 1.94.1,
  cpp 2024.1.5).
- The CORE channel ships: Core patches `0001`/`0002` and libsecp patches `0012` (field backend) and `0013`
  (`lift_x` hint), applied by `provision-vps.sh`. See [`BUILDS.md`](BUILDS.md).
- Lineage: 17 canonical ids in [`reproduce/LINEAGE.tsv`](../reproduce/LINEAGE.tsv), gated by
  `scripts/lineage.sh --check`.

## Board

Live: [`/api/state?slim=1`](https://api.hazync.org/api/state?slim=1) ·
[`/api/meta`](https://api.hazync.org/api/meta) ·
[`/api/spine`](https://api.hazync.org/api/spine) ·
[`/api/spine/proof`](https://api.hazync.org/api/spine/proof) (check with `hazync-verify`). Every
route: [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md). `api.hazync.org/api` is the endpoint workers use; `bitcoinghost.org/hazync/api` was retired
on 2026-09-16 (no worker had used it since 15 Sep).

Snapshot at **2026-09-15 16:58 UTC**:

| | |
|---|---|
| proven | 70,337 blocks |
| folded | 44,882 blocks, 37,922 folds |
| frontier (genesis-anchored, contiguous) | 70,031 |
| spine | `[1..45,648]` |
| chain tip / `pct` | 967,160 / 7.2 |
| contributors | 7 |
| `/api/meta` `method_id` | canonical |
| `/api/meta` `source_sha256` | equals `coordinator/server.py` at `aef03a8` (coordinator restarted 16:19 UTC) |
| sponsorship (`/api/sponsor`) | `open: false`, `payments: false`, `priced: true` |

## Shipped in v0.21

| release | date | what |
|---|---|---|
| v0.21.0 | 2026-09-07 | CORE becomes the shipped guest (`0012`/`0013`); re-baseline to `37987b85`, board reset |
| v0.21.1 | 2026-09-11 | #119 fixed in a vendored `risc0-circuit-rv32im-sys`; receipts no longer fail their own `verify()` |
| v0.21.2 | 2026-09-11 | workers fail loudly (#261, #256, #268); spine absorbs the widest chunk (#272); API does not jam (#265) |
| v0.21.3 | 2026-09-12 | refuse ranges that cannot join genesis and publish the blocker (#281/#283/#284); assembly is not a stall (#286); proved/folded/anchored attribution (#244); `.hzk` names (#278) |
| v0.21.4 | 2026-09-13 | workers report their release (#293); a checkout cannot write to the public board; only single blocks introduce coverage (#281) |
| v0.21.5 | 2026-09-15 | signed claims (#323) with claim grace, per-key cap and re-take wait (#297/#319/#321); worker push alerts (#326); one spelling per range id (#320); block 0 refused (#313); R2 off-site copies (#328) |
| v0.21.6 | 2026-09-15 | workers default to `api.hazync.org` (#332); fold claims (#334); a claim cannot overwrite a just-proven block (#340); no prover from the current directory (#337); spine and sponsor keys off the box, B2 second copies, integrity checks (#331/#335/#336/#338) |

## Open issues

From GitHub on 2026-09-16.

| # | title |
|---|---|
| [#252](https://github.com/bitcoin-ghost/hazync/issues/252) | Aggregate assembly is latency, not work; two of three levers shipped, the measurement has not run |
| [#253](https://github.com/bitcoin-ghost/hazync/issues/253) | Measure #236 (streaming `seg-serve` execute), which shipped unmeasured in v0.21.1 |
| [#277](https://github.com/bitcoin-ghost/hazync/issues/277) | Anchor warp: prove backwards from the anchor in the tip cluster's idle time |
| [#310](https://github.com/bitcoin-ghost/hazync/issues/310) | `/api/claim` is unsigned, so anyone can hold blocks under any public key (workers sign from v0.21.5; closes with `CLAIM_REQUIRE_SIG=1`) |
| [#347](https://github.com/bitcoin-ghost/hazync/issues/347) | Prune bridge bundles once their block is finished for good, keeping checkpoints to rebuild them |
| [#350](https://github.com/bitcoin-ghost/hazync/issues/350) | ⛔ **Measured, no longer a projection (2026-09-18/19)**: the tip bridge hits `MemoryMax=44G` at h=798,257 (108.06M UTXOs), throttles at 99% pressure, then OOMs — **23 kills** 20:27–06:35, ~36 alerts (two hooks per kill) (count from `journalctl`; `dmesg` is a ring buffer and showed only 2). Highest checkpoint ever **800,257** against a tip of 967,626, and it has not advanced past it. `systemd` shows `active (running)` while frozen. Provers unaffected: ~322,000 blocks of bundles sit ahead of the frontier |
| [#351](https://github.com/bitcoin-ghost/hazync/issues/351) | Sponsor bot should use the coordinator's API, not write `coordinator.db` directly |

## Decisions for the operator

Each verified on 2026-09-14; detail in [`THREAT_MODEL.md`](THREAT_MODEL.md#open-items).

- **When to set `CLAIM_REQUIRE_SIG=1`**: once contributors run v0.21.5 or later (the leaderboard's release
  column shows it).
- **Field-backend gate 4**: run the corrupt-signature negative control on the CORE guest
  ([`FIELD_BIGINT2_BACKEND.md`](FIELD_BIGINT2_BACKEND.md) §5b records it not run).
- **The accumulator reference fuzz control**: rerun it (`audit-fuzz/FINDINGS.md` marks it "NEEDS A RERUN"
  since #63).
- **Whether to publish CORE card-years**: [`GOALS.md`](GOALS.md) G2's 44–73 L40S card-years are INFERRED
  from one near-tip block.

## Also open, tracked

- #310 until signed claims are required (`CLAIM_REQUIRE_SIG=1`). #311 is closed: key rotation is off unless
  `ROTATE_ENABLED=1` (#348), so a stolen `key.hex` cannot move a contributor's blocks.
- The worker reads coordinator responses without a bound (`get()` in `coordinator/hazync`).
- Sponsorship payments are not built ([`SPONSORSHIP.md`](SPONSORSHIP.md)).
- Fold claims (#334) are live on the coordinator; whether refused folds fall to near zero is not measured until
  folders run v0.21.6.

## Resolved on 2026-09-14 and 2026-09-15

- **Reporting route**: security and conduct reports go through GitHub private vulnerability reporting
  ([`SECURITY.md`](../SECURITY.md#reporting-a-vulnerability), [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)).
- **Everything that cannot be rebuilt cheaply has two off-site copies**: receipts, the spine, the sponsor keys
  (encrypted) and the ledger, in Cloudflare R2 and Backblaze B2, checked daily with restore drills
  (#328, #331, #335, #338).
- **The chain is re-verified on a schedule**: the genesis proof against Bitcoin every 10 minutes (also from the
  web box), continuity every 10 minutes, every stored proof nightly (#336).
- **#312**: the worker no longer runs a prover found in the current directory (#337).
- **`docs/COORDINATOR_REFERENCE.md` drift** stays a failing CI check.
