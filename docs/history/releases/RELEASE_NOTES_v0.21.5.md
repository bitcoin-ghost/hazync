# Hazync v0.21.5 — claims that belong to their key, and workers that tell you when they stop

> Copy of the GitHub release body at the time; the GitHub release is canonical: <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.5>

Most of this release comes from one key on 2026-09-13 and 09-14. `ghost:dda215` claimed blocks it never
proved (no heartbeats, no submissions, ever), re-claimed them as fast as they were released, and held the
frontier at 67,531 for over three hours. The coordinator half of the fix has been live on the board since
2026-09-14. This release ships the **worker** half, which signs its claims, and a worker that pushes to
your phone when it stops or cannot work.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. The guest, the circuit and the
> verifier logic are unchanged (`git diff v0.21.4..v0.21.5 -- prover/methods/guest coinbase-smt/src` is
> empty). Every existing proof stays valid and the board does not reset. On the prover side only the host's
> lockfile moved: rustls 0.23.42 → 0.23.45 for RUSTSEC-2026-0285 (#322).

---

## Provers: what to do

1. **Upgrade `hazync-worker` and `hazync-run-workers.sh`** (verify `SHA256SUMS.txt.asc` first). The host
   binaries are rebuilt from this release's source but prove exactly as before.
2. **Turn on alerts:** `hazync notify new`, then subscribe to the topic it prints (#326).
3. **Check the clock.** Signed claims are refused outside `BEAT_SKEW`, and a refused clock now pushes.

A quick start and the full operator's manual are in
[`docs/PROVER_OPERATIONS.md`](../../PROVER_OPERATIONS.md) (#327): graceful stops that do not strand a block,
a systemd unit, upgrading, disk, rented GPUs, and a symptom → cause → fix table.

## Claims belong to their key

Claims are an allocation hint: submission is free-running and nothing here refuses anyone's proof. These
rules only decide who is **offered** a block. All four have been live on the coordinator since 2026-09-14.

- **A claim nobody works on is released in minutes (#297, closes #296).** `CLAIM_GRACE` (600 s) applies
  only to a claim that has never sent a heartbeat. A slow prover beats as each segment lands, so it keeps
  the full `CLAIM_TTL` however long the block takes. 600 s is measured: over 55,920 ranges, claim →
  verified is p99 319 s.
- **One key holds at most 4 live claims (#319).** `CLAIM_OPEN_MAX`, `0` for no limit. A further claim gets
  429 until one is proven or lapses. Run more than 4 GPUs under one key? Raise it on your coordinator or use
  more keys.
- **A key cannot re-take a block its own silent claim let lapse (#321).** `CLAIM_RETAKE_WAIT` (3600 s).
  Every other key gets the block as soon as the grace ends.
- **Claims are signed (#323, refs #310).** `/api/claim` checked no signature, so anyone could claim under
  any public key, and with #319 and #321 that became a way to slow down a chosen prover. **This release's
  worker signs `claim:<nonce>:<ts>`.** The coordinator counts signed and unsigned claims apart, so unsigned
  claims under your key cannot use up your signed cap or re-take wait. A bad signature is refused, never
  treated as unsigned.

⏰ **Next:** once contributors are on v0.21.5 the coordinator will set `CLAIM_REQUIRE_SIG=1`, and unsigned
claims (every worker before this release) will get 403. That will be announced before it happens.

## Workers that tell you (#326)

A worker that stopped, or failed every block, used to say so only in a log nobody reads.

- **`hazync notify new`** creates a private ntfy.sh topic, saves it to `$HAZYNC_HOME/ntfy` (mode 600) and
  sends a test push. `hazync notify <topic or URL>` uses your own server; `test`, `off`, and no argument for
  status. `HAZYNC_NTFY` overrides the saved address.
- **The worker pushes** when it stops for good (exit 78: no usable GPU, guest id mismatch), when the
  coordinator rejects a proof (not "already proven", which is a lost race), and when a claim is refused for
  a reason the box must fix (clock, signature, reserved handle).
- **The launcher pushes** when it refuses to start, once after `NOTIFY_FAIL_STREAK` failures in a row
  (default 5), again on recovery, and urgently when a loop stops for good.
- **"Nothing to claim right now" exits 75**, not 1: nothing available, your claim cap, or the rate limit.
  The launcher waits 30 s and does not count it as a failure.
- One problem is pushed at most once per `HAZYNC_NTFY_REPEAT` (3600 s). A push never stops or slows the
  work. It carries handle, host, a block id and an error or log tail, never the key; the topic is the only
  secret, and the ntfy server sees every alert.

## Other worker changes

- **`hazync run N` does not prove a block that already has its own proof (#320).** It asks `/api/block/N`
  first. A block covered only by a wide range can still get its own proof, which is how 30000..30249 were
  repaired.
- **`hazync run 0` is refused before any GPU work (#313)** and names the command to run instead. Block 0 is
  the genesis anchor every range is pinned to; it cannot be proved.
- **Output is line-buffered (#302),** so a long-running spine worker's log keeps up with the board.

## The coordinator (live since 2026-09-14; `hazync-coordinator.py` in this release)

- **Board requests do not wait (#325, closes #324).** `/api/state` and `/api/blockstatus` hand back the
  previous value and rebuild in the background. Measured on a copy of the live database: p90 2.82 s → 0.019 s.
  A cold start, or an entry more than `CACHE_MAX_STALE` (60 s) stale, still rebuilds in line so a failing
  rebuild surfaces as an error.
- **`/api/vranges` cannot exhaust file descriptors (#300).** On 2026-09-13 overlapping rebuilds reached the
  1,024 open-file limit and the board refused proofs for ~21 minutes. The rebuild is now single-flight, its
  TTL 120 s, and the coordinator raises its soft limit to 65,536. `/api/state` gains `progress.folds`.
- **One spelling per range id (#320).** `5-5`, `05`, `+5` and ` 5` were each accepted, verified and stored
  as another copy of block 5. A non-canonical id now gets 400 naming the canonical one, before any
  verification. The 409 for an already-proven block names who proved it and where to download the proof.
- **Block 0 is refused everywhere (#313)** before any row, signature check or verification, which also
  closes the genesis-seed hole #281 left: an unbacked `[0..hi]` added `hi` blocks of coverage. The G1
  retention check counts from block 1.
- **A big block under one live, beating claim is waiting, not stuck (#303).** Block 55,862 (3,261 segments,
  9,830 s) was flagged "may be failing on a bug already fixed". `needs_attention` now separates a re-taken
  claim and a silent one from a continuous, heartbeating hold.
- **Block status and block detail (#301):** `GET /api/blockstatus` returns runs `[lo, hi, status]` (950 B
  gzipped against 342 KB for `/api/vranges`), `?prover=<name>` filters, and `GET /api/block/<n>` returns
  every proof covering one block. ETags now match nginx's weak form, so revalidation returns 304.
- **Operator alerts (#313):** `OnFailure=` on the retention check, backup, node tip, coordinator and bridge,
  a crash hook for the two `Restart=always` services, and an off-box watchdog. RUNBOOK § Alerts.

## Sponsorship: built, still closed

Nothing here is open to the public: `/api/sponsor` reports `open: false, payments: false`, and payments are
not built ([`SPONSORSHIP.md`](../../SPONSORSHIP.md)).

- **Records and quotes (#301),** priced per block by height on a ladder (#315): $1 to 200,000, then $1 more
  per 200,000 blocks, $6 above 1,000,000. A quote reports `waiting`, blocks with no bundle yet (none exist
  above 230,000). The $3–$5 bands are not checked against a measurement.
- **Holds (#304):** a paid sponsorship's blocks are never offered to other workers, and only that
  sponsorship's registered key can prove them.
- **The proving bot (#305, #306, #309)** rents one GPU per pod on RunPod under hard caps (`--max-pods`,
  `--max-usd`, `--max-usd-per-hour`) and terminates every pod on exit, error, stall or cap. Two live trials
  on 2026-09-14 found and fixed a Cloudflare-refused User-Agent and a launch that hung ssh; the second cost
  $0.054 in all. Its blocks are credited to `SPONSOR: <name>`, a prefix only registered keys may use.

## Operations

- **Off-site copies in Cloudflare R2 (#328).** Until 2026-09-15 proof receipts had no copy off the
  coordinator's disk. An hourly append-only mirror uploads new receipts and checks none older than 30 min
  is missing (it alerts if one is); the initial copy was 93,134 receipts, 21.34 GB, 0 failures. Litestream
  replicates the ledger about a second behind, and a restore drill from R2 took 4 s with an identical
  database. Not yet: a second provider (B2) and the spine.
- **Docs audit (#307, #308, #316, #317, #298).** Commands that failed, wrong operating facts and an
  overstated trust base fixed; finished docs moved to `docs/history/`; new `CHANGELOG.md`,
  `COORDINATOR_REFERENCE.md` (generated, CI-checked), `THREAT_MODEL.md`, `GLOSSARY.md`, `STATUS.md`,
  `RELEASE_PROCESS.md`; and a written position on surviving a `METHOD_ID` change (#244).
- **A flaky test fixed (#318):** the sponsor bot's spending-cap test measured machine latency; it now runs
  on simulated time.

## Known and open

- **#311:** key rotation cannot be revoked. **#312:** `_find_host()` will run a prover binary found in the
  current directory.
- **#310** stays open until `CLAIM_REQUIRE_SIG=1`: until then unsigned claims under a key can still crowd
  out that key's own unsigned claims (pre-v0.21.5 workers), and a per-key cap does not limit an attacker
  with many fresh keys.

## Assets

`hazync-host-x86_64-linux-gnu`, `hazync-host-x86_64-linux-gnu-cuda`, `hazync-worker`,
`hazync-run-workers.sh`, `hazync-coordinator.py`, `hazync-verify-x86_64-linux-gnu`, `hazync-verify.wasm`,
plus `hazync-verify-aarch64`, `SHA256SUMS.txt` and `SHA256SUMS.txt.asc` from the signing workflow. Check
them as in [`SECURITY.md`](../../../SECURITY.md#verifying-releases).
