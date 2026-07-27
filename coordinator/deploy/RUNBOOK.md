# Hazync Proof Party — coordinator deploy runbook

Stand up the coordinator **co-located with the archive-node bridge and a full `bitcoind`** on one box, and
wire it under **one domain** (`bitcoinghost.org/hazync`) through an nginx proxy — the box is invisible
backend infrastructure, like the existing `/api/pool/vmN/` proxies.

```
  bitcoinghost.org/hazync          the page (story + live board), served from the web root
  bitcoinghost.org/hazync/api/…    proxied to the bridge box (state / claim / submit / witness)
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
sudo mkdir -p /opt/hazync/coordinator-state
# place the repo at /opt/hazync (host binary at /opt/hazync/prover/target/release/host)

# Run a full bitcoind on this box (no-prune). Then start the archive bridge: it drives the accumulator
# forward and writes per-block bundles the coordinator serves — there is NO witness window to pre-generate.
sudo cp coordinator/deploy/hazync-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-bridge     # emits bundles into HAZYNC_BRIDGE_OUT

sudo chown -R hazync:hazync /opt/hazync
sudo cp coordinator/deploy/hazync-coordinator.service /etc/systemd/system/    # HAZYNC_BRIDGE_OUT must point at the bridge's bundle dir
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-coordinator
curl -s localhost:8899/api/state | head -c 300      # smoke test
# Migrating an EXISTING coordinator onto this box? Use coordinator/deploy/migrate-coordinator.sh
# (WAL-safe DB + receipts, old→new) BEFORE repointing the nginx proxy; decommission the old box only after.
```

Set `TIP_HEIGHT` in the unit to the real chain tip. `RANGE_SIZE=1000`. The unit binds `127.0.0.1`
(behind the proxy); if the web box is a different machine, set `COORD_BIND` to the private-network IP
and firewall `:8899` to the web box only.

## 2. Wire the single domain (on the WEB box)

Paste `coordinator/deploy/nginx-hazync.conf` into the `bitcoinghost.org` `server { }` block in
`/etc/nginx/sites-enabled/bitcoinghost` (set `proxy_pass` to the coordinator's IP if it's a separate
box), then:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s https://bitcoinghost.org/hazync/api/state | head -c 300   # now reachable via the domain
```

## 3. Go-live page (one page) — DONE

`hazync.html` already carries the live Proof Party (`#party` section) in one scroll, wired to the proxied
API (`/hazync/api/...`), and `hazync-party.html` redirects to it. Until this proxy is live it shows a
clearly-labelled **sample-data preview**; the moment `/hazync/api/state` returns real progress it flips to
live data automatically. Nothing to do here except stand up steps 1–2.

## 4. Prove the loop as a downloader

On any box (the coordinator itself can prove the tiny early blocks on CPU — no GPU needed to seed):

```bash
export COORD_URL=https://bitcoinghost.org/hazync
export HAZYNC_HOST=/path/to/host WITNESS_DIR=/tmp/w
./coordinator/hazync id yourname
./coordinator/hazync run 1          # claim → fetch witness → prove → sign → submit → verify
```

Watch the frontier tick up and your name land on the board. That's the public onboarding path proven
end to end.

## 5. Seed real proofs

Prove blocks 1..N to build a genuine genesis frontier. Tiny early blocks are CPU-provable (~60–110s
each) — no GPU capital needed to seed. Scale with a GPU box later.

## 6. Then post to Delving

Once the page feels right and the board shows real (even if small) frontier data.

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
  set `COORD_BIND` to its **private-network IP** (not `0.0.0.0`) and firewall `:8899` to the web box.
  The server now **refuses to bind a public interface** while verification/signatures are permissive
  (`VERIFY_MODE=mock`, `COORD_ALLOW_MOCK`, missing sig lib, `COORD_ALLOW_UNSIGNED`) unless you set
  `COORD_ALLOW_PUBLIC_INSECURE=1` — so a misconfigured redeploy fails loudly instead of crediting
  unverified receipts.
- **Trusted proxy.** The coordinator only honours `X-Forwarded-For` from `TRUSTED_PROXIES`
  (default `127.0.0.1,::1`); set it to the proxy's address if the proxy is remote, else the rate limit
  is bypassable.

## Backup & restore

The DB (`coordinator.db`, the signed ledger) **and** the `proofs/` directory (the re-verifiable STARK
receipts — the artifacts the "you don't have to trust us" claim depends on) must **both** be backed up,
offsite. A same-disk copy dies with the box.

> ⚠️ **`backup.sh` does nothing until it is scheduled.** Shipping the script is not a backup — pick one of
> the two schedulers below and confirm a snapshot actually lands (`ls $HZ_HOME/backups`). Until then the
> ledger + receipts live only on one disk.

**Option A — systemd timer (recommended):** install the units shipped alongside `backup.sh`, set an
offsite `BACKUP_REMOTE` in the service, and enable the timer:

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

**Restore drill** (do this once so you know it works):

```bash
D=/opt/hazync/backups/<STAMP>            # or fetch the snapshot back from the offsite target
cd "$D" && sha256sum -c SHA256SUMS       # verify integrity
sudo systemctl stop hazync-coordinator
cp "$D/coordinator.db" /opt/hazync/coordinator/coordinator.db
tar -C /opt/hazync/coordinator -xzf "$D/proofs.tar.gz"
sudo chown -R hazync:hazync /opt/hazync/coordinator
sudo systemctl start hazync-coordinator
curl -s localhost:8899/api/state | head -c 200   # frontier/proven should match pre-restore
```

## Re-baseline (the guest id changed)

When the guest changes, `METHOD_ID` changes, and **every proof on the board was made against the old
id** — the coordinator will (correctly) reject them all on re-verification. The board must restart from
genesis. This is not a failure; it is the price of a guest change, so batch guest changes deliberately.

The coordinator derives the id it expects from its **own** `HAZYNC_HOST` binary (`expected_method_id()`,
served at `/api/meta`), so the swap is: new binary in, board cleared, workers restarted.

Read the real paths off the unit first — they are env-driven, so do not assume a layout:

```bash
systemctl cat hazync-coordinator | grep -E 'WorkingDirectory|COORD_DB|COORD_PROOFS|HAZYNC_HOST'
```

On the production coordinator those are `COORD_DB=/root/coordinator.db`,
`COORD_PROOFS=/root/hazync-proofs`, `HAZYNC_HOST=/root/hazync-host-x86_64-linux-gnu`. Substitute yours.

```bash
# 1. BACK UP FIRST — the old ledger + receipts are the historical record of the previous baseline.
#    backup.sh honours COORD_DB/COORD_PROOFS, so pass them if they are not under $HZ_HOME.
COORD_DB=/root/coordinator.db COORD_PROOFS=/root/hazync-proofs \
  BACKUP_DIR=/root/hazync-backups /root/hazync/coordinator/deploy/backup.sh
cd /root/hazync-backups/<STAMP> && sha256sum -c SHA256SUMS       # verify before relying on it
```

⚠️ Without `BACKUP_REMOTE` the snapshot sits on the **same disk** as the data. For a re-baseline that is
tolerable *only* because step 3 archives in place rather than deleting — but copy the DB off-box anyway;
it is the attribution ledger and it is small.

```bash
# 2. Stop, swap the host binary (KEEP the old one — it is what re-verifies the archived proofs).
systemctl stop hazync-coordinator
cp /root/hazync-host-x86_64-linux-gnu /root/hazync-host.bak.<OLD_ID_PREFIX>
install -m755 ./host-new /root/hazync-host-x86_64-linux-gnu
/root/hazync-host-x86_64-linux-gnu method-id                     # MUST equal reproduce/METHOD_ID

# 3. Clear the board. Archive, never delete — proofs/ is the artifact the "don't trust us" claim
#    rests on, and the old ledger stays re-verifiable with the archived binary.
mv /root/coordinator.db /root/coordinator.db.<OLD_ID_PREFIX>
mv /root/hazync-proofs /root/hazync-proofs.<OLD_ID_PREFIX> && mkdir /root/hazync-proofs

# 4. Start — init_db() reseeds the open ranges; the frontier restarts at 0.
systemctl start hazync-coordinator
curl -s localhost:8899/api/meta      # method_id == the new id, frontier == 0
```

Then restart the provers (they pre-flight with `hazync selftest`, which now compares against the new
`/api/meta` id and fails loudly on a stale worker binary — so update every worker's `HAZYNC_HOST` too).

## Moderation

Handles are HTML-sanitised and reserved/impersonation names (`satoshi`, `admin`, `bitcoinghost`, …,
env `HANDLE_DENY`) are rejected at claim/submit. To **take down** an abusive entry already on the board,
add its pubkey (hex, one per line) to `MOD_BLOCK_FILE` (default `coordinator/mod_block.txt`) — it is
re-read live, so the entry disappears from the leaderboard/board within the cache TTL (~1.5s), no restart.

### Notes
- **Served window** = the claimable set = the blocks the archive bridge has emitted bundles for (up to
  `tip - HAZYNC_BRIDGE_FINALITY`, default 100). Blocks outside it 404 and the CLI says so; the window grows
  automatically as the bridge follows the chain — nothing to pre-generate. (The legacy
  `gen-witness-window.sh` per-block-witness path still works as a fallback when no bridge is configured,
  but is retired for the live party.)
