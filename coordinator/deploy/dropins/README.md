# systemd drop-ins — hardening and the /root migration

Applied to the live coordinator box on 2026-08-02: first hardening (audit M-1), then the path
migration off `/root` (#58) that unblocked the rest of it. Kept here so both are reproducible rather
than existing only on one machine.

## Current state (measured 2026-08-02, after the migration)

| service | user | ProtectHome | ProtectSystem | NoNewPrivileges |
|---|---|---|---|---|
| `hazync-coordinator` | **hazync** | **true** | **strict** | true |
| `hazync-bridge` | **hazync** | **true** | **strict** | true |

**Both services are now fully unprivileged, with `/root` inaccessible to each.**

The bridge was the harder one, and the blocker was never the bridge — it was bitcoind. bitcoind runs
as root with `-datadir=/root/.bitcoin` and rewrites `.cookie` as `0600 root:root` on every restart, so
an unprivileged bridge could not authenticate, and `ProtectHome=true` was impossible while it needed
to read that cookie.

Solved by giving the bridge its **own client-only datadir** — `/var/lib/hazync/bitcoin-client`, mode
`0600 hazync`, containing nothing but `rpcauth` credentials. `bitcoin-cli` reads those and talks to
the node over localhost RPC, so the bridge never touches `/root` at all. bitcoind gained one
`rpcauth=` line and was restarted (RPC ready again in ~30 s; the node is 864 GB with `txindex=1`).

The credentials were generated **on the box** so the password never transited a log.

## Where things live now

| | path | owner |
|---|---|---|
| checkout | `/opt/hazync` | root |
| DB / state / proofs / witnesses / backups | `/var/lib/hazync/…` | hazync |
| bridge bundles | `/var/lib/hazync/bridge_bundles` | hazync (bridge writes, coordinator reads) |
| host binary | `/usr/local/bin/hazync-host` | root (0755) |
| bitcoind RPC creds | `/var/lib/hazync/bitcoin-client/bitcoin.conf` | hazync, `0600` |

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

## What is still NOT possible, and why

Nothing is held back on the hazync side any more. The one remaining item is bitcoind's own posture:

- **bitcoind still runs as root** with `-datadir=/root/.bitcoin`. That is out of scope here — the
  bridge no longer depends on it, because it authenticates over RPC instead of reading the cookie.
  Hardening bitcoind itself is a separate exercise.

## Applying

```bash
for f in hazync-coordinator-hardening hazync-coordinator-paths-and-user \
         hazync-bridge-hardening hazync-bridge-paths \
         hazync-coordinator-backup-paths hazync-retention-check-paths; do
  svc=$(echo "$f" | sed -E 's/-(hardening|paths-and-user|paths)$//')
  name=$(echo "$f" | grep -oE '(hardening|paths-and-user|paths)$')
  install -D -m 0644 "$f.conf" "/etc/systemd/system/$svc.service.d/$name.conf"
done
systemctl daemon-reload
systemctl restart hazync-bridge hazync-coordinator
```

The bridge drop-in assumes `rpcauth=` is present in bitcoind's `bitcoin.conf` and that
`/var/lib/hazync/bitcoin-client/bitcoin.conf` holds the matching credentials. Without those it cannot
reach the node, because it no longer has any path to the cookie.

## Verifying — `is-active` is not enough

A hardened service that has lost access to a path it needs usually keeps running and says nothing.
Check the things that would go quiet:

```bash
systemctl show hazync-coordinator -p User -p ProtectHome -p ProtectSystem
systemctl show hazync-bridge      -p User -p ProtectHome -p ProtectSystem
sudo -u hazync bitcoin-cli -datadir=/var/lib/hazync/bitcoin-client getblockcount   # bridge -> node
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/api/state?slim=1    # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/api/witness/500     # 200 — bundle read
systemctl start hazync-retention-check && systemctl show hazync-retention-check -p ExecMainStatus
```

Measured after the migration, 2026-08-02: both services active as `hazync` with `ProtectHome=true`
and `ProtectSystem=strict`; unprivileged `getblockcount` returned 960691; `/api/state`,
`/api/witness/500` and `/api/spine` all 200; a write lock taken and released on the DB as `hazync`;
retention gate `exit=0`; backup `exit=0`; and the public board at 46,177 proven with frontier ==
proven throughout.
