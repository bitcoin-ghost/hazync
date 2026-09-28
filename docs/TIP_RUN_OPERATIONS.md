# Tip runs: the operator's manual

Renting a fleet and proving Bitcoin blocks **as the chain mines them**. This is the page for the
person driving `coordinator/tip_smoke.py` with real money.

- New to proving? [`CONTRIBUTING.md`](../CONTRIBUTING.md), then
  [`PROVER_OPERATIONS.md`](PROVER_OPERATIONS.md) — that is the manual for running *your own* prover.
- Proving one block across cards you already have?
  [`FLEET_OPERATIONS.md`](FLEET_OPERATIONS.md).
- This page is the one that spends money.

---

## What a run costs, and what stops it

⛔ **Capacity, not money, is what caps a fleet.** Measured 2026-09-27, 45 requested of each type with
SECURE stock, within the same few minutes:

| card | granted of 45 | $/hr each |
|---|---|---|
| RTX 4090 | **1** | 0.74 |
| RTX PRO 4500 SE | **4** | 0.72 |
| RTX PRO 6000 | **38** | 2.09 |

Two hours later the same PRO 6000 granted **17**. `stockStatus` said `Low` for all of it — `Low` has
meant 1 card and it has meant 38, on the same card, in the same minute. **You cannot survey and then
decide; you can only be trying at the moment stock appears.** Plan for a fleet you can actually get,
not the one you want.

A one-hour session on ~20 RTX PRO 6000 is roughly **$45–65** including the gates.

---

## Before you launch

**1. The identity.** Board work is credited to whoever signs the claim — publicly and permanently.

```sh
export HAZYNC_HOME=/var/lib/hazync/identity-GHOST     # the box's identity directory
python3 -c "import tip_board; print(tip_board.identity()[2])"   # prints the handle
```

⛔ Always pass **`--claim-as "G H O S T"`**. Every box has *some* identity, so a run always claims as
someone: on 2026-09-26 a tip session claimed a block as `hazync-coordinator` and nothing was wrong
with it except the name. `--claim-as` refuses **before anything is rented**.

**2. The repo on the box.** `/opt/hazync` must be at the release you mean to run.

**3. Where it runs.** The driver needs the identity, the RunPod key
(`/root/.hazync/runpod.key`), the live rig and the publish key. It does **not** need a GPU.

---

## The launch

```sh
export HAZYNC_HOME=/var/lib/hazync/identity-GHOST
python3 -u coordinator/tip_smoke.py \
  --session 1 --budget-usd 150 \
  --fresh-tip --clock-from-tip --claim-as "G H O S T" \
  --cards 30 --min-cards 18 --spares 10 \
  --gpu-type "NVIDIA RTX PRO 6000 Blackwell Server Edition" \
  --agg-candidates 4 \
  --live-rig /root/hazync-live-rig \
  --publish-dest root@152.53.86.216: --publish-key /root/.ssh/hazync_publish \
  --rundir /root/tiprun-$(date +%F) 2>&1 | tee -a /root/tiprun-$(date +%F)/run.log
```

Run it detached (`setsid nohup … &`) if you want it to outlive your ssh session. ⚠ Record the exit
status **into the log** — a background wrapper's exit code is the last command's, not the driver's.

---

## The three fleet numbers

| flag | it is a… | who reads it |
|---|---|---|
| `--cards N` | **target** | what to rent, how long to wait for ssh, what the tail cut trims to, what counts as surplus |
| `--min-cards M` | **floor** | the final gate check, the `--adopt` check, the fetch-abandon view |
| `--spares S` | padding | rented so one bad pod is not fatal; unused ones are released |

⛔ **They are not interchangeable, and getting it wrong is expensive in both directions.**

- Treating the **target as a floor** throws away healthy fleets. On 2026-09-27 four runs died holding
  29 of 30, 28 of 29 and 25 of 26 twice — each after paying for every gate. ~$45, no blocks.
- Treating the **floor as a target** shrinks them. The same evening, `--min-cards` passed to
  `wait_for_ssh` and to the tail cut took a 21-card fleet to **16** (it stopped waiting the moment it
  held the floor, releasing five still-booting cards) and then to **10** (the tail cut trims *down
  to* `need`). Ten cards cannot hold tip pace.

**Rule of thumb:** `--cards` = what you want, `--min-cards` ≈ 60% of it, `--spares` ≈ a third.

---

## The other flags that matter

| flag | default | what it does |
|---|---|---|
| `--fresh-tip` | off | prove only blocks mined **after** the fleet is ready. Boot is paid while idle, and each block is timed from a height that did not exist at launch. **This is what makes a tip-hour claim honest.** |
| `--clock-from-tip` | off | start the `--session` window at the **first tip block** instead of when the fleet is ready. Pair it with `--fresh-tip`, which makes the session wait for a block that did not exist at launch — otherwise that wait comes out of the hour |
| `--clock-wait-max H` | 2.0 | with `--clock-from-tip`, give up after this many hours if no tip block ever arrives. A deferred clock has **no other deadline**, so this is what stops a fleet being paid to wait on a bridge that has stopped serving bundles |
| `--tip-max-behind N` | 3 | how many unproved tip bundles may queue before the session gives up on sequence and jumps to the newest, **recording every height it skips**. `0` = never jump, stay sequential whatever the cost |
| `--tip-from H` | — | start the tip floor just below `H`, to close a known gap. Overrides `--fresh-tip`'s floor, which sets itself at the current tip and so can never reach back |
| `--allow-tip-gaps` | off | ⛔ prove a tip block even when it is not the child of the last one proved. Produces a proof chain with a hole in it — recovery only |
| `--no-board-fill` | off | with `--fresh-tip`, sit idle between tip blocks instead of proving board work. Only for measuring tip latency with nothing else on the fleet |
| `--gpu-type` | `auto` | `auto` ranks every type with SECURE stock by *measured* cost per proof, then by price for unmeasured cards, and fills from **one** type where it can. Or pin a type by name |
| `--grow-to N` | 0 | during a session, rent and gate toward N on a background thread; recruits join at a **block boundary** |
| `--adopt FILE` | — | reuse a previous run's `rented.json` instead of renting. Adopted pods are **not** released at the end |
| `--release-adopted` | off | with `--adopt`, hand them back after all |
| `--worker-min-mbit` | 0 | release workers the aggregate cannot push to at this rate. 0 = measure and report only |
| `--agg-candidates` | 3 | how many cards to measure before choosing the aggregate |
| `--claim-as HANDLE` | — | refuse to start unless this box's claim identity is HANDLE |
| `--budget-usd` | 0 | stop when spend reaches this. Charged every loop at the **live** fleet rate |
| `--cleanup` | — | release every pod **both** `rented.json` and `recruited.json` record, and exit. Reports pods on the account that neither knows about, without touching them |

---

## The phases, and how long each takes

| phase | typical | notes |
|---|---|---|
| renting | seconds | capacity refusals are normal; `--spares` absorbs them |
| waiting for ssh | 1–2 min | a pod RunPod never starts is released here |
| aggregate link | ~1 min | measures `--agg-candidates` cards and picks the one that can feed the rest |
| staging the block | seconds | |
| **fetching the prover** | **~14 min cold, ~4 s cached** | 411 MB per card. The single longest gate. Cached **on the pod**, so a relaunch onto surviving pods skips it |
| GPU gate | 2–5 min | a throwaway proof per card. Nothing here is proved for the chain |
| reachability | ~30 s | card-to-card, not driver-to-card |
| **the live page** | ~2 min after the gates | collector → renderer → publisher start here |
| **waiting for the first tip block** | **0–20+ min, not under your control** | `--fresh-tip` waits for a block that did not exist at launch. Measured 2026-09-28: **21 min**, filled with 20 board blocks. Without `--clock-from-tip` this comes out of the hour |
| the session | `--session` hours | from the first tip block, with `--clock-from-tip` |

⚠ **The wait for the first tip block is the one phase whose length nothing here controls** — it is
Bitcoin's block interval, and a 10-minute mean means 20+ minutes is ordinary. With
`--clock-from-tip` the log says so explicitly before it starts:

```
⏱ the clock has NOT started: the 1.0-hour window begins at the first tip block
⏱ CLOCK STARTS: 968983 is the first tip block — the 1.0-hour window runs from now.
  Waited 20.8 min for it, $14.12 spent getting here
```

The summary then reports `blocks_ok` (everything, board fill included) **and**
`blocks_ok_on_clock` (the hour's own result), plus `spend_before_clock_usd` against
`spend_on_clock_usd`. ⛔ **Quote the on-clock pair for anything about following the tip.**
`blocks_ok` counts gap-filling done before the measurement began.

⚠ **The dashboard chain starts after the gates.** Until then `hazync.org/live` serves the **last
frame of a previous run at HTTP 200** — it looks alive and is showing you something else. Judge by
`Last-Modified`, never by the status code.

---

## The proof chain, and how it breaks

The product is a **chain** of block proofs, so a height that goes by unproved is a missing link, not
a slower result. Two things guard it, and you should know what each one refuses.

**The session proves the lowest unproved tip bundle**, not the newest. On 2026-09-28 blocks 968,984
and 968,985 were mined 5 seconds apart, both bundles appeared together, the old selector took the
newest, and 968,984 has no proof to this day. If more than `--tip-max-behind` bundles queue up the
session jumps to the newest and writes a `skipped` line per abandoned height into `tip_ledger.jsonl`,
logs it, and puts the list in the session summary as `tip_blocks_skipped`.

**Each bundle is checked against the last block proved**, after it is fetched and before any GPU
touches it. The bundle names the block it builds on, and we hold the previous block's header, so
this is a real linkage test rather than an integer comparison:

```
in_tip == witness.header[4:36] == reversed(getblockhash h-1)
```

A refusal reads `refusing to prove <h>: block <h> builds on <x> but the block we proved last hashes
to <y> — this is a GAP or a fork, not the next link`. It also refuses a bundle whose stated height
differs from the one asked for, and one whose `in_tip` disagrees with its own header.

⚠ The **first** tip block of a session has nothing to link to and logs `unlinked`, with the reason.
That is expected; an unverifiable link must never be reported as a verified one.

### Closing a gap

`--fresh-tip` sets the floor at the current tip, so a skipped height sits below it for ever and no
later run can reach back. Use `--tip-from`:

```sh
python3 coordinator/tip_smoke.py --session 0.5 --tip-from 968984 --claim-as "G H O S T" \
  --bridge-host localhost --cards 20 --min-cards 12 ...
```

⛔ **Do not pass `--fresh-tip` on a gap-closing run** — it is overridden, and combining them only
makes the intent unclear. The bridge retains every bundle back to the emit floor (1,489 of them,
oldest 967,500, as of 2026-09-28, and nothing prunes them on that box), so a gap can be closed at
any time.

---

## When it goes wrong

Every row below is something that actually happened on 2026-09-27.

| symptom | cause | what to do |
|---|---|---|
| `only N of M cards passed every pre-clock gate` | survivors below `--min-cards` | lower the floor; check the per-card lines above it for *why* they were dropped |
| a card "never answered ssh" seconds after the others | RunPod accepted the pod and never published a port — it never started | normal; `--spares` covers it. If several go at once, suspect the floor being used as a wait target |
| `released … the aggregate could not push to it fast enough` | the tail cut | expected for genuinely slow links. **Several at once means the tail cut is trimming to the wrong number** |
| `GPU_BAD` | the card cannot prove | the gate doing its job. It caught a card that had *also* crawled the prover fetch |
| the log goes quiet for 10+ minutes in the fetch gate | one slow card; the fleet waits | measure the byte count on the laggard twice before killing anything — one went 78% → 99.8% in 40 s |
| the run dies and the pods stay up | the driver was killed without its `finally` | `--cleanup --rundir <dir>`, or adopt them |
| the live page shows an old run | nothing is publishing this one yet | check `Last-Modified` on `frame.png` |

⛔ **Signals that kill the driver.** `SIGTERM` is handled and reaches the teardown. **`SIGUSR1` is
not** — it terminates the process (`REAL_EXIT=138`). The stack-dump hook is in the *coordinator*,
not here.

⛔ **Never judge a run by the absence of error lines.** A stalled run emits no text at all, so a
monitor that reports only changes is silent through exactly the fault you are watching for. Alert on
**silence**, with the phase attached.

---

## Stopping safely

```sh
kill -TERM <driver pid>        # handled: raises through to the finally, which releases the pods
```

Then **verify**, rather than trusting it:

```sh
python3 coordinator/tip_smoke.py --cleanup --rundir <rundir>   # releases anything left in rented.json
```

⛔ Confirm the account is empty afterwards. `terminate` returning ok is not the same as the pod being
gone — the teardown re-lists and says `account check: clean` only when it is.

---

## After the run

- `rundir/run.log` — the run, and `REAL_EXIT` at the end
- `rundir/billing.json`, and a `BILLED:` line naming the total
- `rundir/measurements.json` — harvested **before** release, which is the only chance
- `docs/history/fleet-economics.jsonl` — one row per fleet, teaching `--gpu-type auto` for next time

⛔ **Harvest before release.** Two tip runs in September were torn down with no log fetch, and the
absence was blamed on "nobody ran it" for ten days.
