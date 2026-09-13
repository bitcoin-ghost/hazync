#!/usr/bin/env python3
"""Sponsor proving bot: proves paid sponsorships on rented RunPod GPUs, and measures what that costs.

It runs on the coordinator box, next to the coordinator's database, and:
  * works the sponsorships that HOLD their blocks (paid at least the minimum, status `paid` or
    `proving`), oldest payment first, and moves each from `paid` to `proving` when it starts on it;
  * rents one-GPU pods the way the board fleet does (hazync-board-fleet/fleet.sh), boots each against the
    signed release, and has it prove its assigned heights with the released worker's explicit mode,
    `hazync-worker run <n>`, under the bot's own key. It never uses /api/claim: claims are unsigned, so a
    privilege keyed on the bot's public key could be spoofed by anyone;
  * marks a sponsorship `proven` once every block of its span is covered by verified proofs from anyone
    (the coordinator does the same at submit; this catches blocks that arrived another way);
  * logs every assigned block in `sponsor_work` and every pod in `sponsor_pods`, so `report` gives a
    MEASURED cost per block, with boot and idle time counted in the all-in figure.

Money is the dangerous part. Live mode refuses to start without --max-pods, --max-usd and
--max-usd-per-hour, and every pod is terminated on normal exit, on an exception, on SIGINT/SIGTERM, when
it stalls, when its boot fails and when there is nothing left for it. See docs/SPONSOR_BOT.md.

  python3 sponsor_bot.py                    # plan: what it would do; touches nothing
  python3 sponsor_bot.py run --live --max-pods 2 --max-usd 20 --max-usd-per-hour 2
  python3 sponsor_bot.py trial --blocks 100000,150000-150004 --live --max-pods 1 --max-usd 5 --max-usd-per-hour 1
  python3 sponsor_bot.py report             # measured cost per block, by height band
  python3 sponsor_bot.py stop-all           # terminate every hz-sponsor-* pod
"""
import argparse
import json
import math
import os
import re
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

DB = os.environ.get("COORD_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator.db"))

# The sponsorships whose blocks are HELD for the bot. This is the coordinator's hold rule: paid means paid
# AT LEAST THE MINIMUM, the same test that decides whether a sponsor's name is public (SPONSOR_PUBLIC_SQL),
# so an `underpaid` row, or a `paid` row below its minimum, is never proven on the sponsorship budget.
HELD_SQL = ("status IN ('paid','proving') AND paid_sats IS NOT NULL AND min_sats IS NOT NULL"
            " AND paid_sats >= min_sats")
CLAIM_TTL = int(os.environ.get("CLAIM_TTL", "3600"))   # a claim taken or beaten this recently is live
POD_PREFIX = "hz-sponsor-"
GRAPHQL_URL = "https://api.runpod.io/graphql"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
# 4090 community hosts often never expose public SSH; A40s always did (board fleet, 2026-09-11).
GPU_TYPES = ("NVIDIA GeForce RTX 4090", "NVIDIA A40")
TRIAL_MAX = 1000
# `report` bands: the sponsorship price ladder's height ranges, and everything above it.
BANDS = ((1, 100000), (100001, 150000), (150001, 180000), (180001, 200000), (200001, 230000), (230001, 10 ** 9))

# test_sponsor_bot.py --control sets this to show its cleanup test can fail. Never set it otherwise.
_CONTROL_SKIP_CLEANUP_ON_ERROR = False


# ---------- the database ----------

def connect(db_path=None, readonly=False):
    path = db_path or DB
    c = (sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) if readonly
         else sqlite3.connect(path, timeout=30))
    c.row_factory = sqlite3.Row
    return c


def ensure_tables(c):
    c.executescript("""
      CREATE TABLE IF NOT EXISTS sponsor_work(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sponsorship_id INTEGER,                 -- NULL for a trial block
        height INTEGER NOT NULL, pod_id TEXT, gpu_type TEXT, cost_per_hr REAL,
        assigned_at REAL, proven_at REAL, seconds REAL, usd_estimate REAL,
        outcome TEXT);                          -- NULL while in flight; proven | stalled | failed | cancelled
      CREATE INDEX IF NOT EXISTS sponsor_work_height ON sponsor_work(height);
      CREATE TABLE IF NOT EXISTS sponsor_pods(
        pod_id TEXT PRIMARY KEY, name TEXT, gpu_type TEXT, cost_per_hr REAL,
        created_at REAL, terminated_at REAL, usd_estimate REAL, note TEXT);
    """)
    c.commit()


def queue(db_path=DB):
    """Sponsorships that hold their blocks, oldest payment first. Read-only; no table means none."""
    c = connect(db_path, readonly=True)
    try:
        return [dict(r) for r in c.execute(
            "SELECT id,lo,hi,name,status,paid_at,paid_sats,min_sats FROM sponsorships WHERE "
            + HELD_SQL + " ORDER BY paid_at ASC, id ASC")]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def plan(rows):
    return [f"sponsorship #{r['id']}: would prove blocks {r['lo']} to {r['hi']} "
            f"({r['hi'] - r['lo'] + 1} blocks) for {r['name']}" for r in rows]


def is_covered(c, h):
    return c.execute("SELECT 1 FROM vranges WHERE lo<=? AND hi>=? LIMIT 1", (h, h)).fetchone() is not None


def covered_heights(c, lo, hi):
    """Heights in lo..hi covered by the union of verified proofs, whoever made them."""
    got = set()
    for r in c.execute("SELECT lo, hi FROM vranges WHERE lo<=? AND hi>=?", (hi, lo)):
        got.update(range(max(lo, r["lo"]), min(hi, r["hi"]) + 1))
    return got


def held_rows(c):
    try:
        return c.execute("SELECT id,lo,hi,name,status,paid_at FROM sponsorships WHERE " + HELD_SQL
                         + " ORDER BY paid_at ASC, id ASC").fetchall()
    except sqlite3.OperationalError:
        return []


def is_held(c, sid):
    try:
        return c.execute("SELECT 1 FROM sponsorships WHERE id=? AND " + HELD_SQL, (sid,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def reconcile(c, now=None):
    """Move every held sponsorship whose whole span is covered to `proven`. Returns their ids."""
    now = time.time() if now is None else now
    done = []
    for r in held_rows(c):
        if len(covered_heights(c, r["lo"], r["hi"])) == r["hi"] - r["lo"] + 1:
            cur = c.execute("UPDATE sponsorships SET status='proven', proven_at=? WHERE id=? AND " + HELD_SQL,
                            (now, r["id"]))
            if cur.rowcount:
                done.append(r["id"])
    c.commit()
    return done


def pending_work(c, busy=(), trial=None):
    """[(sponsorship id, or None for a trial, height)] still to prove, in the order to prove them:
    held sponsorships oldest payment first, heights in order, skipping covered and busy heights."""
    busy = set(busy)
    if trial is not None:
        return [(None, h) for h in trial if h not in busy and not is_covered(c, h)]
    out, seen = [], set()
    for r in held_rows(c):
        cov = covered_heights(c, r["lo"], r["hi"])
        for h in range(r["lo"], r["hi"] + 1):
            if h not in cov and h not in busy and h not in seen:
                seen.add(h)
                out.append((r["id"], h))
    return out


def parse_blocks(spec):
    """'100000,150000-150004' -> [100000, 150000, ..., 150004]; at most TRIAL_MAX blocks."""
    heights = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"not a block or a range of blocks: {part!r}")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if lo < 1 or hi < lo:
            raise ValueError(f"write a range lowest first, from block 1: {part!r}")
        if hi - lo + 1 > TRIAL_MAX:
            raise ValueError(f"a trial proves at most {TRIAL_MAX} blocks: {part!r}")
        heights.extend(range(lo, hi + 1))
    seen = set()
    out = [h for h in heights if not (h in seen or seen.add(h))]
    if not out:
        raise ValueError("no blocks given")
    if len(out) > TRIAL_MAX:
        raise ValueError(f"a trial proves at most {TRIAL_MAX} blocks ({len(out)} given)")
    return out


def trial_refusals(c, heights, now=None):
    """Why each trial block may not be proven by the bot: already proven, claimed right now, or held."""
    now = time.time() if now is None else now
    out = []
    for h in heights:
        if is_covered(c, h):
            out.append(f"block {h} is already proven")
            continue
        try:
            claimed = c.execute("SELECT 1 FROM ranges WHERE status='claimed' AND lo<=? AND hi>=?"
                                " AND COALESCE(last_beat, claimed_at) > ? LIMIT 1",
                                (h, h, now - CLAIM_TTL)).fetchone()
        except sqlite3.OperationalError:
            claimed = None
        if claimed:
            out.append(f"block {h} is claimed by a prover right now")
            continue
        try:
            held = c.execute("SELECT id FROM sponsorships WHERE lo<=? AND hi>=? AND " + HELD_SQL + " LIMIT 1",
                             (h, h)).fetchone()
        except sqlite3.OperationalError:
            held = None
        if held:
            out.append(f"block {h} is held for sponsorship #{held['id']}")
    return out


# ---------- RunPod ----------

class RunPodError(RuntimeError):
    pass


class RunPod:
    """The RunPod GraphQL calls the board fleet already uses: deploy, list, terminate."""

    def __init__(self, key, url=GRAPHQL_URL, timeout=60):
        self.key, self.url, self.timeout = key, url, timeout

    @classmethod
    def from_env(cls):
        path = os.path.expanduser(os.environ.get("RUNPOD_API_KEY_FILE", "~/.runpod.key"))
        if not os.path.isfile(path):
            raise SystemExit(f"sponsor_bot: no RunPod API key file at {path} (set RUNPOD_API_KEY_FILE)")
        with open(path) as f:
            key = f.read().strip()
        if not key:
            raise SystemExit(f"sponsor_bot: the RunPod API key file {path} is empty")
        return cls(key)

    @staticmethod
    def _s(v):
        return json.dumps(str(v))            # a JSON string literal is a valid GraphQL string literal

    def _gql(self, query):
        req = urllib.request.Request(self.url, data=json.dumps({"query": query}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.load(r)
        except Exception as e:
            raise RunPodError(f"{type(e).__name__}: {e}") from e
        if d.get("errors") and not d.get("data"):
            raise RunPodError(str(d["errors"])[:300])
        return d.get("data") or {}

    def deploy(self, name, ssh_pubkey, gpu_types=GPU_TYPES):
        """One on-demand 1-GPU pod, trying each GPU type in turn. None when none has capacity."""
        for gt in gpu_types:
            q = ("mutation { podFindAndDeployOnDemand(input: { cloudType: ALL, gpuCount: 1, volumeInGb: 0, "
                 f"containerDiskInGb: 40, gpuTypeId: {self._s(gt)}, name: {self._s(name)}, "
                 f"imageName: {self._s(IMAGE)}, ports: \"22/tcp\", "
                 f"env: [{{key: \"PUBLIC_KEY\", value: {self._s(ssh_pubkey)}}}] }}) {{ id costPerHr }} }}")
            try:
                p = self._gql(q).get("podFindAndDeployOnDemand")
            except RunPodError:
                p = None
            if p and p.get("id"):
                return {"id": p["id"], "name": name, "gpu_type": gt, "cost_per_hr": float(p.get("costPerHr") or 0)}
        return None

    def pods(self):
        q = ("query { myself { pods { id name costPerHr machine { gpuDisplayName } "
             "runtime { ports { ip isIpPublic privatePort publicPort } } } } }")
        out = []
        for p in ((self._gql(q).get("myself") or {}).get("pods") or []):
            ports = ((p.get("runtime") or {}).get("ports")) or []
            ssh = [x for x in ports if x.get("privatePort") == 22 and x.get("isIpPublic")]
            out.append({"id": p["id"], "name": p.get("name") or "", "cost_per_hr": float(p.get("costPerHr") or 0),
                        "ssh": (ssh[0]["ip"], int(ssh[0]["publicPort"])) if ssh else None})
        return out

    def terminate(self, pod_id):
        self._gql(f"mutation {{ podTerminate(input: {{podId: {self._s(pod_id)}}}) }}")


def terminate_confirmed(api, pod_id, sleep=time.sleep, tries=6, wait_s=5.0):
    """podTerminate until RunPod no longer lists the pod. False if it still does after every try."""
    for _ in range(tries):
        try:
            api.terminate(pod_id)
        except Exception:
            pass
        try:
            if pod_id not in {p["id"] for p in api.pods()}:
                return True
        except Exception:
            pass
        sleep(wait_s)
    return False


def stop_all(api, prefix=POD_PREFIX, sleep=time.sleep, wait_s=5.0):
    """Terminate every pod whose name starts with `prefix`. Returns (terminated ids, unconfirmed ids)."""
    done, unconfirmed = [], []
    for p in api.pods():
        if p["name"].startswith(prefix):
            (done if terminate_confirmed(api, p["id"], sleep, wait_s=wait_s) else unconfirmed).append(p["id"])
    return done, unconfirmed


# ---------- a pod over SSH ----------

BOOT_SH = r"""#!/bin/bash
# Written by sponsor_bot.py: the same steps as hazync-board-fleet/fleet.sh.
W=/workspace; mkdir -p $W /root/.hazync; cd $W || exit 1
nvidia-smi --query-gpu=name --format=csv,noheader | grep -q GeForce && for d in /usr/local/cuda*/compat; do [ -d "$d" ] && mv "$d" "${d}.disabled"; done
ldconfig 2>/dev/null
python3 -c "import cryptography" 2>/dev/null || pip install -q cryptography >/dev/null 2>&1
for f in hazync-worker hazync-run-workers.sh hazync-host-x86_64-linux-gnu-cuda; do
  for t in 1 2 3; do [ -s $f ] && break; curl -fsSL -o $f.tmp https://github.com/bitcoin-ghost/hazync/releases/download/@REL@/$f && mv $f.tmp $f; done
done
chmod +x hazync-worker hazync-run-workers.sh hazync-host-x86_64-linux-gnu-cuda
sha256sum -c --quiet want.txt && echo SHA_OK || echo SHA_BAD
ln -sf hazync-host-x86_64-linux-gnu-cuda hazync-host-cuda
# #261: a card can pass boot with no usable CUDA device. A real, tiny GPU prove must succeed first.
timeout 300 ./hazync-host-cuda prove-block > gpu_smoke.log 2>&1 && grep -q VERIFIED gpu_smoke.log && echo GPU_OK || echo "GPU_BAD $(tail -1 gpu_smoke.log | cut -c1-120)"
echo "MID=$(./hazync-host-cuda method-id 2>&1 | grep -oE '[0-9a-f]{64}' | head -1)"
"""


class SshRunner:
    """Boots a pod against the signed release and runs assigned heights on it with `hazync-worker run <n>`."""

    FILES = ("hazync-worker", "hazync-run-workers.sh", "hazync-host-x86_64-linux-gnu-cuda")

    def __init__(self, ssh_key, bot_home, meta_url, release=None, workdir=None):
        self.key, self.home, self.meta_url, self.release = ssh_key, bot_home, meta_url, release
        self.dir = workdir or tempfile.mkdtemp(prefix="sponsor_bot_")
        self.method_id = None
        self.ssh_pubkey = None

    @classmethod
    def from_env(cls):
        key, home = os.environ.get("SPONSOR_BOT_SSH_KEY"), os.environ.get("SPONSOR_BOT_HOME")
        if not key or not home:
            raise SystemExit("sponsor_bot: set SPONSOR_BOT_SSH_KEY (private key path) and SPONSOR_BOT_HOME "
                             "(the bot's key.hex and handle)")
        return cls(os.path.expanduser(key), os.path.expanduser(home),
                   os.environ.get("SPONSOR_BOT_META_URL", "http://127.0.0.1:8899/api/meta"),
                   os.environ.get("SPONSOR_BOT_RELEASE") or None)

    def prepare(self):
        """Everything checked locally before a cent is spent: identity, SSH key, program ID, signed manifest."""
        for f in ("key.hex", "handle"):
            if not os.path.isfile(os.path.join(self.home, f)):
                raise SystemExit(f"sponsor_bot: no {f} in {self.home} (the bot's identity)")
        if not os.path.isfile(self.key) or not os.path.isfile(self.key + ".pub"):
            raise SystemExit(f"sponsor_bot: the SSH key {self.key} or its .pub is missing")
        with open(self.key + ".pub") as f:
            self.ssh_pubkey = f.read().strip()
        with urllib.request.urlopen(self.meta_url, timeout=30) as r:
            mid = str(json.load(r).get("method_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", mid):
            raise SystemExit(f"sponsor_bot: {self.meta_url} gave no program ID")
        self.method_id = mid
        tag = self.release
        if not tag:
            req = urllib.request.Request("https://api.github.com/repos/bitcoin-ghost/hazync/releases/latest",
                                         headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                tag = json.load(r).get("tag_name", "")
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag or ""):
            raise SystemExit(f"sponsor_bot: could not resolve a release tag (got {tag!r})")
        self.release = tag
        base = f"https://github.com/bitcoin-ghost/hazync/releases/download/{tag}/"
        for f in ("SHA256SUMS.txt", "SHA256SUMS.txt.asc"):
            urllib.request.urlretrieve(base + f, os.path.join(self.dir, f))
        v = subprocess.run(["gpg", "--verify", "SHA256SUMS.txt.asc", "SHA256SUMS.txt"], cwd=self.dir,
                           capture_output=True, text=True)
        if "Good signature" not in (v.stdout + v.stderr):
            raise SystemExit("sponsor_bot: the release manifest's signature is not good; nothing was started")
        with open(os.path.join(self.dir, "SHA256SUMS.txt")) as f:
            want = [line for line in f if line.split() and line.split()[-1] in self.FILES]
        if len(want) != len(self.FILES):
            raise SystemExit(f"sponsor_bot: the {tag} manifest does not list all of {', '.join(self.FILES)}")
        with open(os.path.join(self.dir, "want.txt"), "w") as f:
            f.write("".join(want))
        with open(os.path.join(self.dir, "boot.sh"), "w") as f:
            f.write(BOOT_SH.replace("@REL@", tag))
        return self

    def _opts(self):
        return ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", "-i", self.key]

    def _ssh(self, pod, cmd, timeout=60):
        ip, port = pod.ssh
        return subprocess.run(["ssh", "-n", *self._opts(), "-p", str(port), f"root@{ip}", cmd],
                              capture_output=True, text=True, timeout=timeout)

    def _scp(self, pod, paths, dest):
        ip, port = pod.ssh
        return subprocess.run(["scp", "-q", *self._opts(), "-P", str(port), *paths, f"root@{ip}:{dest}"],
                              capture_output=True, text=True, timeout=120)

    def boot(self, pod):
        detail = "not attempted"
        for _ in range(3):
            try:
                ok = (self._ssh(pod, "mkdir -p /workspace /root/.hazync && chmod 700 /root/.hazync").returncode == 0
                      and self._scp(pod, [os.path.join(self.dir, "boot.sh"), os.path.join(self.dir, "want.txt")],
                                    "/workspace/").returncode == 0
                      and self._scp(pod, [os.path.join(self.home, "key.hex"), os.path.join(self.home, "handle")],
                                    "/root/.hazync/").returncode == 0)
                if not ok:
                    detail = "ssh or scp failed"
                else:
                    out = self._ssh(pod, "chmod 600 /root/.hazync/key.hex; bash /workspace/boot.sh", 900).stdout
                    if "SHA_OK" in out and "GPU_OK" in out and f"MID={self.method_id}" in out:
                        return True, "SHA_OK GPU_OK MID ok"
                    detail = " ".join(out.split())[-200:] or "boot printed nothing"
            except subprocess.TimeoutExpired:
                detail = "boot timed out"
            time.sleep(10)
        return False, detail

    def start(self, pod, heights):
        hs = " ".join(str(int(h)) for h in heights)
        cmd = ("cd /workspace && : > sponsor-run.log && setsid nohup bash -c 'for n in " + hs + "; do "
               "HAZYNC_HOST=/workspace/hazync-host-cuda ./hazync-worker run $n; echo \"DONE $n rc=$?\"; done; "
               "echo ALLDONE' >> /workspace/sponsor-run.log 2>&1 < /dev/null & disown; exit 0")
        r = self._ssh(pod, cmd)
        if r.returncode != 0:
            raise RuntimeError(f"could not start work on {pod.name}: {r.stderr.strip()[:200]}")

    def status(self, pod):
        try:
            r = self._ssh(pod, "tail -n 1 /workspace/sponsor-run.log 2>/dev/null", 30)
        except subprocess.TimeoutExpired:
            return "unreachable"
        if r.returncode != 0:
            return "unreachable"
        return "finished" if r.stdout.strip() == "ALLDONE" else "running"


# ---------- the bot ----------

class BotRefused(RuntimeError):
    pass


class Pod:
    def __init__(self, info, created):
        self.id, self.name = info["id"], info["name"]
        self.gpu_type = info.get("gpu_type") or ""
        self.cost_per_hr = float(info.get("cost_per_hr") or 0)
        self.created, self.terminated = created, None
        self.state = "waiting_ssh"           # waiting_ssh | booting | idle | working | terminated
        self.ssh = None
        self.boot_result = None
        self.assigned = []                   # [{"sid", "height", "row"}] not yet proven
        self.segment_start = self.last_progress = self.finished_since = None

    @property
    def live(self):
        return self.terminated is None

    def rate(self, ceiling):
        return self.cost_per_hr if self.cost_per_hr > 0 else ceiling     # an unknown price counts as the ceiling

    def spend(self, now, ceiling):
        end = self.terminated if self.terminated is not None else now
        return self.rate(ceiling) * max(0.0, end - self.created) / 3600.0


class Bot:
    def __init__(self, db_path, api, runner, *, max_pods, max_usd, max_usd_per_hour, pod_price_ceiling=1.0,
                 stall_s=45 * 60, ssh_timeout_s=15 * 60, boot_timeout_s=30 * 60, fail_grace_s=300,
                 blocks_per_pod=25, sleep_s=30, budget_lead_s=180, capacity_backoff_s=300, confirm_wait_s=5.0,
                 trial=None, clock=time.time, sleep=time.sleep, log=None):
        for flag, v in (("--max-pods", max_pods), ("--max-usd", max_usd), ("--max-usd-per-hour", max_usd_per_hour)):
            if v is None or v <= 0:
                raise BotRefused(f"live mode needs a positive {flag}")
        if pod_price_ceiling <= 0 or pod_price_ceiling > max_usd_per_hour:
            raise BotRefused("--pod-price-ceiling must be positive and no more than --max-usd-per-hour")
        self.db_path, self.api, self.runner = db_path, api, runner
        self.max_pods, self.max_usd, self.max_usd_per_hour = int(max_pods), float(max_usd), float(max_usd_per_hour)
        self.ceiling = float(pod_price_ceiling)
        self.stall_s, self.ssh_timeout_s, self.boot_timeout_s = stall_s, ssh_timeout_s, boot_timeout_s
        self.fail_grace_s, self.blocks_per_pod, self.sleep_s = fail_grace_s, max(1, int(blocks_per_pod)), sleep_s
        self.budget_lead_s, self.capacity_backoff_s, self.confirm_wait_s = budget_lead_s, capacity_backoff_s, confirm_wait_s
        self.trial = list(trial) if trial is not None else None
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {m}", flush=True))
        self.pods = []
        self.stop_reason = None
        self.unconfirmed = []
        self.prefix = f"{POD_PREFIX}{int(time.time())}-"   # wall time, not self.clock: unique per run
        self._n = 0
        self._capacity_at = 0.0

    # --- money ---
    def _live(self):
        return [p for p in self.pods if p.live]

    def hourly(self):
        return sum(p.rate(self.ceiling) for p in self._live())

    def spend(self, now=None):
        now = self.clock() if now is None else now
        return sum(p.spend(now, self.ceiling) for p in self.pods)

    def _over_budget(self, now):
        return self.spend(now) + self.hourly() * self.budget_lead_s / 3600.0 >= self.max_usd

    # --- the loop ---
    def run(self):
        c = connect(self.db_path)
        ensure_tables(c)
        if self.trial is not None:
            bad = trial_refusals(c, self.trial, self.clock())
            if bad:
                c.close()
                more = f" (and {len(bad) - 10} more)" if len(bad) > 10 else ""
                raise BotRefused("; ".join(bad[:10]) + more)
        failed = True
        try:
            while True:
                now = self.clock()
                if self.trial is None:
                    for sid in reconcile(c, now):
                        self.log(f"sponsorship #{sid} is proven")
                self._observe(c, now)
                if self._over_budget(now):
                    self.stop_reason = "budget"
                    self.log(f"spend ${self.spend(now):.2f} has reached the ${self.max_usd:.2f} cap: stopping")
                    break
                self._assign(c, now)
                self._launch(c, now)
                if not self._live() and not pending_work(c, (), self.trial):
                    self.stop_reason = self.stop_reason or "done"
                    break
                self.sleep(self.sleep_s)
            failed = False
        finally:
            if not (failed and _CONTROL_SKIP_CLEANUP_ON_ERROR):
                saved = self._ignore_signals()
                try:
                    self.terminate_all(c, "the bot stopped on an error or a signal" if failed
                                       else f"the bot finished ({self.stop_reason})")
                finally:
                    self._restore_signals(saved)
            c.close()
        return self.stop_reason

    def _observe(self, c, now):
        live = self._live()
        waiting = [p for p in live if p.state == "waiting_ssh"]
        if waiting:
            try:
                seen = {p["id"]: p for p in self.api.pods()}
            except Exception as e:
                self.log(f"could not list pods: {e}")
                seen = {}
            for p in waiting:
                info = seen.get(p.id)
                if info and info.get("ssh"):
                    p.ssh, p.state = info["ssh"], "booting"
                    threading.Thread(target=self._boot, args=(p,), daemon=True).start()
                elif now - p.created > self.ssh_timeout_s:
                    self._terminate(c, p, "no public SSH in time")
        for p in [p for p in live if p.live and p.state == "booting"]:
            if p.boot_result is not None:
                ok, detail = p.boot_result
                if ok:
                    p.state = "idle"
                    self.log(f"{p.name} is ready ({p.gpu_type}, ${p.rate(self.ceiling):.2f}/h)")
                else:
                    self._terminate(c, p, f"boot failed: {detail}")
            elif now - p.created > self.boot_timeout_s:
                self._terminate(c, p, "boot timed out")
        for p in [p for p in live if p.live and p.state == "working"]:
            self._progress(c, p, now)

    def _boot(self, pod):
        try:
            pod.boot_result = tuple(self.runner.boot(pod))
        except Exception as e:
            pod.boot_result = (False, f"{type(e).__name__}: {e}")

    def _progress(self, c, p, now):
        done = [a for a in p.assigned if is_covered(c, a["height"])]
        if done:
            # Blocks run one after another on a pod, and several can land between two looks: the time
            # since the last one landed is split evenly between them.
            each = max(0.0, now - p.segment_start) / len(done)
            for a in done:
                c.execute("UPDATE sponsor_work SET proven_at=?, seconds=?, usd_estimate=?, outcome='proven'"
                          " WHERE id=? AND outcome IS NULL", (now, each, each * p.rate(self.ceiling) / 3600.0, a["row"]))
            c.commit()
            p.assigned = [a for a in p.assigned if a not in done]
            p.segment_start = p.last_progress = now
        if any(a["sid"] is not None and not is_held(c, a["sid"]) for a in p.assigned):
            self._terminate(c, p, "a sponsorship it was proving is no longer held")
            return
        if not p.assigned:
            p.state, p.finished_since = "idle", None
            return
        if now - p.last_progress > self.stall_s:
            self._terminate(c, p, f"stalled: no assigned block proven in {int((now - p.last_progress) / 60)} min",
                            outcome="stalled")
            return
        try:
            st = self.runner.status(p)
        except Exception:
            st = "unreachable"
        if st == "finished":
            p.finished_since = p.finished_since or now
            if now - p.finished_since > self.fail_grace_s:
                self._terminate(c, p, "its worker finished without proving every assigned block", outcome="failed")
        else:
            p.finished_since = None

    def _busy(self):
        return {a["height"] for p in self._live() for a in p.assigned}

    def _assign(self, c, now):
        for p in [p for p in self._live() if p.state == "idle"]:
            todo = pending_work(c, self._busy(), self.trial)
            if not todo:
                self._terminate(c, p, "nothing left to prove")
                continue
            chunk = todo[:self.blocks_per_pod]
            items = []
            for sid, h in chunk:
                cur = c.execute("INSERT INTO sponsor_work(sponsorship_id, height, pod_id, gpu_type, cost_per_hr, assigned_at)"
                                " VALUES(?,?,?,?,?,?)", (sid, h, p.id, p.gpu_type, p.rate(self.ceiling), now))
                items.append({"sid": sid, "height": h, "row": cur.lastrowid})
            for sid in sorted({sid for sid, _ in chunk if sid is not None}):
                c.execute("UPDATE sponsorships SET status='proving' WHERE id=? AND status='paid'", (sid,))
            c.commit()
            p.assigned, p.state = items, "working"
            p.segment_start = p.last_progress = now
            p.finished_since = None
            self.runner.start(p, [h for _, h in chunk])
            self.log(f"{p.name}: proving {len(chunk)} block(s) from {chunk[0][1]}")

    def _launch(self, c, now):
        waiting = [p for p in self._live() if p.state in ("waiting_ssh", "booting", "idle")]
        todo = pending_work(c, self._busy(), self.trial)
        want = math.ceil(len(todo) / self.blocks_per_pod) - len(waiting)
        while want > 0 and now >= self._capacity_at:
            if len(self._live()) >= self.max_pods:
                break
            if self.hourly() + self.ceiling > self.max_usd_per_hour + 1e-9:
                break
            if self.spend(now) + (self.hourly() + self.ceiling) * self.budget_lead_s / 3600.0 >= self.max_usd:
                break
            self._n += 1
            info = self.api.deploy(f"{self.prefix}{self._n}", self.runner.ssh_pubkey)
            if not info:
                self._capacity_at = now + self.capacity_backoff_s
                self.log("no GPU capacity on RunPod; trying again later")
                break
            p = Pod(info, self.clock())
            self.pods.append(p)
            c.execute("INSERT OR REPLACE INTO sponsor_pods(pod_id, name, gpu_type, cost_per_hr, created_at)"
                      " VALUES(?,?,?,?,?)", (p.id, p.name, p.gpu_type, p.cost_per_hr, p.created))
            c.commit()
            self.log(f"deployed {p.name} ({p.gpu_type}, ${p.cost_per_hr:.2f}/h)")
            if p.cost_per_hr > self.ceiling:
                self._terminate(c, p, f"its price, ${p.cost_per_hr:.2f}/h, is over the ${self.ceiling:.2f}/h ceiling")
                self._capacity_at = now + self.capacity_backoff_s
                break
            want -= 1

    # --- stopping pods ---
    def _terminate(self, c, p, note, outcome="cancelled"):
        if not p.live:
            return
        if p.assigned:
            for a in p.assigned:
                c.execute("UPDATE sponsor_work SET outcome=? WHERE id=? AND outcome IS NULL", (outcome, a["row"]))
            c.commit()
            p.assigned = []
        confirmed = terminate_confirmed(self.api, p.id, self.sleep, wait_s=self.confirm_wait_s)
        p.terminated, p.state = self.clock(), "terminated"
        if not confirmed:
            self.unconfirmed.append(p.id)
            note += " (TERMINATION NOT CONFIRMED)"
        c.execute("UPDATE sponsor_pods SET terminated_at=?, usd_estimate=?, note=? WHERE pod_id=?",
                  (p.terminated, p.spend(p.terminated, self.ceiling), note, p.id))
        c.commit()
        self.log(f"terminated {p.name}: {note}")

    def terminate_all(self, c, reason):
        for p in self._live():
            self._terminate(c, p, reason)
        # A deploy whose answer was lost still made a pod: sweep by this run's name prefix too.
        try:
            strays = [p for p in self.api.pods() if p["name"].startswith(self.prefix)]
        except Exception as e:
            self.log(f"could not list pods to check for strays: {e}")
            strays = []
        for s in strays:
            if not terminate_confirmed(self.api, s["id"], self.sleep, wait_s=self.confirm_wait_s):
                if s["id"] not in self.unconfirmed:
                    self.unconfirmed.append(s["id"])
        if self.unconfirmed:
            self.log("TERMINATION NOT CONFIRMED for pod(s) " + ", ".join(self.unconfirmed)
                     + ": terminate them by hand in RunPod, they may still be charging")

    @staticmethod
    def _ignore_signals():
        saved = {}
        try:
            for s in (signal.SIGINT, signal.SIGTERM):
                saved[s] = signal.signal(s, signal.SIG_IGN)
        except ValueError:          # not the main thread
            pass
        return saved

    @staticmethod
    def _restore_signals(saved):
        for s, h in saved.items():
            try:
                signal.signal(s, h)
            except ValueError:
                pass


def install_signal_handlers():
    """SIGINT and SIGTERM stop the loop through its `finally`, which terminates every pod."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, stop)


# ---------- measuring ----------

def report(c):
    """Cost per proven block by height band, from the bot's own log. MEASURED, as an estimate of the bill:
    each pod's costPerHr times its wall time, split between the blocks it proved."""
    try:
        rows = c.execute("SELECT height, seconds, usd_estimate FROM sponsor_work"
                         " WHERE outcome='proven' AND seconds IS NOT NULL").fetchall()
        pods = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(usd_estimate), 0) AS usd FROM sponsor_pods"
                         " WHERE terminated_at IS NOT NULL").fetchone()
        outcomes = {r[0]: r[1] for r in c.execute(
            "SELECT COALESCE(outcome, 'in flight'), COUNT(*) FROM sponsor_work GROUP BY 1")}
    except sqlite3.OperationalError:
        return {"bands": [], "pods": 0, "pod_usd": 0.0, "blocks_proven": 0, "all_in_usd_per_block": None, "outcomes": {}}
    bands = []
    for lo, hi in BANDS:
        rs = [r for r in rows if lo <= r["height"] <= hi]
        if not rs:
            continue
        secs = [r["seconds"] for r in rs]
        usd = [r["usd_estimate"] or 0.0 for r in rs]
        bands.append({"lo": lo, "hi": hi, "blocks": len(rs), "median_seconds": statistics.median(secs),
                      "max_seconds": max(secs), "median_usd": statistics.median(usd), "max_usd": max(usd)})
    n = len(rows)
    return {"bands": bands, "pods": pods["n"], "pod_usd": float(pods["usd"]), "blocks_proven": n,
            "all_in_usd_per_block": (float(pods["usd"]) / n) if n else None, "outcomes": outcomes}


def format_report(r):
    lines = ["MEASURED by the sponsor bot (each pod's costPerHr x its wall time; an estimate of the bill,",
             "not RunPod's invoice). Per block: the pod time between one block landing and the next."]
    if not r["bands"]:
        lines.append("  nothing proven yet: run `trial` first")
    for b in r["bands"]:
        top = "and above" if b["hi"] >= 10 ** 9 else f"to {b['hi']:,}"
        lines.append(f"  blocks {b['lo']:,} {top}: {b['blocks']} proven, median {b['median_seconds']:.0f} s"
                     f" (${b['median_usd']:.4f}), max {b['max_seconds']:.0f} s (${b['max_usd']:.4f})")
    if r["blocks_proven"]:
        lines.append(f"  all in: {r['pods']} pod(s), ${r['pod_usd']:.2f}, ${r['all_in_usd_per_block']:.4f} per proven"
                     " block (boot and idle time included)")
    if r["outcomes"]:
        lines.append("  outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(r["outcomes"].items())))
    return "\n".join(lines)


def plan_text(db_path=DB, trial=None):
    rows = queue(db_path)
    lines = plan(rows)
    try:
        c = connect(db_path, readonly=True)
    except sqlite3.OperationalError:
        return lines or ["no held sponsorships in the queue"]
    try:
        todo = pending_work(c, (), trial)
        if trial is not None:
            bad = trial_refusals(c, trial)
            lines.append(f"trial: {len(trial)} block(s), {len(bad)} refused" + ("" if not bad else ": " + "; ".join(bad[:5])))
        elif not rows:
            lines.append("no held sponsorships in the queue")
        lines.append(f"{len(todo)} block(s) still to prove")
        bands = report(c)["bands"]
        if todo and bands:
            est = 0.0
            for _, h in todo:
                b = next((b for b in bands if b["lo"] <= h <= b["hi"]), None)
                est += b["median_usd"] if b else 0.0
            unmeasured = sum(1 for _, h in todo if not any(b["lo"] <= h <= b["hi"] for b in bands))
            lines.append(f"projected GPU cost at the measured medians: ${est:.2f}"
                         + (f"; {unmeasured} block(s) in bands NOT MEASURED yet" if unmeasured else ""))
        elif todo:
            lines.append("projected cost: NOT MEASURED (run `trial` first)")
    finally:
        c.close()
    return lines


# ---------- the command line ----------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--live"]:
        raise SystemExit("sponsor_bot: use `run --live` with --max-pods, --max-usd and --max-usd-per-hour")
    ap = argparse.ArgumentParser(prog="sponsor_bot.py", description="Proves paid sponsorships on RunPod GPUs.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("plan", help="what it would do (default); touches nothing")

    def live_opts(p):
        p.add_argument("--live", action="store_true", help="actually rent GPUs")
        p.add_argument("--max-pods", type=int)
        p.add_argument("--max-usd", type=float, help="total spend cap for this run")
        p.add_argument("--max-usd-per-hour", type=float, help="cap on the hourly rate of all live pods")
        p.add_argument("--pod-price-ceiling", type=float, default=1.0, help="$/h; a dearer pod is terminated at once")
        p.add_argument("--stall-min", type=float, default=45.0)
        p.add_argument("--blocks-per-pod", type=int, default=25)
        p.add_argument("--tick", type=float, default=30.0)
    live_opts(sub.add_parser("run", help="prove held sponsorships"))
    t = sub.add_parser("trial", help="prove chosen blocks without a sponsorship, to measure cost")
    t.add_argument("--blocks", required=True)
    live_opts(t)
    sub.add_parser("report", help="measured cost per block")
    sub.add_parser("stop-all", help="terminate every hz-sponsor-* pod")
    a = ap.parse_args(argv)
    cmd = a.cmd or "plan"

    if cmd == "plan":
        print("\n".join(plan_text()))
        return 0
    if cmd == "report":
        c = connect(DB, readonly=True)
        try:
            print(format_report(report(c)))
        finally:
            c.close()
        return 0
    if cmd == "stop-all":
        done, unconfirmed = stop_all(RunPod.from_env())
        print(f"terminated {len(done)} pod(s)" + (f": {', '.join(done)}" if done else ""))
        if unconfirmed:
            print("NOT CONFIRMED, terminate by hand: " + ", ".join(unconfirmed))
            return 4
        return 0

    trial = None
    if cmd == "trial":
        try:
            trial = parse_blocks(a.blocks)
        except ValueError as e:
            raise SystemExit(f"sponsor_bot: {e}")
    if not a.live:
        print("\n".join(plan_text(DB, trial)))
        raise SystemExit("sponsor_bot: dry run; add --live with --max-pods, --max-usd and --max-usd-per-hour to rent GPUs")
    missing = [f for f, v in (("--max-pods", a.max_pods), ("--max-usd", a.max_usd),
                              ("--max-usd-per-hour", a.max_usd_per_hour)) if v is None]
    if missing:
        raise SystemExit("sponsor_bot: live mode refuses to start without " + ", ".join(missing))
    try:
        bot = Bot(DB, None, None, max_pods=a.max_pods, max_usd=a.max_usd, max_usd_per_hour=a.max_usd_per_hour,
                  pod_price_ceiling=a.pod_price_ceiling, stall_s=a.stall_min * 60, blocks_per_pod=a.blocks_per_pod,
                  sleep_s=a.tick, trial=trial)
    except BotRefused as e:
        raise SystemExit(f"sponsor_bot: {e}")
    bot.api = RunPod.from_env()
    bot.runner = SshRunner.from_env().prepare()
    install_signal_handlers()
    try:
        reason = bot.run()
    except BotRefused as e:
        raise SystemExit(f"sponsor_bot: {e}")
    c = connect(DB, readonly=True)
    try:
        print(format_report(report(c)))
    finally:
        c.close()
    if bot.unconfirmed:
        return 4
    return 3 if reason == "budget" else 0


if __name__ == "__main__":
    sys.exit(main())
