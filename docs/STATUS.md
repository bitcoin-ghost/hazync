# Status — 2026-09-14

One page for where the project stands. It replaces the status blocks scattered through other docs, and
it is rewritten at each release. Goals are in [`GOALS.md`](GOALS.md); what changed in each release is in
[`../CHANGELOG.md`](../CHANGELOG.md) and the `RELEASE_NOTES_*` files. Board figures move by the minute, so
use the live API; the snapshot below is dated.

## Release

- **Latest: [v0.21.4](https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.4)**, published
  2026-09-13 ([`RELEASE_NOTES_v0.21.4.md`](RELEASE_NOTES_v0.21.4.md)). Assets: `hazync-host-x86_64-linux-gnu`,
  `hazync-host-x86_64-linux-gnu-cuda`, `hazync-worker`, `hazync-run-workers.sh`, `hazync-coordinator.py`,
  `hazync-verify-x86_64-linux-gnu`, `hazync-verify-aarch64`, `hazync-verify.wasm`, `SHA256SUMS.txt`,
  `SHA256SUMS.txt.asc`. Check them as in [`SECURITY.md`](../SECURITY.md#verifying-releases).
- `main` is 8 commits past the tag, at `951a08a`.

## Guest

- **Canonical `METHOD_ID`: `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`**
  ([`reproduce/METHOD_ID`](../reproduce/METHOD_ID)), canonical since 2026-09-07. v0.21.1-v0.21.4 did not
  re-baseline.
- Inputs: Bitcoin Core v28.0, secp256k1 v0.5.1, risc0 `=3.0.5` (rzup `cargo-risczero` 3.0.5, rust 1.94.1,
  cpp 2024.1.5).
- The CORE channel ships: Core patches `0001`/`0002` and libsecp patches `0012` (field backend) and `0013`
  (`lift_x` hint), applied by `provision-vps.sh`. See [`CORE_VS_GHOST.md`](CORE_VS_GHOST.md).
- Lineage: 17 canonical ids in [`reproduce/LINEAGE.tsv`](../reproduce/LINEAGE.tsv), gated by
  `scripts/lineage.sh --check`.

## Board

Live: [`/api/state?slim=1`](https://bitcoinghost.org/hazync/api/state?slim=1) ·
[`/api/meta`](https://bitcoinghost.org/hazync/api/meta) ·
[`/api/spine`](https://bitcoinghost.org/hazync/api/spine) ·
[`/api/spine/proof`](https://bitcoinghost.org/hazync/api/spine/proof) (check with `hazync-verify`). Every
route: [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md).

Snapshot at **2026-09-14 08:00:49 UTC**:

| | |
|---|---|
| proven | 65,636 blocks |
| folded | 24,606 blocks, 20,280 folds |
| frontier (genesis-anchored, contiguous) | 65,619 |
| spine | `[1..25,350]` |
| chain tip / `pct` | 966,936 / 6.786 |
| contributors | 7 |
| `verify_mode` / `signatures` | `real` / `ed25519` |
| `/api/meta` `method_id` | canonical |
| `/api/meta` `source_sha256` | equals `coordinator/server.py` at `951a08a` |
| sponsorship (`/api/sponsor`) | `open: false`, `payments: false`, `priced: true`, `btc_usd: null` |

## Shipped in v0.21

| release | date | what |
|---|---|---|
| v0.21.0 | 2026-09-07 | CORE becomes the shipped guest (`0012`/`0013`); re-baseline to `37987b85`, board reset |
| v0.21.1 | 2026-09-11 | #119 fixed in a vendored `risc0-circuit-rv32im-sys`; receipts no longer fail their own `verify()` |
| v0.21.2 | 2026-09-11 | workers fail loudly (#261, #256, #268); spine absorbs the widest chunk (#272); API does not jam (#265) |
| v0.21.3 | 2026-09-12 | refuse ranges that cannot join genesis and publish the blocker (#281/#283/#284); assembly is not a stall (#286); proved/folded/anchored attribution (#244); `.hzk` names (#278) |
| v0.21.4 | 2026-09-13 | workers report their release (#293); a checkout cannot write to the public board; only single blocks introduce coverage (#281) |

## Open issues

From GitHub on 2026-09-14.

| # | title |
|---|---|
| [#209](https://github.com/bitcoin-ghost/hazync/issues/209) | Ghost's next build: the four levers reopened by "fastest wins" |
| [#244](https://github.com/bitcoin-ghost/hazync/issues/244) | Proof durability: layer 1 (lineage) shipped; layer 2 (accepted set of method ids) needs a decision |
| [#252](https://github.com/bitcoin-ghost/hazync/issues/252) | Aggregate assembly is latency, not work; two of three levers shipped, the measurement has not run |
| [#253](https://github.com/bitcoin-ghost/hazync/issues/253) | Measure #236 (streaming `seg-serve` execute), which shipped unmeasured in v0.21.1 |
| [#277](https://github.com/bitcoin-ghost/hazync/issues/277) | Anchor warp: prove backwards from the anchor in the tip cluster's idle time |

Open pull requests: #306 (sponsor bot User-Agent), #307 and #308 (documentation).

## Needs the operator

Each verified on 2026-09-14; detail in [`THREAT_MODEL.md`](THREAT_MODEL.md#open-items).

- **Decide #244 layer 2**: which method ids a verifier accepts.
- **Run field-backend gate 4**, the corrupt-signature negative control, on the CORE guest
  ([`FIELD_BIGINT2_BACKEND.md`](FIELD_BIGINT2_BACKEND.md) §5b records it not run).
- **Rerun the accumulator reference fuzz control** (`audit-fuzz/`), not recorded since #63 changed the
  reference `Stump`.
- **CORE card counts and card-years rest on one block** (962,000); measure across eras before quoting them.
- **Bound the worker's reads** of coordinator responses (`get()` in `coordinator/hazync`).
- **Sponsorship**: payments are not built ([`SPONSORSHIP.md`](SPONSORSHIP.md)).
  [`SPONSOR_BOT.md`](SPONSOR_BOT.md) says the bot has never run live, yet the live leaderboard lists a
  `SPONSOR: Hazync trial` contributor; one of the two needs updating.
