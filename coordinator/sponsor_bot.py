#!/usr/bin/env python3
"""Sponsor proving bot: proves paid sponsorships on rented RunPod GPUs, and measures what that costs.

It runs on the coordinator box as its own user, with NO access to the coordinator's database (#351): everything
it needs from the board it asks the coordinator's signed, loopback-only bot API for (/api/bot/*), and the
coordinator checks every change it makes. The bot's own cost log is a database of its own. It:
  * works the sponsorships that HOLD their blocks (paid at least the minimum, status `paid` or
    `proving`), oldest payment first, and moves each from `paid` to `proving` when it starts on it;
  * rents one-GPU pods the way the board fleet does (hazync-board-fleet/fleet.sh), boots each against the
    signed release, and has it prove its assigned heights with the released worker's explicit mode,
    `hazync-worker run <n>`. It never uses /api/claim: claims are unsigned;
  * submits each sponsorship's blocks under its OWN key, with the handle "SPONSOR: <sponsor name>" (one
    more key, "SPONSOR: Hazync trial", for every trial). One key per sponsorship because the board adds
    up work by key and shows the handle a key last submitted with. Keys are registered in the
    coordinator's `sponsor_keys` table before a pod uses them; the coordinator refuses a handle starting
    "SPONSOR" from any other key;
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
  python3 sponsor_bot.py bot-key            # create the bot's API key if needed; print its public half
  python3 sponsor_bot.py import-log <db>    # copy sponsor_work and sponsor_pods from a coordinator.db copy
"""
import argparse
import hashlib
import json
import math
import os
import re
import secrets
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

def bot_db_path():
    """The bot's own database (the cost log): SPONSOR_BOT_DB, else bot.db in SPONSOR_BOT_HOME, else None."""
    if os.environ.get("SPONSOR_BOT_DB"):
        return os.environ["SPONSOR_BOT_DB"]
    home = os.environ.get("SPONSOR_BOT_HOME")
    return os.path.join(home, "bot.db") if home else None


# Which sponsorships hold their blocks (paid AT LEAST the minimum, `paid` or `proving`) is the coordinator's rule
# and is decided there: /api/bot/queue lists exactly those, so an `underpaid` row, or a `paid` row below its
# minimum, is never proven on the sponsorship budget.
POD_PREFIX = "hz-sponsor-"
GRAPHQL_URL = "https://api.runpod.io/graphql"
# RunPod's API sits behind Cloudflare, which refuses Python's default "Python-urllib/3.x" User-Agent with
# HTTP 403 "error code: 1010" (measured 2026-09-14). Without this header the bot could not deploy, list or
# terminate a pod, and every refusal was logged as "no GPU capacity".
USER_AGENT = "hazync-sponsor-bot/1 (+https://github.com/bitcoin-ghost/hazync)"
# After this many refused deploy requests in a row the bot stops: an API it cannot talk to will not start
# answering by itself, and it could not terminate a pod either.
RUNPOD_REFUSALS_MAX = 3
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
# 4090 community hosts often never expose public SSH; A40s always did (board fleet, 2026-09-11).
GPU_TYPES = ("NVIDIA GeForce RTX 4090", "NVIDIA A40")
TRIAL_MAX = 1000
# The board handle for a sponsorship's key. The coordinator cleans handles with server.clean_handle and lets
# a registered sponsor key's handle run to len(prefix) + SPONSOR_NAME_MAX, so a 40-character name is kept.
HANDLE_PREFIX = "SPONSOR: "
NAME_MAX = 40
HANDLE_CAP = len(HANDLE_PREFIX) + NAME_MAX
TRIAL_NAME = "Hazync trial"
# `report` bands: the sponsorship price ladder's height ranges, and everything above it.
BANDS = ((1, 200000), (200001, 400000), (400001, 600000), (600001, 800000), (800001, 1000000), (1000001, 10 ** 9))
# Where the coordinator serves a block from, in its order (server.bundle_path): the bridge's bundle, then the
# legacy witness, with the same variables and default as the coordinator. Set HAZYNC_BRIDGE_OUT for the bot as
# for the coordinator: without it only the legacy witnesses count, and no pod is rented for anything above them.
BRIDGE_DIR = os.environ.get("HAZYNC_BRIDGE_OUT", "")
WITNESS = os.environ.get("WITNESS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "witnesses"))

# test_sponsor_bot.py --control sets these to show its cleanup and landed-proof tests can fail. Never set them otherwise.
_CONTROL_SKIP_CLEANUP_ON_ERROR = False
_CONTROL_IGNORE_LANDED = False
_CONTROL_IGNORE_BUNDLES = False


# ---------- the coordinator's bot API ----------

class CoordError(RuntimeError):
    """The coordinator's bot API could not be reached, or refused a request."""


COORD_ERRORS_MAX = 10     # ticks in a row the coordinator may fail before the run stops (every pod terminated)


class Coord:
    """The coordinator's sponsor bot API (/api/bot/*, server.py), signed with the bot's ed25519 key.

    The signed message is server.bot_message, written out again here because the bot does not import the
    coordinator; test_sponsor_bot.py checks the two agree."""

    def __init__(self, url, key_hex, timeout=30):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.url, self.timeout = url.rstrip("/"), timeout
        self.sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex.strip()))

    @staticmethod
    def key_path():
        if os.environ.get("SPONSOR_BOT_KEY_FILE"):
            return os.environ["SPONSOR_BOT_KEY_FILE"]
        home = os.environ.get("SPONSOR_BOT_HOME")
        return os.path.join(home, "bot.key") if home else None

    @classmethod
    def from_env(cls):
        path = cls.key_path()
        if not path or not os.path.isfile(path):
            raise SystemExit("sponsor_bot: no bot API key (SPONSOR_BOT_KEY_FILE, or bot.key in SPONSOR_BOT_HOME); "
                             "create one with `sponsor_bot.py bot-key` and give its public half to the coordinator")
        with open(path) as f:
            return cls(os.environ.get("SPONSOR_BOT_COORD_URL", "http://127.0.0.1:8899"), f.read())

    @staticmethod
    def message(method, path, ts, nonce, raw):
        return (f"hazync-sponsor-bot-v1\n{method}\n{path}\n{ts}\n{nonce}\n"
                f"{hashlib.sha256(raw or b'').hexdigest()}").encode()

    def _call(self, method, path, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        sig = self.sk.sign(self.message(method, path, ts, nonce, raw)).hex()
        req = urllib.request.Request(self.url + path, data=raw if method == "POST" else None, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                              "X-Hazync-Bot-Ts": ts, "X-Hazync-Bot-Nonce": nonce, "X-Hazync-Bot-Sig": sig})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("error") or ""
            except ValueError:
                detail = ""
            raise CoordError(f"the coordinator answered {e.code} to {method} {path.split('?')[0]}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise CoordError(f"the coordinator could not be reached for {method} {path.split('?')[0]}: {e}") from None

    def queue(self):
        return self._call("GET", "/api/bot/queue")["sponsorships"]

    def blocks(self, heights):
        """{height: {"covered", "proofs": [{"pubkey", "ts"}], "claimed", "held"}}"""
        hs, out = sorted({int(h) for h in heights}), {}
        for i in range(0, len(hs), 1000):
            got = self._call("GET", "/api/bot/blocks?h=" + ",".join(str(h) for h in hs[i:i + 1000]))["blocks"]
            out.update({int(k): v for k, v in got.items()})
        return out

    def sponsorship(self, sid):
        return self._call("GET", f"/api/bot/sponsorship/{int(sid)}")

    def register_key(self, pubkey, sid, handle):
        return self._call("POST", "/api/bot/key", {"pubkey": pubkey, "sponsorship_id": sid, "handle": handle})

    def proving(self, sid):
        return self._call("POST", "/api/bot/proving", {"sponsorship_id": int(sid)})

    def reconcile(self):
        return self._call("POST", "/api/bot/reconcile", {})["proven"]


def bot_key(path):
    """Create the bot's API key at `path` if it is missing (mode 600); return its public half in hex."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        sk = Ed25519PrivateKey.generate()
        _write_private(path, sk.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    with open(path) as f:
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))
    return sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()


# ---------- the bot's own database: the cost log ----------

def connect(db_path=None, readonly=False):
    path = db_path or bot_db_path()
    if not path:
        raise SystemExit("sponsor_bot: set SPONSOR_BOT_DB, or SPONSOR_BOT_HOME (the cost log is bot.db there)")
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
        outcome TEXT,                           -- NULL while in flight; proven | stalled | failed | cancelled
        pubkey TEXT);                           -- the identity the block was proved under
      CREATE INDEX IF NOT EXISTS sponsor_work_height ON sponsor_work(height);
      CREATE TABLE IF NOT EXISTS sponsor_pods(
        pod_id TEXT PRIMARY KEY, name TEXT, gpu_type TEXT, cost_per_hr REAL,
        created_at REAL, terminated_at REAL, usd_estimate REAL, note TEXT);
      CREATE TABLE IF NOT EXISTS regen_queue(
        height INTEGER PRIMARY KEY,             -- one row per height; re-queueing is a no-op
        sponsorship_id INTEGER,                 -- the paid sponsorship that wants it
        queued_at REAL NOT NULL,
        started_at REAL, finished_at REAL,      -- started and not finished == in flight, at most one
        pid INTEGER,                            -- the replay's pid, so a killed bot's row can be reaped
        rung INTEGER,                           -- the archived checkpoint it seeded from
        outcome TEXT,                           -- NULL while queued or running; done | refused | failed
        detail TEXT);
      CREATE INDEX IF NOT EXISTS regen_queue_outcome ON regen_queue(outcome);
    """)
    if "pubkey" not in {r[1] for r in c.execute("PRAGMA table_info(sponsor_work)")}:
        c.execute("ALTER TABLE sponsor_work ADD COLUMN pubkey TEXT")
    c.commit()


def import_log(src, c):
    """Copy sponsor_work and sponsor_pods from a copy of the coordinator's database, where the bot kept its log
    before #351, into the bot's own. Rows already present (by id and pod id) are left alone. Returns (work, pods)."""
    ensure_tables(c)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    s.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in s.execute("PRAGMA table_info(sponsor_work)")}
        wcols = [k for k in ("id", "sponsorship_id", "height", "pod_id", "gpu_type", "cost_per_hr", "assigned_at",
                             "proven_at", "seconds", "usd_estimate", "outcome", "pubkey") if k in cols]
        pcols = ["pod_id", "name", "gpu_type", "cost_per_hr", "created_at", "terminated_at", "usd_estimate", "note"]
        w = p = 0
        if wcols:
            for r in s.execute(f"SELECT {','.join(wcols)} FROM sponsor_work"):
                w += c.execute(f"INSERT OR IGNORE INTO sponsor_work({','.join(wcols)}) VALUES({','.join('?' * len(wcols))})",
                               tuple(r)).rowcount
        if s.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sponsor_pods'").fetchone():
            for r in s.execute(f"SELECT {','.join(pcols)} FROM sponsor_pods"):
                p += c.execute(f"INSERT OR IGNORE INTO sponsor_pods({','.join(pcols)}) VALUES({','.join('?' * len(pcols))})",
                               tuple(r)).rowcount
        c.commit()
    finally:
        s.close()
    return w, p


# ---------- identities ----------

def board_handle(name):
    """"SPONSOR: <name>" exactly as the board will show it: server.clean_handle's rule (printable only,
    no < > & " ', trimmed), capped at HANDLE_CAP."""
    h = "".join(ch for ch in HANDLE_PREFIX + str(name or "") if ch.isprintable() and ch not in "<>&\"'").strip()
    return h[:HANDLE_CAP]


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def identity(home, sponsorship_id, name):
    """The key a sponsorship's blocks are proved under, in $home/identities/<id | trial>/ as the worker
    expects (key.hex, the raw ed25519 seed in hex, and handle). Created once and reused."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
    tag = "trial" if sponsorship_id is None else str(int(sponsorship_id))
    d = os.path.join(home, "identities", tag)
    os.makedirs(d, mode=0o700, exist_ok=True)
    kp = os.path.join(d, "key.hex")
    if not os.path.isfile(kp):
        sk = Ed25519PrivateKey.generate()
        _write_private(kp, sk.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    with open(kp) as f:
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))
    pub = sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    hp = os.path.join(d, "handle")
    if sponsorship_id is not None and name is None and os.path.isfile(hp):
        with open(hp) as f:                      # a lookup: keep the handle the key was made with
            handle = f.read().strip()
    else:
        handle = board_handle(TRIAL_NAME if sponsorship_id is None else name)
        if not os.path.isfile(hp) or open(hp).read().strip() != handle:
            _write_private(hp, handle + "\n")
    return {"tag": tag, "home": d, "pubkey": pub, "handle": handle}


def register_key(coord, ident, sponsorship_id):
    """Register a key with the coordinator (sponsor_keys), so it may use a "SPONSOR" handle. Idempotent. The
    coordinator checks the handle against the sponsorship's name and that the sponsorship holds."""
    coord.register_key(ident["pubkey"], sponsorship_id, ident["handle"])


def queue(coord):
    """Sponsorships that hold their blocks, oldest payment first, as the coordinator decides."""
    return coord.queue()


def plan(rows):
    return [f"sponsorship #{r['id']}: would prove blocks {r['lo']} to {r['hi']} "
            f"({r['hi'] - r['lo'] + 1} blocks) for {r['name']}" for r in rows]


def has_bundle(h):
    """True when the coordinator can serve block h to a worker, so a pod can prove it."""
    if _CONTROL_IGNORE_BUNDLES:
        return True
    files = ([os.path.join(BRIDGE_DIR, f"bundle_{int(h)}.json")] if BRIDGE_DIR else []) \
        + [os.path.join(WITNESS, f"block_{int(h)}.json")]
    return any(os.path.exists(f) for f in files)


# Measured on the live coordinator's own bridge journal, 2026-09-18: 230,000 -> 742,257 is 508,000 blocks
# in 41.4 h of walking. There is no single rate for the chain — 0.019 s/block below 100,000 against 0.596
# near the tip, because the cost follows the UTXO set — so this is the average across the gap, which is
# the span these replays actually walk.
REGEN_S_PER_BLOCK = 0.294
# A replay longer than this is REPORTED, never suggested: it is a decision, not a chore.
REGEN_WALK_WARN = 50000
CKPT_ARCHIVE = os.environ.get("HAZYNC_CKPT_ARCHIVE", "/srv/bulk/hazync/checkpoints")

# test_regen_advice.py --control sets this, to show the "no checkpoint below it" refusal can fail.
_CONTROL_IGNORE_MISSING_RUNG = False


def regen_advice(plan, waiting, s_per_block=REGEN_S_PER_BLOCK, warn_blocks=REGEN_WALK_WARN):
    """One line on whether held blocks with no bundle can be regenerated, and what that would cost.

    Pure. `plan` is what regen_bundles.plan() returned, so the "strictly below" rule lives in ONE place
    rather than being restated here where the two could drift apart.
    """
    if not plan:
        return None
    if plan["missing"] and not _CONTROL_IGNORE_MISSING_RUNG:
        return (f"{waiting} held block(s) have no bundle and CANNOT be regenerated: no archived checkpoint "
                f"below height {min(plan['missing'])}. A replay without one starts at GENESIS (~6 days) "
                f"while looking exactly like a working job.")
    span = plan["hi"] - plan["rung"]
    hours = span * s_per_block / 3600.0
    if span > warn_blocks:
        return (f"{waiting} held block(s) have no bundle. Regeneration is possible but expensive: the "
                f"nearest checkpoint below them is {plan['rung']:,}, so the replay walks {span:,} blocks "
                f"(~{hours:.1f} h at the measured {s_per_block} s/block) to produce {waiting}. NOT started "
                f"— hazync#379 puts checkpoints inside the gap, cutting this to ~{warn_blocks:,} blocks.")
    return (f"{waiting} held block(s) have no bundle; regenerate from checkpoint {plan['rung']:,} with a "
            f"{span:,}-block replay (~{hours:.1f} h): "
            f"coordinator/deploy/regen_bundles.py --heights <heights> --apply")


def regen_note(heights):
    """regen_advice() for these heights against the archived checkpoints, or None.

    ⛔ NEVER RAISES. This runs at startup, before a single pod is rented; an exception here would stop
    every sponsorship over what is only a reporting improvement. Any failure falls back to the caller's
    plain message.

    ⛔ The import is lazy on purpose: regen_bundles imports THIS module inside its own main() for
    --from-waiting, so importing it at module level here would be a cycle.
    """
    try:
        import sys
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy")
        if d not in sys.path:
            sys.path.insert(0, d)
        import regen_bundles
        return regen_advice(regen_bundles.plan(heights, regen_bundles.archived_rungs(CKPT_ARCHIVE)),
                            len(heights))
    except Exception:
        return None


# ---------- the regeneration queue (hazync#347) ----------
#
# regen_advice() above REPORTS. This QUEUES and RUNS, one replay at a time, because the operator's ruling is
# that coordinator CPU may be spent on a block SOMEONE HAS PAID FOR: "if the compute falls onto our
# coordinator then no i dont want it at all" / "yes its fine for sponsor bot as they are paying". Payment is
# the rate limiter, so only held (paid) sponsorships reach this queue — never a public request.
#
# ⛔ ONE AT A TIME IS THE SAFETY PROPERTY, NOT TIDINESS. A replay is ~10 GB of accumulator against a 62.8 GiB
# box that also serves the board. Two at once is two bridges on one machine, which is exactly the shape that
# OOM-killed the live bridge 23 times overnight on 2026-09-18. regen_bundles.py has no lock of its own.

REGEN_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "regen_bundles.py")
BRIDGE_BINARY = "hazync-host-bridge"

# test_regen_queue.py --control sets these, to show the one-at-a-time and bridge guards can fail.
_CONTROL_IGNORE_ONE_AT_A_TIME = False
_CONTROL_IGNORE_BRIDGE_BUSY = False


def bridge_running(proc="/proc"):
    """PIDs of any bridge already walking on this box, excluding this process. Empty when none.

    ⛔ READ /proc, NEVER `pgrep -f hazync-host-bridge`. pgrep's own command line contains the pattern, and
    so does this module (REGEN_BIN is logged, and the replay's argv names the binary), so a name match
    reports a bridge that is only the bot talking about one. Matching basename(argv[0]) exactly means the
    process must BE the bridge, not mention it. A clean box reporting a phantom process has cost hours
    three separate times.
    """
    me, out = os.getpid(), []
    try:
        names = os.listdir(proc)
    except OSError:
        return []
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(os.path.join(proc, name, "cmdline"), "rb") as f:
                argv0 = f.read().split(b"\0")[0]
        except OSError:
            continue                      # the process exited between listdir and open: not a bridge
        if argv0 and os.path.basename(argv0.decode("utf-8", "replace")) == BRIDGE_BINARY:
            out.append(int(name))
    return sorted(out)


def regen_enqueue(c, pairs, now):
    """Queue (sponsorship_id, height) pairs for regeneration. Already-queued heights are left alone. Count added."""
    n = 0
    for sid, h in pairs:
        n += c.execute("INSERT OR IGNORE INTO regen_queue(height, sponsorship_id, queued_at) VALUES(?,?,?)",
                       (int(h), sid, now)).rowcount
    c.commit()
    return n


def regen_inflight(c):
    """The row whose replay was started and has not finished, or None. At most one by construction."""
    return c.execute("SELECT * FROM regen_queue WHERE started_at IS NOT NULL AND finished_at IS NULL "
                     "ORDER BY started_at LIMIT 1").fetchone()


def regen_next(c):
    """The oldest queued height not yet attempted, or None."""
    return c.execute("SELECT * FROM regen_queue WHERE started_at IS NULL AND outcome IS NULL "
                     "ORDER BY queued_at, height LIMIT 1").fetchone()


def regen_reap(c, now, alive=None):
    """Fail any row left in flight by a bot that stopped mid-replay. Returns the heights reaped.

    ⛔ Without this a killed bot leaves a row started-but-never-finished, and the one-at-a-time guard then
    refuses every future replay for ever — a queue that silently stops working and reports nothing.
    """
    alive = bridge_running() if alive is None else alive
    reaped = []
    for r in c.execute("SELECT height, pid FROM regen_queue WHERE started_at IS NOT NULL "
                       "AND finished_at IS NULL").fetchall():
        if r["pid"] and r["pid"] in alive:
            continue                       # still walking: leave it alone
        c.execute("UPDATE regen_queue SET finished_at=?, outcome='failed', "
                  "detail='the bot stopped while this replay was running' WHERE height=?", (now, r["height"]))
        reaped.append(r["height"])
    if reaped:
        c.commit()
    return reaped


def regen_start(c, height, pid, rung, now):
    c.execute("UPDATE regen_queue SET started_at=?, pid=?, rung=? WHERE height=?", (now, pid, rung, height))
    c.commit()


def regen_finish(c, height, rc, detail, now):
    """Map regen_bundles.main()'s exit codes onto an outcome. They are pinned by its own docstring:
    0 ran, 1 something is wrong, 2 could not check (no rung below, no bridge, no space)."""
    outcome = {0: "done", 2: "refused"}.get(rc, "failed")
    c.execute("UPDATE regen_queue SET finished_at=?, outcome=?, detail=? WHERE height=?",
              (now, outcome, (detail or "")[:500], height))
    c.commit()
    return outcome


def covered(coord, heights):
    """The heights among `heights` covered by verified proofs, whoever made them."""
    return {h for h, f in coord.blocks(heights).items() if f["covered"]} if heights else set()


def reconcile(coord):
    """Ask the coordinator to move every held sponsorship whose whole span is covered to `proven`. Their ids."""
    return coord.reconcile()


def reconcile_work(c, coord):
    """Correct work rows logged `cancelled`, `failed` or `stalled` whose block was in fact proven under the row's
    own key while its pod was still alive. A proof can land after the bot's last look and before it stops the
    pod: trial 2's pod proved blocks 90000 and 90001 in the minute before the bot stopped on an error, and both
    were logged `cancelled`. A proof that lands after the pod was stopped is left alone, since it cannot be told
    apart from another pod's work. Seconds run from that pod's previous proof, or from the assignment, to this
    proof's own timestamp. Returns the number of rows corrected."""
    if _CONTROL_IGNORE_LANDED:
        return 0
    try:
        work = c.execute(
            "SELECT w.id, w.height, w.pod_id, w.cost_per_hr, w.assigned_at, w.outcome, w.pubkey, p.terminated_at"
            " FROM sponsor_work w JOIN sponsor_pods p ON p.pod_id = w.pod_id"
            " WHERE p.terminated_at IS NOT NULL AND w.pubkey IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return 0
    # Only pods with a row that could need correcting; every row of such a pod still counts for its timeline.
    pods = {w["pod_id"] for w in work if w["outcome"] in ("cancelled", "failed", "stalled")}
    work = [w for w in work if w["pod_id"] in pods]
    if not work:
        return 0
    facts = coord.blocks({w["height"] for w in work})
    rows = sorted(({"id": w["id"], "height": w["height"], "pod_id": w["pod_id"], "cost_per_hr": w["cost_per_hr"],
                    "assigned_at": w["assigned_at"], "outcome": w["outcome"], "ts": pr["ts"]}
                   for w in work for pr in facts[w["height"]]["proofs"]
                   if pr["pubkey"] == w["pubkey"] and pr["ts"] is not None
                   and w["assigned_at"] <= pr["ts"] <= w["terminated_at"]),
                  key=lambda r: (r["pod_id"], r["ts"]))
    fixed, last, seen = 0, {}, set()
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        start = max(r["assigned_at"], last.get(r["pod_id"], r["assigned_at"]))
        last[r["pod_id"]] = r["ts"]
        if r["outcome"] not in ("cancelled", "failed", "stalled"):
            continue
        if c.execute("SELECT 1 FROM sponsor_work WHERE height=? AND outcome='proven' LIMIT 1", (r["height"],)).fetchone():
            continue
        secs = max(0.0, r["ts"] - start)
        cur = c.execute("UPDATE sponsor_work SET outcome='proven', proven_at=?, seconds=?, usd_estimate=?"
                        " WHERE id=? AND outcome=?",
                        (r["ts"], secs, secs * (r["cost_per_hr"] or 0.0) / 3600.0, r["id"], r["outcome"]))
        fixed += cur.rowcount
    c.commit()
    return fixed


def pending_work(coord, busy=(), trial=None, need_bundle=True):
    """[(sponsorship id, or None for a trial, height)] still to prove, in the order to prove them:
    held sponsorships oldest payment first, heights in order, skipping covered and busy heights, and heights
    with no bundle yet (unless need_bundle is False): no pod can prove those, so renting one would only spend."""
    busy = set(busy)
    ok = has_bundle if need_bundle else (lambda h: True)
    if trial is not None:
        cov = covered(coord, trial)
        return [(None, h) for h in trial if h not in busy and h not in cov and ok(h)]
    out, seen = [], set()
    for r in coord.queue():
        for h in r["todo"]:                     # the coordinator's uncovered heights, in order
            if h not in busy and h not in seen and ok(h):
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


def trial_refusals(coord, heights):
    """Why each trial block may not be proven by the bot: already proven, no bundle yet, claimed right now, or held."""
    facts = coord.blocks(heights) if heights else {}
    out = []
    for h in heights:
        f = facts[h]
        if f["covered"]:
            out.append(f"block {h} is already proven")
        elif not has_bundle(h):
            out.append(f"block {h} has no bundle yet, so no pod can prove it")
        elif f["claimed"]:
            out.append(f"block {h} is claimed by a prover right now")
        elif f["held"] is not None:
            out.append(f"block {h} is held for sponsorship #{f['held']}")
    return out


# ---------- RunPod ----------

class RunPodError(RuntimeError):
    pass


class RunPod:
    """The RunPod GraphQL calls the board fleet already uses: deploy, list, terminate."""

    def __init__(self, key, url=GRAPHQL_URL, timeout=60):
        self.key, self.url, self.timeout = key, url, timeout

    @staticmethod
    def key_path():
        """RUNPOD_API_KEY_FILE, else runpod.key in SPONSOR_BOT_HOME. Never the home directory: on the
        coordinator box the bot runs as `hazync`, which has none."""
        path = os.environ.get("RUNPOD_API_KEY_FILE")
        if not path and os.environ.get("SPONSOR_BOT_HOME"):
            path = os.path.join(os.environ["SPONSOR_BOT_HOME"], "runpod.key")
        if not path:
            raise SystemExit("sponsor_bot: set RUNPOD_API_KEY_FILE, or SPONSOR_BOT_HOME with runpod.key in it")
        return path

    @classmethod
    def from_env(cls):
        path = cls.key_path()
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
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}
        if USER_AGENT:
            headers["User-Agent"] = USER_AGENT
        req = urllib.request.Request(self.url, data=json.dumps({"query": query}).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                body = e.read()[:200].decode(errors="replace").strip()
            except Exception:
                body = ""
            raise RunPodError(f"HTTP {e.code} {e.reason}: {body}") from e
        except Exception as e:
            raise RunPodError(f"{type(e).__name__}: {e}") from e
        if d.get("errors") and not d.get("data"):
            raise RunPodError(str(d["errors"])[:300])
        return d.get("data") or {}

    def deploy(self, name, ssh_pubkey, gpu_types=GPU_TYPES):
        """One on-demand 1-GPU pod, trying each GPU type in turn. None when RunPod answered and no type had
        capacity. RunPodError when RunPod refused the request for every type: that is not a capacity miss,
        and treating it as one hid a Cloudflare 403 behind "no GPU capacity" on the first trial."""
        refused = None
        answered = False
        for gt in gpu_types:
            q = ("mutation { podFindAndDeployOnDemand(input: { cloudType: ALL, gpuCount: 1, volumeInGb: 0, "
                 f"containerDiskInGb: 40, gpuTypeId: {self._s(gt)}, name: {self._s(name)}, "
                 f"imageName: {self._s(IMAGE)}, ports: \"22/tcp\", "
                 f"env: [{{key: \"PUBLIC_KEY\", value: {self._s(ssh_pubkey)}}}] }}) {{ id costPerHr }} }}")
            try:
                p = self._gql(q).get("podFindAndDeployOnDemand")
                answered = True
            except RunPodError as e:
                refused, p = e, None
            if p and p.get("id"):
                return {"id": p["id"], "name": name, "gpu_type": gt, "cost_per_hr": float(p.get("costPerHr") or 0)}
        if refused is not None and not answered:
            raise refused
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
    """Boots a pod against the signed release and runs assigned heights on it with `hazync-worker run <n>`,
    each under its sponsorship's identity."""

    FILES = ("hazync-worker", "hazync-run-workers.sh", "hazync-host-x86_64-linux-gnu-cuda")

    # Nothing here reads $HOME or ~/.ssh: on the coordinator box the bot runs as `hazync`, which has no home
    # directory. Keys, known_hosts, the gpg keyring and scratch files all live under SPONSOR_BOT_HOME, and
    # every ssh and scp call names its key, its known_hosts file and no config file.

    def __init__(self, ssh_key, bot_home, meta_url, release=None, workdir=None, gnupghome=None):
        self.key, self.home, self.meta_url, self.release = ssh_key, bot_home, meta_url, release
        self.known_hosts = os.path.join(bot_home, "known_hosts")
        self.gnupghome = gnupghome or os.path.join(bot_home, "gnupg")
        if workdir is None:
            os.makedirs(os.path.join(bot_home, "work"), mode=0o700, exist_ok=True)
            workdir = tempfile.mkdtemp(prefix="run-", dir=os.path.join(bot_home, "work"))
        self.dir = workdir
        self.method_id = None
        self.ssh_pubkey = None

    @classmethod
    def from_env(cls):
        home = os.environ.get("SPONSOR_BOT_HOME")
        if not home or not os.path.isabs(home):
            raise SystemExit("sponsor_bot: set SPONSOR_BOT_HOME to an absolute path (identities, the SSH key, "
                             "known_hosts and the gpg keyring live there)")
        key = os.environ.get("SPONSOR_BOT_SSH_KEY") or os.path.join(home, "ssh", "id_ed25519")
        return cls(key, home, os.environ.get("SPONSOR_BOT_META_URL", "http://127.0.0.1:8899/api/meta"),
                   os.environ.get("SPONSOR_BOT_RELEASE") or None,
                   gnupghome=os.environ.get("SPONSOR_BOT_GNUPGHOME") or None)

    def prepare(self):
        """Everything checked locally before a cent is spent: identity, SSH key, program ID, signed manifest."""
        os.makedirs(os.path.join(self.home, "identities"), mode=0o700, exist_ok=True)
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
        if not self.verify_manifest():
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

    def verify_manifest(self):
        """gpg --verify with the keyring in SPONSOR_BOT_HOME (GNUPGHOME), not the user's."""
        env = dict(os.environ, GNUPGHOME=self.gnupghome)
        v = subprocess.run(["gpg", "--batch", "--verify", "SHA256SUMS.txt.asc", "SHA256SUMS.txt"], cwd=self.dir,
                           capture_output=True, text=True, env=env)
        return "Good signature" in (v.stdout + v.stderr)

    def _opts(self):
        # Fresh pods reuse IPs and ports with new host keys, so host keys are not checked; they are still
        # recorded in SPONSOR_BOT_HOME rather than a home directory that does not exist.
        return ["-F", "/dev/null", "-i", self.key, "-o", "IdentitiesOnly=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]

    # A timed-out ssh or scp is a FAILED STEP for that pod, never an exception: on the first trial an uncaught
    # TimeoutExpired from starting work stopped the whole bot.
    @staticmethod
    def _run(argv, timeout):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")

    def _ssh(self, pod, cmd, timeout=60):
        ip, port = pod.ssh
        return self._run(["ssh", "-n", *self._opts(), "-p", str(port), f"root@{ip}", cmd], timeout)

    def _scp(self, pod, paths, dest):
        ip, port = pod.ssh
        return self._run(["scp", "-q", *self._opts(), "-P", str(port), *paths, f"root@{ip}:{dest}"], 120)

    def boot(self, pod):
        detail = "not attempted"
        for _ in range(3):
            try:
                ok = (self._ssh(pod, "mkdir -p /workspace /root/.hazync-ids && chmod 700 /root/.hazync-ids").returncode == 0
                      and self._scp(pod, [os.path.join(self.dir, "boot.sh"), os.path.join(self.dir, "want.txt")],
                                    "/workspace/").returncode == 0)
                if not ok:
                    detail = "ssh or scp failed"
                else:
                    out = self._ssh(pod, "bash /workspace/boot.sh", 900).stdout
                    if "SHA_OK" in out and "GPU_OK" in out and f"MID={self.method_id}" in out:
                        return True, "SHA_OK GPU_OK MID ok"
                    detail = " ".join(out.split())[-200:] or "boot printed nothing"
            except subprocess.TimeoutExpired:
                detail = "boot timed out"
            time.sleep(10)
        return False, detail

    def start(self, pod, work):
        """work: [{"height", "home", "pubkey", "handle", "tag"}]. Every identity the blocks need is copied to
        /root/.hazync-ids/<tag>/ and each block runs with HAZYNC_HOME pointing at its own. Bundles and
        witnesses are shared, not kept per identity."""
        copied = getattr(pod, "identities", None)
        if copied is None:
            copied = pod.identities = set()
        for w in {w["tag"]: w for w in work}.values():
            if w["tag"] in copied:
                continue
            dest = f"/root/.hazync-ids/{w['tag']}"
            if (self._ssh(pod, f"mkdir -p {dest} && chmod 700 {dest}").returncode != 0
                    or self._scp(pod, [os.path.join(w["home"], "key.hex"), os.path.join(w["home"], "handle")],
                                 dest + "/").returncode != 0
                    or self._ssh(pod, f"chmod 600 {dest}/key.hex").returncode != 0):
                raise StartFailed(f"could not copy identity {w['tag']} to {pod.name}")
            copied.add(w["tag"])
        lines = ["#!/bin/bash", "cd /workspace"]
        for w in work:
            lines.append(f"HAZYNC_HOME=/root/.hazync-ids/{w['tag']} BUNDLE_DIR=/workspace/bundles "
                         f"WITNESS_DIR=/workspace/witnesses HAZYNC_HOST=/workspace/hazync-host-cuda "
                         f"./hazync-worker run {int(w['height'])}; echo \"DONE {int(w['height'])} rc=$?\"")
        lines.append("echo ALLDONE")
        script = os.path.join(self.dir, f"run-{pod.id}.sh")
        with open(script, "w") as f:
            f.write("\n".join(lines) + "\n")
        if self._scp(pod, [script], "/workspace/sponsor-run.sh").returncode != 0:
            raise StartFailed(f"could not copy the work list to {pod.name}")
        r = self._ssh(pod, launch_command())
        if r.returncode != 0:
            raise StartFailed(f"could not start work on {pod.name}: {r.stderr.strip()[:200]}")

    def status(self, pod):
        try:
            r = self._ssh(pod, "tail -n 1 /workspace/sponsor-run.log 2>/dev/null", 30)
        except subprocess.TimeoutExpired:
            return "unreachable"
        if r.returncode != 0:
            return "unreachable"
        return "finished" if r.stdout.strip() == "ALLDONE" else "running"


def launch_command(workdir="/workspace"):
    """Start the work list detached, so the ssh that starts it returns at once.

    Only `setsid -f` goes to the background, with stdin, stdout and stderr all redirected. The first trial used
    `cd ... && : > log && setsid nohup bash run.sh >> log 2>&1 < /dev/null & disown`: `&` backgrounds the whole
    `&&` list in a subshell whose own stdout and stderr are still the ssh channel, so ssh waited for the proving
    run to end and the call timed out (reproduced with pipes: the old command held them open, this returns)."""
    return (f"cd {workdir} && : > sponsor-run.log && setsid -f bash {workdir}/sponsor-run.sh"
            f" >> {workdir}/sponsor-run.log 2>&1 < /dev/null; exit 0")


# ---------- the bot ----------

class BotRefused(RuntimeError):
    pass


class StartFailed(RuntimeError):
    """Work could not be started on ONE pod (an ssh or scp step failed or timed out). The bot terminates that
    pod and puts its blocks back in the queue; it is not a reason to stop the run."""


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
                 trial=None, identity_home=None, coord=None, clock=time.time, sleep=time.sleep, log=None):
        for flag, v in (("--max-pods", max_pods), ("--max-usd", max_usd), ("--max-usd-per-hour", max_usd_per_hour)):
            if v is None or v <= 0:
                raise BotRefused(f"live mode needs a positive {flag}")
        if pod_price_ceiling <= 0 or pod_price_ceiling > max_usd_per_hour:
            raise BotRefused("--pod-price-ceiling must be positive and no more than --max-usd-per-hour")
        self.db_path, self.api, self.runner, self.coord = db_path, api, runner, coord
        self.max_pods, self.max_usd, self.max_usd_per_hour = int(max_pods), float(max_usd), float(max_usd_per_hour)
        self.ceiling = float(pod_price_ceiling)
        self.stall_s, self.ssh_timeout_s, self.boot_timeout_s = stall_s, ssh_timeout_s, boot_timeout_s
        self.fail_grace_s, self.blocks_per_pod, self.sleep_s = fail_grace_s, max(1, int(blocks_per_pod)), sleep_s
        self.budget_lead_s, self.capacity_backoff_s, self.confirm_wait_s = budget_lead_s, capacity_backoff_s, confirm_wait_s
        self.trial = list(trial) if trial is not None else None
        self.identity_home = identity_home
        self._identities = {}
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {m}", flush=True))
        self.pods = []
        self.stop_reason = None
        self.unconfirmed = []
        self.prefix = f"{POD_PREFIX}{int(time.time())}-"   # wall time, not self.clock: unique per run
        self._n = 0
        self._capacity_at = 0.0
        self._refusals = 0
        self._coord_errors = 0
        self._regen_proc = None            # the one in-flight replay, or None
        self._regen_height = None
        # ⛔ Its OWN counter, not self._n. _n is the pod sequence number and only moves when a pod is
        # deployed — and while the queue is waiting for a bridge, nothing is being rented. Keyed on _n the
        # "waiting" line would print every tick or, far worse, never, leaving the queue silently stalled
        # with nothing on the record to say why.
        self._regen_waits = 0

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
        if not self.identity_home:
            raise BotRefused("no identity home (SPONSOR_BOT_HOME): the bot keeps one key per sponsorship there")
        if self.coord is None:
            raise BotRefused("no coordinator API client: the bot reads and changes the board only through /api/bot/")
        # Before a cent is spent: the coordinator must have the bot API and accept this bot's key. A coordinator
        # without it would not keep held blocks for the bot or register its keys.
        try:
            self.coord.queue()
        except CoordError as e:
            raise BotRefused(f"the coordinator's sponsor bot API is not usable: {e}")
        c = connect(self.db_path)
        ensure_tables(c)
        try:
            fixed = reconcile_work(c, self.coord)
            if fixed:
                self.log(f"corrected {fixed} work row(s) whose block was proven before its pod was stopped")
            if self.trial is None:
                have = {h for _, h in pending_work(self.coord)}
                unbundled = [h for _, h in pending_work(self.coord, need_bundle=False) if h not in have]
                if unbundled:
                    # ⛔ "wait for the bridge" is a wait FOR EVER in the 418,269-967,499 gap: the live
                    # bridge runs HAZYNC_BRIDGE_EMIT_FROM=967500, so it walks those heights writing no
                    # bundle and will never come back for them. Say whether they can be made from an
                    # archived checkpoint instead, and what that costs.
                    self.log(regen_note(unbundled) or
                             f"{len(unbundled)} held block(s) have no bundle yet and wait for the bridge:"
                             " no pod is rented for them")
                    # ...and then QUEUE them. These are held sponsorships, so they are paid for; the drain
                    # below runs one replay at a time and refuses while any bridge is walking.
                    byh = {h: sid for sid, h in pending_work(self.coord, need_bundle=False)}
                    added = regen_enqueue(c, [(byh.get(h), h) for h in unbundled], now=self.clock())
                    if added:
                        self.log(f"queued {added} height(s) for regeneration, one replay at a time")
                reaped = regen_reap(c, self.clock())
                if reaped:
                    self.log(f"{len(reaped)} replay(s) were in flight when a previous bot stopped, "
                             f"marked failed so the queue is not blocked for ever: {reaped[:10]}")
            if self.trial is not None:
                bad = trial_refusals(self.coord, self.trial)
                if bad:
                    more = f" (and {len(bad) - 10} more)" if len(bad) > 10 else ""
                    raise BotRefused("; ".join(bad[:10]) + more)
        except CoordError as e:
            c.close()
            raise BotRefused(f"the coordinator's sponsor bot API failed before anything was rented: {e}")
        except BotRefused:
            c.close()
            raise
        failed = True
        try:
            while True:
                now = self.clock()
                # The coordinator going away for a moment (a restart) is not a reason to throw away every pod; one
                # that stays away is. Money is checked every look regardless, from the bot's own figures.
                try:
                    if self.trial is None:
                        for sid in reconcile(self.coord):
                            self.log(f"sponsorship #{sid} is proven")
                    self._observe(c, now)
                    coord_ok = True
                except CoordError as e:
                    coord_ok = False
                    self._coord_error(e)
                if self.stop_reason == "coordinator":
                    break
                if self._over_budget(now):
                    self.stop_reason = "budget"
                    self.log(f"spend ${self.spend(now):.2f} has reached the ${self.max_usd:.2f} cap: stopping")
                    break
                if coord_ok:
                    try:
                        self._regen(c, now)
                        self._assign(c, now)
                        self._launch(c, now)
                        if self.stop_reason == "runpod":
                            break
                        # ⛔ AN IN-FLIGHT REPLAY HOLDS THE LOOP OPEN. Without the last clause the bot sees no
                        # live pod and nothing provable (the block has no bundle yet — that is the whole
                        # point) and declares itself done, exiting and orphaning a ~2 h walk it started and
                        # paid for. The bundle would land with nobody left to prove it.
                        if (not self._live() and not pending_work(self.coord, (), self.trial)
                                and self._regen_proc is None and regen_inflight(c) is None):
                            # The last blocks can land after this pass's reconcile, just before their pod is stopped.
                            if self.trial is None:
                                for sid in reconcile(self.coord):
                                    self.log(f"sponsorship #{sid} is proven")
                            self.stop_reason = self.stop_reason or "done"
                            break
                        self._coord_errors = 0
                    except CoordError as e:
                        self._coord_error(e)
                        if self.stop_reason == "coordinator":
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

    def _regen(self, c, now):
        """Advance the regeneration queue by AT MOST ONE replay. Never raises.

        ⛔ NEVER RAISES, for the same reason regen_note() does not: _assign and _launch run immediately
        after this in the same try, with pods live and money already spent. A queue problem must not stop a
        run that is proving blocks.

        ⛔ NON-BLOCKING. A replay is hours and the tick is seconds, so it is started with Popen and polled
        on later ticks — never waited on here.
        """
        try:
            if self._regen_proc is not None:
                rc = self._regen_proc.poll()
                if rc is None:
                    return                                     # still walking; look again next tick
                out = ""
                try:
                    out = (self._regen_proc.stdout.read() or "") if self._regen_proc.stdout else ""
                except Exception:
                    pass
                last = (out.strip().splitlines() or [""])[-1][:200]
                outcome = regen_finish(c, self._regen_height, rc, last, now)
                self.log(f"regeneration of block {self._regen_height} {outcome} (exit {rc}): {last}")
                self._regen_proc, self._regen_height = None, None
                return

            if regen_inflight(c) is not None and not _CONTROL_IGNORE_ONE_AT_A_TIME:
                return                                         # another row is mid-replay: one at a time

            row = regen_next(c)
            if row is None:
                return

            busy = bridge_running()
            if busy and not _CONTROL_IGNORE_BRIDGE_BUSY:
                # Not an error and not a refusal of the row — it stays queued and is tried again next tick.
                # Said on the first wait and then rarely, so a long wait is on the record without flooding it.
                if self._regen_waits % 20 == 0:
                    self.log(f"regeneration of block {row['height']} waits: a bridge is already walking "
                             f"(pid {busy[0]}). Two replays on one box is what OOM-killed the bridge.")
                self._regen_waits += 1
                return
            self._regen_waits = 0

            sys.path.insert(0, os.path.dirname(REGEN_BIN)) if os.path.dirname(REGEN_BIN) not in sys.path else None
            import regen_bundles
            p = regen_bundles.plan([row["height"]], regen_bundles.archived_rungs(CKPT_ARCHIVE))
            if not p or p["missing"]:
                regen_finish(c, row["height"], 2,
                             "no archived checkpoint below it; a replay would start at genesis", now)
                self.log(f"regeneration of block {row['height']} refused: no archived checkpoint below it "
                         f"— a replay without a seed starts at GENESIS (~6 days) looking like a working job.")
                return

            proc = subprocess.Popen([sys.executable, REGEN_BIN, "--heights", str(row["height"]), "--apply"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            regen_start(c, row["height"], proc.pid, p["rung"], now)
            self._regen_proc, self._regen_height = proc, row["height"]
            span = row["height"] - p["rung"]
            self.log(f"regenerating block {row['height']} from checkpoint {p['rung']:,} "
                     f"({span:,}-block replay, ~{span * REGEN_S_PER_BLOCK / 3600.0:.1f} h), pid {proc.pid}")
        except Exception as e:
            self.log(f"the regeneration queue could not be advanced this look ({e}); trying again")

    def _coord_error(self, e):
        self._coord_errors += 1
        if self._coord_errors >= COORD_ERRORS_MAX:
            self.stop_reason = "coordinator"
            self.log(f"the coordinator failed {self._coord_errors} looks in a row ({e}): stopping")
        else:
            self.log(f"the coordinator failed this look ({e}); trying again")

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
        cov = covered(self.coord, [a["height"] for a in p.assigned])
        done = [a for a in p.assigned if a["height"] in cov]
        if done:
            self._record_proven(c, p, done, now)
        sids = {a["sid"] for a in p.assigned if a["sid"] is not None}
        if any(not self.coord.sponsorship(sid)["held"] for sid in sorted(sids)):
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

    def _record_proven(self, c, p, done, now):
        # Blocks run one after another on a pod, and several can land between two looks: the time
        # since the last one landed is split evenly between them.
        each = max(0.0, now - p.segment_start) / len(done)
        for a in done:
            c.execute("UPDATE sponsor_work SET proven_at=?, seconds=?, usd_estimate=?, outcome='proven'"
                      " WHERE id=? AND outcome IS NULL", (now, each, each * p.rate(self.ceiling) / 3600.0, a["row"]))
        c.commit()
        p.assigned = [a for a in p.assigned if a not in done]
        p.segment_start = p.last_progress = now

    def _identity(self, sid):
        """The sponsorship's key, created on first use and registered with the coordinator before any pod gets it."""
        if sid not in self._identities:
            name = None if sid is None else self.coord.sponsorship(sid)["name"]
            ident = identity(self.identity_home, sid, name)
            register_key(self.coord, ident, sid)
            self._identities[sid] = ident
        return self._identities[sid]

    def _busy(self):
        return {a["height"] for p in self._live() for a in p.assigned}

    def _assign(self, c, now):
        for p in [p for p in self._live() if p.state == "idle"]:
            todo = pending_work(self.coord, self._busy(), self.trial)
            if not todo:
                self._terminate(c, p, "nothing left to prove")
                continue
            chunk = todo[:self.blocks_per_pod]
            # Everything the coordinator must agree to comes first -- keys registered, sponsorships `proving` --
            # so a refusal leaves nothing half-assigned in the log.
            idents = {sid: self._identity(sid) for sid in {sid for sid, _ in chunk}}
            for sid in sorted({sid for sid, _ in chunk if sid is not None}):
                self.coord.proving(sid)
            items, work = [], []
            for sid, h in chunk:
                ident = idents[sid]
                cur = c.execute("INSERT INTO sponsor_work(sponsorship_id, height, pod_id, gpu_type, cost_per_hr,"
                                " assigned_at, pubkey) VALUES(?,?,?,?,?,?,?)",
                                (sid, h, p.id, p.gpu_type, p.rate(self.ceiling), now, ident["pubkey"]))
                items.append({"sid": sid, "height": h, "row": cur.lastrowid})
                work.append({"height": h, **ident})
            c.commit()
            p.assigned, p.state = items, "working"
            p.segment_start = p.last_progress = now
            p.finished_since = None
            try:
                self.runner.start(p, work)
            except StartFailed as e:
                # This pod could not start its work: terminate it and let the blocks go back to the queue for
                # another pod. Anything else still stops the run, with every pod terminated.
                self._terminate(c, p, f"could not start work: {e}", outcome="failed")
                continue
            self.log(f"{p.name}: proving {len(chunk)} block(s) from {chunk[0][1]}")

    def _launch(self, c, now):
        waiting = [p for p in self._live() if p.state in ("waiting_ssh", "booting", "idle")]
        todo = pending_work(self.coord, self._busy(), self.trial)
        want = math.ceil(len(todo) / self.blocks_per_pod) - len(waiting)
        while want > 0 and now >= self._capacity_at:
            if len(self._live()) >= self.max_pods:
                break
            if self.hourly() + self.ceiling > self.max_usd_per_hour + 1e-9:
                break
            if self.spend(now) + (self.hourly() + self.ceiling) * self.budget_lead_s / 3600.0 >= self.max_usd:
                break
            self._n += 1
            try:
                info = self.api.deploy(f"{self.prefix}{self._n}", self.runner.ssh_pubkey)
            except RunPodError as e:
                self._refusals += 1
                self._capacity_at = now + self.capacity_backoff_s
                if self._refusals >= RUNPOD_REFUSALS_MAX:
                    self.stop_reason = "runpod"
                    self.log(f"RunPod refused the deploy request {self._refusals} times in a row ({e}): stopping")
                else:
                    self.log(f"RunPod refused the deploy request ({e}); trying again later")
                break
            self._refusals = 0
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
        # A block can land after the last look: count it as proven, not as `outcome`. Asking the coordinator must
        # never stop a pod being terminated: if it cannot answer, the blocks are logged as `outcome` and
        # reconcile_work corrects them on a later run.
        landed = []
        if not _CONTROL_IGNORE_LANDED and p.assigned:
            try:
                cov = covered(self.coord, [a["height"] for a in p.assigned])
                landed = [a for a in p.assigned if a["height"] in cov]
            except CoordError as e:
                self.log(f"could not ask the coordinator which of {p.name}'s blocks landed ({e})")
        if landed:
            self._record_proven(c, p, landed, self.clock())
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


def plan_text(coord, db_path=None, trial=None):
    rows = queue(coord)
    lines = plan(rows)
    todo = pending_work(coord, (), trial)
    if trial is not None:
        bad = trial_refusals(coord, trial)
        lines.append(f"trial: {len(trial)} block(s), {len(bad)} refused" + ("" if not bad else ": " + "; ".join(bad[:5])))
    elif not rows:
        lines.append("no held sponsorships in the queue")
    lines.append(f"{len(todo)} block(s) still to prove")
    waiting = len(pending_work(coord, (), trial, need_bundle=False)) - len(todo)
    if waiting:
        lines.append(f"{waiting} more block(s) wait for bundles: no pod can prove them until the bridge builds them")
    path = db_path or bot_db_path()
    if not path or not os.path.exists(path):
        if todo:
            lines.append("projected cost: NOT MEASURED (no cost log yet; run `trial` first)")
        return lines
    c = connect(path, readonly=True)
    try:
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

def connect_path():
    path = bot_db_path()
    if not path:
        raise SystemExit("sponsor_bot: set SPONSOR_BOT_DB, or SPONSOR_BOT_HOME (the cost log is bot.db there)")
    return path


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
    sub.add_parser("bot-key", help="create the bot's API key if needed and print its public half")
    il = sub.add_parser("import-log", help="copy the cost log from a copy of the coordinator's database")
    il.add_argument("source")
    a = ap.parse_args(argv)
    cmd = a.cmd or "plan"

    if cmd == "bot-key":
        path = Coord.key_path()
        if not path:
            raise SystemExit("sponsor_bot: set SPONSOR_BOT_KEY_FILE or SPONSOR_BOT_HOME")
        print(bot_key(path))
        return 0
    if cmd == "import-log":
        c = connect()
        try:
            w, p = import_log(a.source, c)
        finally:
            c.close()
        print(f"imported {w} work row(s) and {p} pod(s)")
        return 0
    if cmd == "plan":
        try:
            print("\n".join(plan_text(Coord.from_env())))
        except CoordError as e:
            raise SystemExit(f"sponsor_bot: {e}")
        return 0
    if cmd == "report":
        c = connect()
        try:
            ensure_tables(c)
            # The report is the bot's own figures; checking them against the coordinator is a bonus, not a need.
            try:
                fixed = reconcile_work(c, Coord.from_env())
            except (CoordError, SystemExit) as e:
                fixed = 0
                print(f"could not check the log against the coordinator, so nothing was corrected: {e}")
            if fixed:
                print(f"corrected {fixed} work row(s) whose block was proven before its pod was stopped")
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
    coord = Coord.from_env()
    if not a.live:
        try:
            print("\n".join(plan_text(coord, None, trial)))
        except CoordError as e:
            raise SystemExit(f"sponsor_bot: {e}")
        print("sponsor_bot: dry run; add --live with --max-pods, --max-usd and --max-usd-per-hour to rent GPUs",
              file=sys.stderr)
        return 2
    missing = [f for f, v in (("--max-pods", a.max_pods), ("--max-usd", a.max_usd),
                              ("--max-usd-per-hour", a.max_usd_per_hour)) if v is None]
    if missing:
        raise SystemExit("sponsor_bot: live mode refuses to start without " + ", ".join(missing))
    try:
        bot = Bot(connect_path(), None, None, coord=coord, max_pods=a.max_pods, max_usd=a.max_usd, max_usd_per_hour=a.max_usd_per_hour,
                  pod_price_ceiling=a.pod_price_ceiling, stall_s=a.stall_min * 60, blocks_per_pod=a.blocks_per_pod,
                  sleep_s=a.tick, trial=trial)
    except BotRefused as e:
        raise SystemExit(f"sponsor_bot: {e}")
    bot.runner = SshRunner.from_env().prepare()
    bot.api = RunPod.from_env()
    bot.identity_home = bot.runner.home
    install_signal_handlers()
    try:
        reason = bot.run()
    except BotRefused as e:
        raise SystemExit(f"sponsor_bot: {e}")
    c = connect(bot.db_path, readonly=True)
    try:
        print(format_report(report(c)))
    finally:
        c.close()
    if bot.unconfirmed:
        return 4
    if reason == "runpod":
        return 5
    if reason == "coordinator":
        return 6
    return 3 if reason == "budget" else 0


if __name__ == "__main__":
    sys.exit(main())
