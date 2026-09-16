# Hazync v0.21.6 — workers move to api.hazync.org, and folders stop colliding

> Copy of the GitHub release body at the time; the GitHub release is canonical: <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.6>

Workers now talk to the coordinator at **`https://api.hazync.org`**, Hazync's own name on its own web box.
Folders reserve the pair they fold instead of racing each other for the same eight. The rest is a claim bug that
could overwrite a proven block, a worker that no longer runs a prover it finds in the current directory, and
backups: the spine and sponsor keys off the box, a second copy of everything at another provider, and checks that
re-verify the chain against Bitcoin.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. Nothing under `prover/`, `verifier/`,
> `guest/`, `methods/` or `reproduce/` changed since v0.21.5, so every existing proof stays valid, the board
> does not reset, and the host binaries are the same build as v0.21.5.

---

## Provers: what to do

1. **Upgrade `hazync-worker` and `hazync-run-workers.sh`** (verify `SHA256SUMS.txt.asc` first). The host
   binaries are unchanged from v0.21.5; replacing them is harmless.
2. **Nothing to configure for the new address.** The worker reads its coordinator each time it starts and saves
   nothing, so an upgraded worker uses `api.hazync.org` on its next start. `COORD_URL` still overrides it.
3. **Folders:** fold claims are used only by a key with proven work. A new key folds unclaimed, exactly as before,
   until its first verified proof.

## Workers use api.hazync.org (#332)

- `DEFAULT_COORD` is `https://api.hazync.org`, served by the Hazync web box, which forwards to the coordinator.
- **`hazync.org` keeps working** as a proxy, never a redirect, so workers that do not upgrade are
  unaffected.
- Before this change shipped, the coordinator was set to trust the web box as a proxy (so workers behind it are
  rate-limited one by one, not as a single client), and all three board pods ran on `api.hazync.org` for a
  rehearsal: claims, submits, folds and spine updates all returned 200.

## Fold claims (#334, closes #333)

Nothing reserved a fold pair. `/api/foldable` offered the 8 lowest pairs, every folder picked one of the same 8,
and the second fold of a pair was refused `409 already proven`. On 2026-09-15 that was 1,809 refused folds in
14 hours, about one fold in five thrown away.

- **More candidates:** `/api/foldable` offers up to 32 pairs (`FOLDABLE_DEFAULT`). Older workers already pick at
  random, so they spread out as soon as the coordinator runs this.
- **60 s fold claims:** `POST /api/foldclaim`, signed over `foldclaim:<result>:<nonce>:<ts>`, reserves a pair for
  `FOLD_CLAIM_TTL` (60 s). A claimed pair is left out of `/api/foldable` for everyone.
- **Only a key with proven work may claim** (`403 unproven` otherwise), at most 2 live claims per key
  (`FOLD_CLAIM_CAP`, `429`). Keys are free to make, so without this a few throwaway keys could hide every pair.
- **Submitting stays open:** a valid fold is accepted from anyone, claimed or not, and a verified fold releases
  its claim at once. Claims live in memory; a restart forgets them.
- `/api/state` gains `fold_claims`, and `/api/block/<n>` gains `fold_claim`; hazync.org's block page shows
  "Being folded".
- The coordinator has run this since 2026-09-15 16:19 UTC. **Not measured yet:** whether refused folds fall to
  near zero once folders run this worker.

## A claim could overwrite a proven block (#340, closes #339)

`claim()` reads the frontier outside its lock and coverage inside it. A proof for frontier+1 landing between the
two reads made that block look like it could not join the frontier, so it was offered again, and the claim's
`INSERT OR REPLACE` turned its verified row back into a claim. Nine rows on the live board were affected.

- A cover now counts as unable to join only once the frontier snapshot has seen it for `FRONTIER_SETTLE` (30 s).
  A real blocker is still offered again, at most 30 s later. `/api/state` uses the same rule, so it stops flagging
  a block proven moments ago.
- `coordinator/repair-reclaimed-ranges.py` restores damaged rows from their verified submission, only when the
  block is at or below the frontier, the claim is dead, the key matches and the proof file hashes to the receipt.
  It is a dry run unless given `--apply`.

## The worker no longer runs a prover from the current directory (#337, closes #312)

With `HAZYNC_HOST` unset, the CLI searched the current directory for a prover binary, and a planted binary can
print the canonical `METHOD_ID`, so the id checks could not catch it. The current directory is no longer searched,
and the bare name `host` is accepted only beside the CLI. `HAZYNC_HOST` still wins. `run-workers.sh` always sets
`HAZYNC_HOST`, so its loops were never exposed.

## Operations: backups and integrity checks

- **The spine off the box (#331):** every 10 minutes the spine is verified with `hazync-verify` and copied to R2,
  one pair per height, never overwritten. Before this it existed only on the coordinator's disk.
- **Sponsor signing keys off the box, encrypted (#335):** hourly, to the operator's GPG key. Neither the server nor
  the storage provider can decrypt them. A restore was tested and the decrypted keys matched the live files.
- **A second provider, Backblaze B2 (#338):** receipts hourly, the spine every 10 minutes, the sponsor keys hourly,
  and a daily online snapshot of the ledger (Litestream allows one replica, and it goes to R2). The first copy was
  107,228 receipts, 24.6 GB, with 0 missing. The daily summary checks every copy in both places and restores the
  newest B2 ledger copy.
- **Integrity checks (#336):** every 10 minutes the genesis proof is checked against the archive node (hash and
  chainwork) and, from the web box, against mempool.space; block-by-block continuity every 10 minutes; every stored
  proof re-verified nightly. Failures push to the operator once, hourly while failing, and on recovery.
- **The daily summary waits a full hourly cycle (#330)** before calling a receipt missing (grace 1,800 → 7,200 s).

## Known and open

- **#310:** unsigned claims are still accepted until the coordinator sets `CLAIM_REQUIRE_SIG=1`, after
  contributors move to v0.21.5 or later.
- **#311:** key rotation cannot be revoked.
- **#341:** `test_sponsor_bot` fails about 3% of CI runs (a test-harness race, not a coordinator bug).
- Still open from before: #277 (anchor warp), #253 and #252 (unmeasured aggregate work), #244 (layer 2 of proof
  durability), #209.

## Assets

`hazync-host-x86_64-linux-gnu`, `hazync-host-x86_64-linux-gnu-cuda`, `hazync-worker`,
`hazync-run-workers.sh`, `hazync-coordinator.py`, `hazync-verify-x86_64-linux-gnu`, `hazync-verify.wasm`,
plus `hazync-verify-aarch64`, `SHA256SUMS.txt` and `SHA256SUMS.txt.asc` from the signing workflow. Check
them as in [`SECURITY.md`](../../../SECURITY.md#verifying-releases).
