# The sponsor proving bot

Status: **built and tested against fakes; never run live.** Nothing in this document has been measured on
RunPod yet. `trial` exists to measure it.

`coordinator/sponsor_bot.py` proves paid sponsorships on rented RunPod GPUs and logs what each block cost.
It runs on the coordinator box as its own user, **with no access to the coordinator's database** (#351): it
reads and changes the board only through the coordinator's signed, loopback-only bot API, and keeps its cost
log in a database of its own. See `docs/SPONSORSHIP.md` for the sponsorship flow itself.

## The coordinator's bot API (#351)

The bot rents GPUs with real money and runs pod-handling code against a third-party API, so it is the part most
likely to be compromised. It used to write `coordinator.db` directly, which meant a compromised bot could rewrite
the board's record. Now:

- **Routes** (`/api/bot/`, `docs/COORDINATOR_REFERENCE.md`):
  - `GET queue`: sponsorships that hold, oldest payment first, each with `todo` (heights nobody has proven)
  - `GET blocks?h=`: per height, `covered`, `proofs` (key and time), a live `claimed`, `held`
  - `GET sponsorship/<id>`: name, status, whether it holds
  - `POST key`: register a sponsorship's key
  - `POST proving`: `paid` to `proving`
  - `POST reconcile`: mark proven every hold whose span is covered
- **The coordinator decides every change.** A key is registered only for a sponsorship that holds, only with the
  handle its name gives, and never for a second sponsorship; `proving` only while the sponsorship holds. The bot
  never supplies a status or a row.
- **Authentication:** the bot's own ed25519 key (`SPONSOR_BOT_KEY_FILE`, default `bot.key` in
  `SPONSOR_BOT_HOME`), whose public half the coordinator reads from `SPONSOR_BOT_PUBKEY_FILE`. Each request is
  signed over the method, path with query, timestamp, a nonce and the body's hash; the timestamp must be within
  `SPONSOR_BOT_SKEW` (120 s) and a nonce is accepted once.
- **Loopback only:** the routes answer only a direct connection from `127.0.0.1` or `::1` with no
  `X-Forwarded-For`. The public proxies cannot reach them, signed or not.
- **A coordinator restart is ridden out.** A look where the coordinator fails is logged and retried; after
  10 failed looks in a row (`COORD_ERRORS_MAX`) the run stops, terminates every pod and exits with code 6. Spend
  is checked every look regardless, from the bot's own figures, and a pod is terminated even when the
  coordinator cannot say which of its blocks landed (the log is corrected on a later run).

## What it relies on

The coordinator holds a sponsorship's blocks for the bot while the sponsorship is paid at least its
minimum:

```
status IN ('paid','proving') AND paid_sats IS NOT NULL AND min_sats IS NOT NULL AND paid_sats >= min_sats
```

Normal workers are never offered held blocks. A proof of a held block is accepted only from a key the bot registered for that sponsorship (`403` for anyone else). The coordinator marks
a sponsorship `proven` at submit when its whole span is covered.

The bot:

- **Works the holds.** Oldest payment first, heights in order, skipping heights that are already covered
  or that a live pod already has. Several pods can split one sponsorship. The queue is the coordinator's.
- **Moves `paid` to `proving`** when it gives a pod the first of a sponsorship's blocks.
- **Reconciles.** Every loop it marks `proven` any held sponsorship whose span is covered by verified proofs
  from anyone, because blocks can arrive by paths other than submit (`POST /api/bot/reconcile`).
- **Never claims.** The bot picks blocks from the coordinator's queue and each pod proves them with the
  released worker's explicit mode, `hazync-worker run <n>`, which submits without claiming.

## Identities: one key per sponsorship

The bot submits a sponsorship's blocks as **`SPONSOR: <sponsor name>`**, under a key of its own for that
sponsorship. One key per sponsorship, not one for the bot, because the board adds up work by key and shows
the handle a key last submitted with: a single bot key would show every sponsor's blocks under whichever
sponsor was proven last.

- **Where:** `<SPONSOR_BOT_HOME>/identities/<sponsorship id>/`, with `key.hex` (the raw ed25519 seed in
  hex, the worker's format) and `handle`, mode 600 in a 700 directory. Created the first time a
  sponsorship is worked, and reused after that.
- **Trials:** every trial shares one key, `identities/trial/`, with the handle `SPONSOR: Hazync trial`.
- **The handle** is cleaned exactly as the coordinator's `clean_handle` cleans it (printable characters
  only, no `< > & " '`, trimmed), so `O'Brien` is `SPONSOR: OBrien` in the file and on the board. A
  sponsor name is 1 to 40 characters, and a registered sponsor key's handle may be 49, so no name is cut.
- **On a pod:** every identity its blocks need is copied to `/root/.hazync-ids/<id>/`, and each
  `hazync-worker run <n>` runs with `HAZYNC_HOME` pointing at that block's identity. Bundles and witnesses
  are shared (`BUNDLE_DIR`, `WITNESS_DIR` under `/workspace`), not kept per identity.
- **`sponsor_work.pubkey`** records the key each block was proved under.

### Registration, and the reserved prefix

- **`sponsor_keys`** (in the coordinator's schema: `pubkey`, `sponsorship_id`, NULL for the trial key,
  `handle`, `created_at`) lists the bot's keys. The bot registers a key through `POST /api/bot/key` before any
  pod is given it, and refuses to run against a coordinator whose bot API does not accept its key.
- **The coordinator refuses any handle whose folded form starts with `sponsor` from a key that is not in
  `sponsor_keys`**, with the same message as a reserved handle, in `submit`, spine submit, `rotate` (for
  the new key) and `claim`. Folded means NFKC, lowercase, letters and digits only, with `0` read as `o` and
  `5` as `s`, so `SPONSOR :`, `s.p.o.n.s.o.r` and `Sp0nsor` are all caught.
- **Not caught:** look-alike letters from other scripts, such as a Cyrillic `о`.
- **Also refused:** an ordinary handle that happens to start that way, such as `Sponsorship fan`. The
  2026-09-13 production copy had no such handle.
- **Handle length:** ordinary handles keep the 48-character cap; a registered sponsor key may use 49.

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
- **RunPod must accept the bot's requests.** Every RunPod call sends the bot's own User-Agent: RunPod's
  API sits behind Cloudflare, which refuses Python's default `Python-urllib` User-Agent with HTTP 403
  (`error code: 1010`). Without it the first live trial (2026-09-14) could not deploy, list or terminate
  a pod, and logged every refusal as "no GPU capacity". A refused request is now logged with RunPod's
  status and message; after 3 refused deploy requests in a row the bot stops (nothing is running, since
  every deploy was refused) and exits with code 5.
- **A pod that cannot start its work is replaced, not fatal.** Work is launched with
  `setsid -f bash sponsor-run.sh >> sponsor-run.log 2>&1 < /dev/null`, so the ssh that starts it returns at
  once. The first trial backgrounded the whole `cd && ... && setsid nohup ...` list instead, which kept the
  ssh channel open until the proving run ended; the call timed out and the uncaught timeout stopped the bot
  (its cleanup still terminated the pod). A timed-out ssh or scp is now a failed step: the pod is terminated
  with its blocks logged `failed`, and those blocks go to the next pod.
- **Blocks with no bundle wait.** The bot works only blocks the coordinator can serve a bundle or witness for,
  checked the same way as `server.bundle_path`. A held block without one is not queued and no pod is rented for
  it; `plan` and the run's log say how many wait. A trial refuses such a block.
- **`stop-all`** terminates every pod whose name starts with `hz-sponsor-`.

## Pods

The same recipe as the board fleet (`hazync-board-fleet/fleet.sh`):

- **Deploy:** `podFindAndDeployOnDemand` with 1 GPU, an RTX 4090, falling back to an A40 (4090 community
  hosts often never expose public SSH), image `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`,
  port 22, 40 GB disk, named `hz-sponsor-<run>-<n>`.
- **Boot over SSH:**
  - download the Latest release's worker, run script and CUDA host
  - check them against the PGP-verified `SHA256SUMS.txt`
  - run a GPU smoke prove (`prove-block`, because a card can pass boot with no usable CUDA device, #261)
  - check the program ID against the coordinator's `/api/meta`
- **Work:** the assigned heights run one after another with `hazync-worker run <n>`. Progress is read from
  the coordinator's API, not the pod.

## The cost log

The bot's own SQLite database, `SPONSOR_BOT_DB` (default `bot.db` in `SPONSOR_BOT_HOME`). The coordinator never
read these tables, so they left its database with the bot (#351).

- **`sponsor_work`:** one row per assigned block, with the sponsorship (NULL for a trial), height, pod, GPU
  type, price, assigned and proven times, seconds, estimated dollars and outcome (`proven`, `stalled`,
  `failed`, `cancelled`). A block's seconds are the pod time between the previous block landing and this
  one. When several land between two looks, that time is split evenly between them.
- **A block that lands before its pod is stopped is logged `proven`.** Stopping a pod first checks its
  assigned blocks against the proofs. Trial 2's pod proved blocks 90000 and 90001 in the minute before the
  bot stopped on an error, and both were logged `cancelled`. `run`, `trial` and `report` also correct older
  rows. A `cancelled`, `failed` or `stalled` row becomes `proven` when its block was proven under the row's own
  key between its assignment and its pod's termination. Its seconds run from that pod's previous proof to this
  one. A proof that lands after the pod was stopped is left alone.
- **`sponsor_pods`:** one row per pod, with its price, lifetime, estimated spend and why it was terminated.
  `report` divides all pod spend by the blocks proven, so boot and idle time are counted in the all-in
  figure.

## Setup on the coordinator box

The bot runs as its own user, `hazync-sponsor`, which has **no home directory** and **no access to
`coordinator.db`**. Nothing the bot does reads `$HOME`, `~/.ssh` or `~/.gnupg`: everything lives under
`SPONSOR_BOT_HOME`, set to `/var/lib/hazync/sponsor-bot` (owned by `hazync-sponsor`, mode 700).

```
/var/lib/hazync/sponsor-bot/
  identities/<sponsorship id>/key.hex, handle   one per sponsorship, created by the bot
  identities/trial/key.hex, handle              the one trial key
  ssh/id_ed25519, id_ed25519.pub                the pods' SSH key (SPONSOR_BOT_SSH_KEY defaults here)
  known_hosts                                   written by ssh; pods' host keys are not checked
  gnupg/                                        keyring holding the maintainer's release key
  runpod.key                                    the RunPod API key, mode 600 (RUNPOD_API_KEY_FILE defaults here)
  bot.key                                       the bot's API key, mode 600 (`sponsor_bot.py bot-key` creates it)
  bot.db                                        the cost log (SPONSOR_BOT_DB defaults here)
  work/run-*/                                   the release manifest, boot script and work lists per run
```

- **SSH:** every `ssh` and `scp` call passes `-F /dev/null -i <key> -o IdentitiesOnly=yes
  -o UserKnownHostsFile=<SPONSOR_BOT_HOME>/known_hosts -o GlobalKnownHostsFile=/dev/null
  -o StrictHostKeyChecking=no`. Host keys are not checked because fresh pods reuse IPs and ports with new
  host keys.
- **gpg:** `gpg --verify` runs with `GNUPGHOME=<SPONSOR_BOT_HOME>/gnupg` (or `SPONSOR_BOT_GNUPGHOME`).
  Import the maintainer's release key there once:
  `GNUPGHOME=/var/lib/hazync/sponsor-bot/gnupg gpg --import <key>`, fingerprint
  `777F E81F 8CC0 77FD 3D08 055E 852C 2B31 90F5 B928`. A manifest without a good signature stops the bot
  before anything is rented.
- **RunPod:** the API key file is `RUNPOD_API_KEY_FILE`, or `runpod.key` in `SPONSOR_BOT_HOME`. There is no
  fallback to a home directory, and the key is never printed.
- **Optional:** `SPONSOR_BOT_RELEASE` to pin a release tag (default: GitHub Latest), and
  `SPONSOR_BOT_META_URL` (default `http://127.0.0.1:8899/api/meta`).
- **The coordinator:** `SPONSOR_BOT_COORD_URL` (default `http://127.0.0.1:8899`, a direct loopback address;
  never a proxy). On the coordinator's unit, `SPONSOR_BOT_PUBKEY_FILE` names a file holding the output of
  `sponsor_bot.py bot-key`.
- **`HAZYNC_BRIDGE_OUT`** (and `WITNESS_DIR`, if the coordinator sets it): the same values as the
  coordinator's unit, `/var/lib/hazync/bridge_bundles` on the box. Without `HAZYNC_BRIDGE_OUT` the bot sees only
  the legacy witnesses, so it rents nothing for any block above them.
- **Measured on the box, 2026-09-14:** outbound HTTPS to RunPod and GitHub and outbound SSH work, and
  `cryptography` 41.0.7, `ssh`, `ssh-keygen` and `gpg` are installed.

## Commands

```
python3 sponsor_bot.py                    # plan: the queue, blocks left, projected cost; touches nothing
python3 sponsor_bot.py run --live --max-pods 2 --max-usd 20 --max-usd-per-hour 2
python3 sponsor_bot.py trial --blocks 100000,150000-150004 --live --max-pods 1 --max-usd 5 --max-usd-per-hour 1
python3 sponsor_bot.py report             # measured cost per block, by height band
python3 sponsor_bot.py stop-all
python3 sponsor_bot.py bot-key            # create bot.key if missing; print its public half for the coordinator
python3 sponsor_bot.py import-log <db>    # copy sponsor_work and sponsor_pods from a copy of coordinator.db
```

- **Tuning options:** `--pod-price-ceiling`, `--stall-min`, `--blocks-per-pod` (default 25) and `--tick`
  (seconds between looks, default 30).
- **Without `--live`:** `run` and `trial` print the plan and exit with code 2.
- **`trial`:** proves chosen blocks without a sponsorship, to measure cost before the price ladder is
  trusted. It refuses any block already proven, without a bundle, claimed by a prover within the last hour,
  or held for a sponsorship, and at most 1,000 blocks.

## Moving an existing box to the API (#351)

1. Deploy the coordinator with the bot API. It does nothing until `SPONSOR_BOT_PUBKEY_FILE` is set.
2. As `hazync-sponsor`: `sponsor_bot.py bot-key`. Put the printed public key in a root-owned file, for example
   `/etc/hazync/sponsor-bot.pub`, set `SPONSOR_BOT_PUBKEY_FILE` to it on the coordinator's unit, and restart it.
3. As root, copy the old log out: `sqlite3 /var/lib/hazync/coordinator.db ".backup /tmp/coord-copy.db"`, give
   the copy to `hazync-sponsor`, and run `sponsor_bot.py import-log /tmp/coord-copy.db` as that user. It copies
   each row once; run it again and it copies nothing. Delete the copy.
4. `sponsor_bot.py` (plan) as `hazync-sponsor` must list the queue. That is the check that the key, the loopback
   address and the coordinator's file agree.
5. Take the ledger out of the bot's reach: `chmod 640` on `coordinator.db` and its `-wal`/`-shm`, plus
   `UMask=0027` on the coordinator's unit — SQLite recreates those files on every checkpoint with the process
   umask, so the chmod alone does not hold. Both drop-ins are in `coordinator/deploy/dropins/`. Writers are
   unaffected (the coordinator and litestream run as `hazync`; the backup and integrity timers run as root).

Done on server 1 on 2026-09-17: the bot's plan reads the queue over the API, and
`sudo -u hazync-sponsor sqlite3 /var/lib/hazync/coordinator.db` is refused "unable to open database file".

## What is tested, and what is not

`coordinator/test_sponsor_bot.py` runs the bot's real RunPod client against a fake GraphQL server over
HTTP, the real coordinator request handler over HTTP for the bot API, and a fake pod that proves blocks by
writing verified proofs into a real coordinator database, with time running 3,600 times faster. It checks:

- the caps, the GPU fallback and the price ceiling
- every termination path: stall, failed boot, exception, SIGTERM, `stop-all`
- the queue order, `proving` and `proven`, trial refusals, and the cost log and report
- blocks with no bundle: never given to a pod, and a queue of only such blocks rents nothing
- a block that lands before its pod is stopped, and the correction of older rows
- the bot and the coordinator sign the same bytes, and `sponsor_bot.py` opens no coordinator table
- a coordinator restart mid-run is ridden out; a coordinator that stays away stops the run with every pod terminated
- `import-log` copies the old log once, and a coordinator that refuses the bot's key rents nothing

`coordinator/test_sponsor_bot_api.py` attacks the API itself: unsigned, wrong-key, tampered body, path and query,
replayed, stale, proxied and non-loopback requests, no key configured, and every transition the coordinator
must refuse. Its `--control` removes authentication and the checks, and must fail.

`--control` skips pod cleanup on an error, ignores proofs that land before a pod is stopped, and counts every
block as having a bundle. It must fail.

**Not tested:**

- the real RunPod API
- SSH, the boot script and real proving (`SshRunner`)
- RunPod's billing

**Also:** the bot's pods do not send heartbeats, because explicit runs never do, so the board shows held
blocks as held, not claimed.
