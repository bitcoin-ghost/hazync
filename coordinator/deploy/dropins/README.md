# systemd hardening drop-ins

Applied to the live coordinator box on 2026-08-02 in response to audit finding M-1, and kept here so
the hardening is reproducible rather than something that exists only on one machine.

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
