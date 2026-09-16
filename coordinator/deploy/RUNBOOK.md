# Hazync Proof Party — coordinator deploy runbook

Stand up the coordinator **co-located with the archive-node bridge and a full `bitcoind`** on one box, and
wire it under **one domain** (`hazync.org`) through an nginx proxy — the box is invisible
backend infrastructure, like the existing `/api/pool/vmN/` proxies.

```
  hazync.org          the page (story + live board), served from the web root
  api.hazync.org/api/…    proxied to the bridge box (state / claim / submit / witness)
        one URL for people · one box for data
```

**Architecture (current — post-cutover 2026-07-23):** `bitcoind` (full node) → `hazync-bridge.service`
(drives one resident Utreexo forest forward and writes per-block bundles to `HAZYNC_BRIDGE_OUT`) →
coordinator (serves those bundles via `/api/witness/<n>` as a local read, and **verifies** submitted
receipts on CPU — no GPU). Proving + folding happen on contributors' GPU boxes. The older separate
cheap-CPU box with a pre-generated per-block-witness window is **retired** (see the note at the end).

---

## 0. Build the `host` binary (needs muscle, briefly)

Building `host` (RISC0 + Bitcoin Core) wants real RAM/CPU — a $10/mo box will choke. Build it once on a
capable box (or reuse a GPU box), then copy just the binary to the cheap coordinator.

```bash
git clone https://github.com/bitcoin-ghost/hazync /opt/hazync && cd /opt/hazync
./provision-vps.sh                 # CPU build (do NOT set GPU=1 — the coordinator only verifies)
# → /opt/hazync/prover/target/release/host
```

Verifying is light, so the cheap coordinator box runs the binary fine — only the *build* needs muscle.

## 1. Coordinator box

```bash
sudo useradd -r -m -d /opt/hazync -s /usr/sbin/nologin hazync   # or reuse an existing user
# Every data path in the shipped units is under /var/lib/hazync, and nothing here creates it. It must
# exist and be hazync-owned BEFORE the first start, or sqlite cannot create the DB and the service
# dies at startup with "unable to open database file".
sudo install -d -o hazync -g hazync /var/lib/hazync /var/lib/hazync/coord_state /var/lib/hazync/proofs \
     /var/lib/hazync/spine /var/lib/hazync/witnesses /var/lib/hazync/bridge_bundles
# place the repo at /opt/hazync (host binary at /opt/hazync/prover/target/release/host)
# The bridge unit authenticates to bitcoind with its own rpcauth datadir,
# /var/lib/hazync/bitcoin-client — set that up first; see coordinator/deploy/dropins/README.md.

# Run a full bitcoind on this box (no-prune). Then start the archive bridge: it drives the accumulator
# forward and writes per-block bundles the coordinator serves — there is NO witness window to pre-generate.
sudo cp coordinator/deploy/hazync-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-bridge     # emits bundles into HAZYNC_BRIDGE_OUT

# the checkout stays root-owned: the services write only under /var/lib/hazync
sudo cp coordinator/deploy/hazync-coordinator.service /etc/systemd/system/    # HAZYNC_BRIDGE_OUT must point at the bridge's bundle dir
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-coordinator
curl -s localhost:8899/api/state | head -c 300      # smoke test
# Migrating an EXISTING coordinator onto this box? Use coordinator/deploy/migrate-coordinator.sh
# (WAL-safe DB + receipts, old→new) BEFORE repointing the nginx proxy; decommission the old box only after.
```

`TIP_HEIGHT` is a **floor**, not the ceiling, and it is the *last* of three answers the coordinator
consults. `chain_tip()` takes the highest of: the node height published by the tip timer (below),
the highest bundle the bridge can serve, and this constant. Each is a lower bound on the truth and
none is reliably the truth alone.

**Do not hand-set this to "the real chain tip" and consider it done.** A constant is correct on the
day it is written and wrong every day after. Install the tip timer in §1b so the real height is
published automatically; leave `TIP_HEIGHT` at whatever the unit ships with, as the fallback for when
the publisher is absent or stale. Setting it too low understates % complete and, worse, rejects valid
submissions above it as out of range.

`RANGE_SIZE=1000`. The unit binds `127.0.0.1` (behind the proxy); if the web box is a different
machine, firewall `:8899` to the web box only — see §7, and note that a private-network IP is NOT
available on this topology.

## 1b. Publish the node height (required for a correct chain tip)

The coordinator runs as `hazync`, and bitcoind's datadir is typically mode 700 with a 600 cookie owned
by root, so it cannot ask the node how tall the chain is. A small root-side timer publishes the height
to a file instead, and the coordinator reads that.

Without this the board advertises a compiled-in constant. It happened: the public board showed a chain
height of 958,301 while the node was at 962,795, drifting further every day, and submissions for real
blocks above the constant were refused.

```bash
sudo install -m 0755 coordinator/deploy/hazync-node-tip.sh  /opt/hazync/coordinator/deploy/
sudo install -m 0644 coordinator/deploy/hazync-node-tip.service /etc/systemd/system/
sudo install -m 0644 coordinator/deploy/hazync-node-tip.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hazync-node-tip.timer
sudo systemctl start hazync-node-tip.service        # publish once now
cat /var/lib/hazync/node_tip                        # should print the node's height
```

Check `HAZYNC_BITCOIN_DATADIR` and `TIP_FILE` in the unit match this box. The file must be readable by
the coordinator's user; the script chowns it to `hazync:hazync` by default (`TIP_FILE_OWNER`).

Knobs, all optional:

| Variable | Default | What it does |
|---|---|---|
| `TIP_FILE` | `/var/lib/hazync/node_tip` | where the height is published and read |
| `TIP_FILE_MAX_AGE` | `3600` | older than this and the file is ignored, falling back to the floor |
| `TIP_FILE_OWNER` | `hazync:hazync` | ownership the publisher sets |

The staleness window is the point of the design. A height left behind by a dead publisher looks
exactly like a live one, so anything older than `TIP_FILE_MAX_AGE` is treated as absent. A broken node
degrades to the old floor behaviour instead of pinning the board to a number that has quietly stopped
moving. The timer refreshes every 5 minutes, so an hour is roughly a dozen consecutive failures before
the board notices.

The publisher writes atomically and writes **nothing** on failure, so a node that is still starting
leaves the last good height in place rather than truncating it.

## 1a. Updating a running coordinator

**Deploy by moving the checkout to a tag. Do not copy files into it.**

```bash
DRY_RUN=1 ./coordinator/deploy/deploy-coordinator.sh v0.13.1   # what would change
./coordinator/deploy/deploy-coordinator.sh v0.13.1             # do it
```

The script fetches, refuses if tracked files were edited in place, backs up `coordinator/`, checks
out the tag, and **restarts only if `coordinator/server.py` actually changed** — then verifies the
unit is active and `/api/state` answers 200. Per-box differences belong in the systemd unit's
environment (`COORD_DB`, `COORD_PROOFS`, `HAZYNC_HOST`, `TIP_HEIGHT`, …), never in edited files.

**Why this is the method, and `scp server.py` is not.** Copy-and-restart works, and it is how the box
reached 144 commits behind its own `HEAD` with three "modified" tracked files that were really newer
copies pasted over an old tree (#48). `git describe` then described the checkout, and the checkout
described nothing — the box could not say what it was running.

The cost of that is not untidiness. Before the spine/fold deploy the live `server.py` had to be diffed
against every plausible commit to establish that overwriting it would not destroy a production fix. It
happened to match `93b9bff` exactly, so the deploy was provably additive — but that was luck. The next
time the answer could be "matches nothing", with no way to tell a stale copy from a deliberate one.

**A restart is not free.** It interrupts a live proving fleet mid-submission, so the script decides on
the served file rather than on "something changed". Checking out `v0.13.1` on 2026-08-01 changed no
served file and the right number of restarts was zero — the unit was never touched, and the board did
not notice.

If the script refuses because someone edited in place: commit the change upstream and deploy a tag
containing it. `--force` discards it (after the backup) and should be the rare case, not the habit.

---

## 2. Wire the single domain (on the WEB box)

Paste `coordinator/deploy/nginx-hazync.conf` into the `bitcoinghost.org` `server { }` block in
`/etc/nginx/sites-enabled/bitcoinghost` (set `proxy_pass` to the coordinator's IP if it's a separate
box), then:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s https://api.hazync.org/api/state | head -c 300   # now reachable via the domain
```

## 3. Go-live page (one page) — DONE

The page is not in this repository: `hazync.html` and `hazync-party.html` live in the website's own
repository and web root. As recorded when this step was done: `hazync.html` already carries the live Proof Party (`#party` section) in one scroll, wired to the proxied
API (`/hazync/api/...`), and `hazync-party.html` redirects to it. Until this proxy is live it shows a
clearly-labelled **sample-data preview**; the moment `/hazync/api/state` returns real progress it flips to
live data automatically. Nothing to do here except stand up steps 1–2.

## 4. Prove the loop as a downloader

On any box (the coordinator itself can prove the tiny early blocks on CPU — no GPU needed to seed):

```bash
export COORD_URL=https://hazync.org/
export HAZYNC_HOST=/path/to/host WITNESS_DIR=/tmp/w
./coordinator/hazync id yourname
./coordinator/hazync run 1          # claim → fetch witness → prove → sign → submit → verify
```

Watch the frontier tick up and your name land on the board. That's the public onboarding path proven
end to end.

## 5. Seed real proofs

Prove blocks 1..N to build a genuine genesis frontier. Tiny early blocks are CPU-provable (~60–110s
each) — no GPU capital needed to seed. Scale with a GPU box later.

## 6. Then post to Delving — DONE (historical)

Once the page feels right and the board shows real (even if small) frontier data. The post is linked
from the top of the root `README.md`.

---

## 7. Harden for a public launch

Before opening submissions to the public (a Delving/HN post), do these — they close the DoS and
data-durability gaps a public write endpoint exposes:

- **nginx rate/conn limits + micro-cache.** Use the updated `coordinator/deploy/nginx-hazync.conf`: it
  adds `limit_req`/`limit_conn` (an anonymous GET flood on `/api/state` is otherwise the cheapest
  board-takedown), a 1-second cache on `/api/state`, and `client_max_body_size 8m` (else folded
  multi-block receipts 413 at the proxy). Part A of that file goes in the `http { }` context — remember
  `sudo mkdir -p /var/cache/nginx/hazync && sudo chown www-data: /var/cache/nginx/hazync`.
- **Secure bind.** The unit binds `127.0.0.1` (behind the proxy). If the coordinator is a separate box,
  firewall `:8899` to the web box.

  ⛔ **This previously said "set `COORD_BIND` to its private-network IP (not `0.0.0.0`)". That is not
  possible on the live topology and the instruction was unfollowable** (hazync#218). The coordinator
  has `lo`, one PUBLIC `eth0` (152.53.93.164) and `docker0`; the web box (83.136.255.218) proxies to
  it **over the public internet**. There is no private IP to bind, and `COORD_BIND=127.0.0.1` would
  take the board dark. `COORD_BIND=0.0.0.0` is correct here — the access control is the firewall,
  not the bind address.

  Measured on the coordinator 2026-09-08: `ufw` **active**, default deny incoming, `22` and `8333`
  open, and `:8899` reachable only from the web box. The `INPUT` chain also carries hand-added rules
  ahead of ufw's (`-P INPUT DROP`, the `--dport 8899` ACCEPTs, then a DROP), so **there are two
  sources of truth for one port** — read `iptables -S INPUT` as well as `ufw status`, or you will
  believe a rule that something else overrides.

  ✅ Removed 2026-09-08: a hand-added ACCEPT for `94.237.17.228`, an UpCloud prover in the same range
  as the retired `hazync-b200` — 0 packets in 90 s against the web box's 1,690, and gone from the
  account. Its `RATE_EXEMPT` entry went with it (hazync#218).

  ⛔ **Retire the exemption when you retire the box.** A stale firewall ACCEPT lets a stranger reach
  the port; a stale `RATE_EXEMPT` entry lets them reach it WITHOUT LIMITS, which is the flood this
  section exists to stop. Provider addresses go back to a pool when a box is deleted.
  ⚠ And do not judge an allow-rule by its packet counter alone — a dormant counter shows silence, not
  decommissioning. That rule was referenced in two places and described as "our own prover"; what
  settled it was the account inventory.
  The server now **refuses to bind a public interface** while verification/signatures are permissive
  (`VERIFY_MODE=mock`, `COORD_ALLOW_MOCK`, missing sig lib, `COORD_ALLOW_UNSIGNED`) unless you set
  `COORD_ALLOW_PUBLIC_INSECURE=1` — so a misconfigured redeploy fails loudly instead of crediting
  unverified receipts.
- **Trusted proxy.** The coordinator only honours `X-Forwarded-For` from `TRUSTED_PROXIES`
  (default `127.0.0.1,::1`); set it to the proxy's address if the proxy is remote, else the rate limit
  is bypassable.

## Tunables (claim lifecycle, folding, wide ranges)

The defaults are what the coordinator does when a variable is unset.

| Variable | Where | Default | Effect |
|---|---|---|---|
| `MAX_ATTEMPTS` | coordinator | `3` | Park a range as `failed` after this many **block-implicating** failures |
| `MAX_ENV_FAILURES` | coordinator | `12` | Looser cap for **capacity** failures (OOM, worker restarts) |
| `CLAIM_WIDTH` | coordinator | `1` | blocks per claim-next assignment; `1` = per-block |
| `CLAIM_TTL` | coordinator | `3600` | A claim stops being held after this long with no heartbeat |
| `CLAIM_GRACE` | coordinator | `600` | …or after this long if it never sent one (#296) |
| `CLAIM_MAX` | coordinator | `86400` | …and after this long regardless |
| `HAZYNC_FOLD_CONCURRENCY` | worker CLI | `1` | Folds run concurrently within a tree level |

Two counters, not one, because attempt counting alone cannot tell *"this block is unprovable"* from
*"this box was full"*. An OOM signature or a deliberate shutdown is evidence about the machine; a parse
error or image-id mismatch is evidence about the block. Without the split, a capacity incident parks
perfectly good blocks — which is backwards, since parking exists to stop burning GPU on blocks that
genuinely cannot be proved.

`HAZYNC_FOLD_CONCURRENCY` is bounded by **GPU VRAM** and overcommitting does not degrade gracefully — it
OOMs mid-fold. Measured on a 46 GB L40S: K=1 → 8,949 MiB peak; K=2 → 11,742 MiB and 1.5x faster; K=4 →
OOM. A single fold already drives the GPU to 100% utilisation, so the win is scheduling-gap sized, not
linear in K. There is no safe auto-detect across card sizes; raise it per box.

### Staged rollout

Deploy in this order, verifying each before the next. Each stage is independently reversible.

1. **`backup.sh`** — inert until `BACKUP_REMOTE` is set. Verify: the next nightly run still writes a
   snapshot. Rollback: restore the file.
2. **Worker CLI** — deploy to **one** worker first, leave the others on the old build. Verify: that
   worker proves and submits several blocks. Rollback: put back the previous worker CLI (the
   `hazync-worker` asset of the prior release). This used to name `/root/v10_hazync_cli`, a pre-#58 path.
3. **Coordinator** — take a fresh DB backup first. The schema migration is **additive**, and the
   previous coordinator runs unchanged against the migrated schema, so rollback is a file swap and a
   restart, *not* a backup restore. Verify: claims granted, submits verified, frontier advancing, and
   `/api/state` returning its `failed` and `blocked` fields (`blocked` replaced `frontier_blocker`,
   which was removed in `d8d71d2`).
4. **`CLAIM_WIDTH`** — as its own change, never on the same restart as step 3, or a regression is
   ambiguous between the two. Verify by watching a range **COMPLETE**: claim, prove, fold locally,
   submit, frontier advances by the full width.

   ⚠️ **Widening was tried at 1000 on 2026-07-28 and stalled the board.** A 1000-block range is a
   ~67-minute commitment; a hard failure anywhere in it discards the entire range, and with OOMs
   occurring regularly not one range completed. Throughput fell from 2,220 blocks/hr to 1 block in 40
   minutes while the GPUs stayed busy, because the frontier cannot advance until a range COMPLETES.
   Failure probability scales with duration — pick a width the board can reliably finish (start at
   100, not 1000), and treat "a worker is progressing through a range" as NOT the same evidence as
   "a range completed".

Before deploying to a live coordinator, dry-run the migration against a **copy of the real DB** (the
newest backup snapshot works). A migration that fails on production is the worst place to discover it.

## Alerts

Every hazync failure pushes to a phone via [ntfy](https://ntfy.sh). Added 2026-09-14, after the G1
retention check had failed every night since 2026-09-12 with nobody knowing.

| what | where it runs | fires when |
|---|---|---|
| `OnFailure=hazync-alert@%n.service` | coordinator: retention check, backup, node tip, coordinator, bridge | the unit enters `failed` |
| `ExecStopPost=+hazync-alert.sh --crash %n` | coordinator: `hazync-coordinator`, `hazync-bridge` | an unclean stop (`SERVICE_RESULT != success`), max 1 per unit per 15 min |
| `hazync-watchdog.timer` (every 5 min) | **web box** | `/api/meta` fails 2 probes in a row (direct and via nginx); re-sends hourly, one RECOVERED |

⚠ The crash hook is not optional: `Restart=always` with `RestartSec>=3` never reaches systemd's start
limit, so a crash-looping coordinator never enters `failed` and `OnFailure=` never fires. ⚠ The
watchdog is off-box on purpose: a dead coordinator runs no hook of its own.

Config: `/etc/hazync/alert.env` (`0600 root`) holds `NTFY_URL=https://ntfy.sh/<topic>` on BOTH boxes.
The topic is the only secret (anyone with it can read and post) and is not in git. Subscribe to it
in the ntfy app.

```bash
# coordinator (root)
install -m 0755 coordinator/deploy/hazync-alert.sh /usr/local/bin/
install -m 0644 coordinator/deploy/hazync-alert@.service /etc/systemd/system/
for u in hazync-coordinator hazync-bridge hazync-coordinator-backup hazync-retention-check hazync-node-tip; do
  install -D -m 0644 coordinator/deploy/dropins/$u-alert.conf /etc/systemd/system/$u.service.d/alert.conf
done
systemctl daemon-reload                      # no restart needed; ExecStopPost= applies to the next stop
# web box
sudo install -m 0755 coordinator/deploy/hazync-alert.sh coordinator/deploy/watchdog/hazync-watchdog.sh /usr/local/bin/
sudo install -m 0644 coordinator/deploy/watchdog/hazync-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-watchdog.timer
```

**Test the live path, not just the script** — a push that never reaches the phone is the failure:
`hazync-alert.sh "Hazync: test" "hello"` on each box, and on the coordinator a throwaway unit with
`ExecStart=/bin/false` + `OnFailure=hazync-alert@%n.service` proves the systemd wiring end to end.
`coordinator/deploy/test-alerting.sh` (CI) covers the scripts only.

## Integrity checks

Every proof and spine step is verified once, when it is submitted. These re-check what the board claims, and push to
the phone once when a check starts failing, at most hourly while it lasts, and once when it recovers
(`hazync-run-check.sh`). Exit codes: 0 holds, 1 integrity failure, 2 could not check. Both 1 and 2 push.

| Check | Where | When | Fails when |
|---|---|---|---|
| `check-spine.py` | coordinator | every 10 min | the genesis proof does not verify, is not from the canonical guest, disagrees with its head record, ends on a block hash or chainwork our archive node does not have at that height, or has not advanced for 2 h while block hi+1 is proven |
| `check-spine.py --url` | web box | every 10 min | the copy `api.hazync.org` serves does not verify, or its tip is not mempool.space's block at that height (blockstream.info as fallback). Explorers publish no chainwork |
| `check-continuity.py` | coordinator | every 10 min | a block from 1 to the spine's tip has no record of its own, a seam does not link (tip hash or boundary digest), block 1 does not start at genesis, or the last block does not end on the spine's tip |
| `check-proofs.py` | coordinator | nightly 04:17 UTC | a stored proof no longer hashes to the receipt that was accepted, no longer verifies, or verifies to a different range than its record |

Measured before switching them on (2026-09-15): the genesis proof verifies in 50 ms and its tip hash and chainwork
matched our node at block 41,539; the continuity walk took 0.24 s over 41,539 blocks with no gaps; one stored proof
verifies in 34-54 ms, so the nightly pass over 103,992 files is about 90 CPU-minutes.

```bash
# coordinator (root), from a checkout at the merged commit. Uses hazync-alert.sh and alert.env from "Alerts".
install -m 755 coordinator/check-spine.py            /usr/local/sbin/hazync-check-spine
install -m 755 coordinator/check-continuity.py       /usr/local/sbin/hazync-check-continuity
install -m 755 coordinator/check-proofs.py           /usr/local/sbin/hazync-check-proofs
install -m 755 coordinator/deploy/hazync-run-check.sh /usr/local/sbin/hazync-run-check
install -m 644 coordinator/deploy/hazync-check-{spine,continuity,proofs}.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start hazync-check-spine.service hazync-check-continuity.service   # first runs: read them in the journal
systemctl enable --now hazync-check-spine.timer hazync-check-continuity.timer hazync-check-proofs.timer

# web box (root). hazync-verify from a signed release: check SHA256SUMS.txt.asc and the file's sha256 first.
install -m 755 check-spine.py      /usr/local/sbin/hazync-check-spine
install -m 755 hazync-run-check.sh /usr/local/sbin/hazync-run-check
install -m 644 hazync-check-spine-remote.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl start hazync-check-spine-remote.service
systemctl enable --now hazync-check-spine-remote.timer
```

## Backup & restore

The DB (`coordinator.db`, the signed ledger) **and** the `proofs/` directory (the re-verifiable STARK
receipts — the artifacts the "you don't have to trust us" claim depends on) must **both** be backed up,
offsite. A same-disk copy dies with the box.

### Offsite copies in Cloudflare R2 (since 2026-09-15)

Until 2026-09-15 the receipts had **no** copy off the coordinator: the nightly `backup.sh` shipped the
ledger only (`BACKUP_REMOTE_DB_ONLY=1`) to the web box. These now run beside it:

| What | How | Where in R2 | How far behind |
|---|---|---|---|
| Proof receipts, singles and folds (`proof_*.bin`) | `hazync-offsite-proofs.timer`, hourly: `hazync-offsite-proofs.py copy`, then `check` | `hazync-proofs/proofs-<first 8 of METHOD_ID>/` | up to ~1 h |
| The spine (`spine.bin` + `spine.json`) | `hazync-offsite-spine.timer`, every 10 min: `hazync-offsite-proofs.py spine --verify /usr/local/bin/hazync-verify` | `hazync-proofs/spine-<first 8 of METHOD_ID>/spine_<lo>-<hi>.{bin,json}`, one pair per height, never overwritten | up to ~10 min |
| Sponsor signing identities (`/var/lib/hazync/sponsor-bot/identities/`), **encrypted** | `hazync-offsite-keys.timer`, hourly: `hazync-offsite-proofs.py keys --pubkey /etc/hazync/backup/sponsor-keys.pub.asc --recipient 777FE81F…` | `hazync-proofs/sponsor-keys/identities-<sha256 of the tar, 16 hex>.tar.gpg`, one per state, never overwritten | up to ~1 h |
| Ledger | Litestream (`litestream.service` + `dropins/litestream-*.conf`, config from `litestream.yml.example`) | `hazync-ledger/coordinator/` | ~1 s |

- **The spine is the one proof that cannot be rebuilt cheaply**: losing it means re-absorbing every block from
  genesis. Until 2026-09-15 it had no copy off the box. A copy is uploaded only when `spine.bin` matches the
  sha256 and size in `spine.json` (the coordinator replaces the two one after the other) and
  `hazync-verify` accepts it as genesis-anchored; otherwise the unit fails and pages the phone. The `.bin`
  goes up before the `.json`, so a `.json` in R2 marks a complete pair. `hazync-verify` is the signed
  release asset `hazync-verify-x86_64-linux-gnu`, checked against `SHA256SUMS.txt` before installing.

- **Receipts are append-only**, as in `backup.sh`: a file already in R2 is never overwritten or deleted,
  and names are namespaced by guest id because they repeat across re-baselines. `check` fails the unit,
  and so pages the phone, if any receipt older than 30 min is missing from R2 or differs in size.
- **Why a Python script and not rclone.** Ubuntu 24.04's rclone 1.60 reports `501 NotImplemented` for every
  upload to R2: the PUT succeeds, and the HEAD it sends afterwards is refused (fixed by `no_head = true`).
  Even then it never started a transfer against the flat 97,000-file directory. The script lists R2
  once and uploads the difference. It measured 35 receipts/s at a 64 Mbit/s cap.
- **Litestream 0.5 allows one replica per database.** The second copy of the ledger is a daily snapshot in
  B2 (below), not a second Litestream replica.
- **Keys:** `/etc/hazync/backup/r2.keys`, `root:root 0600`, one line `<key id> <secret> <endpoint>`. It is a
  Cloudflare *account* API token with Object Read & Write on `hazync-proofs` and `hazync-ledger` only.
  Listing all buckets or opening any other bucket returns `AccessDenied`.

**Restore the ledger from R2** (into a scratch path, never over the live file):

```bash
install -d -o hazync -g hazync -m 700 /var/lib/hazync/restore-test
runuser -u hazync -- litestream restore -config /etc/litestream.yml \
  -o /var/lib/hazync/restore-test/coordinator.db /var/lib/hazync/coordinator.db
sqlite3 /var/lib/hazync/restore-test/coordinator.db 'PRAGMA integrity_check'
```

Drilled on 2026-09-15: it took 4 s, integrity ok, and submission/vrange/contributor counts were identical to live, 5 s behind.

**Phone notifications** (ntfy, same topic as every other alert):

| When | Priority | From |
|---|---|---|
| The hourly mirror fails, or its check finds a receipt older than 30 min missing from R2 | high | `hazync-offsite-proofs.service` `OnFailure=` |
| Litestream crashes | high | `dropins/litestream-alert.conf` |
| Litestream is not running; the newest ledger change in R2 is over 15 min old; R2 cannot be listed | high, re-sent every 6 h, one low RECOVERED | `hazync-offsite-watch.timer` (every 10 min) |
| Litestream logs WARN/ERROR lines | default, at most one push an hour | `hazync-offsite-watch.timer` |
| The spine copy fails: `spine.bin` and `spine.json` still disagree after 5 reads, `hazync-verify` rejects it, or the upload fails | high | `hazync-offsite-spine.service` `OnFailure=` |
| The sponsor keys copy fails: the public key file is not the pinned fingerprint or cannot encrypt, encryption fails, the ciphertext is not to that key, or the upload fails | high | `hazync-offsite-keys.service` `OnFailure=` |
| Daily at 08:00 UK time: receipts in R2 vs disk (a receipt counts as missing once it is older than `OFFSITE_PROOF_GRACE_SECS`, 2 h, so it has missed a whole hourly run), the mirror's 24 h, ledger lag, a **restore drill**, and the newest **spine copy** downloaded, checked against its sha256, verified with `hazync-verify`, and compared with the live spine (a problem over `OFFSITE_SPINE_LAG_SECS`, 1 h, behind), and whether the current **sponsor identities** have a copy in R2 encrypted to the pinned key (a problem once unchanged for `OFFSITE_KEYS_GRACE_SECS`, 2 h, with no copy; or when the key expires within `OFFSITE_KEY_EXPIRY_WARN_DAYS`, 60) | low if all good, high if not | `hazync-offsite-summary.timer` |

The restore drill restores the ledger from R2 into `/var/lib/hazync/restore-drill`, checks integrity,
compares it with the live ledger, and deletes it. Priorities come from `ALERT_PRIORITY` / `ALERT_TAGS`
in `hazync-alert.sh`; without them every caller still rings at high.

**Check the receipt mirror by hand:** `/usr/local/sbin/hazync-offsite-proofs check --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs`

**Copy the spine by hand:** `/usr/local/sbin/hazync-offsite-proofs spine --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs --verify /usr/local/bin/hazync-verify`

**Restore the spine from R2**: take the newest pair under `spine-<first 8 of METHOD_ID>/` (highest `hi`),
check it, and only then put it in place with the coordinator stopped:

```bash
sha256sum spine_1-<hi>.bin        # must equal "sha256" in spine_1-<hi>.json
hazync-verify spine_1-<hi>.bin    # must say VERIFIED — genesis-anchored
install -o hazync -g hazync -m 644 spine_1-<hi>.bin  /var/lib/hazync/spine/spine.bin
install -o hazync -g hazync -m 644 spine_1-<hi>.json /var/lib/hazync/spine/spine.json
```

The spine workers then absorb forward from `hi`; the receipts they need are in R2 too.

**Sponsor keys: encrypted, and only the operator can decrypt them.** The coordinator holds only the operator's
PUBLIC key (`/etc/hazync/backup/sponsor-keys.pub.asc`, `gpg --armor --export 777FE81F…`), pinned by fingerprint
in `hazync-offsite-keys.service`. The secret key never goes on the box or into R2.

- **Restore** (on the machine that holds the operator's secret key), newest object under `sponsor-keys/`:
  ```bash
  gpg --decrypt identities-<hash>.tar.gpg > identities.tar
  tar -tvf identities.tar                       # identities/<sponsorship>/key.hex and handle
  # on the coordinator, with the sponsor bot stopped:
  tar -xf identities.tar -C /var/lib/hazync/sponsor-bot/ && chown -R hazync:hazync /var/lib/hazync/sponsor-bot/identities
  find /var/lib/hazync/sponsor-bot/identities -type d -exec chmod 700 {} + -o -type f -exec chmod 600 {} +
  ```
- **Before the key expires** (the daily summary warns 60 days ahead): `gpg --quick-set-expire 777FE81F… 2y` and
  `gpg --quick-set-expire 777FE81F… 2y '*'` on the operator's machine, then re-export the public key over
  `/etc/hazync/backup/sponsor-keys.pub.asc`.
- **Copy the keys by hand:** `/usr/local/sbin/hazync-offsite-proofs keys --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs --pubkey /etc/hazync/backup/sponsor-keys.pub.asc --recipient 777FE81F8CC077FD3D08055E852C2B3190F5B928`

### Second copies in Backblaze B2 (since 2026-09-15)

Everything in R2 has a second copy at another provider, in the B2 bucket `hazync-backup` (region
`us-east-005`: private, default encryption on, Object Lock enabled with no default retention). A leaked or
revoked Cloudflare token, or a closed Cloudflare account, must not take every off-box copy with it. The same
script writes both; only the keys file and bucket differ.

| What | How | Where in B2 | How far behind |
|---|---|---|---|
| Proof receipts | `hazync-offsite-proofs-b2.timer`, hourly at :53 UTC: `copy`, then `check` | `hazync-backup/proofs-<first 8 of METHOD_ID>/` | up to ~1 h |
| The spine | `hazync-offsite-spine-b2.timer`, every 10 min (:08): `spine --verify /usr/local/bin/hazync-verify` | `hazync-backup/spine-<first 8 of METHOD_ID>/spine_<lo>-<hi>.{bin,json}` | up to ~10 min |
| Sponsor signing identities, **encrypted** | `hazync-offsite-keys-b2.timer`, hourly at :07 UTC: `keys` | `hazync-backup/sponsor-keys/identities-<hash>.tar.gpg` | up to ~1 h |
| Ledger | `hazync-offsite-ledger-b2.timer`, daily 04:47 UTC: `ledger` (SQLite online backup, `integrity_check`, gzip) | `hazync-backup/ledger/coordinator-<UTC stamp>.db.gz`, one per run, never overwritten | up to ~1 day |

- **Every unit pages the phone on failure** through `OnFailure=`, exactly like its R2 twin.
- **The daily summary adds B2 lines**: receipts in B2 vs disk, the B2 mirror's 24 h, the spine copy verified,
  the sponsor keys copy, and a **restore drill of the newest ledger copy**: downloaded, unpacked,
  integrity-checked and compared with the live ledger; a problem once it is older than
  `OFFSITE_B2_LEDGER_LAG_SECS` (26 h). `OFFSITE_B2_KEYS=` (empty) turns the B2 lines off; a configured keys
  file that is missing is a problem, not a skip.
- **Nothing in B2 is ever deleted by these scripts.** The ledger adds one gzipped copy a day (37.5 MB on
  2026-09-15, from a 113 MB ledger), about 14 GB a year at that size.
- **Keys:** `/etc/hazync/backup/b2.keys`, `root:root 0600`, one line
  `<keyID> <applicationKey> https://s3.us-east-005.backblazeb2.com hazync-backup`. A B2 application key
  restricted to `hazync-backup`, Read and Write.

**Install** (coordinator, root, from a checkout at the merged commit):

```bash
cd coordinator/deploy
install -m 755 hazync-offsite-proofs.py /usr/local/sbin/hazync-offsite-proofs
install -m 755 hazync-offsite-watch.py /usr/local/sbin/hazync-offsite-watch
install -m 644 hazync-offsite-{proofs,spine,keys,ledger}-b2.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start hazync-offsite-ledger-b2.service hazync-offsite-spine-b2.service hazync-offsite-keys-b2.service
systemctl enable --now hazync-offsite-{proofs,spine,keys,ledger}-b2.timer
```

The first receipt copy is large (24.4 GB / 106,691 receipts on 2026-09-15); run it by hand, then enable the
hourly timer, so the hourly unit's 45 min limit does not kill it:
`hazync-offsite-proofs copy --keys /etc/hazync/backup/b2.keys --bucket hazync-backup --threads 32 --bwlimit-mbit 100`.

**Restore the ledger from B2** (into a scratch path, never over the live file):

```bash
install -d -m 700 /var/lib/hazync/restore-test
python3 - <<'EOF'
import boto3, gzip, shutil
kid, secret, endpoint, bucket = open("/etc/hazync/backup/b2.keys").read().split()
s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=kid, aws_secret_access_key=secret)
keys = sorted(o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="ledger/")
              for o in page.get("Contents", []))
print("newest:", keys[-1])
with gzip.GzipFile(fileobj=s3.get_object(Bucket=bucket, Key=keys[-1])["Body"]) as g, \
        open("/var/lib/hazync/restore-test/coordinator.db", "wb") as f:
    shutil.copyfileobj(g, f)
EOF
sqlite3 /var/lib/hazync/restore-test/coordinator.db 'PRAGMA integrity_check'
```

The spine and the sponsor keys restore exactly as from R2 (above), from the same paths in `hazync-backup`.

> ⚠️ **`backup.sh` does nothing until it is scheduled.** Shipping the script is not a backup — pick one of
> the two schedulers below and confirm a snapshot actually lands (`ls $HZ_HOME/backups`). Until then the
> ledger + receipts live only on one disk.

**Option A — systemd timer (recommended):** install the units shipped alongside `backup.sh`, set an
offsite `BACKUP_REMOTE` in the service, and enable the timer:

⚠️ **Read the paths off the running unit — and NOT with `systemctl cat`.** They are env-driven and
differ per deployment, so this section deliberately does not tell you what they are.

`systemctl cat` prints the unit *file*, including lines a drop-in has superseded. On the production
coordinator it still shows `/root/...` values, and `/root/hazync` **does not exist on that box** — the
paths moved on 2026-08-02 (#58) because `/root` is `0700 root` and blocked `User=`, `ProtectHome=` and
`ProtectSystem=strict`. Only `systemctl show` reports what the service actually runs:

```bash
systemctl show hazync-coordinator -p Environment | tr ' ' '\n' | grep -E 'COORD_DB|COORD_PROOFS'
```

This paragraph previously asserted "production uses `/root/...`" and was wrong for three weeks — the
re-baseline section further down had been updated by #58 and this one had not. That is why it now names
a *command* instead of a path: a command cannot go stale.

If they differ, set the same values in the backup service (`systemctl edit`, see the comments in the
unit). `backup.sh` aborts loudly on a path that doesn't exist, isn't a SQLite file, or isn't the
coordinator schema — a wrong path used to yield a *green* snapshot containing an empty database.

```bash
sudo cp coordinator/deploy/hazync-coordinator-backup.{service,timer} /etc/systemd/system/
sudo systemctl edit hazync-coordinator-backup.service   # add: Environment=BACKUP_REMOTE=rclone:hazync-backup:hazync
sudo systemctl daemon-reload
sudo systemctl enable --now hazync-coordinator-backup.timer
systemctl start hazync-coordinator-backup.service       # run once now; then check $HZ_HOME/backups
systemctl list-timers hazync-coordinator-backup.timer   # confirm it is scheduled
```

**Option B — cron:**

```bash
# daily, offsite (rclone or rsync target); keeps 14 local snapshots
17 3 * * *  BACKUP_REMOTE=rclone:hazync-backup:hazync /opt/hazync/coordinator/deploy/backup.sh >> /var/log/hazync-backup.log 2>&1
```

### Knobs

| Variable | Default | What it does |
|---|---|---|
| `BACKUP_REMOTE` | unset | offsite target, `user@host:/path` or `rclone:remote:path`. Unset means the snapshot sits on the same disk as the data. |
| `BACKUP_REMOTE_DB_ONLY` | `0` | ship only the ledger offsite (~45 MB) and keep receipts local |
| `BACKUP_REMOTE_PROOFS` | `0` | mirror receipts offsite as well, append-only and namespaced by guest id |
| `BACKUP_KEEP` | `14` | local snapshots retained |
| `BACKUP_NET_TIMEOUT` | `600` | seconds of no I/O before a transfer is abandoned |
| `BACKUP_SSH_CONNECT_TIMEOUT` | `30` | ssh connect timeout for the offsite copy and its prune |

**Size `BACKUP_KEEP` against the receipt store, not out of habit.** Every snapshot is a *full* copy of
the receipts, so 14 of them is 14x the thing being protected, against a store that grows with the
party. On 2026-08-17 that was 78 GB of local backups for an 8.6 GB live store, and the live box was set
to `2` (not re-checked since). The script default is still `14`, and no drop-in in this repo sets
`BACKUP_KEEP`: a box that overrides it should commit the override as a drop-in, or declare the key in
`unit-drift-allow.txt`, or `scripts/check-unit-drift.sh` (run with `HAZYNC_UNITS` including
`hazync-coordinator-backup`) reports it as drift.

**Think before enabling `BACKUP_REMOTE_PROOFS`.** It was enabled when the store was 759 MB and the
target had 14 GB free. Two weeks later it filled that target's root filesystem to 100%, nginx could no
longer buffer proxied responses, and the public board went down. A backup took out the thing it was
protecting. The receipt store is a few hundred GB at full chain, so this needs a target sized for it,
not a web VM.

**Timeouts exist because a stall is worse than a failure.** rsync waits forever on a peer that has
stopped reading. When the offsite target filled, the sender hung for three and a half hours holding
9 GB of RAM, and because systemd will not start a unit that is already `activating`, the daily timer
became a no-op and *every* backup silently stopped. Nothing appeared in `systemctl is-failed`, because
nothing had failed. The unit now sets `TimeoutStartSec=60m` (a healthy run is about five minutes) and
every network call is bounded, so a stall becomes a visible failed unit that the next firing retries.

Local rotation runs **before** the offsite copy, so a remote that is full, slow or gone can no longer
prevent local retention from happening.

**Restore drill** (do this once so you know it works):

The paths below are the shipped units' (`BACKUP_DIR`, `COORD_DB`, `COORD_PROOFS` under
`/var/lib/hazync`); read yours with `systemctl show` as above. The checkout stays root-owned.

```bash
D=/var/lib/hazync/backups/<STAMP>        # or fetch the snapshot back from the offsite target
cd "$D" && sha256sum -c SHA256SUMS       # verify integrity
sudo systemctl stop hazync-coordinator
sudo install -o hazync -g hazync -m 0644 "$D/coordinator.db" /var/lib/hazync/coordinator.db
sudo tar -C /var/lib/hazync -xzf "$D/proofs.tar.gz"      # entries are proofs/..., so this is COORD_PROOFS
sudo chown -R hazync:hazync /var/lib/hazync/proofs
sudo systemctl start hazync-coordinator
curl -s localhost:8899/api/state | head -c 200   # frontier/proven should match pre-restore
```

## Re-baseline and releases

Moved to [`docs/RELEASE_PROCESS.md`](../../docs/RELEASE_PROCESS.md) on 2026-09-14: cutting a release, deploying
the browser verifier to both sites, and the whole re-baseline section that was here — reading an id only off
a binary you watched get built, the `latest` pointer, CUDA build disk space, `RZUP_TIMEOUT`, canonical build
paths, `rebaseline-id.sh`, ride-alongs, the list of things that must change with the id, the board cutover
on this box (back up, swap the host binary for coordinator and bridge, archive the board and the spine),
and purging the provers' bundle caches.

## Moderation

Handles are HTML-sanitised and reserved/impersonation names (`satoshi`, `admin`, `bitcoinghost`, …,
env `HANDLE_DENY`) are rejected at claim/submit. To **take down** an abusive entry already on the board,
add its pubkey (hex, one per line) to `MOD_BLOCK_FILE` (default `coordinator/mod_block.txt`) — it is
re-read live, so the entry disappears from the leaderboard/board within the cache TTL (~1.5s), no restart.
That list only hides a key: it does not stop it claiming.

**Claim hogs.** One key holds at most `CLAIM_OPEN_MAX` live claims (default 4); a further claim gets `429`
until one of its blocks is proven or its claim lapses (`CLAIM_GRACE` without a beat, `CLAIM_TTL` after its
last beat). Added 2026-09-14 after `ghost:dda215` held 26 to 61 never-beaten claims for blocks another
contributor then proved. A fleet running more than 4 GPUs under ONE key needs it raised (or a key per box).
Public requests reach the coordinator through the web box, which is in `RATE_EXEMPT`, so the per-IP rate
limit does not stop a single client there; the per-key cap does.

**A key does not take back a block it let lapse.** When a claim that never beat is released by the grace,
every other key is offered the block at once, but the key that let it lapse waits `CLAIM_RETAKE_WAIT` (default
3600 s) before it may claim that block again. Added 2026-09-14: the cap alone left `ghost:dda215` re-claiming
frontier block 67,532 every time its claim lapsed, and nobody else was offered it. If the frontier is stuck
behind a claimed block anyway, prove that block directly: `hazync run <n>` needs no claim and is accepted
whoever holds one.

**Signed claims (#310).** A worker that signs its claim (over `claim:<nonce>:<ts>`, like a beat) is the only one
whose claims count against its own cap and re-take wait; unsigned claims sent under the same key are counted
apart, so they cannot fill its slots or keep its blocks from it. A signature that does not verify is refused.
Workers up to v0.21.4 sign nothing, so unsigned claims stay accepted. Once contributors run a release that
signs, set `CLAIM_REQUIRE_SIG=1` and restart: unsigned claims then get `403`.

### Notes
- **Served window** = the claimable set = the blocks the archive bridge has emitted bundles for (up to
  `tip - HAZYNC_BRIDGE_FINALITY`, default 100). Blocks outside it 404 and the CLI says so; the window grows
  automatically as the bridge follows the chain — nothing to pre-generate. (The legacy
  `gen-witness-window.sh` per-block-witness path still works as a fallback when no bridge is configured,
  but is retired for the live party.)
