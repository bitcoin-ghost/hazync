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

## Tunables (claim lifecycle, folding, wide ranges)

All default to the previous behaviour, so deploying changes nothing until one is set deliberately.

| Variable | Where | Default | Effect |
|---|---|---|---|
| `MAX_ATTEMPTS` | coordinator | `3` | Park a range as `failed` after this many **block-implicating** failures |
| `MAX_ENV_FAILURES` | coordinator | `12` | Looser cap for **capacity** failures (OOM, worker restarts) |
| `CLAIM_WIDTH` | coordinator | `1` | blocks per claim-next assignment; `1` = per-block |
| `CLAIM_TTL` | coordinator | `1800` | Reap a claim with no heartbeat for this long |
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
   worker proves and submits several blocks. Rollback: restore `/root/v10_hazync_cli`.
3. **Coordinator** — take a fresh DB backup first. The schema migration is **additive**, and the
   previous coordinator runs unchanged against the migrated schema, so rollback is a file swap and a
   restart, *not* a backup restore. Verify: claims granted, submits verified, frontier advancing, and
   `/api/state` returning the new `failed[]` and `frontier_blocker` fields.
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

## Backup & restore

The DB (`coordinator.db`, the signed ledger) **and** the `proofs/` directory (the re-verifiable STARK
receipts — the artifacts the "you don't have to trust us" claim depends on) must **both** be backed up,
offsite. A same-disk copy dies with the box.

> ⚠️ **`backup.sh` does nothing until it is scheduled.** Shipping the script is not a backup — pick one of
> the two schedulers below and confirm a snapshot actually lands (`ls $HZ_HOME/backups`). Until then the
> ledger + receipts live only on one disk.

**Option A — systemd timer (recommended):** install the units shipped alongside `backup.sh`, set an
offsite `BACKUP_REMOTE` in the service, and enable the timer:

⚠️ **First check the paths match the coordinator's own unit** — they are env-driven and differ per
deployment (production uses `/root/...`, not the `/opt/hazync` defaults):

```bash
systemctl cat hazync-coordinator | grep -E 'COORD_DB|COORD_PROOFS'
```

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

### The CUDA release build needs 25 GB free, and failing costs more than a failed build

`build-release.sh cuda` consumed 21 GB -> 4.4 GB on the GPU box, burning **3.7 GB/minute** while
unpacking the CUDA toolkit. It was killed a minute short of filling the root filesystem — which on a
box running services is how things die silently, not merely how a build fails.

**Check before starting, not after:**

```bash
df -h /            # want 25 GB+ free; 21 GB is NOT enough despite looking close
```

21 GB looked adequate right up until it did not. The consumption is not linear: most of it lands in a
few minutes during the toolkit unpack, so a comfortable-looking figure five minutes in means nothing.

**Where to reclaim it on a box that is short:**

- `docker system prune -af` — 5.3 GB of unused images, always safe
- `/usr/local/cuda-13.x` — RISC0 3.0.5 kernels do NOT build against 13.x, so a host-side 13.x install
  is dead weight for this purpose (~4.8 GB). `provision-vps.sh` installs 12.6 inside the container.
- `~/.hazync/receipts` — the prover's LOCAL copies. The coordinator holds the board's own store, so
  clearing these loses nothing, and a re-baseline invalidates them regardless (~3.7 GB for 17k).

**Pass `SKIP_GROTH16=1`.** groth16 is a runtime rzup component the host does not link against, so the
release build does not need it — it only costs a 488 MB download and, on a slow link, three timeouts.

### Set RZUP_TIMEOUT when running build-release.sh

`build-release.sh` forwards `RZUP_TIMEOUT` **only when the caller sets it** — deliberately, since a
hardcoded default in a wrapper defeats the default it wraps. The consequence is easy to walk into: the
groth16 component is a 488 MB download, and on anything short of a datacentre link it times out at the
default and burns 3 x 300 s of retries before warning and carrying on.

```bash
RZUP_TIMEOUT=7200 ./prover/build-release.sh cpu
```

It is not fatal — groth16 is a RUNTIME rzup component, not something the host links against, so the
build completes and the binary is fine without it. It just costs fifteen minutes of a release for
nothing. Cost it once on 2026-08-03.

### Anything that PRODUCES a proof must be built at the CANONICAL paths

A guest's image id embeds absolute build paths, so a host built anywhere else produces proofs that
verify against **nothing published**. This is easy to walk into: the build succeeds, the binary works,
the proofs look fine, and they are worthless.

Measured on 2026-08-03 while regenerating the SNARK fixtures — the same tree produced **three
different ids**:

| built at | id |
|---|---|
| `/home/…/dev/projects/hazync` (dev box) | `1bed31ef…` |
| `/root/hazync-rebuild` (coordinator, scratch) | `1112670d…` |
| `/hazync-zkvm` (container / canonical) | `dfc9eeda…` |

Only the third can produce a publishable proof. To prove or wrap on a box that is not the container,
reproduce the container's environment exactly:

```
HOME=/root                       # so CARGO_HOME=/root/.cargo
HAZYNC_BASE=/root/hazync-build   # Core + secp sources
REPO_DIR=/hazync-zkvm            # the checkout itself
```

Then `REPO_DIR=/hazync-zkvm HAZYNC_PROVISION=build ./provision-vps.sh`. Verify before proving anything:

```bash
/hazync-zkvm/prover/target/release/host method-id   # MUST equal reproduce/METHOD_ID
```

If it does not match, stop — everything proved with that binary is scrap.

### The mechanical half is SCRIPTED — do not hand-edit it

**`./scripts/rebaseline-id.sh <new-64-hex-id>`**, then let `check-versions.sh` verify.

This script already existed and nothing referenced it, so the 2026-08-03 re-baseline was done by hand:
nine sites across eleven locations, each discovered by a gate failure, in five rounds. Someone then
started writing a *second* script before noticing the first. If you take one thing from this section,
take the command above.

The id comes from the container and **only** from the container — a local build produces a different
id BY DESIGN, because the ELF embeds absolute build paths and normalising them is what
`reproduce/Dockerfile` is for:

```bash
docker build -t hazync-repro -f reproduce/Dockerfile .
docker run --rm hazync-repro                 # prints the canonical METHOD_ID
./scripts/rebaseline-id.sh <that id>         # rewrites every known site
./scripts/check-versions.sh                  # the backstop, not the discovery mechanism
```

If `check-versions` names a site the script missed, **add it to the script** rather than hand-editing.
That is the whole point: the gate finding something should be rare and should teach the script.

**What the script deliberately will not do**, and you must:

1. **Write the supersession note** into `reproduce/METHOD_ID` — why the id moved and what it cost. A
   script cannot write that, and it is the part future readers actually need.
2. **Regenerate `prover/testdata/snark/*.snark`.** They are PROOFS made by the old guest; a proof
   carries its guest id inside it, so they cannot be re-pointed, only re-made. Until then `snark-verify`
   fails, and it *should*. Needs Groth16 on a **CPU** host — it crashes on every CUDA build we ship
   (#20).
3. **Check the WASM and the published verifiers by EMBEDDED ID, never by size.** Swapping one 64-hex
   literal for another is length-preserving, so a stale artifact is byte-identical in size to a correct
   one, and sha256 + PGP both pass over it — they attest the bytes are the bytes, not that they are
   right.

```bash
strings <artifact> | grep -c <new-id>   # want 1
strings <artifact> | grep -c <old-id>   # want 0
```

The aarch64 verifier is no longer committed (#85) — `release-sign.yml` builds it, asserts its embedded
id, and attaches it. Nothing guest-dependent should be committed under `verifier/dist/` again; a gate
now fails if it is.

### Ride-alongs — guest changes worth batching into ANY re-baseline

The board reset is the expensive part, and it costs the same whether one thing changes or five. So a
change that is not worth a re-baseline on its own becomes free once one is happening anyway. Check this
list whenever a re-baseline is planned:

- [ ] **Journal byte-packing** (was issue #22). risc0 serde commits each `u8` as its own 32-bit word, so
      a 32-byte Utreexo root goes on the wire as 128 bytes. Packing recovers 96 B per root: measured
      ~20% on a genesis-anchored `[1..1000]` wrap (3,441 B -> ~2,770 B) and ~25% on a projected
      full-chain wrap (~5,360 B -> ~4,020 B). **Deliberately NOT worth a re-baseline alone** — a few KB
      stays a few KB, and it buys no product capability, since 4 KB and 5.4 KB are equally trivial for a
      phone to fetch and verify. Free if the guest is changing regardless. Treat the percentages as an
      inferred two-point fit, not a measurement.

### Things that MUST be updated when the id changes

**The browser verifier is on this list and is easy to forget.** `verifier-wasm` embeds `METHOD_ID_HEX`
via the verifier crate, so the `.wasm` served at `/hazync/verify/` is a pinned artifact exactly like the
native binaries. Left stale it rejects every new proof, on the page the README leads with.

⚠ **Size is not a staleness signal for ANY of these.** Swapping one 64-hex literal for another is
length-preserving: the correct post-re-baseline `.wasm` is byte-for-byte the same size as the stale one
(1,063,349 B both sides, measured 2026-08-02). Check the embedded id, never the size:

```bash
strings <artifact> | grep -c <new-id>   # want 1
strings <artifact> | grep -c <old-id>   # want 0
```

**Deploy the wasm in the SAME cutover as the coordinator binary swap and board reset.** Earlier and the
browser rejects the still-live old board; later and it rejects the new one. There is no safe order
other than together.


Easy to miss, and each fails in a way that looks like something else:

- [ ] `reproduce/METHOD_ID` — the source of truth; update it FIRST.
- [ ] `verifier/src/main.rs` `METHOD_ID_HEX` — the standalone verifier embeds the id as a literal (it
      cannot import it without dragging in the guest build). `scripts/check-versions.sh` fails the build
      if this drifts, but it CANNOT check `verifier/dist/*` — rebuild and replace those binaries too.
- [ ] `prover/testdata/snark/*.snark` — the CI Groth16 fixtures are pinned to the id and will start
      failing `ci_snark_verify.sh`. Regenerate per `prover/testdata/snark/README.md`.
- [ ] **the archive bridge's binary** — it produces the bundles everyone else consumes. Missing it once
      already stalled the board dead while every other component looked healthy.
- [ ] docs stating the current id (`docs/PROVING.md`, `SECURITY.md`, `docs/ROADMAP.md`) — `check-versions.sh` enforces.

The coordinator derives the id it expects from its **own** `HAZYNC_HOST` binary (`expected_method_id()`,
served at `/api/meta`), so the swap is: new binary in, board cleared, workers restarted.

Read the real paths off the unit first — they are env-driven, so do not assume a layout:

```bash
systemctl cat hazync-coordinator | grep -E 'WorkingDirectory|COORD_DB|COORD_PROOFS|HAZYNC_HOST'
```

On the production coordinator those are `COORD_DB=/var/lib/hazync/coordinator.db`,
`COORD_PROOFS=/var/lib/hazync/proofs`, `HAZYNC_HOST=/usr/local/bin/hazync-host`, with the checkout at
`/opt/hazync`. Substitute yours — and note these MOVED on 2026-08-02 (#58): everything used to live
under `/root`, which is `0700 root` and therefore blocked `User=`, `ProtectHome=` and
`ProtectSystem=strict` on both units.

**Both services now resolve the same binary** (`/usr/local/bin/hazync-host`). That is not cosmetic: it
structurally removes the split-binary failure described in step 2 below, where the bridge was left on
an old guest while the coordinator was upgraded. There is now one path to swap, not two.

```bash
# 1. BACK UP FIRST — the old ledger + receipts are the historical record of the previous baseline.
#    backup.sh honours COORD_DB/COORD_PROOFS, so pass them if they are not under $HZ_HOME.
COORD_DB=/var/lib/hazync/coordinator.db COORD_PROOFS=/var/lib/hazync/proofs \
  BACKUP_DIR=/var/lib/hazync/backups /opt/hazync/coordinator/deploy/backup.sh
cd /var/lib/hazync/backups/<STAMP> && sha256sum -c SHA256SUMS    # verify before relying on it
```

⚠️ Without `BACKUP_REMOTE` the snapshot sits on the **same disk** as the data. For a re-baseline that is
tolerable *only* because step 3 archives in place rather than deleting — but copy the DB off-box anyway;
it is the attribution ledger and it is small.

```bash
# 2. Stop, swap the host binary (KEEP the old one — it is what re-verifies the archived proofs).
#
#    ⚠️ THE COORDINATOR IS NOT THE ONLY BINARY. The archive bridge PRODUCES the witnesses everyone
#    else consumes, and it is a separate service with its own ExecStart path. Missing it is what
#    caused the v0.10.0 stall: the bridge stayed on a pre-v0.9.0 host, emitted bundles without the
#    `txs` field, and every prover panicked with "missing field `txs`" the moment the board reached
#    the first bundle it had written. Swap BOTH, then run scripts/check-deployment.sh --local.
systemctl stop hazync-coordinator hazync-bridge
cp /usr/local/bin/hazync-host /root/hazync-host.bak.<OLD_ID_PREFIX>   # KEEP: re-verifies archived proofs
install -m755 ./host-new /usr/local/bin/hazync-host
/usr/local/bin/hazync-host method-id                             # MUST equal reproduce/METHOD_ID

# 3. Clear the board. Archive, never delete — proofs/ is the artifact the "don't trust us" claim
#    rests on, and the old ledger stays re-verifiable with the archived binary.
mv /var/lib/hazync/coordinator.db /var/lib/hazync/coordinator.db.<OLD_ID_PREFIX>
mv /var/lib/hazync/proofs /var/lib/hazync/proofs.<OLD_ID_PREFIX>
mkdir /var/lib/hazync/proofs && chown hazync:hazync /var/lib/hazync/proofs

# ⚠ THE SPINE TOO — it is a PROOF and it does not live with the others.
# COORD_SPINE defaults to <server.py dir>/spine, i.e. INSIDE the checkout, and it is untracked — so
# `git checkout <tag>` does not touch it and it survives every step above. Left in place, the
# coordinator keeps serving a genesis-anchored proof made by the OLD guest, and /api/spine/proof —
# the exact command the README's 30-second demo tells strangers to run — fails against the new
# verifier. Missed on 2026-08-02 and caught only by walking the published path as a stranger.
mv /opt/hazync/coordinator/spine /opt/hazync/coordinator/spine.<OLD_ID_PREFIX>
mkdir /opt/hazync/coordinator/spine && chown hazync:hazync /opt/hazync/coordinator/spine
# ⚠ the services run as USER hazync since #58 — anything you recreate by hand must be chowned, or the
#   coordinator starts, serves reads, and silently fails every write. That includes the PARENT
#   directory: /var/lib/hazync itself must be hazync-owned, or sqlite cannot CREATE the new DB and
#   the service dies at startup with "unable to open database file". Hit on 2026-08-02 despite this
#   warning already being written — an existing file is writable, a missing one needs a writable dir.

# 4. Start BOTH — init_db() reseeds the open ranges; the frontier restarts at 0.
systemctl start hazync-coordinator hazync-bridge
curl -s localhost:8899/api/meta      # method_id == the new id, frontier == 0

# 5. Verify no component was left behind. This is the check that would have caught the v0.10.0 stall.
./scripts/check-deployment.sh --local
```

### Then purge the provers' bundle caches

Regenerating bundles is **not enough**. The contributor CLI caches by height
(`BUNDLE_DIR/bundle_<h>.json`) and skips re-fetching when the file exists, so a prover that already
pulled a stale bundle keeps replaying it and keeps failing — long after the bridge is fixed. This cost
real time to spot, because the coordinator was serving the correct bundle the whole while.

On every prover:

```bash
pkill -f reprove_worker.sh; pkill -f 'hazync run'
rm -rf "$BUNDLE_DIR" ~/.hazync/bundles          # whatever BUNDLE_DIR the workers use
./coordinator/run-workers.sh 4                   # re-fetches cleanly; refuses to start on an id mismatch
```

Confirm a bundle actually re-fetched (a fresh mtime, and `txs` present) before assuming it worked.

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
