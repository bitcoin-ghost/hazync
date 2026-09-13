# The sponsor proving bot

Status: **built and tested against fakes; never run live.** Nothing in this document has been measured on
RunPod yet. `trial` exists to measure it.

`coordinator/sponsor_bot.py` proves paid sponsorships on rented RunPod GPUs and logs what each block cost.
It runs on the coordinator box, next to the coordinator's database. See `docs/SPONSORSHIP.md` for the
sponsorship flow itself.

## What it relies on

The coordinator holds a sponsorship's blocks for the bot while the sponsorship is paid at least its
minimum:

```
status IN ('paid','proving') AND paid_sats IS NOT NULL AND min_sats IS NOT NULL AND paid_sats >= min_sats
```

Normal workers are never offered held blocks. Proofs from anyone are still accepted. The coordinator marks
a sponsorship `proven` at submit when its whole span is covered.

The bot:

- **Works the holds.** Oldest payment first, heights in order, skipping heights that are already covered
  or that a live pod already has. Several pods can split one sponsorship.
- **Moves `paid` to `proving`** when it gives a pod the first of a sponsorship's blocks.
- **Reconciles.** Every loop it marks `proven` any held sponsorship whose span is covered by verified proofs
  from anyone, because blocks can arrive by paths other than submit.
- **Never claims.** `/api/claim` is unsigned, so a privilege keyed on the bot's public key could be
  spoofed. The bot picks blocks from the database and each pod proves them with the released worker's
  explicit mode, `hazync-worker run <n>`, which submits without claiming.

## Money safety

- **Caps.** Live mode refuses to start without `--max-pods`, `--max-usd` (total for the run) and
  `--max-usd-per-hour`.
- **Spend** is each pod's `costPerHr` times its wall time, from creation to confirmed termination. It is an
  estimate of the bill, not RunPod's invoice. How RunPod rounds billing time is NOT MEASURED.
- **Hourly cap.** A pod is only deployed if the live pods' hourly rate plus `--pod-price-ceiling` (default
  $1.00/h) stays within `--max-usd-per-hour`. A pod that comes back dearer than the ceiling is terminated
  at once.
- **Total cap.** No pod is deployed that could reach `--max-usd`, and once spend reaches it every pod is
  terminated and the run exits with code 3.
- **Every pod is terminated** on normal exit, on an exception, on SIGINT or SIGTERM, when it stalls (no
  newly proven assigned block for `--stall-min`, default 45), when its boot fails, when there is no public
  SSH in 15 minutes, when its worker finishes without proving its blocks, when a sponsorship it was proving
  is no longer held, and when there is nothing left for it.
- **Termination is confirmed.** `podTerminate` is repeated until RunPod no longer lists the pod. A run also
  sweeps any pod carrying its own name prefix, in case a deploy's answer was lost. A pod that cannot be
  confirmed gone is printed loudly and the bot exits with code 4: terminate it by hand.
- **`stop-all`** terminates every pod whose name starts with `hz-sponsor-`.

## Pods

The same recipe as the board fleet (`hazync-board-fleet/fleet.sh`):

- **Deploy:** `podFindAndDeployOnDemand` with 1 GPU, an RTX 4090, falling back to an A40 (4090 community
  hosts often never expose public SSH), image `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`,
  port 22, 40 GB disk, named `hz-sponsor-<run>-<n>`.
- **Boot over SSH:**
  - copy the bot's identity
  - download the Latest release's worker, run script and CUDA host
  - check them against the PGP-verified `SHA256SUMS.txt`
  - run a GPU smoke prove (`prove-block`, because a card can pass boot with no usable CUDA device, #261)
  - check the program ID against the coordinator's `/api/meta`
- **Work:** the assigned heights run one after another with `hazync-worker run <n>`. Progress is read from
  the database, not the pod.

## The cost log

- **`sponsor_work`:** one row per assigned block, with the sponsorship (NULL for a trial), height, pod, GPU
  type, price, assigned and proven times, seconds, estimated dollars and outcome (`proven`, `stalled`,
  `failed`, `cancelled`). A block's seconds are the pod time between the previous block landing and this
  one. When several land between two looks, that time is split evenly between them.
- **`sponsor_pods`:** one row per pod, with its price, lifetime, estimated spend and why it was terminated.
  `report` divides all pod spend by the blocks proven, so boot and idle time are counted in the all-in
  figure.

## Setup on the coordinator box

- **Identity:** a directory for the bot, with `key.hex` (mode 600) and `handle`. Make it with
  `HAZYNC_HOME=<dir> hazync-worker id "hazync sponsor"`. The handle must not normalise to a reserved word
  (`hazync` alone is refused). Set `SPONSOR_BOT_HOME` to the directory.
- **SSH:** a key pair for the pods. Set `SPONSOR_BOT_SSH_KEY` to the private key; the `.pub` next to it is
  given to RunPod.
- **RunPod:** the API key in a file, mode 600. Set `RUNPOD_API_KEY_FILE` (default `~/.runpod.key`). The key
  is never printed.
- **gpg** with the maintainer's release key imported (`777F E81F 8CC0 77FD 3D08 055E 852C 2B31 90F5 B928`).
  A manifest without a good signature stops the bot before anything is rented.
- **Optional:** `SPONSOR_BOT_RELEASE` to pin a release tag (default: GitHub Latest), and
  `SPONSOR_BOT_META_URL` (default `http://127.0.0.1:8899/api/meta`).
- **`COORD_DB`:** the coordinator's database, as for the coordinator.

## Commands

```
python3 sponsor_bot.py                    # plan: the queue, blocks left, projected cost; touches nothing
python3 sponsor_bot.py run --live --max-pods 2 --max-usd 20 --max-usd-per-hour 2
python3 sponsor_bot.py trial --blocks 100000,150000-150004 --live --max-pods 1 --max-usd 5 --max-usd-per-hour 1
python3 sponsor_bot.py report             # measured cost per block, by height band
python3 sponsor_bot.py stop-all
```

- **Tuning options:** `--pod-price-ceiling`, `--stall-min`, `--blocks-per-pod` (default 25) and `--tick`
  (seconds between looks, default 30).
- **Without `--live`:** `run` and `trial` print the plan and exit with code 2.
- **`trial`:** proves chosen blocks without a sponsorship, to measure cost before the price ladder is
  trusted. It refuses any block already proven, claimed by a prover within the last hour, or held for a
  sponsorship, and at most 1,000 blocks.

## What is tested, and what is not

`coordinator/test_sponsor_bot.py` runs the bot's real RunPod client against a fake GraphQL server over
HTTP, and a fake pod that proves blocks by writing verified proofs into a real coordinator database, with
time running 3,600 times faster. It checks:

- the caps, the GPU fallback and the price ceiling
- every termination path: stall, failed boot, exception, SIGTERM, `stop-all`
- the queue order, `proving` and `proven`, trial refusals, and the cost log and report

`--control` skips pod cleanup on an error, and must fail.

**Not tested:**

- the real RunPod API
- SSH, the boot script and real proving (`SshRunner`)
- RunPod's billing

**Also:** the bot's pods do not send heartbeats, because explicit runs never do, so the board shows held
blocks as held, not claimed.
