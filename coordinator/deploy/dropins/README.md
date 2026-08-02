# systemd drop-ins — hardening and the /root migration

Applied to the live coordinator box on 2026-08-02: first hardening (audit M-1), then the path
migration off `/root` (#58) that unblocked the rest of it. Kept here so both are reproducible rather
than existing only on one machine.

## Current state (measured 2026-08-02, after the migration)

| service | user | ProtectHome | ProtectSystem |
|---|---|---|---|
| `hazync-coordinator` | **hazync** | **true** | **strict** |
| `hazync-bridge` | root | read-only | strict |

The coordinator — the internet-facing service — is now fully unprivileged with `/root` inaccessible.

**The bridge cannot drop privilege yet, and the reason is bitcoind, not the bridge.** bitcoind runs as
root with `-datadir=/root/.bitcoin` and writes `.cookie` as `0600 root:root`, rewriting it on every
restart. An unprivileged bridge cannot authenticate. Closing that needs either `rpcauth` credentials
in `bitcoin.conf` or bitcoind's datadir moving out of `/root` — both are bitcoind changes, so they are
out of scope here. `ProtectHome=read-only` is the most that can be done meanwhile.

## Where things live now

| | path | owner |
|---|---|---|
| checkout | `/opt/hazync` | root |
| DB / state / proofs / witnesses / backups | `/var/lib/hazync/…` | hazync |
| bridge bundles | `/var/lib/hazync/bridge_bundles` | root, `0644` (coordinator reads) |
| host binary | `/usr/local/bin/hazync-host` | root |

The migration was a same-filesystem rename of ~85 GB (73 GB of bundles, 12 GB of proofs) and took
**0 seconds**. The 53,350 proof files were chowned *before* the outage, since chown does not disturb
open file descriptors — so the board was down only for the renames and a daemon-reload.

**Four units referenced these paths**, not two: the coordinator, the bridge, `hazync-coordinator-backup`
and `hazync-retention-check`. Missing either of the last two would have broken backups or the G1
retention gate silently.

## Why drop-ins and not the unit files next door

**The live units have drifted substantially from `../hazync-*.service`.** They are not slightly
different — they are a different deployment:

| | repo unit | live |
|---|---|---|
| paths | `/opt/hazync/...` | everything under `/root` |
| user | `User=hazync` | root, no `User=` |
| bind | `127.0.0.1` | `0.0.0.0` |
| extras | — | `ratelimit.conf`, `canonical-binary.conf`, `height-cap.conf` |

Those extra drop-ins carry real operational history: a rate-limit correction, a binary override that
fixed a board stall at block 18310, and an emergency height cap added when the bridge was on course to
fill the disk in ~10 hours. **Deploying the repo units over that would have destroyed all of it** —
and, because of the `ProtectHome` conflict below, would have silently broken witness serving at the
same time.

So the hardening was added as drop-ins, leaving every existing directive untouched.

## What is deliberately NOT set

- **`User=` / `Group=`** — `ExecStart`, the datadir, the DB, the state dir and the proof store are all
  under `/root`. Dropping privilege needs a path migration first (#58).
- **`ProtectHome=`** — `/root` must stay readable *and writable*: `coordinator.db`, `coord_state` and
  `hazync-proofs` live there, and `/root/bridge_bundles` + `/root/witnesses` must be readable. This is
  exactly the conflict in #60.
- **`ProtectSystem=strict`** — same reason; it would make `/root` read-only.

`ProtectSystem=full` is used instead: `/usr`, `/boot` and `/etc` become read-only, `/root` is
untouched. That is a real gain with none of the path risk.

## Applying

```bash
install -m 0644 hazync-coordinator-hardening.conf \
  /etc/systemd/system/hazync-coordinator.service.d/hardening.conf
install -m 0644 hazync-bridge-hardening.conf \
  /etc/systemd/system/hazync-bridge.service.d/hardening.conf
systemctl daemon-reload
systemctl restart hazync-bridge hazync-coordinator
```

## Verifying — `is-active` is not enough

A hardened service that has lost access to a path it needs usually keeps running and says nothing.
Check the things that would go quiet:

```bash
systemctl show hazync-coordinator -p NoNewPrivileges -p ProtectSystem -p PrivateTmp
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/api/state?slim=1   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/api/witness/500    # 200 — bundle dir readable
ls -la /root/coordinator.db                                                       # mtime advancing
```

Measured after applying, 2026-08-02: both services active, `/api/state` 200, `/api/witness/500` 200,
DB written, bridge resumed from its checkpoint at height 220000 (its configured cap), and the public
board served 46,124 proven with frontier 46,124.
