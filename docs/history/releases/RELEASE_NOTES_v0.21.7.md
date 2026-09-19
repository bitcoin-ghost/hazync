# Hazync v0.21.7 — a board block across many cards, and the height cap comes off

> Copy of the GitHub release body at the time; the GitHub release is canonical: <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.7>

A board block can now be proved across **N cards** and still produce the receipt the coordinator accepts. Workers
survive a dropped link instead of dying. The bridge's height cap is gone, so blocks above 418,268 are claimable.
Sponsorship payments run through BTCPay. The rest is backups that work above 5 GB, a coordinator that can rebuild
any bundle in the gap, and twelve test suites that CI was never running.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. Measured twice on 2026-09-18: hosts built
> from `3f6b5c8` and from `2419ee2` both reproduced it. Every existing proof stays valid and the board does not
> reset.

---

## Provers: what to do

1. **Upgrade `hazync-worker` and `hazync-run-workers.sh`** (verify `SHA256SUMS.txt.asc` first).
2. ⛔ **If you prove board blocks across cards, you MUST be on v0.21.7.** An older worker does not understand
   mode 6's progress lines, so its claim is never beaten and the coordinator hands your block to someone else ten
   minutes in. See below.
3. **Nothing to reconfigure.** All new settings have defaults that preserve the old behaviour.

---

## A board block across many cards (mode 6)

Before this release the fast path proved a *fixture* and its receipt was **not submittable**; the submittable path
(`prove-range-bridge`) got **one card however large the block**. Mode 6 closes that: `seg-serve` serves a bridge
range, so a board block gets N cards and still emits the `KIND_RANGE` receipt the coordinator accepts.

```sh
# segment coordinator — ONE board block, from its bridge bundle.
# HAZYNC_BIND is REQUIRED if workers are on other machines (see below).
HAZYNC_RANGE=74928 HAZYNC_BRIDGE_OUT=/workspace HAZYNC_PORT=9110 HAZYNC_BIND=0.0.0.0 ./host seg-serve

# each worker, one per card
CUDA_VISIBLE_DEVICES=0 HAZYNC_WORKER_ID=w0 ./host seg-connect <coordinator-host>:9110
```

- No `HAZYNC_BLOCK`, no `HAZYNC_CHUNKS` — a range is one session, and the parallelism is segments across cards.
- **Put a worker on the coordinator's own card too**: `seg-serve` distributes, it does not prove.

### Measured

| block | cards | total |
|---|---|---|
| 230,000 (693 KB bundle, 72 segments) | 1 | 331.5 s |
| | 3 (two machines, two continents) | 130.9 s |
| | 4 | **96.0 s** |
| 418,268 (16 MB bundle, 1,650 segments) | 2 MIG slices | 3,665.5 s (61.1 min) |
| | 4 | **1,454.9 s (24.2 min)** |

⚠ **Adding cards shortens only the distributed part.** On the 4-card 16 MB run, `execution` (84.2 s) and
`assembly` (228.7 s) are serial — 21% of the total. They do not divide, so speedup flattens as cards are added.

## ⛔ `HAZYNC_BIND` — remote workers cannot attach without it

`seg-serve` binds `127.0.0.1` by default (#365), because the wire is **unauthenticated**. A worker on another
machine therefore gets `Connection refused` against an otherwise healthy coordinator.

| setting | effect |
|---|---|
| unset | `127.0.0.1` — local workers only (safe default) |
| `HAZYNC_BIND=0.0.0.0` | all interfaces — remote workers can attach |
| `HAZYNC_SEG_REMOTE=1` | implies `0.0.0.0` |

The shell now says so when it binds loopback, instead of leaving you to discover it from a refused connection
(#401). **Only expose the port on a network you trust**, or tunnel it — anyone who can reach it can feed work in.

## Workers survive a dropped link (#402)

A worker whose link dropped used to exit, and somebody restarted it by hand. It now reconnects.

| variable | default | what it does |
|---|---|---|
| `HAZYNC_RECONNECT_MAX_S` | `600` | give up after this long **disconnected**; `0` restores the old exit-on-drop |

Safe by construction, because the server already handled it: a dropped connection returns the work it owed and
those segments requeue, and work is tracked by **tag, not position**, so a returning worker takes *fresh* work. A
reconnecting worker cannot double-prove. `SEG_EOF` still ends the run — that is the coordinator saying there is no
more work, not a link failure.

## A distributed prove must beat its claim (#368)

A board block is claimed before it is proved, and a claim that never reports progress is released after
`CLAIM_GRACE` (600 s). Mode 6 prints a **different shape** from a single-card prove:

```
     91/2352 segments  124s elapsed, ~3071s left     <- number FIRST, noun PLURAL
```

against a single-card `segment 91/2352`. `hazync` understands both since #368. **An older worker does not** — its
progress never rises and the block is handed to someone else. This is why mode 6 needs a v0.21.7 worker.

## The bridge height cap is gone (#398, #399)

The bridge carried `HAZYNC_BRIDGE_TO=967300` while `EMIT_FROM=967500`, so it halted before it could ever emit and
the board was frozen at 418,268. The cap is removed and the drop-ins are in git, with the history recorded
(220000 → 418257 → 967300 → removed). A **free-space check** runs hourly alongside it: 0 ok / 1 low / 2
could-not-check, floor `HAZYNC_DISK_FLOOR_GB=500`. It contains no delete path.

⛔ **Removing the cap does not by itself unfreeze the board, and this release does not claim it does.**
`EMIT_FROM=967500` still gates emission, and the bridge cannot yet walk that far. Measured on server 1 on
2026-09-18/19: it reaches ~44 GiB RSS by **h=800,257** (112.1M UTXOs), meets its `MemoryMax=44G` cgroup
ceiling, throttles at ~99% memory pressure, and is then OOM-killed — **23 kills** between 20:27:08 and
06:35:29, one alert each (`journalctl`; an earlier note said "two", read from `dmesg`, which is a ring
buffer holding only the last two). ⛔ **And it progresses nowhere**: the highest checkpoint ever reached
is **h=800,257**, and the last three resumes were all *from* 800,257 — once the parallel backfill grew to
~21.5 GiB, each ~12-minute cycle reloads ~29 GiB of state, walks a few hundred blocks and dies. That is
[#350](https://github.com/bitcoin-ghost/hazync/issues/350), and it is a resident-state/sizing problem, not a
configuration one. ⚠ **The two phases alert differently**: each OOM kill fires an alert
(`OnFailure=hazync-alert@%n.service`), but during the *throttle* that precedes it `systemd` reports
`active (running)` and nothing fires at all — a 21:41→01:08 freeze passed unnoticed. Judge the frozen
phase by `wchan=mem_cgroup_handle_over_high` and flat CPU ticks, never by unit state.

⚠ **The provers are not waiting on any of this.** Bundles exist contiguously to **418,268** and the board's
frontier is **93,333**, so roughly **322,000 blocks of witnesses already sit ahead of the fleet** — months of
work at any plausible size. #350 gates *tip-following* and closing the 418,269–967,499 gap, not the board.

⛔ **As of 2026-09-19 the bridge is deliberately STOPPED**, not crash-looping — the text above describes what
it did before it was stopped. It was shut down at 06:54 UTC+2 after 23 OOM kills and ~36 alerts, with its
h=800,257 checkpoint intact, and it will stay down until the memory cap is raised. Nothing in this release
depends on it: the bridge produces witnesses for heights **above 418,268**, and the frontier is still below
94,000. **Provers, contributors and the public board are entirely unaffected** — the board gained ~900 blocks
during the thrash and has kept advancing since the bridge went down.

The fix is a two-part configuration change, both prepared and neither shipped in this release because neither
is a release artifact: `HAZYNC_BRIDGE_CKPT=200` so a killed cycle banks its progress, and
`MemoryHigh=48G`/`MemoryMax=52G` so the cap matches what the post-#355 code actually needs (~49 GiB peak
against the measured tip set of 165,212,120 coins). The cap raise waits on the parallel backfill walk
releasing its ~21.5 GiB.

## Any bundle in the gap can be rebuilt on demand (#374, #377, #378, #383)

`EMIT_FROM=967500` leaves 418,269–967,499 with no bundle, because storing them needs >9 TB. Instead the bridge
keeps a **checkpoint rung every 25,000 blocks**, and the coordinator regenerates a gap height from the nearest
rung when asked. Pruning removes bundles whose block is finished for good — dry-run by default, and it refuses
unless a checkpoint below exists. The sponsor bot now says a stranded block can be *rebuilt* rather than logging
"wait for the bridge", which inside the gap is a wait that never ends.

## Sponsorship payments (#371, #372)

Coordinator invoices and settlement through BTCPay, and the sponsor bot now uses the coordinator's **signed API**
instead of writing `coordinator.db` directly. ⚠ **Payments remain switched off** until the 2-of-3 wallet exists.

## Key rotation is off unless you turn it on (#311, #348)

`/api/rotate` returns **410** unless `ROTATE_ENABLED=1`. On the live board the `rotations` table has zero rows, so
a stolen `key.hex` cannot move anyone's blocks.

## Backups

- **Files over 5 GB can be mirrored at all** (#390). `put_object` is single-part and S3/R2/B2 refuse above 5 GB, so
  a rung larger than that silently could not be uploaded. Now `upload_fileobj` with a 64 MiB part size.
- **The rate limit is charged per part** and progress is reported on the clock, not per file (#393).
- **Checkpoint rungs are mirrored** append-only (#386, #387).
- **A throttled listing no longer pages you** (#403). Litestream makes ~300 R2 list calls an hour and R2 throttles
  ~0.7% of them; the watcher reported that in the same words it would use for a destroyed backup. It now retries
  transients only — `AccessDenied` still alerts after one attempt.

## Housekeeping

- **Twelve coordinator test suites were never registered in CI** (#382, #385) — including the 1,004-line sponsor
  bot suite guarding the component that rents GPUs. All twelve pass; the set costs 26 s.
- The archiver no longer alerts for a walk that has not checkpointed yet (#394).
- `pod-prove` takes its fixture from the checkout instead of a 404 URL (#396).
- Nightly unit-drift check on the coordinator (#380, #391).

## Verified before release

Proved on rented cards from `main`, evidence in
[`docs/V0217_PRERELEASE_TRIAL_2026-09-18.md`](../../V0217_PRERELEASE_TRIAL_2026-09-18.md):

- **A real submission to the live board** — block 92,864, credited on the public board, confirmed by downloading
  the served proof back and matching its size to the receipt.
- **Genesis anchoring**, with a control: the verifier **refused** a mid-chain proof on the genesis pin.
- **Groth16 wrap and verify**: 226,946 B → 4,217 B in 49 s with the journal provably unchanged across the wrap.
  ⚠ Groth16 wrapping is **CPU-only** — it crashes in `sppark` on CUDA builds (#20, upstream won't-fix).
