# Running a prover: quick start and operator's manual

[`CONTRIBUTING.md`](../CONTRIBUTING.md) takes you from nothing to your first proof. This page is for what
comes after: keeping workers running for days, upgrading, stopping without stranding a block, watching the
board, and fixing what goes wrong.

Figures here are measured on the project's own cards (RunPod A40s and RTX 4090s) unless they say
otherwise, and each carries its date. Features marked **v0.21.5** need that release or later.

## Quick start: one box, one GPU

```
# 1. download the prover, the CLI and the launcher, keeping their asset names, and verify them
#    (CONTRIBUTING.md, Step 1; SECURITY.md#verifying-releases)

# 2. who you are, and a backup of the key that proves it
./hazync id yourname
cp ~/.hazync/key.hex /somewhere/safe/    # lose this and your blocks stay under a name nobody can sign for

# 3. check the setup before spending GPU hours
./hazync selftest

# 4. (v0.21.5) get a push on your phone when a worker needs you
./hazync notify new

# 5. start two workers, and watch
HAZYNC_HOST=$PWD/hazync-host-x86_64-linux-gnu-cuda ./run-workers.sh 2
tail -f ~/hazync-workers/worker_1.log
```

To stop: [Stopping](#starting-stopping-restarting). To keep it running across reboots:
[systemd](#keeping-it-running-across-reboots).

## What a worker does

`run-workers.sh N` starts N loops. Each runs one CLI command, and when it exits starts it again.

| `MODE` | the loops run | notes |
|---|---|---|
| `prove` (default) | `hazync run`: claim the earliest open block, prove it, submit it | the job that moves the frontier |
| `fold` | `hazync fold`: merge two adjacent proven ranges | seconds per fold; any number of people can fold |
| `spine` | `hazync spine`: absorb the next chunk into the genesis-anchored spine | serial: **one spine worker across all your boxes** |
| `mixed` | N−2 prove, 1 folds, 1 spine (the spine only when N > 2) | run `mixed` on **one** box and `prove` on the rest |

- **Someone has to be proving new blocks.** On 2026-09-14 the project turned its only proving card into a
  folder; nobody else was proving single blocks, and the frontier stood still for 40 minutes until it was
  switched back. Folding and the spine only use what provers have already made.
- **One GPU job at a time per box.** Every worker takes `/tmp/hazync-gpu.lock` while it uses the card
  (`HAZYNC_GPU_LOCK`), because two provers on one card fail when their memory peaks coincide. Extra
  workers on one card help only while the others are fetching a bundle or waiting on the coordinator; the
  project's own proving cards run two.
- **One prove per card.** At the CUDA default segment size a prove peaks at about 22 GB of VRAM (L40S,
  2026-08-28; CONTRIBUTING, "Got more than one card?").

## Claims: who is offered which block

A bare `hazync run` claims the block it proves; `hazync run <n>` or a span claims nothing. The coordinator
accepts a valid proof at any height from anyone, whoever holds the claim. Claims only decide who is
**offered** which block.

| rule | default | what it means for you |
|---|---|---|
| heartbeat | while proving | a worker that beats keeps its block, however slow the block is |
| `CLAIM_GRACE` | 600 s | a claim that never beat is released after 10 minutes |
| `CLAIM_TTL` | 3,600 s | a claim is released an hour after its last heartbeat |
| `CLAIM_MAX` | 86,400 s | and after a day regardless |
| `CLAIM_OPEN_MAX` | 4 | one key holds at most 4 live claims, across all your boxes; a fifth is "nothing to claim right now" |
| `CLAIM_RETAKE_WAIT` | 3,600 s | a key does not get back a block its own claim let lapse without a heartbeat; everyone else is offered it at once |
| signed claims | from **v0.21.5** workers | the claim is signed with your key, so nobody else can use up your 4 claims |

These are the coordinator's settings; the public board runs the defaults (2026-09-14).

**Proving a block by hand.** If a block is stuck behind someone's claim, `hazync run <n>` proves it without
one. Do not run it on a box whose own loop may be proving the same block: both use
`~/.hazync/receipts/.work-<n>`, and on 2026-09-14 the loop's prove crashed when a hand-run prove of the same
block started beside it. Check `worker_<i>.log` first, or use another box.

## Starting, stopping, restarting

**Start.** `run-workers.sh N` refuses to start if the prover's guest id differs from the coordinator's
(`/api/meta`), or if the box has a GPU that cannot prove a test block. It warns if no handle is set.

**Quick stop.** `./run-workers.sh N --stop` kills the loops and whatever they are running. A block that was
being proven stays claimed for up to an hour after its last heartbeat, so nobody else is offered it in that
time.

**Graceful stop** (before an upgrade, or whenever you can wait): stop the loops, let each block in flight
finish, then confirm nothing is left.

```
# stop the loops; a fold or prove already running finishes its one block
for p in $(ps -eo pid,args | awk '$2 ~ /^hazync-(worker|fold|spine)-loop-/ {print $1}'); do kill $p; done
# a spine worker loops until the spine catches up, and holds no claim: stop it now
for p in $(ps -eo pid,args | awk '$3 == "./hazync-worker" && $4 == "spine" {print $1}'); do kill $p; done
# wait for the blocks in flight
while ps -eo args | grep -qE '^python3 ./hazync-worker (run|fold)'; do sleep 5; done
./run-workers.sh N --stop     # confirms nothing is left
```

⚠ **Match processes with `ps | awk`, as above, not `pkill -f` inside `ssh host '…'` or `bash -c '…'`.**
`pkill -f` matches the pattern anywhere in a command line, and the shell running your one-liner has the
pattern in its own command line, so it kills that shell. `run-workers.sh --stop` has the same property: a
process whose command line mentions `hazync-worker` counts as a survivor.

**Restart** after a graceful stop: run `run-workers.sh N` again.

## Keeping it running across reboots

`run-workers.sh` starts its loops in the background and exits, so a oneshot unit that stays active fits it.
Adjust the user, paths and N:

```ini
# /etc/systemd/system/hazync-workers.service
[Unit]
Description=Hazync workers
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=prover
WorkingDirectory=/opt/hazync
Environment=HAZYNC_HOST=/opt/hazync/hazync-host-x86_64-linux-gnu-cuda
Environment=LOG_DIR=/home/prover/hazync-workers
ExecStart=/opt/hazync/hazync-run-workers.sh 2
ExecStop=/opt/hazync/hazync-run-workers.sh 2 --stop
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-workers
```

- `systemctl stop` is the **quick stop**. Before an upgrade, do the graceful stop first.
- The unit's `HAZYNC_HOME` is the service user's `~/.hazync`, so run `hazync id` and `hazync notify` as that
  user.
- Tested 2026-09-14 as a **user** unit (`systemctl --user`, the same `[Service]` section without `User=`)
  with a stand-in worker: the unit starts the loops and stays active, stop leaves nothing running, and it
  enables for boot. Not yet run on a GPU box.
- Many rented GPU hosts (RunPod pods, for example) are containers without systemd: start the launcher from
  the provider's start command instead.

## Upgrading to a new release

1. **Download the new files beside the old ones**, keeping the asset names, with `SHA256SUMS.txt` and
   `SHA256SUMS.txt.asc`, and verify them
   ([`SECURITY.md`](../SECURITY.md#verifying-releases)): `gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt`, then
   `sha256sum -c --ignore-missing SHA256SUMS.txt`.
2. **Check the guest id** against the coordinator. A re-baseline changes it, and a mismatched prover has
   every proof rejected:
   ```
   ./hazync-host-x86_64-linux-gnu-cuda method-id
   curl -s 'https://api.hazync.org/api/meta' | python3 -c 'import json,sys; print(json.load(sys.stdin)["method_id"])'
   ```
3. **Graceful stop**, swap the files in (keep the old ones until the new ones have proved something), start
   again, and run `./hazync selftest`.

The launcher refuses to start on a mismatch, and a running worker whose proof is rejected for one stops
itself (exit 78), so a missed upgrade shows up as stopped workers, not as silent waste.

## Watching it

**The board.** <https://bitcoinghost.org/hazync>, or from a shell:

```
curl -s 'https://api.hazync.org/api/state?slim=1' | python3 -c '
import json, sys
d = json.load(sys.stdin); p = d["progress"]; b = d.get("blocked") or {}
print("frontier", p["frontier"], "| proven", p["proven"], "| spine", p["spine_hi"], "| tip", p["tip"])
print("next block for the frontier:", b.get("block"), b.get("status"), "| waited", b.get("stalled_for"), "s |", b.get("why", ""))
print("needs attention:", b.get("needs_attention"))'
```

- **frontier**: every block from genesis to here is proven and joins up. It is what users of the proofs
  care about.
- **next block for the frontier** (`blocked`): the block the frontier needs, its status, how long the
  frontier has waited, and the coordinator's reason. A big block under a live, beating claim is waiting,
  not stuck; `needs_attention` is the coordinator's judgement that it is actually stuck.
- **your row**: `leaderboard` entries carry `handle`, `proved`, `folded`, `anchored` and the client
  `version` your workers last reported.

**Logs.** `LOG_DIR/worker_<i>.log` (default `~/hazync-workers`). Lines worth knowing:

| line | meaning |
|---|---|
| `claimed block N` | this worker was offered block N |
| `✓ range N: the coordinator re-verified your proof` | it landed |
| `range N was already proved by someone else` | a race lost; not a failure |
| `nothing to claim right now: …` | (v0.21.5) nothing free, or this key holds its 4 claims; the loop waits 30 s |
| `FATAL: …` | the worker stopped; the line says why |

**Exit codes**, for your own supervisor: `0` done; `1` failed, the loop retries after 5 s; `75` nothing to
claim right now (v0.21.5), the loop waits 30 s; `78` stopped for good (no usable GPU, guest id mismatch),
the loop exits.

**Alerts** (v0.21.5). `hazync notify new` creates a private ntfy topic and sends a test push. Workers then
push when they stop for good, when a proof is rejected, or when a claim is refused for a reason the box must
fix (a wrong clock, for example), and the launcher pushes when it refuses to start, after
`NOTIFY_FAIL_STREAK` failures in a row (default 5), and on recovery. One push per problem an hour. The topic
is the only secret: keep it private. CONTRIBUTING, "Get a push when a worker needs you".

## Disk

| what | where | grows | measured (2026-09-14) |
|---|---|---|---|
| receipts | `~/.hazync/receipts/<range>.bin` | one per block you prove, never deleted | 228 KB largest; 2,225 receipts in 881 MB on a card running since 2026-09-11 (proving, then folding) |
| bundles | `LOG_DIR/bundles_<i>/` (launcher) or `~/.hazync/bundles/` | one per block fetched, never deleted | about 22 MB for 2,324 early bundles; they grow with height |
| spine scratch | `~/.hazync/receipts/.spine-work/` | while a spine worker runs; removed when it exits | 720 MB under one long-running spine worker |
| logs | `LOG_DIR/worker_<i>.log` | appended | 1.1 MB after days |

- A **receipt that has been accepted** is on the board; `run`, `fold` and `spine` do not read your local copy
  again. Keep any that were not accepted: `hazync submit <range>` sends one again.
- **Bundles** can be deleted while no worker is running; they are fetched again when needed. At height
  220,000 the median bundle is 2.16 MB and the largest 16.87 MB, so a prover working in that era fills a disk
  much faster than one near genesis.
- The project's cards have 40 GB of container disk, and the busiest had used 4.4 GB after days.

## Rented GPUs

- **A card can pass boot with no usable GPU.** The launcher's test prove catches it before any loop starts,
  and a worker that hits it stops itself (exit 78, an urgent alert with v0.21.5) rather than claiming block
  after block (#261).
- **Public SSH is not guaranteed.** On RunPod, RTX 4090 community hosts often never exposed public SSH: 3 of
  7 on 2026-09-11, then 3 of 3 on a retry, and 1 of 4 on 2026-09-14. The A40s used on those days always
  did.
- **A card with nothing to do still bills.** Stopping the workers does not stop the pod. On 2026-09-12 a card
  was left to finish one block and then sat idle for about three hours at $0.49/h.
- **Your key is on that machine.** Whoever controls the host can read `~/.hazync/key.hex`, and a key rotation
  cannot be undone ([#311](https://github.com/bitcoin-ghost/hazync/issues/311)). Use hosts you trust with
  your name.

## Identity

- `~/.hazync/key.hex` is who you are; `~/.hazync/handle` is only the name shown. Back up the key (CONTRIBUTING,
  Step 2).
- **The same identity on several boxes** is fine, and is how the project runs its own cards. The claim limit
  (4 live claims) is per key, across all of them.
- **`HAZYNC_HOME`** points a worker at a different identity: a separate key and handle, credited
  separately.
- **A new box**: `hazync rotate /path/to/old/key.hex` moves the old key's blocks onto the new identity. Both
  keys sign; it cannot be undone.

## When something is wrong

| you see | likely cause | what to do |
|---|---|---|
| launcher: `FATAL: guest id mismatch` | a prover binary from before a re-baseline | [upgrade](#upgrading-to-a-new-release) |
| launcher: `FATAL: this box has a GPU but cannot prove on it` | driver or device | `nvidia-smi`; replace the host or fix the driver |
| log: `no usable CUDA device`, and the loop stopped | same | same |
| log: `nothing to claim right now` all the time | a busy board, or more than 4 proving workers on one key | normal on a busy board; otherwise run fewer proving workers per key, or fold instead |
| log: `claim timestamp outside +/-120s` | the box's clock is wrong | turn on time sync (`timedatectl set-ntp true`) |
| log: `the claim signature does not match that pubkey` | `key.hex` changed under a running worker | restart the workers; check `~/.hazync` |
| log: `refusing to POST … from a source checkout` | the CLI is running from a git checkout | use the release CLI, or point `COORD_URL` at your own coordinator |
| board: frontier waiting on a block `claimed` for hours | a big block under a live worker | usually waiting, not stuck; if `needs_attention` is true, read `why`, and a box not already proving it can run `hazync run <n>` |
| disk full | receipts and bundles | [Disk](#disk) |

Still stuck: open an issue with the **Board or prover problem** template, including `hazync selftest`
output and the log lines around the failure.
