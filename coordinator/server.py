#!/usr/bin/env python3
"""
Hazync Proof Party — coordinator service (MVP).

Hands out block ranges + witnesses, receives signed proof receipts, VERIFIES them (nobody can cheat —
a bad proof fails verification), records signed attribution in an open ledger, and serves the
data-driven dashboard. Stdlib only for the core; ed25519 signature checking uses `cryptography` if
present (else runs in dev mode, clearly flagged).

Run:  python3 server.py            # serves http://localhost:8899  (dashboard + /api)
Config via env:
  COORD_PORT=8899          COORD_DB=coordinator.db        COORD_WEB=./web
  TIP_HEIGHT=958301        RANGE_SIZE=1000                SEED_RANGES=60
  WITNESS_DIR=./witnesses  (per-range witness files: witness_<lo>-<hi>.json)
  HAZYNC_HOST=../prover/target/release/host              # for receipt verification (verify-range)
  VERIFY_MODE=real|mock    # 'mock' accepts any receipt (dev/testing without a GPU-proved receipt)
The full submit→verify→credit loop is real; VERIFY_MODE=mock only stubs the STARK check so the rest
can be tested without a GPU.
"""
import os, json, sqlite3, hashlib, subprocess, base64, time, threading, tarfile, io, re, unicodedata, secrets, bisect, math
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
from urllib.parse import urlparse, parse_qs

PORT       = int(os.environ.get("COORD_PORT", "8899"))
BIND       = os.environ.get("COORD_BIND", "0.0.0.0")   # set to 127.0.0.1 when behind a reverse proxy
DB         = os.environ.get("COORD_DB", "coordinator.db")
WEB        = os.environ.get("COORD_WEB", os.path.join(os.path.dirname(__file__), "web"))
TIP_FLOOR  = int(os.environ.get("TIP_HEIGHT", "958301"))  # a FLOOR now, not the answer — see chain_tip()
TIP_TTL    = float(os.environ.get("TIP_CACHE_TTL", "300"))
# Where the node's REAL height is published. The coordinator runs as `hazync` and bitcoind's datadir is
# root-only (mode 700, cookie 600), so it cannot ask the node itself; a small root-side timer writes the
# height here instead (see deploy/hazync-node-tip.*). Absent or stale -> we fall back to the floor,
# which is exactly the behaviour that existed before this file was consulted at all.
TIP_FILE     = os.environ.get("TIP_FILE", "/var/lib/hazync/node_tip")
TIP_FILE_AGE = float(os.environ.get("TIP_FILE_MAX_AGE", "3600"))   # older than this = not to be trusted
RANGE_SIZE = int(os.environ.get("RANGE_SIZE", "1000"))
SEED       = int(os.environ.get("SEED_RANGES", "60"))
WITNESS    = os.environ.get("WITNESS_DIR", os.path.join(os.path.dirname(__file__), "witnesses"))
BRIDGE_DIR = os.environ.get("HAZYNC_BRIDGE_OUT", "")   # archive-node bundle dir (co-located); serves bundle_<n>.json

# ── How high does this coordinator go? ────────────────────────────────────────────────────────────
#
# This used to be one constant, `TIP_HEIGHT`, hardcoded to a chain height. That is a value which goes
# stale at ~144 blocks a day by construction, and it was answering three questions that do not have
# the same answer:
#
#   1. Which range ids are ACCEPTABLE on submission?  Should be generous. Rejecting a valid proof of a
#      real block because our constant lagged the chain is the worst failure of the three.
#   2. Which blocks may we HAND OUT?                  The honest ceiling is what the bridge can serve.
#                                                     Offering work we cannot supply a bundle for just
#                                                     burns a contributor's time.
#   3. What is the progress DENOMINATOR?              Public, so it should not silently overstate.
#
# So: scan for the highest block we can actually serve, cache it, and derive both answers from that.
#
# `chain_tip()` is floored at TIP_HEIGHT and used for (1) and (3). The floor matters — without it a
# bridge that is still backfilling would shrink the valid-id window under submissions that are already
# in flight, and an unmounted bridge directory would take the board to zero.
#
# `provable_tip()` is NOT floored and is used for (2). If we cannot serve a single bundle it returns 0
# and `pick` honestly reports that it has nothing, rather than handing out a height that will fail.
_tip_cache = {"t": 0.0, "v": None}
_tip_lock  = threading.Lock()

def _servable_high(force=False):
    """Highest block with a bundle (or legacy witness) on local disk, or None. Cached for TIP_TTL.

    One `scandir` per directory rather than a stat per height: the bundle set is ~220,000 files and a
    per-height probe would be O(chain). Not a `max()` over a listing comprehension either — the point
    is to touch each name once and keep no list.
    """
    now = time.time()
    with _tip_lock:
        if not force and _tip_cache["v"] is not None and now - _tip_cache["t"] < TIP_TTL:
            return _tip_cache["v"]
    hi = None
    for d, pre in ((BRIDGE_DIR, "bundle_"), (WITNESS, "block_")):
        if not d:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    n = e.name
                    if n.startswith(pre) and n.endswith(".json"):
                        core = n[len(pre):-5]
                        if core.isdigit():
                            v = int(core)
                            if hi is None or v > hi:
                                hi = v
        except OSError:
            continue          # missing or unreadable directory is "nothing here", not a crash
    with _tip_lock:
        _tip_cache.update(t=now, v=hi)
    return hi

def node_tip():
    """The archive node's real height, or None if we cannot currently trust an answer.

    Published to a file by a root-side timer because bitcoind's datadir is not readable by the user this
    process runs as. Staleness is the whole point of the check: if the writer dies, the last number it
    left behind would otherwise freeze the denominator at a value that looks live and is not — which is
    the failure this function exists to end, just with a different constant."""
    try:
        st = os.stat(TIP_FILE)
        if time.time() - st.st_mtime > TIP_FILE_AGE:
            return None                       # writer has stopped; prefer an honest fallback to a fossil
        with open(TIP_FILE) as f:
            v = int(f.read().strip())
        return v if v > 0 else None
    except (OSError, ValueError):
        return None                           # missing, unreadable or garbage — same as never configured

def chain_tip():
    """Exclusive upper bound for range-id validation and the progress denominator. Never below TIP_HEIGHT.

    Takes the highest of three answers, because each one is a lower bound on the truth and none of them
    is reliably the truth on its own: the node's height when a fresh one is published, whatever the
    bridge can serve, and the compiled-in floor. A hardcoded floor used alone goes stale the day it is
    written — the board reported a chain height of 958,301 while the node sat at 962,795 — and it
    silently overstates progress, which point (3) above says it must not do."""
    h = _servable_high()
    n = node_tip()
    return max(TIP_FLOOR, 0 if h is None else h + 1, 0 if n is None else n + 1)

def provable_tip():
    """Exclusive upper bound for ALLOCATION. Zero when we cannot serve anything at all."""
    h = _servable_high()
    return 0 if h is None else h + 1
# Bulk bundle sync (#69). Seeding a new coordinator from a peer means ~220,000 bundles; one request
# each is not a sync, it is a denial of service you perform on yourself. The cap is per REQUEST, not
# per operator — a client walks it in chunks — and defaults to one RANGE_SIZE so a chunk is the same
# unit everything else here is measured in.
BULK_MAX   = int(os.environ.get("BULK_MAX", str(RANGE_SIZE)))
HOST_BIN   = os.environ.get("HAZYNC_HOST", "")
VERIFY     = os.environ.get("VERIFY_MODE", "mock" if not HOST_BIN else "real")
STATE_DIR  = os.environ.get("COORD_STATE", os.path.join(os.path.dirname(__file__), "state"))
PROOFS_DIR = os.environ.get("COORD_PROOFS", os.path.join(os.path.dirname(__file__), "proofs"))  # kept, downloadable
SPINE_DIR  = os.environ.get("COORD_SPINE", os.path.join(os.path.dirname(__file__), "spine"))    # the genesis-anchored head
GENESIS_TIP = os.environ.get("GENESIS_TIP", "6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000")


def is_genesis_anchored(in_tip, lo):
    """Does this range descend from the real genesis, rather than merely being a valid transition?

    ONE definition, used by both the frontier rule (which decides what may advance the chain) and the
    `anchored` label reported to clients (#59). They were written out separately, and two copies of a
    security-relevant predicate drift — the dangerous direction being a label that says "anchored"
    for something the frontier would refuse.

    Sound because `verify-any` pins the full genesis in-boundary (`assert_genesis_in_boundary`)
    whenever `in_tip` is the genesis tip, so a range cannot fabricate its way into this by asserting a
    genesis in-tip with an invented UTXO set or difficulty. `lo == 1` is required as well: block 0 is
    unprovable, so a genesis-descended range starts at 1.
    """
    try:
        return in_tip == GENESIS_TIP and int(lo) == 1
    except (TypeError, ValueError):
        return False

# Block 0 is the most famous block in Bitcoin, so people WILL try to prove it, submit it and sponsor it.
# It cannot be proved: it is the in-boundary every range proof is pinned to (`is_genesis_anchored`), in
# the same way Bitcoin Core writes it into chainparams rather than validating it. Every entry point says
# so in the same words, and refuses BEFORE doing any work -- no DB row, no signature check, no STARK
# verification -- so the curious cost the board nothing.
GENESIS_MESSAGE = ("Block 0 is the genesis block. It is the fixed starting point every Hazync proof is anchored "
                   "to -- Bitcoin itself never validates it, it is written into the software -- so there is "
                   "nothing to prove and no proof of it can exist. Proving starts at block 1.")

def genesis_refusal(lo, hi, what="Submit"):
    """(400, body) for any range that includes block 0, else None. `what` names the retry."""
    if lo != 0:
        return None
    msg = GENESIS_MESSAGE + (f" {what} blocks 1 to {hi} instead." if hi >= 1 else "")
    return 400, {"error": msg, "genesis": True}

# A claim expires this long after it is TAKEN — not after a heartbeat stops, because there are no
# heartbeats. A worker that dies mid-block leaves nothing to reap; the block simply reopens on its own.
CLAIM_TTL  = int(os.environ.get("CLAIM_TTL", "3600"))    # 1 hour, then anyone may take it
CLAIM_MAX  = int(os.environ.get("CLAIM_MAX", "86400"))   # hard cap: release a claim after this long regardless
# #296: a claim that has NEVER heartbeat is released after this, not after CLAIM_TTL.
#
# Liveness is COALESCE(last_beat, claimed_at), which is right and deliberate — workers predating the
# beat send none, and falling back to claimed_at keeps them working (#251). But it gives a claim that
# has never beaten the full hour, identical to one being actively proved. Measured on the live board
# 2026-09-13: 237 blocks held by two contributors who were not proving them, while the most productive
# prover on the board held TWO claims, because a healthy claim turns over in seconds.
#
# 600s is not a guess. Measured over 55,920 successful ranges, claim -> VERIFIED is p50 27s, p90 101s,
# p99 319s — so this is nearly double the p99 of a whole completed prove, and the grace only has to
# cover claim -> FIRST beat, which is shorter still: the beat is progress-gated and segment 1 lands in
# about 4s even on a 1,012-segment block. The headroom is for a slow witness fetch on a poor link.
#
# ⚠ Releasing a claim CANCELS NOTHING. Submission is free-running, so a worker that finishes after its
# claim lapsed still submits successfully; the cost of being wrong here is duplicate work, not lost
# work. That is what makes a short grace safe.
CLAIM_GRACE = int(os.environ.get("CLAIM_GRACE", "600"))  # never-beaten claims: released after this
# Live claims one key may hold at once (0: no limit). A worker proves one block at a time, so an honest key
# holds about one per GPU; measured 2026-09-14, no proving key held more than 1. `ghost:dda215` held 26 to 61
# blocks it never proved, re-claiming in bursts as CLAIM_GRACE released them: another contributor's worker on
# the same IP proved 9,116 of its 9,133 claimed blocks, and the frontier waited over an hour behind one.
CLAIM_OPEN_MAX = int(os.environ.get("CLAIM_OPEN_MAX", "4"))
# Seconds before a key may re-take a block its OWN claim let lapse without a single beat (0: straight away). The
# grace (#296) frees such a block early so OTHER workers can take it; the same key must not simply take it back,
# or one unworked block consumes that key and nobody else is offered it (the 39,318 rule). Measured 2026-09-14:
# `ghost:dda215` re-claimed frontier block 67,532 every time it lapsed, for over three hours.
CLAIM_RETAKE_WAIT = int(os.environ.get("CLAIM_RETAKE_WAIT", "3600"))
# A claim is LIVE (it holds its block) while it beats within CLAIM_TTL, or has never beaten and is inside
# CLAIM_GRACE, and is younger than CLAIM_MAX. One definition, for the blocks that are held and for the cap.
LIVE_CLAIM_SQL = ("status='claimed' AND COALESCE(last_beat, claimed_at) > ? AND claimed_at > ?"
                  " AND (last_beat IS NOT NULL OR claimed_at > ?)")


def _live_claim_args(now):
    return (now - CLAIM_TTL, now - CLAIM_MAX, now - CLAIM_GRACE)
# How far a signed beat's timestamp may sit from ours. It bounds REPLAY of a captured beat, so it
# wants to be small; it also has to absorb ordinary clock drift on a contributor's box plus request
# latency, so it cannot be tiny. Two minutes is comfortably above NTP-corrected drift and well under
# CLAIM_TTL, so a replayed beat can extend an abandoned claim by at most this much.
BEAT_SKEW  = int(os.environ.get("BEAT_SKEW", "120"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))  # park a range as 'failed' after this many BLOCK-implicating failures
MAX_ENV_FAILURES = int(os.environ.get("MAX_ENV_FAILURES", "12"))  # separate, looser cap for environmental (capacity) failures

# Failure signatures that say something about the BOX, not the block. An out-of-memory on an
# oversubscribed GPU is not evidence that a block is unprovable — on 2026-07-28 block 29664 failed
# repeatedly this way and then proved perfectly once worker count dropped from 4 to 2. Counting those
# toward MAX_ATTEMPTS would park good blocks during any capacity incident, which is precisely backwards:
# the whole point of parking is to stop burning GPU on blocks that CANNOT be proved.
#
# These still count, against a looser cap, so a permanently mis-sized box cannot loop forever in silence.
_ENV_ERR = ("out of memory", "oom", "cudaerror", "cuda error", "hash_rows",
            "illegal memory access", "allocation failed", "no cuda-capable device",
            # A deliberate shutdown says nothing about the block either. Workers are restarted routinely
            # (config changes, redeploys) and each restart releases whatever was in flight, so counting
            # those would park good blocks purely for being unlucky enough to be mid-prove at the time.
            "received signal", "keyboardinterrupt", "systemexit")

def is_env_failure(err):
    e = (err or "").lower()
    return any(s in e for s in _ENV_ERR)
# Width of a claim-next assignment, in blocks. 1 = per-block (the default and the safe operating point).
#
# This replaces the earlier boolean SERVE_WIDE, which implicitly meant RANGE_SIZE (1000). That was tried
# in production on 2026-07-28 and STALLED THE BOARD: a 1000-block range is a ~67-minute commitment, a
# hard failure anywhere in it discards the whole range, and with OOMs occurring regularly not one range
# ever completed — throughput went from 2,220 blocks/hr to 1 block in 40 minutes while the GPUs stayed
# busy. The frontier cannot advance until a range COMPLETES, so a width you cannot reliably finish is
# worse than no widening at all.
#
# The fold-distribution argument for widening still holds (#28) — it is the reliability that has to be
# earned. Failure probability scales with duration, so the width must be short enough to survive the
# board's actual failure rate. Raise it deliberately and watch a range COMPLETE before trusting it.
try:
    CLAIM_WIDTH = max(1, int(os.environ.get("CLAIM_WIDTH", "1")))
except ValueError:
    CLAIM_WIDTH = 1
MAX_BODY   = int(os.environ.get("MAX_BODY", str(8 << 20)))   # reject POST bodies larger than this (8 MiB)
MAX_HANDLE = int(os.environ.get("MAX_HANDLE", "48"))         # cap contributor handle length
RATE_MAX   = int(os.environ.get("RATE_MAX", "120"))          # max writes (POST) per IP per window
RATE_MAX_GET = int(os.environ.get("RATE_MAX_GET", "600"))    # max reads (GET /api/*) per IP per window
RATE_WINDOW= int(os.environ.get("RATE_WINDOW", "60"))        # rate-limit window (seconds)
RATE_MAP_MAX = int(os.environ.get("RATE_MAP_MAX", "50000"))  # bound the rate map (evict stale keys past this)
STATE_TTL  = float(os.environ.get("STATE_CACHE_TTL", "1.5")) # coalesce /api/state recomputes under load
# Only honour X-Forwarded-For when the direct peer is a known proxy — otherwise any client could spoof
# it to bypass the per-IP rate limit and grow the rate map without bound.
TRUSTED_PROXIES = set(x.strip() for x in os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if x.strip())
# Reserved / impersonation handles that may not be registered on the public board (normalised: letters
# and digits only, lowercased). A takedown list of pubkeys lives in MOD_BLOCK_FILE (see blocked_pubkeys).
HANDLE_DENY = set(x.strip().lower() for x in os.environ.get("HANDLE_DENY",
    "satoshi,satoshinakamoto,admin,administrator,official,bitcoinghost,bitcoinghostofficial,hazync,"
    "moderator,mod,root,system,team,support,staff").split(",") if x.strip())
MOD_BLOCK_FILE = os.environ.get("MOD_BLOCK_FILE", os.path.join(os.path.dirname(__file__), "mod_block.txt"))

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    HAVE_ED = True
except Exception:
    HAVE_ED = False

_lock = threading.Lock()
_rate = {}          # (kind, ip) -> [timestamps] sliding window, guarded by _rate_lock
_rate_lock = threading.Lock()
# vranges is served separately (#35): it is ~99.9% of the old /api/state payload and changes only when a
# range is verified, so a 10s cache + ETag turns a re-poll into a 304 instead of re-shipping the index.
# 120 s, not 10. A rebuild of the full index takes ~25 s at 69k rows (5.8 MB), so a 10 s cache under
# ordinary board traffic was rebuilding back to back; see vranges_cached. Also the TTL of /api/spine/segments.
VRANGES_TTL = float(os.environ.get("VRANGES_CACHE_TTL", "120"))
_state_lock = threading.Lock()
# Bound concurrent STARK verifications. submit() runs verify-any OUTSIDE _lock (so it can't stall
# claims/heartbeats), but without a cap a burst of submits would spawn unlimited concurrent `host
# verify-any` subprocesses (each up to 120s + a multi-MiB receipt) and exhaust this small box's CPU/RAM.
_verify_sem = threading.Semaphore(int(os.environ.get("VERIFY_CONCURRENCY", str(max(1, (os.cpu_count() or 2))))))

# Hosts exempt from rate limiting (comma-separated IPs). A party's OWN provers legitimately issue far
# more claim/heartbeat/submit traffic from a single IP than any limit sensible for the public, and the
# tempting fix — raising RATE_MAX to a huge number — disables the limiter for everyone, on a public
# write endpoint. Exempt the known prover instead and keep real limits for the rest.
RATE_EXEMPT = {x.strip() for x in os.environ.get("RATE_EXEMPT", "").split(",") if x.strip()}

def rate_ok(ip, kind="w", limit=RATE_MAX):
    """Sliding-window per-IP limiter (kind 'w'=writes/POST, 'r'=reads/GET). True if within budget.
    The map is bounded: once it grows past RATE_MAP_MAX we evict keys whose window has fully aged out,
    so a spoofed/rotating source key can't grow it without limit."""
    if ip in RATE_EXEMPT:
        return True
    now = time.time()
    with _rate_lock:
        if len(_rate) > RATE_MAP_MAX:
            for k in [k for k, v in _rate.items() if not v or now - v[-1] >= RATE_WINDOW]:
                _rate.pop(k, None)
        key = (kind, ip)
        q = [t for t in _rate.get(key, ()) if now - t < RATE_WINDOW]
        if len(q) >= limit:
            _rate[key] = q
            return False
        q.append(now); _rate[key] = q
        return True

def note_client_version(c, pk, body):
    """Record the release a contributor's worker is running (#293). Advisory: nothing is refused for it.

    Written on claim AND on submit, because the two answer different questions. A worker that claims
    and never submits is the failure this exists to diagnose — block 39,413 pinned the frontier for
    five hours with claims, heartbeats, zero submissions and no error — so recording only on submit
    would be silent for exactly the case that needs it.

    Never overwrites a known version with an unknown one: a contributor running several boxes may
    have one on an old CLI that sends no User-Agent at all, and letting that erase what the others
    reported would make the field flicker between a version and nothing."""
    v = (body or {}).get("_client_version")
    if not v:
        return
    try:
        c.execute("UPDATE contributors SET last_version=?, last_version_at=? WHERE pubkey=?",
                  (str(v)[:32], time.time(), pk))
    except Exception:
        pass          # a column that is not there yet must never cost someone their proof

def blocked_pubkeys():
    """Takedown list: pubkeys (hex, lowercased) to hide from the public board. One per line in
    MOD_BLOCK_FILE ('#' comments allowed). Re-read each call so a moderator edit takes effect without a
    restart; the file is tiny. Missing file → empty set (no moderation)."""
    try:
        with open(MOD_BLOCK_FILE) as f:
            return set(l.strip().lower() for l in f if l.strip() and not l.startswith("#"))
    except Exception:
        return set()

def handle_reserved(h):
    """True if a handle normalises (letters+digits, lowercased) to a reserved/impersonation name that
    may not be registered — blocks 'Satoshi Nakamoto', 'bitcoinghost official', 'admin', etc."""
    norm = "".join(ch for ch in str(h or "").lower() if ch.isalnum())
    return norm in HANDLE_DENY

# Handles beginning "SPONSOR" belong to the sponsor proving bot's registered keys (`sponsor_keys`): the
# bot submits a sponsorship's blocks as "SPONSOR: <name>". Checked on a folded form so the obvious fakes
# are caught too: NFKC (fullwidth letters), lowercase, letters and digits only, and the digits that pass
# for the prefix's letters (0 for o, 5 for s). "SPONSOR :", "s.p.o.n.s.o.r" and "Sp0nsor" all fold to
# "sponsor...". NOT caught: look-alike letters from other scripts (a Cyrillic о). Also refused: any
# ordinary handle that happens to start that way ("Sponsorship fan"), which is the price of the rule.
SPONSOR_HANDLE_PREFIX = "SPONSOR: "
_HANDLE_FOLD = str.maketrans({"0": "o", "5": "s"})

def _handle_fold(h):
    t = unicodedata.normalize("NFKC", str(h or "")).lower()
    return "".join(ch for ch in t if ch.isalnum()).translate(_HANDLE_FOLD)

def sponsor_key_registered(pubkey):
    if not pubkey:
        return False
    try:
        c = db()
        try:
            return c.execute("SELECT 1 FROM sponsor_keys WHERE pubkey=?", (str(pubkey).lower(),)).fetchone() is not None
        finally:
            c.close()
    except sqlite3.OperationalError:          # a database from before the table: nothing is registered
        return False

def handle_cap(pubkey):
    """MAX_HANDLE, except a registered sponsor key's handle, which fits "SPONSOR: " and a whole name."""
    return max(MAX_HANDLE, len(SPONSOR_HANDLE_PREFIX) + SPONSOR_NAME_MAX) if sponsor_key_registered(pubkey) else MAX_HANDLE

def handle_refused(handle, pubkey):
    """handle_reserved, plus the sponsor prefix for any key the sponsor bot has not registered."""
    return handle_reserved(handle) or (_handle_fold(handle).startswith("sponsor") and not sponsor_key_registered(pubkey))

def clean_handle(h, cap=None):
    """A display handle: printable, trimmed, length-capped, and stripped of HTML-significant characters
    (< > & " ') so it is safe to render on the public dashboard. This is the single server-side choke
    point (CLI, API, and any future consumer all pass through it); the dashboard also escapes at every
    render sink, so the two layers are defence-in-depth against stored XSS. `cap` defaults to MAX_HANDLE
    (handle_cap gives a registered sponsor key room for a whole name)."""
    h = "".join(ch for ch in str(h or "anon")
                if ch.isprintable() and ch not in "<>&\"'").strip()
    return (h[:(cap or MAX_HANDLE)] or "anon")

def is_hex(s, nbytes):
    """True if s is exactly nbytes of lowercase/upper hex (ed25519 pubkey=32, sig=64)."""
    try:
        return isinstance(s, str) and len(s) == nbytes * 2 and bytes.fromhex(s) is not None
    except Exception:
        return False

DB_BUSY_TIMEOUT = float(os.environ.get("DB_BUSY_TIMEOUT", "15"))

def db():
    c = sqlite3.connect(DB, timeout=DB_BUSY_TIMEOUT)
    c.row_factory = sqlite3.Row
    return c

def raise_open_file_limit(want=65536):
    """Lift the soft open-file limit towards the hard one, and say what it ended up as.

    systemd hands a service a SOFT limit of 1024 however high the hard limit is (524288 on the box), and
    Python never raises it. Every in-flight request holds a socket, and every one touching the database
    holds the DB, its -wal and often a temp file: on 2026-09-13 a stampede of /api/vranges rebuilds took
    the process to exactly 1024 descriptors, after which `sqlite3.connect` failed with "unable to open
    database file" and every claim and submit timed out. Raising the soft limit is always permitted."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception as e:                    # not fatal: the service still runs at its old limit
        print(f"[hazync-coordinator] WARNING: could not raise the open-file limit: {e}", flush=True)
        return None

def init_db():
    c = db()
    # #265: WAL, so readers never block writers. In the default rollback journal every /api/state,
    # /api/meta and frontier read holds a SHARED lock that stops claim/submit/beat from writing; under a
    # 10-card fleet the reads overlapped continuously, writers starved while holding _lock, and the board
    # API jammed (148 threads, 504s). journal_mode=WAL is persistent in the file; setting it here means a
    # fresh DB -- or one restored from a pre-WAL backup -- cannot bring the rollback journal back.
    mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        print(f"[hazync-coordinator] WARNING: journal_mode={mode}, not WAL -- reads will block writes (#265)", flush=True)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS ranges(
        id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER,
        status TEXT DEFAULT 'open',            -- open | claimed | verified
        assignee TEXT, handle TEXT,
        receipt_sha TEXT, claimed_at REAL, verified_at REAL, last_beat REAL);
      CREATE TABLE IF NOT EXISTS contributors(
        pubkey TEXT PRIMARY KEY, handle TEXT, blocks INTEGER DEFAULT 0, first_seen REAL,
        -- #293: the client release last seen from this contributor, and when. Nullable on purpose:
        -- every worker in the field predates this, so "unknown" is the honest answer for them and
        -- must not be confused with "old". Advisory only — nothing is ever refused for its version.
        last_version TEXT, last_version_at REAL);
      CREATE TABLE IF NOT EXISTS submissions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, range_id TEXT, pubkey TEXT, handle TEXT,
        receipt_sha TEXT, sig TEXT, verified INTEGER, note TEXT, ts REAL);
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS vranges(
        id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, in_tip TEXT, out_tip TEXT,
        pubkey TEXT, handle TEXT, ts REAL, out_leaves INTEGER, range_work TEXT);
      -- #113 key rotation. APPEND-ONLY: no row here ever rewrites `vranges` or `submissions`, so the
      -- ledger still says exactly which key signed which proof and the audit trail is unchanged.
      -- Attribution is resolved at READ time by following the edges. `old_pubkey` is the PRIMARY KEY,
      -- which is what makes the graph a forest of simple paths: a key may rotate at most once, so it
      -- can never fork into two heads and leave "which one owns the blocks" ambiguous.
      CREATE TABLE IF NOT EXISTS rotations(
        old_pubkey TEXT PRIMARY KEY, new_pubkey TEXT NOT NULL,
        msg_ts REAL, sig_old TEXT, sig_new TEXT, created REAL);
    """)
    n = c.execute("SELECT COUNT(*) FROM ranges").fetchone()[0]
    if n == 0:
        rows = []
        for i in range(SEED):
            lo = i * RANGE_SIZE
            hi = lo + RANGE_SIZE - 1
            rows.append((f"{lo}-{hi}", lo, hi))
        c.executemany("INSERT INTO ranges(id,lo,hi) VALUES(?,?,?)", rows)
        print(f"[seed] created {SEED} ranges of {RANGE_SIZE} blocks (0..{SEED*RANGE_SIZE-1})")
    try: c.execute("ALTER TABLE ranges ADD COLUMN last_beat REAL")  # migrate older DBs
    except Exception: pass
    for col in ("attempts INTEGER DEFAULT 0", "env_failures INTEGER DEFAULT 0", "last_error TEXT",
                "last_failed_at REAL", "last_assignee TEXT",    # failure tracking
                "claim_nonce TEXT"):                             # #268: makes a retried claim idempotent
        try: c.execute(f"ALTER TABLE ranges ADD COLUMN {col}")
        except Exception: pass
    for col in ("out_leaves INTEGER", "range_work TEXT",
                "in_bhash TEXT", "out_bhash TEXT"):  # H7/S1: full-boundary continuity digest
        try: c.execute(f"ALTER TABLE vranges ADD COLUMN {col}")
        except Exception: pass
    # #293: the live board's contributors table predates these, and CREATE TABLE IF NOT EXISTS does
    # not add a column to a table that already exists — so the running coordinator would keep the old
    # shape and every read of last_version would raise. Migrated the same way as the columns above.
    for col in ("last_version TEXT", "last_version_at REAL"):
        try: c.execute(f"ALTER TABLE contributors ADD COLUMN {col}")
        except Exception: pass
    # Sponsorship (docs/SPONSORSHIP.md). A sponsorship paid at least its minimum HOLDS its blocks until
    # they are proven: coverage_and_held() keeps them out of every claim (SPONSOR_HOLD_SQL). Its name is
    # published only then too, and nothing can set a paid status until payments are connected. `requested` -> `invoiced` -> `paid` -> `proving` -> `proven`; a payment
    # below the minimum settles as `underpaid`; or `expired` / `cancelled` / `refunded`.
    # min_usd is the minimum in whole dollars and min_sats the sats it came to at the bitcoin price when
    # it was requested (the public rule compares against min_sats), pledged_sats what the sponsor said
    # they would pay, paid_sats what actually settled. The private status link is stored only as its sha256.
    c.executescript("""
      CREATE TABLE IF NOT EXISTS sponsorships(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lo INTEGER NOT NULL, hi INTEGER NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'requested',
        created_at REAL NOT NULL,
        min_usd INTEGER, min_sats INTEGER, pledged_sats INTEGER, paid_sats INTEGER,
        invoice_id TEXT, paid_at REAL, proven_at REAL, token_hash TEXT, note TEXT);
      CREATE INDEX IF NOT EXISTS sponsorships_span ON sponsorships(lo, hi);
      -- The sponsor proving bot's keys: one per sponsorship (sponsorship_id NULL for its trial key). A
      -- handle beginning "SPONSOR" is refused for any key that is not here, so nobody else can put a
      -- sponsor's name on the board. The bot inserts a key before any pod uses it.
      CREATE TABLE IF NOT EXISTS sponsor_keys(
        pubkey TEXT PRIMARY KEY, sponsorship_id INTEGER, handle TEXT NOT NULL, created_at REAL NOT NULL);
    """)
    # A table from before the minimum and the link (a preview copy made on 2026-09-13) has neither, and
    # CREATE TABLE IF NOT EXISTS leaves it as it is. Add what is missing before indexing token_hash.
    have = {r[1] for r in c.execute("PRAGMA table_info(sponsorships)")}
    for col, decl in (("min_usd", "INTEGER"), ("min_sats", "INTEGER"), ("pledged_sats", "INTEGER"),
                      ("paid_sats", "INTEGER"), ("token_hash", "TEXT")):
        if col not in have:
            c.execute(f"ALTER TABLE sponsorships ADD COLUMN {col} {decl}")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS sponsorships_token ON sponsorships(token_hash)")
    # claim() counts one key's live claims under the global lock (CLAIM_OPEN_MAX): an index, not a table scan.
    c.execute("CREATE INDEX IF NOT EXISTS ranges_assignee_status ON ranges(assignee, status)")
    c.commit(); c.close()

def parse_any_range(rid):
    """Validate a range id for SUBMISSION (not for claiming). Any `n` or `lo-hi` with lo <= hi < chain_tip().

    Claims are restricted to an aligned grid so two claim ids can never partially overlap — that is
    what parse_range enforces, and it is right for allocation. Submissions are a different question:
    **folding produces arbitrary widths by construction** (`[100..199] + [200..299] -> [100..299]`),
    so requiring grid alignment here rejects the output of `hazync fold` outright. It did: a folded
    [1..2] came back "invalid range id" after the fold had already been done.

    Accepting any range costs nothing, because the id is not what is trusted — verify_receipt pins
    the real [lo..hi] out of the receipt and rejects a submission whose id disagrees with it. An
    invented id buys a row that then fails verification.

    Still strict about SHAPE: every part must parse as an int, so this remains safe to use as the
    path sanitiser for /api/proof/<id>.
    """
    try:
        parts = [int(x) for x in str(rid).split("-")]
    except Exception:
        return None
    if len(parts) == 1:
        n = parts[0]
        return (n, n) if 0 <= n < chain_tip() else None
    if len(parts) != 2:
        return None
    lo, hi = parts
    if lo < 0 or hi >= chain_tip() or hi < lo:
        return None
    return (lo, hi)

def parse_range(rid):
    """Validate a claim id. Two accepted forms:
         'n'      → a single block n (any n in [0, chain_tip())) — 'I just want to do one block'.
         'lo-hi'  → a range, must be RANGE_SIZE-aligned and exactly RANGE_SIZE long.
       Aligned ranges and single blocks are the only shapes allowed, so two different claim
       ids can never partially overlap (no double-claim ambiguity). Returns (lo, hi)."""
    try:
        parts = [int(x) for x in str(rid).split("-")]
    except Exception:
        return None
    if len(parts) == 1:                                  # single block
        n = parts[0]
        return (n, n) if 0 <= n < chain_tip() else None
    if len(parts) != 2:
        return None
    lo, hi = parts
    if lo < 0 or hi >= chain_tip() or hi < lo:
        return None
    width = hi - lo + 1
    # Two accepted grids: the legacy RANGE_SIZE one (existing board ids) and the current CLAIM_WIDTH.
    # Both require alignment, so ids on the same grid can never PARTIALLY overlap. Full containment
    # across grids is still possible and is caught by overlapping(), which is interval-based.
    for g in {RANGE_SIZE, CLAIM_WIDTH}:
        if g > 1 and width == g and lo % g == 0:
            return lo, hi
    return None

_SRC_SHA = {"v": None}
def source_sha256():
    """sha256 of the server source this process is ACTUALLY running, exposed via /api/meta.

    Deployment drift is invisible otherwise. On 2026-07-28 the production coordinator was found on a
    stale branch (`fix/r1-hardening-rebaseline`, months of commits behind main) with UNCOMMITTED local
    edits to server.py and backup.sh — changes that existed nowhere in git. Nothing reported it, and a
    naive redeploy would have silently destroyed them.

    Hashing the file rather than shelling out to `git` is deliberate: a deployment need not be a git
    checkout, and `git describe` reports the checkout, not what the running process actually loaded.
    This catches an in-place edit that git status would show as clean if the file were untracked.

    Compare against the repo with scripts/check-deployment.sh."""
    if _SRC_SHA["v"] is None:
        try:
            with open(os.path.abspath(__file__), "rb") as fh:
                _SRC_SHA["v"] = hashlib.sha256(fh.read()).hexdigest()
        except Exception:
            _SRC_SHA["v"] = "unknown"                  # never fail a request over provenance reporting
    return _SRC_SHA["v"]

def overlapping(c, lo, hi, exclude_id):
    """Any live range whose block interval intersects [lo, hi], other than exclude_id.

    The old guard was ID-based: claim-next skipped ids already in ('claimed','verified','failed'). That
    is only sound while every claim is the same shape. It is not — parse_range accepts BOTH a single
    block and a RANGE_SIZE-aligned range, and those overlap: block 29664 sits inside 29000-29999, yet
    the two ids are different strings, so both could be claimed and proved at once. Nothing detected it.

    It has been latent only because claim-next exclusively serves single blocks, so the shapes are never
    mixed in practice. Serving wider ranges (#28) is exactly what would activate it — against the ~34k
    single-block receipts already on the board — so the check has to become interval-based first.

    Note 'failed' counts as live: a parked range still owns its interval, otherwise a wide range could
    be claimed straight over the top of the very block that is failing."""
    return [r["id"] for r in c.execute(
        "SELECT id FROM ranges WHERE status IN ('claimed','verified','failed') "
        "AND id != ? AND lo <= ? AND hi >= ?", (exclude_id, hi, lo))]

# ── peer coordinators (hazync#69) ─────────────────────────────────────────────────────────────────
#
# A second coordinator is a full peer — its own archive node, bridge and board — not a mirror. Two of
# them will hand the same height to different provers, which is WASTEFUL, not incorrect: the duplicate
# proof is perfectly valid, it just bought nothing. At one contributor that is invisible; at scale it
# is the whole problem.
#
# This is the cheap 80% of the fix: ask peers what they have already proven and stop offering those
# heights. No protocol, no consensus, no claim gossip — just an exclusion set. Collisions shrink to the
# in-flight window (someone proving a height a peer has claimed but not yet finished), which is bounded
# by one proof time rather than by the whole board.
#
# FAILS OPEN, deliberately and in both directions:
#   * no peers configured -> no fetch, no behaviour change at all. This is inert until someone opts in.
#   * a peer unreachable  -> its heights are simply not excluded, and local work continues.
# A coordinator that stalled because a PEER was down would be a worse availability story than the one
# this feature exists to improve. The cost of getting it wrong is a duplicate proof; the cost of
# blocking is an idle fleet.
#
# NOT trusted, and it does not need to be: an entry here only ever REMOVES a height from what we offer.
# A malicious peer's worst case is withholding work from our provers — a denial of service against
# ourselves, visible as an idle board — never accepting a proof we should have rejected. Nothing here
# touches verification, the frontier, or what lands on the board.
def bundle_path(blk):
    """The file that serves block `blk`, or None.

    Same precedence as /api/witness/<h>: the archive-node bundle first (in-boundary + real root_prev +
    inclusion proofs, provable with NO replay), then the legacy per-block witness. Kept as one function
    so the single and bulk endpoints cannot drift into serving different files for the same height.
    """
    for f in ([os.path.join(BRIDGE_DIR, f"bundle_{blk}.json")] if BRIDGE_DIR else []) \
             + [os.path.join(WITNESS, f"block_{blk}.json")]:
        if os.path.exists(f):
            return f
    return None


def bulk_plan(frm, count):
    """Which heights a bulk request will serve, and which it cannot. Pure — no I/O beyond existence.

    Returns (heights, missing, error). `error` is a string when the request itself is malformed, in
    which case the caller returns 400 rather than an empty archive: "you asked for something invalid"
    and "that range is genuinely empty" are different answers and a syncing client must be able to
    tell them apart. Silently returning nothing for a bad `from` would look like the end of the chain.
    """
    if frm is None or frm < 0:
        return [], [], "from must be a non-negative integer"
    if count is None or count < 1:
        return [], [], "count must be at least 1"
    if count > BULK_MAX:
        return [], [], f"count exceeds BULK_MAX ({BULK_MAX}) — request the range in chunks"
    heights, missing = [], []
    for h in range(frm, frm + count):
        (heights if bundle_path(h) else missing).append(h)
    return heights, missing, None


PEERS = [u.strip().rstrip("/") for u in os.environ.get("PEER_COORDINATORS", "").split(",") if u.strip()]
PEER_TTL = int(os.environ.get("PEER_TTL", "300"))
_peer_cache = {"t": 0.0, "heights": set()}

def peer_proven_heights():
    """Heights peers report as proven. Empty set when no peers, unreachable, or malformed."""
    if not PEERS:
        return set()
    now = time.time()
    with _state_lock:
        if now - _peer_cache["t"] < PEER_TTL:
            return _peer_cache["heights"]
    got = set()
    for base in PEERS:
        try:
            req = urllib.request.Request(f"{base}/api/vranges", headers={"User-Agent": "hazync-coordinator"})
            with urllib.request.urlopen(req, timeout=10) as r:
                # Bound the read: a peer (or something impersonating one) must not be able to make us
                # allocate unbounded memory just by answering.
                doc = json.loads(r.read(64 * 1024 * 1024).decode())
            for v in doc.get("vranges", []):
                lo, hi = int(v["lo"]), int(v["hi"])
                if 0 <= lo <= hi and hi - lo < 1_000_000:
                    got.update(range(lo, hi + 1))
        except Exception:
            continue          # unreachable or junk: contribute nothing, never raise
    with _state_lock:
        _peer_cache.update(t=now, heights=got)
    return got

_peer_busy_cache = {"t": 0.0, "heights": set()}
# A peer's IN-FLIGHT claims are advisory, so they get a much tighter cap than proven heights. A claim
# is one block or a small chunk; anything larger is not a claim we should honour, whether it comes from
# a bug or from a peer trying to reserve the chain.
PEER_BUSY_MAX_WIDTH = int(os.environ.get("PEER_BUSY_MAX_WIDTH", "10000"))
PEER_BUSY_MAX_TOTAL = int(os.environ.get("PEER_BUSY_MAX_TOTAL", "200000"))

def peer_busy_heights():
    """Heights peers say are CLAIMED right now — work in flight, not yet proven (hazync#69).

    `peer_proven_heights` stops us redoing FINISHED work. This stops us starting work someone else is
    doing at this moment, which is the rest of the collision window the issue describes.

    Three things make this safe to act on despite coming from an untrusted peer:

      * **Stale claims are ignored.** `/api/state` already marks a claim stale once its heartbeat
        exceeds CLAIM_TTL. An abandoned claim on a peer must not reserve a block here for an hour.
      * **It is capped**, per entry and in total. A peer cannot reserve the chain by reporting one
        enormous claim, by accident or otherwise.
      * **It is a PREFERENCE, not a veto** — see `pick`. If avoiding peer claims leaves nothing to do,
        we take the work anyway. Duplicate work is waste; an idle prover is also waste, and a peer
        must never be able to choose the second one for us.

    Fails open, like its sibling: an unreachable or malformed peer contributes nothing.
    """
    if not PEERS:
        return set()
    now = time.time()
    with _state_lock:
        if now - _peer_busy_cache["t"] < PEER_TTL:
            return _peer_busy_cache["heights"]
    got = set()
    for base in PEERS:
        try:
            req = urllib.request.Request(f"{base}/api/state?slim=1",
                                         headers={"User-Agent": "hazync-coordinator"})
            with urllib.request.urlopen(req, timeout=10) as r:
                doc = json.loads(r.read(64 * 1024 * 1024).decode())
            for b in doc.get("board", []):
                if b.get("status") != "claimed" or b.get("stale"):
                    continue
                lo, hi = int(b["lo"]), int(b["hi"])
                if 0 <= lo <= hi and hi - lo < PEER_BUSY_MAX_WIDTH:
                    got.update(range(lo, hi + 1))
                if len(got) > PEER_BUSY_MAX_TOTAL:
                    got = set()                 # implausible: treat the whole peer as uninformative
                    break
        except Exception:
            continue
    with _state_lock:
        _peer_busy_cache.update(t=now, heights=got)
    return got

def sync_from_peers(limit=200):
    """Pull proofs peers have that we do not, RE-VERIFY each, and adopt the ones that pass (hazync#69).

    This is what makes multiple coordinators easy rather than hard, and it is worth being explicit
    about why there is no consensus protocol here:

      * A proof is SELF-AUTHENTICATING. It verifies against METHOD_ID no matter who is holding it, so
        there is no canonical store to agree on — every coordinator simply keeps its own copy.
      * The frontier is a PURE FUNCTION of the verified set (`_frontier_chain`). Two coordinators
        holding the same set compute the same frontier by construction. Convergence is arithmetic,
        not agreement.

    So federation reduces to: fetch the peer's index, download what we lack, verify it OURSELVES, and
    store it. The union converges. No leader, no quorum, no clock.

    WE NEVER TRUST THE PEER. Every receipt goes through the same `verify_receipt()` a submission does —
    real STARK verification against our own METHOD_ID — and a range whose receipt does not prove what
    the peer claims is dropped. The worst a hostile peer can do is waste our bandwidth serving junk we
    reject; it cannot put anything on our board.

    That contract covers the RECEIPT. It does not cover the peer's other strings, and audit #3 (F-4)
    found the gap: the range id is peer-controlled and reaches a filesystem path. Every peer-supplied
    id is now shape-validated through `parse_any_range` before it is used for anything at all. If you
    add a new peer-supplied field here, validate it at the point of entry — this docstring's promise
    is about proofs, and it is not self-executing for everything else that arrives in the same JSON.

    Attribution is preserved: the peer reports the handle that earned it, and we record that rather
    than crediting ourselves. Adopting someone's proof is not the same as having proved it.

    `limit` bounds one pass so a fresh coordinator syncing a large peer makes steady progress instead
    of one enormous transaction.
    """
    if not PEERS:
        return {"adopted": 0, "rejected": 0, "peers": 0}
    c = db()
    have = {r["id"] for r in c.execute("SELECT id FROM vranges")}
    c.close()
    adopted = rejected = 0
    for base in PEERS:
        try:
            req = urllib.request.Request(f"{base}/api/vranges", headers={"User-Agent": "hazync-coordinator"})
            with urllib.request.urlopen(req, timeout=15) as r:
                doc = json.loads(r.read(64 * 1024 * 1024).decode())
        except Exception:
            continue                                   # peer down: nothing to do, never fatal
        for v in doc.get("vranges", []):
            if adopted + rejected >= limit:
                break
            rid = str(v.get("id") or f'{v["lo"]}-{v["hi"]}' if v.get("lo") != v.get("hi") else str(v.get("lo")))
            # AUDIT #3 F-4 — VALIDATE THE PEER'S id BEFORE IT IS USED FOR ANYTHING.
            #
            # `rid` is peer-controlled and reaches a URL, a SQL parameter and — the one that matters —
            # open(os.path.join(PROOFS_DIR, f"proof_{rid}.bin"), "wb"). A `/` or `..` in it is an
            # arbitrary file write. Today that is incidentally unreachable on Linux, because the
            # "proof_" prefix becomes the first path component and traversal past it needs a directory
            # literally named proof_* to exist, so resolution fails with ENOENT and the surrounding
            # `except` swallows it. That is luck, not a control: one stray mkdir away, on an input this
            # feature's own contract calls untrusted, in code that is not wired up yet and will be.
            #
            # parse_any_range requires every part to parse as an int, which is exactly the shape gate
            # the submit path already relies on to sanitise /api/proof/<id>. Same function, so the two
            # paths cannot diverge on what an id is allowed to be.
            if parse_any_range(rid) is None:
                rejected += 1
                continue
            if rid in have:
                continue
            try:
                preq = urllib.request.Request(f"{base}/api/proof/{rid}",
                                              headers={"User-Agent": "hazync-coordinator"})
                with urllib.request.urlopen(preq, timeout=60) as pr:
                    receipt = pr.read(MAX_BODY)
            except Exception:
                continue
            # The claimed range is the peer's word; verify_receipt checks the RECEIPT proves it.
            rng = {"id": rid, "lo": int(v["lo"]), "hi": int(v["hi"])}
            ok, _note, meta = verify_receipt(receipt, rng)
            if not ok or not meta:
                rejected += 1
                continue
            handle = clean_handle(v.get("handle") or "peer")
            with _lock:
                c = db()
                c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')",
                          (rid, rng["lo"], rng["hi"]))
                c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,"
                          "out_leaves,range_work,in_bhash,out_bhash)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rid, int(meta["lo"]), int(meta["hi"]), meta["in_tip"], meta["out_tip"],
                           "", handle, time.time(), meta.get("out_leaves", 0),
                           str(meta.get("range_work", "0")), str(meta.get("in_bhash", "")),
                           str(meta.get("out_bhash", ""))))
                c.commit(); c.close()
                _frontier_invalidate()        # #265: a new verified range can move the frontier
            try:
                os.makedirs(PROOFS_DIR, exist_ok=True)
                with open(os.path.join(PROOFS_DIR, f"proof_{rid}.bin"), "wb") as pf:
                    pf.write(receipt)
            except Exception:
                pass
            have.add(rid)
            adopted += 1
    return {"adopted": adopted, "rejected": rejected, "peers": len(PEERS)}

def coverage_and_held(c, now):
    """The two sets `claim` and `pick` must agree on: blocks already COVERED, and blocks HELD.

    They did not agree, and the disagreement is what made the 30,050 freeze read as self-healing while
    it was not. `pick` asked whether a RANGE ID `str(n)` existed; `claim` asked whether the HEIGHT n was
    covered by any verified range. A wide range covers heights whose ids never existed, so `/api/pick`
    returned block 30,051 all day -- a block `claim()` would never hand out. Anyone debugging from the
    endpoint concluded the board was already recovering.

    One function, so the two answers cannot drift again.
    """
    proven = set()
    for row in c.execute("SELECT lo, hi FROM vranges"):
        proven.update(range(row["lo"], row["hi"] + 1))
    # #296: a claim that has never beaten is held only for CLAIM_GRACE. One that HAS beaten keeps the
    # full CLAIM_TTL, however slow it is — the test is progress, not speed.
    held = {r["lo"] for r in c.execute("SELECT lo FROM ranges WHERE " + LIVE_CLAIM_SQL, _live_claim_args(now))}
    # Sponsor holds (SPONSOR_HOLD_SQL) are held whoever asks. Adding them HERE is what keeps them out of every
    # path that hands out blocks: claim()'s scan, its frontier-blocker re-offer (#284), and pick(). The
    # sponsor bot does not claim; it proves its blocks with `hazync-worker run <n>`.
    for sp in _sponsor_holds(c):
        held.update(range(sp["lo"], sp["hi"] + 1))
    return proven, held

def pick(body):
    """Suggest the next open BLOCK after the frontier. Per-block is the DEFAULT proving unit: one block
    per `hazync run` — no fold, low memory, and it matches the board's per-block proofs (so `/api/proof/<n>`
    stays valid). Block 1 pins to genesis. A bigger aligned chunk is opt-in via `hazync run <lo>-<hi>`."""
    fr = frontier_hi()
    c = db()
    proven, held = coverage_and_held(c, time.time())
    c.close()
    peers = peer_proven_heights()          # empty unless PEER_COORDINATORS is set
    busy = peer_busy_heights()             # ditto — heights a peer is proving RIGHT NOW (#69)

    # TWO PASSES, and the second one is the point.
    #
    # Pass 1 avoids both finished peer work and peer work in flight. Pass 2 drops the in-flight part.
    # Without that fallback, a peer could idle every other coordinator by claiming a wide span — and so
    # could a peer that simply died holding claims, until its TTL expired. Duplicate work is waste; an
    # idle prover is also waste, and a peer must not get to choose which one we suffer.
    #
    # Proven heights are NOT relaxed in pass 2: redoing finished work buys nothing at any time, and
    # unlike a claim it cannot be a transient state we are racing.
    # The ceiling is what the BRIDGE can serve, not a hardcoded chain height. `witness_available` is
    # the exact check and the ceiling is what stops it running away: without the bound, a coordinator
    # with no bundles would stat its way through two million heights before admitting it has nothing.
    _ceiling = provable_tip()
    for avoid_busy in (True, False):
        n = max(1, fr + 1)
        for _ in range(2_000_000):
            if n >= _ceiling:
                break
            # The SAME test claim() applies, so what this suggests is what that would hand out. The
            # frontier's own blocker is the one exception, and it belongs to claim(): re-offering a
            # covered block is an allocation decision, and pick is advice.
            if n not in proven and n not in held and n not in peers \
               and not (avoid_busy and n in busy) and witness_available(n):
                return 200, {"range": str(n), "lo": n, "hi": n, "cmd": f"hazync run {n}"}
            n += 1
        if not busy:
            break                          # pass 2 would ask exactly the same question
    return 404, {"error": "no open block available"}

def verify_sig(pubkey_hex, sig_hex, message: bytes) -> bool:
    """ed25519 signature over the receipt bytes. Fails closed if the crypto lib is missing, unless
    COORD_ALLOW_UNSIGNED=1 is explicitly set (dev/testing) — otherwise a missing lib would let anyone
    spoof any pubkey on the public board."""
    if not HAVE_ED:
        return bool(os.environ.get("COORD_ALLOW_UNSIGNED"))  # fail closed on a public board
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pk.verify(bytes.fromhex(sig_hex), message)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------- #113 key rotation ---------------
# A contributor who loses the box holding `key.hex` loses the ability to add to their own total, and
# their existing blocks are stranded under a name nobody can sign for again. They do still hold the
# one thing that proves continuity — the ability to sign with the OLD key — and until now there was
# nothing to present it to.
#
# The message is signed by BOTH keys. That is what makes the endpoint safe without any operator
# involvement: signing with the old key is the only way to make the claim at all (so nobody can annex
# your blocks), and requiring the new key too means nobody can push their history onto someone else's
# identity without that person's consent. `msg_ts` bounds replay.
ROTATE_MSG_VERSION = "hazync-rotate-v1"
ROTATE_MAX_SKEW    = float(os.environ.get("ROTATE_MAX_SKEW", "300"))  # seconds either side of our clock
ROTATE_MAX_DEPTH   = 32   # cycle/runaway guard; a real contributor rotates a handful of times at most

def rotate_message(old_pk: str, new_pk: str, ts) -> bytes:
    """The exact bytes both keys sign. Lowercased and integer-truncated so the client and the server
    cannot disagree about casing or float formatting and produce a signature that will not verify."""
    return f"{ROTATE_MSG_VERSION}:{old_pk.lower()}:{new_pk.lower()}:{int(ts)}".encode()

def rotation_map():
    """All rotation edges as {old: new}. Small (one row per rotation ever), so read it whole."""
    c = db()
    try:
        rows = c.execute("SELECT old_pubkey,new_pubkey FROM rotations").fetchall()
    except Exception:
        return {}                       # table absent on a coordinator that has not migrated yet
    finally:
        c.close()
    return {r["old_pubkey"]: r["new_pubkey"] for r in rows}

def resolve_pubkey(pk, rmap=None):
    """Follow rotation edges to the current head. Terminates on a cycle or a corrupt chain rather than
    hanging the request thread — `rotate()` refuses to create a cycle, but a hand-edited DB must not be
    able to wedge the board."""
    if rmap is None:
        rmap = rotation_map()
    cur = (pk or "").lower()
    seen = {cur}
    for _ in range(ROTATE_MAX_DEPTH):
        nxt = rmap.get(cur)
        if nxt is None or nxt in seen:
            return cur
        seen.add(nxt)
        cur = nxt
    return cur

def meta_get(k):
    c = db(); r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone(); c.close()
    return r["v"] if r else None

def meta_set(k, v):
    c = db(); c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v))); c.commit(); c.close()

def verify_receipt(receipt: bytes, rng):
    """Verify a submitted range receipt on CPU — no folding, no GPU (the 'verify-only' coordinator).

    Runs `host verify-any` (real STARK verification, without the genesis assertion), confirms the receipt
    is for the claimed [lo..hi], and reports the boundary tips. The coordinator records each verified
    range and chains them by tip (out_tip of k == in_tip of k+1) to compute the genesis-anchored frontier
    — so any block can be proved OUT OF ORDER and verified independently, and the frontier advances as
    contiguous runs connect. Forging/wrong proofs fail verify-any; a range claiming the wrong [lo..hi] is
    rejected. Folding into one succinct proof, when wanted, is separate GPU work. Returns
    (ok, note, meta) where meta = {in_tip, out_tip}. 'mock' stubs the STARK step for GPU-less testing.
    """
    if VERIFY == "mock":
        if not os.environ.get("COORD_ALLOW_MOCK"):  # S2: fail closed — never silently accept-everything in prod
            return False, "mock verification is disabled; set COORD_ALLOW_MOCK=1 to allow (GPU-less testing only)", None
        # anchored=False, not omitted: a missing key would default to False anyway, but stating it
        # keeps the mock's shape identical to the real path — the drift the comment at the other mock
        # records having happened once already.
        return True, "mock-verified (VERIFY_MODE=mock)", {"in_tip": "mock:%d" % rng["lo"], "out_tip": "mock:%d" % rng["hi"], "out_leaves": 0, "range_work": "0", "in_bhash": "0", "out_bhash": "0", "anchored": False}
    if not HOST_BIN or not os.path.exists(HOST_BIN):
        return False, "no HAZYNC_HOST binary configured for real verification", None
    os.makedirs(STATE_DIR, exist_ok=True)
    # unique per receipt+thread: verification now runs lock-free, so concurrent submits must not share a path
    tmp = os.path.join(STATE_DIR, f"in_{rng['id']}_{hashlib.sha256(receipt).hexdigest()[:12]}_{threading.get_ident()}.bin")
    with open(tmp, "wb") as f:
        f.write(receipt)
    try:
        r = subprocess.run([HOST_BIN, "verify-any", tmp], capture_output=True, timeout=120)
        out = r.stdout.decode(errors="replace")  # S3: parse ONLY stdout, never fold stderr/RUST_LOG in
        # take the single line the verifier prints (it starts with RANGE-OK) — no free-text can inject keys
        line = next((l for l in out.splitlines() if l.startswith("RANGE-OK")), None)
        if r.returncode != 0 or line is None:
            both = (r.stdout + r.stderr).decode(errors="replace")
            # The host classifies the failure: "MISMATCH" == a different guest build (the common
            # contributor mistake); otherwise a genuinely INVALID proof (forged/tampered/corrupt). Don't
            # mask a real forgery as a benign build mismatch — report each as what it is.
            if "MISMATCH" in both:
                return False, ("receipt rejected: your prover's guest image id (METHOD_ID) does not match this "
                               "coordinator's — you built a different guest. Use the prebuilt release binary, or "
                               "the reproducible build (reproduce/Dockerfile); expected id is in reproduce/METHOD_ID."), None
            return False, "receipt rejected — not a valid proof (forged/tampered/corrupt): " + both[-160:], None
        kv = dict(t.split("=", 1) for t in line[len("RANGE-OK"):].split() if "=" in t)
        lo, hi = int(kv["lo"]), int(kv["hi"])
        # Genesis seed: block 0 is unprovable (its in-boundary IS the genesis anchor), so a claimed
        # [0..hi] range is satisfied by a [1..hi] receipt whose in_tip verify-any pins to GENESIS_TIP.
        # Accept it and report the PROVEN [1..hi] so the frontier chains from prev_hi=0 -> lo=1.
        genesis_seed = (rng["lo"] == 0 and lo == 1 and hi == rng["hi"])
        if not genesis_seed and (lo != rng["lo"] or hi != rng["hi"]):
            return False, f"receipt proves [{lo}..{hi}], not the claimed [{rng['lo']}..{rng['hi']}]", None
        # "verified" and "genesis-anchored" are DIFFERENT claims, and callers conflate them (#59).
        # A verified mid-chain range attests a correct transition between the boundaries it states —
        # not that those boundaries descend from the real genesis. Surface which one this is.
        #
        # Derived here rather than read from the host's `anchored=` token on purpose: this is the
        # SAME condition _frontier_chain uses to decide what may advance the frontier (in_tip ==
        # GENESIS_TIP and lo == 1), so the label cannot drift from the rule that actually governs.
        # It also does not depend on which host binary is installed — an older one omits the token,
        # and defaulting a missing token to "not anchored" would mislabel genuinely anchored ranges.
        anchored = is_genesis_anchored(kv["in_tip"], lo)
        return True, f"range [{lo}..{hi}] VERIFIED", {"lo": lo, "hi": hi, "in_tip": kv["in_tip"], "out_tip": kv["out_tip"],
                "out_leaves": int(kv.get("out_leaves", 0)), "range_work": kv.get("range_work", "0"),
                "in_bhash": kv.get("in_bhash", ""), "out_bhash": kv.get("out_bhash", ""),
                "anchored": anchored}
    except Exception as e:
        return False, f"verify error: {e}", None
    finally:
        try: os.remove(tmp)
        except Exception: pass

# ── the spine: the genesis-anchored head (#30) ────────────────────────────────────────────────────
#
# The board proves blocks; the spine is the single artifact that says "everything from genesis to N is
# valid", and it ADVANCES rather than being re-folded (`spine [1..N] + chunk [N+1..M] -> [1..M]`).
#
# The coordinator does not build it — extending is a PROVE op and this box has no GPU. It stores,
# verifies and serves it. Whoever advances the spine is a LIVENESS single point of failure, not a
# soundness one: every extension is re-verified here, and because per-block receipts are retained
# anyone can rebuild the spine from scratch without re-proving anything.

def spine_head():
    """Current spine metadata, or None. Cheap: reads a small json, never the receipt."""
    try:
        with open(os.path.join(SPINE_DIR, "spine.json")) as f:
            return json.load(f)
    except Exception:
        return None

def verify_spine(receipt: bytes):
    """Verify a submitted spine head. Returns (ok, note, meta).

    TWO checks, deliberately, because they answer different questions and neither implies the other:

      `verify-range`  enforces the FULL genesis in-boundary — lo == 1, in_tip == genesis, the empty
                      accumulator, nBits, epoch start and the median-time window. Gate on its exit
                      code only; nothing is parsed out of it, so there is no free-text to trust.
      `verify-any`    re-verifies and prints one machine-readable RANGE-OK line, which is where lo/hi
                      and the tips come from.

    Using verify-any alone would accept a range that is valid but anchored anywhere — exactly the
    fabricated-anchor case the genesis pin exists to refuse. Parsing verify-range's prose instead
    would mean trusting free text for consensus-relevant numbers. So: gate on one, read from the other.
    """
    if VERIFY == "mock":
        if not os.environ.get("COORD_ALLOW_MOCK"):
            return False, "mock verification is disabled; set COORD_ALLOW_MOCK=1 (GPU-less testing only)", None
        # Must carry EVERY key submit_spine reads, or mock mode dies on a KeyError deep inside the
        # commit path — which is the one mode that exists specifically for boxes without a GPU.
        # verify_receipt's mock already returns the full shape; this one did not, and nothing noticed
        # until a test drove it.
        return True, "mock-verified (VERIFY_MODE=mock)", {"lo": 1, "hi": 0, "out_tip": "mock",
                                                          "in_tip": GENESIS_TIP, "out_leaves": 0,
                                                          "range_work": "0"}
    if not HOST_BIN or not os.path.exists(HOST_BIN):
        return False, "no HAZYNC_HOST binary configured for real verification", None
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, f"spine_{hashlib.sha256(receipt).hexdigest()[:12]}_{threading.get_ident()}.bin")
    with open(tmp, "wb") as f:
        f.write(receipt)
    try:
        g = subprocess.run([HOST_BIN, "verify-range", tmp], capture_output=True, timeout=180)
        if g.returncode != 0:
            both = (g.stdout + g.stderr).decode(errors="replace")
            if "MISMATCH" in both:
                return False, ("spine rejected: guest image id (METHOD_ID) does not match this coordinator's — "
                               "it was built against a different guest."), None
            # The usual cause is a receipt that verifies but is not anchored at genesis.
            return False, "spine rejected — not a genesis-anchored range proof: " + both[-200:], None
        r = subprocess.run([HOST_BIN, "verify-any", tmp], capture_output=True, timeout=180)
        line = next((l for l in r.stdout.decode(errors="replace").splitlines() if l.startswith("RANGE-OK")), None)
        if r.returncode != 0 or line is None:
            return False, "spine rejected — verify-any produced no RANGE-OK line", None
        kv = dict(t.split("=", 1) for t in line[len("RANGE-OK"):].split() if "=" in t)
        lo, hi = int(kv["lo"]), int(kv["hi"])
        if lo != 1:
            return False, f"spine must start at block 1, got [{lo}..{hi}]", None
        return True, f"spine [1..{hi}] VERIFIED genesis-anchored", {
            "lo": lo, "hi": hi, "in_tip": kv.get("in_tip", ""), "out_tip": kv.get("out_tip", ""),
            "out_leaves": int(kv.get("out_leaves", 0)), "range_work": kv.get("range_work", "0")}
    except Exception as e:
        return False, f"spine verify error: {e}", None
    finally:
        try: os.remove(tmp)
        except Exception: pass

def rotate(body):
    """#113 — move attribution from one key to a new one, proven by a signature from BOTH.

    Records an edge; never rewrites history. `/api/state` resolves through it, so the leaderboard and
    the contributor count follow immediately while `vranges` and `submissions` keep saying exactly
    which key signed what.

    The old key is NOT retired. A still-running box that nobody has stopped keeps submitting happily
    and its work resolves forward to the head, which is the failure-free outcome; retiring it would
    turn a forgotten worker into silent data loss."""
    old  = (body.get("old_pubkey") or "").strip().lower()
    new  = (body.get("new_pubkey") or "").strip().lower()
    s_old, s_new = body.get("sig_old", ""), body.get("sig_new", "")
    if HAVE_ED:
        if not is_hex(old, 32) or not is_hex(new, 32):
            return 400, {"error": "old_pubkey and new_pubkey must be 32-byte hex (ed25519)"}
        if not is_hex(s_old, 64) or not is_hex(s_new, 64):
            return 400, {"error": "sig_old and sig_new must be 64-byte hex (ed25519)"}
    if not old or not new:
        return 400, {"error": "old_pubkey and new_pubkey required"}
    if old == new:
        return 400, {"error": "old_pubkey and new_pubkey are the same key"}
    try:
        ts = float(body.get("ts"))
    except (TypeError, ValueError):
        return 400, {"error": "ts required (unix seconds, and must be inside the signed message)"}
    if abs(time.time() - ts) > ROTATE_MAX_SKEW:
        return 400, {"error": f"ts is more than {int(ROTATE_MAX_SKEW)}s from server time — replay guard"}

    msg = rotate_message(old, new, ts)
    if not verify_sig(old, s_old, msg):
        return 403, {"error": "old-key signature invalid"}
    if not verify_sig(new, s_new, msg):
        return 403, {"error": "new-key signature invalid"}

    blk = blocked_pubkeys()
    if old in blk or new in blk:
        return 403, {"error": "key is on the moderation list"}

    with _lock:
        rmap = rotation_map()
        if old in rmap:
            return 409, {"error": f"{old[:10]} has already rotated to {rmap[old][:10]}"}
        # Following the NEW key must not lead back to the old one. Without this, A->B then B->A makes a
        # closed loop with no head, and every read of either identity depends on which key it started
        # from. resolve_pubkey() would survive it (bounded), but the totals would be nonsense.
        if resolve_pubkey(new, rmap) == old:
            return 400, {"error": "rotation would create a cycle"}
        c = db()
        c.execute("INSERT INTO rotations(old_pubkey,new_pubkey,msg_ts,sig_old,sig_new,created)"
                  " VALUES(?,?,?,?,?,?)", (old, new, ts, s_old, s_new, time.time()))
        # The head needs a contributors row for its handle, and it may never have submitted anything —
        # rotating to a brand-new key is the whole point. Carry the old handle unless one was supplied.
        row = c.execute("SELECT handle FROM contributors WHERE pubkey=?", (old,)).fetchone()
        handle = clean_handle(body.get("handle") or (row["handle"] if row else None), handle_cap(new))
        if handle_refused(handle, new):
            c.close()
            return 400, {"error": "that handle is reserved — please pick another"}
        c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                  (new, handle, time.time()))
        c.commit(); c.close()
        head = resolve_pubkey(old)
    return 200, {"ok": True, "old": old, "new": new, "resolved": head, "handle": handle}

def submit_spine(body):
    """Accept an extended spine. Monotonic: a head that does not advance is refused."""
    pk, sig = body.get("pubkey", ""), body.get("sig", "")
    receipt_b64, handle = body.get("receipt", ""), clean_handle(body.get("handle"), handle_cap(pk))
    if not receipt_b64: return 400, {"error": "receipt required"}
    if handle_refused(handle, pk): return 400, {"error": "that handle is reserved — please pick another"}
    if HAVE_ED and not is_hex(pk, 32): return 400, {"error": "pubkey must be 32-byte hex (ed25519)"}
    if HAVE_ED and not is_hex(sig, 64): return 400, {"error": "sig must be 64-byte hex (ed25519)"}
    if len(receipt_b64) > MAX_BODY: return 413, {"error": "receipt too large"}
    try: receipt = base64.b64decode(receipt_b64)
    except Exception: return 400, {"error": "receipt must be base64"}
    if not verify_sig(pk, sig, receipt):
        return 403, {"error": "signature invalid"}

    cur = spine_head()
    with _verify_sem:
        ok, note, meta = verify_spine(receipt)
    if not ok:
        return 400, {"error": note}

    # Monotonic under the lock. Two workers may extend concurrently (duplicate spine work is harmless
    # by design); the shorter result must not overwrite the longer one.
    with _lock:
        cur = spine_head()
        if cur and meta["hi"] <= int(cur.get("hi", 0)):
            return 409, {"error": f"spine already at [1..{cur['hi']}]; submitted [1..{meta['hi']}] does not advance it",
                         "head": cur}
        os.makedirs(SPINE_DIR, exist_ok=True)
        head = {"lo": 1, "hi": meta["hi"], "out_tip": meta["out_tip"], "out_leaves": meta["out_leaves"],
                "range_work": meta["range_work"], "sha256": hashlib.sha256(receipt).hexdigest(),
                "bytes": len(receipt), "handle": handle, "pubkey": pk, "ts": time.time()}
        # Write both atomically-ish: receipt first, then the json that advertises it. A crash between
        # the two leaves a stale json pointing at a shorter spine, which is safe; the reverse would
        # advertise a head whose bytes are absent.
        tmp_bin = os.path.join(SPINE_DIR, ".spine.bin.tmp")
        with open(tmp_bin, "wb") as f: f.write(receipt)
        os.replace(tmp_bin, os.path.join(SPINE_DIR, "spine.bin"))
        tmp_js = os.path.join(SPINE_DIR, ".spine.json.tmp")
        with open(tmp_js, "w") as f: json.dump(head, f)
        os.replace(tmp_js, os.path.join(SPINE_DIR, "spine.json"))

        # Record it as WORK, not just as an artifact (#114). Until now this path verified a
        # signature and then discarded the identity: `spine.json` kept the handle of whoever last
        # advanced the head, and nothing recorded the other N-1 absorptions at all. A contributor
        # running `MODE=spine` — which CONTRIBUTING actively recommends — watched the activity feed
        # stay silent and their leaderboard number stay frozen while their GPU ran flat out, and the
        # only rational conclusion available to them was that it was broken.
        #
        # `contributors.blocks` is deliberately NOT incremented. That column means "blocks of chain
        # covered", and an absorption covers nothing new — it re-expresses blocks already proven as
        # one checkable file. Adding to it would double-count coverage and inflate the board's
        # headline number. Crediting effort separately is a real design question; showing the work is
        # not, so this does the second and leaves the first open.
        #
        # `spine:` prefix rather than a bare `1-N` so consumers can tell an absorption from a fold of
        # the same span. It is deliberately NOT parseable by parse_any_range, which is what sanitises
        # /api/proof/<id>: there is no per-range receipt to serve for a spine head, and a row that
        # cannot be turned into a path cannot be used to reach for one.
        try:
            c = db()
            c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
                      " VALUES(?,?,?,?,?,1,?,?)",
                      (f"spine:1-{meta['hi']}", pk, handle, head["sha256"], sig,
                       f"spine advanced to [1..{meta['hi']}] ({len(receipt)} bytes)", head["ts"]))
            c.commit(); c.close()
        except Exception as e:
            # The spine is already written and advertised; failing to log it must not fail the
            # submission. Losing a feed row is cosmetic, rejecting a valid spine is not.
            print(f"[spine] submitted ok but could not record the submission row: {e}")
    return 200, {"ok": True, "note": note, "head": head}

def _tree_node(lo, hi):
    """True if [lo..hi] is a node of the canonical fold tree.

    Width must be a power of two and the range must be ALIGNED to its own width (blocks are numbered
    from 1, so [1..2] and [3..4] are nodes; [2..3] is not). This is what makes folding converge.
    """
    w = hi - lo + 1
    return w > 0 and (w & (w - 1)) == 0 and (lo - 1) % w == 0

def foldable(limit=8):
    """Sibling pairs of the canonical fold tree whose parent does not exist yet (#37).

    THIS USED TO OFFER ANY ADJACENT PAIR, AND THAT DOES NOT CONVERGE. Every fold produces a range that
    immediately becomes a new operand, so "any adjacent pair whose exact span is missing" wanders into
    every (start, width) combination instead of building a tree. Measured on the live board before this
    was fixed: **581 folds covering 96 blocks**, where a tree needs 95 — 486 of them redundant, and the
    widths produced were 8 ranges of width 2, 8 of width 3, 8 of width 4 … which is O(n^2) by
    inspection. Every one of those proofs is VALID; they are just work nobody needed.

    So: only two aligned siblings of equal width may fold, and their parent is the next node up. That
    is N-1 folds for N blocks, at log depth, which is what the design assumed all along.

    Still unallocated and still advisory — several candidates are returned so concurrent workers spread
    out, and a duplicate fold is discarded as already proven. Cheap waste is fine; unbounded waste is
    not.

    NOTHING AT OR BELOW THE SPINE'S HEAD. The spine absorbs from hi+1 upward, so a node that starts at
    or below its head can never be absorbed, and the spine already proves every block it covers from
    genesis. Offering lowest-first without this floor pointed every folder at exactly that region.
    Measured on the live board, 2026-09-11: two cards folding bought the spine nothing, 228 blocks/hr
    against 230 without them. The pairs on offer sat at or behind the spine (a 128-block node finished
    85 blocks behind it), and where folders and spine met, the spine reached N+1 before (N+1, N+2) was
    folded on 92 of 93 steps. With the floor, folding starts just above the spine and runs ahead of it.
    """
    head = spine_head()
    try:
        floor = int(head.get("hi", 0)) if head else 0
    except (TypeError, ValueError, AttributeError):
        floor = 0
    with _lock:
        c = db()
        rows = c.execute("SELECT id, lo, hi FROM vranges ORDER BY lo").fetchall()
        c.close()
    starts, have = {}, set()
    for r in rows:
        starts.setdefault(r["lo"], []).append(r)
        have.add((r["lo"], r["hi"]))
    out = []
    for r in rows:
        lo, hi = r["lo"], r["hi"]
        if lo <= floor:
            continue                      # at or below the spine: it can never absorb this node
        if not _tree_node(lo, hi):
            continue                      # not a tree node — folding from it does not converge
        w = hi - lo + 1
        if ((lo - 1) // w) % 2 != 0:
            continue                      # right-hand sibling; its left partner drives the fold
        for s in starts.get(hi + 1, ()):
            if s["hi"] - s["lo"] + 1 != w:
                continue                  # siblings must be the same width
            if (lo, s["hi"]) in have:
                continue                  # parent already exists
            out.append({"left": r["id"], "right": s["id"], "lo": lo, "hi": s["hi"],
                        "result": (str(lo) if lo == s["hi"] else f"{lo}-{s['hi']}")})
            if len(out) >= limit:
                return out
    return out

FRONTIER_TTL = float(os.environ.get("FRONTIER_CACHE_TTL", "2"))
_sf_lock = threading.Lock()
_sf = {}            # key -> {"t": ts, "v": value} and key+":busy" -> threading.Event while recomputing


def _single_flight(key, ttl, fn):
    """#265: a TTL cache that recomputes on ONE thread. While a recompute is running, other callers get
    the previous value (stale-while-revalidate); only a cold start makes them wait for the first value.
    A plain TTL cache stampedes the moment a recompute takes longer than its TTL -- every request misses
    and recomputes, which is exactly what piled 148 threads onto the coordinator."""
    now = time.time()
    with _sf_lock:
        e = _sf.get(key)
        if e is not None and now - e["t"] < ttl:
            return e["v"]
        ev = _sf.get(key + ":busy")
        leader = ev is None
        if leader:
            ev = _sf[key + ":busy"] = threading.Event()
    if not leader:
        if e is not None:
            return e["v"]
        ev.wait(timeout=60)
        with _sf_lock:
            e = _sf.get(key)
        if e is not None:
            return e["v"]
    try:
        v = fn()
        with _sf_lock:
            _sf[key] = {"t": time.time(), "v": v}
        return v
    finally:
        if leader:
            with _sf_lock:
                _sf.pop(key + ":busy", None)
            ev.set()


def _frontier_chain_cached():
    """#265: the frontier walk for DISPLAY and pre-flight callers only (frontier_hi -> /api/meta, which every
    worker hits; frontier_proof -> state). It loads all of vranges, so it is single-flight with a short TTL,
    and invalidated whenever this server writes vranges. `_frontier_chain` itself stays uncached: it is the
    S1/F1/H9 trust boundary that seam_fuzz drives directly, and a cache there would answer for other data."""
    return _single_flight("frontier_chain", FRONTIER_TTL, _frontier_chain)


def _frontier_invalidate():
    with _sf_lock:
        _sf.pop("frontier_chain", None)


def _frontier_chain():
    """Select the MOST-WORK genesis-anchored chain (Bitcoin's rule), not merely the tallest one.

    Verified ranges form a DAG — a valid seam requires `b.lo == a.hi + 1`, so `lo` strictly increases —
    and among EVERY genesis-anchored, seam-continuous, height-contiguous chain we pick the one with the
    greatest cumulative `range_work`. This is what stops a party who can *prove* a longer LOW-difficulty
    genesis fork from shadowing the real chain: a taller fork with less total work loses, exactly as in
    Bitcoin. (Greatest-hi — the previous rule, itself the fix for the single-block-at-a-boundary stall —
    is subsumed: on the honest chain more blocks means more work; only a fork makes the two rules differ.)

    A seam binds the FULL boundary — tip-hash linkage (`b.in_tip == a.out_tip`) AND UTXO/difficulty/MTP
    continuity (`b.in_bhash == a.out_bhash`, S1/F1) AND height contiguity (`b.lo == a.hi + 1`, H9). The
    genesis-connecting range's in-boundary is pinned by `verify-any` (assert_genesis_in_boundary), so a
    genesis anchor means `in_tip == GENESIS_TIP` and `lo == 1`. Returns (hi, tip_hash, cum_work, leaves).
    """
    c = db()
    rows = [dict(r) for r in c.execute(
        "SELECT lo,hi,in_tip,out_tip,out_leaves,range_work,in_bhash,out_bhash "
        "FROM vranges ORDER BY lo, ts").fetchall()]  # F2: deterministic order; lo-asc is a topo order
    c.close()
    def rwork(r):
        try: return int(r["range_work"] or 0)
        except Exception: return 0
    # predecessors indexed by their out-boundary tip, for O(1) seam lookup
    by_out = {}
    for i, r in enumerate(rows):
        r["_i"] = i
        by_out.setdefault(r["out_tip"], []).append(r)
    # DP in lo order (a seam strictly increases lo, so predecessors are processed first): best[_i] = the
    # max cumulative range_work of any genesis-anchored seam-chain ending at that range (absent => the
    # range is not reachable from genesis and can never be the frontier).
    best = {}
    frontier = (0, GENESIS_TIP, 0, 0)   # (hi, tip_hash, cum_work, leaves) — empty until a genesis chain lands
    for r in rows:
        if not r["in_bhash"]:
            continue  # F3: no boundary digest (pre-migration / NULL) — not chainable
        if is_genesis_anchored(r["in_tip"], r["lo"]):
            cw = rwork(r)                      # genesis-anchored: verify-any pinned its full in-boundary (H9 lo==1)
        else:
            best_pred = None
            for p in by_out.get(r["in_tip"], ()):            # tip-hash linkage
                if (str(p["out_bhash"]) == str(r["in_bhash"])  # S1/F1: full-boundary continuity
                        and p["hi"] + 1 == r["lo"]             # H9: height contiguity
                        and p["_i"] in best):                 # predecessor reachable from genesis
                    pw = best[p["_i"]]
                    if best_pred is None or pw > best_pred:
                        best_pred = pw
            if best_pred is None:
                continue  # no genesis-anchored seam-chain reaches this range
            cw = best_pred + rwork(r)
        best[r["_i"]] = cw
        if cw > frontier[2] or (cw == frontier[2] and r["hi"] > frontier[0]):
            frontier = (r["hi"], r["out_tip"], cw, r["out_leaves"] or 0)
    return frontier

def frontier_hi():
    """Highest block covered by a contiguous, boundary-continuous chain of verified ranges from genesis."""
    return _frontier_chain_cached()[0]

def proven_count():
    """Distinct blocks covered by any verified range. A single block can legitimately be verified both
    inside an aligned range (e.g. 0-999) and as a standalone single-block range (500); a naive
    SUM(hi-lo+1) would count it twice and inflate the headline number/percentage. Merge the intervals
    so each height counts once. vranges are RANGE_SIZE-coarse (+ a few singles), so this stays cheap."""
    c = db()
    rows = c.execute("SELECT lo,hi FROM vranges").fetchall()
    c.close()
    total, cur_lo, cur_hi = 0, None, None
    for lo, hi in sorted((r["lo"], r["hi"]) for r in rows):
        if cur_hi is None or lo > cur_hi + 1:
            if cur_hi is not None: total += cur_hi - cur_lo + 1
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None: total += cur_hi - cur_lo + 1
    return total

def fold_spans(c):
    """Every verified range that was MADE BY FOLDING, in submission order, as (lo, hi) spans.

    One walk, one definition, two callers: `folded_count` (how much of the chain has been folded) and
    `build_vranges` (which rows to flag so a client need not guess). Before this each caller invented
    its own test and they disagreed -- the block map called any wide range a fold, which credits the
    #281 overlap ranges with 250 blocks that were proved outright, not folded.

    ⛔ ORDER IS LOAD-BEARING. `is_fold_seam` asks what was ALREADY VERIFIED when a range arrived, so
    the walk must be in `ts` order. /api/vranges is served ORDER BY lo, and computing this from that
    payload yields ZERO folds -- a fold's children sort after it, so every seam test fails. That is why
    the flag is published rather than left to the client to work out.
    """
    by_start, ends_at, spans = {}, {}, []
    for r in c.execute("SELECT id,lo,hi FROM vranges ORDER BY ts ASC, rowid ASC"):
        lo, hi = r["lo"], r["hi"]
        if hi > lo and is_fold_seam(lo, hi, by_start, ends_at):
            spans.append((r["id"], lo, hi))
        ends_at.setdefault(lo, set()).add(hi)
        if by_start.get(lo, -1) < hi:
            by_start[lo] = hi
    return spans

def folded_count():
    """Distinct blocks inside at least one range that was made by folding.

    BLOCKS, not fold operations. The leaderboard's `folded` column counts operations (8,393 today);
    the home page states a percentage of the chain, so it needs the 9,854 blocks those operations
    cover. Merged the same way as `proven_count`, because a block is folded into several nodes as the
    tree deepens and must count once.

    Measured on the live board: 0.146s over 54,667 rows, and `state()` is cached.
    """
    c = db()
    spans = [(lo, hi) for _id, lo, hi in fold_spans(c)]
    c.close()
    total, cur_lo, cur_hi = 0, None, None
    for lo, hi in sorted(spans):
        if cur_hi is None or lo > cur_hi + 1:
            if cur_hi is not None: total += cur_hi - cur_lo + 1
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None: total += cur_hi - cur_lo + 1
    return total

def is_fold_seam(lo, hi, by_start, ends_at):
    """Was [lo..hi] made by FOLDING two ranges that were already verified, or by proving it outright?

    Both shapes are legitimate and the submit gate says so in as many words: a range either covers
    fresh territory, or is exactly tiled by ranges already on the board. Only the second is a fold, and
    until now the leaderboard could not tell them apart — so a fold was credited as if its blocks had
    been proven by whoever folded them, and the columns summed to more chain than exists.

    `fold-range` is BINARY — [lo..m] + [m+1..hi] — so the test is exact rather than heuristic: does some
    earlier range start at `lo` and end at an `m` whose successor range ends exactly at `hi`. Measured
    against a slower rule that asks whether strictly-narrower earlier ranges tile the span at all: same
    verdict on every row, 0.18s instead of minutes.

    It matters for seven ranges today, all of them from the #281 overlap: bip-448's five that broke new
    ground with bad bounds, and the two G H O S T proved to repair the frontier over blocks that already
    looked covered. Calling those folds would take ~250 blocks of real proving off two people."""
    for m in ends_at.get(lo, ()):               # earlier ranges [lo..m]
        if by_start.get(m + 1) == hi:           # and an earlier [m+1..hi] meeting it at the seam
            return True
    return False

def contributions_by_pubkey():
    """Per-contributor work, split by KIND: proved / folded / anchored.

    `blocks` alone was never the full story and quietly overstated itself. It counted distinct blocks
    covered by ANY range someone submitted, so folding a wide range credited the folder with blocks
    other people proved: measured on the live board, the columns summed to 43,868 against 42,711 blocks
    actually proven — 1,157 counted twice, once for the prover and once for the folder. Splitting the
    kinds stops that, and gives folding and anchoring their own numbers instead of dissolving them into
    somebody else's.

    ⚠ The proved column still does not SUM to the headline, and should not be made to. Two people can
    prove the same block: during the #281 overlap repair, G H O S T re-proved [30051..30100] and
    [30101..30149] over bounds bip-448 had already covered, so the columns run exactly 99 blocks above
    `proven` today. Both did that work. What was wrong before was crediting a FOLDER with blocks someone
    else proved; genuine duplicate proving is not the same thing and is not hidden here.

    ANCHORING counts spine absorptions. It deliberately adds no blocks: an absorption covers nothing
    new, it re-expresses blocks already proven as one checkable file. That is why it could never be
    folded into `blocks` without inflating the headline, and why it needs a column of its own — the
    open question the spine-write comment records.

    Keyed by RESOLVED pubkey (#113), so a rotated key's work lands on its current head.

    The resolve has to happen BEFORE the sort, and that is the whole subtlety. Two keys belonging to
    the same person interleave — the old box proved 100-199 and the new one 150-249 — so summing their
    separately-merged totals would count 150-199 twice and hand a rotation a phantom bonus. Merging is
    only overlap-safe over a single ordered sequence, so the merged identity must be re-sorted as one."""
    rmap = rotation_map()
    c = db()
    # ts order, because "already verified when this arrived" is what separates a fold from a proof.
    vr = c.execute("SELECT pubkey,lo,hi FROM vranges ORDER BY ts ASC, rowid ASC").fetchall()
    spine = c.execute("SELECT pubkey FROM submissions WHERE range_id LIKE 'spine:1-%'").fetchall()
    c.close()

    folded, anchored, by_start, ends_at, rows = {}, {}, {}, {}, []
    for r in vr:
        lo, hi = r["lo"], r["hi"]
        if hi > lo and is_fold_seam(lo, hi, by_start, ends_at):
            pk = resolve_pubkey(r["pubkey"], rmap)
            folded[pk] = folded.get(pk, 0) + 1
        else:
            rows.append(r)                      # a proof: its blocks count toward `proved`
        ends_at.setdefault(lo, set()).add(hi)
        if by_start.get(lo, -1) < hi:
            by_start[lo] = hi
    for s in spine:
        pk = resolve_pubkey(s["pubkey"], rmap)
        anchored[pk] = anchored.get(pk, 0) + 1
    items = sorted((resolve_pubkey(r["pubkey"], rmap), r["lo"], r["hi"]) for r in rows)
    out, cur_pk, cur_lo, cur_hi = {}, None, None, None
    for pk, lo, hi in items:
        if pk != cur_pk:
            if cur_hi is not None: out[cur_pk] = out.get(cur_pk, 0) + (cur_hi - cur_lo + 1)
            cur_pk, cur_lo, cur_hi = pk, lo, hi
            continue
        if lo > cur_hi + 1:
            out[cur_pk] = out.get(cur_pk, 0) + (cur_hi - cur_lo + 1)
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None: out[cur_pk] = out.get(cur_pk, 0) + (cur_hi - cur_lo + 1)
    return {pk: {"proved": out.get(pk, 0), "folded": folded.get(pk, 0), "anchored": anchored.get(pk, 0)}
            for pk in set(out) | set(folded) | set(anchored)}

def frontier_proof():
    """The genesis-anchored frontier as a chain-state (the real committed proof output the hero panel
    shows). Empty (height 0) until the first genesis-anchored proof lands."""
    hi, tip_hash, cum_work, leaves = _frontier_chain_cached()
    return {"height": hi, "tip_hash": tip_hash, "cum_work": cum_work, "leaves": leaves}

def timeline(fr, segs=240):
    """Whole-chain genesis→tip strip, bucketed into `segs` segments (bounded payload at any chain size).

    Each segment reports the strongest status of the blocks it covers:
      'frontier' — inside the contiguous genesis-anchored frontier (solid green, done + chained)
      'ahead'    — verified but past the frontier (out-of-order proof, not yet connected to genesis)
      'claimed'  — someone is proving it right now
      'open'     — nobody on it
    Returns {segs, per_seg (bytes 0=open/1=claimed/2=ahead/3=frontier), frontier_seg}.
    """
    per = bytearray(segs)  # 0 open
    _tip = chain_tip()
    bps = _tip / segs if segs else _tip
    c = db()
    vr = c.execute("SELECT lo,hi FROM vranges").fetchall()
    cl = c.execute("SELECT lo,hi FROM ranges WHERE status='claimed'").fetchall()
    c.close()
    def mark(lo, hi, val):
        s0 = int(lo / bps); s1 = min(segs - 1, int(hi / bps))
        for s in range(max(0, s0), s1 + 1):
            if per[s] < val: per[s] = val
    for r in cl: mark(r["lo"], r["hi"], 1)          # claimed
    for r in vr: mark(r["lo"], r["hi"], 2)          # verified (ahead)
    fr_seg = int(fr / bps) if bps else 0
    if fr > 0:                                       # fr==0 means nothing proven — no green
        for s in range(min(fr_seg + 1, segs)):       # contiguous frontier overrides to solid green
            if s * bps <= fr: per[s] = 3
    return {"segs": segs, "per_seg": list(per), "frontier_seg": fr_seg}

def build_vranges(c, blk):
    """The full verified-range index, so a client can browse or search ANY block, not just the frontier
    window. Extracted from state() so /api/state and /api/vranges cannot drift into two answers."""
    # Which rows are folds can only be decided in ts order (see fold_spans), and this is served in lo
    # order -- so the answer is computed here and published. `fold` is emitted only when true: it is
    # ~8k of 55k rows, and a false on every row would add a quarter of a megabyte to a 4 MB payload.
    folds = {rid for rid, _lo, _hi in fold_spans(c)}
    out = []
    for r in c.execute("SELECT id,lo,hi,handle,pubkey FROM vranges ORDER BY lo"):
        v = dict(lo=r["lo"], hi=r["hi"],
                 handle=(r["handle"] if (r["pubkey"] or "").lower() not in blk else "[removed]"))
        if r["id"] in folds:
            v["fold"] = 1
        if os.path.exists(os.path.join(PROOFS_DIR, f"proof_{r['id']}.bin")):
            v["proof"] = f"/api/proof/{r['id']}"      # downloadable receipt, re-verifiable by anyone
        out.append(v)
    return out

def spine_segments():
    """Who absorbed each block into the spine, as runs of [lo..hi] by one contributor.

    Every absorption already writes a permanent row — `spine:1-<hi>`, with the absorbing pubkey and
    handle — so this invents nothing; it re-reads rows the coordinator has kept all along. The feed
    showed them (`recent` is the same table, `LIMIT 40`) but only for about as long as it took the
    next forty submissions to arrive, so the site could name who proved and who folded a block and
    then went silent on who anchored it — the step that actually made it checkable in one go.

    The spine only ever moves forward, so a row is exactly "this contributor took the head from where
    it was to <hi>", and the blocks in between are theirs. Consecutive rows by the same pubkey are one
    run, which is why this is a handful of segments rather than one entry per advance.

    ⛔ Keyed on PUBKEY, not handle. Two anonymous contributors both present as null and merging them
    would credit one person with the other's work; a rename mid-run would split it for no reason.

    A row at or below the head it already had covers no new blocks — a re-advertised head, or two
    submissions racing — and is skipped rather than emitted as an empty or backwards segment.

    ⛔ THE FIRST ROW IS THE ONE ASSUMPTION HERE. A row says the head reached <hi>; it does not say
    where it came from, so the earliest surviving row is taken to have started at block 1. If the log
    were ever truncated at the bottom, that assumption hands its contributor every block below their
    advance — one silent, plausible-looking, wrong segment. It cannot happen by ordinary operation
    (nothing deletes from `submissions`, and a re-baseline invalidates the spine receipt itself, so a
    rebuilt spine starts from 0 with a fresh log), but it is an assumption rather than a fact, so the
    first recorded advance is published as `first_advance_hi` for a caller that wants to check it
    rather than discovering the over-credit by reading a name that looks odd."""
    c = db()
    try:
        blk = blocked_pubkeys()      # the moderation list state() and recent apply, so handles agree
        rows = c.execute("SELECT range_id,pubkey,handle FROM submissions"
                         " WHERE range_id LIKE 'spine:1-%' ORDER BY ts ASC, rowid ASC").fetchall()
    finally:
        c.close()
    segs, prev_hi, prev_pk, first_hi = [], 0, None, None
    for s in rows:
        try:
            hi = int(s["range_id"].split("-", 1)[1])
        except (IndexError, ValueError):
            continue                 # not a head this understands; skip rather than guess a height
        if hi <= prev_hi:
            continue
        pk = (s["pubkey"] or "").lower()
        if segs and pk == prev_pk:
            segs[-1]["hi"] = hi
            # Someone who names themselves partway through a run, or renames, is shown under the name
            # they use NOW — carrying the first row's handle would leave a run reading "anonymous"
            # under a contributor who has since said who they are.
            segs[-1]["handle"] = "[removed]" if pk in blk else s["handle"]
        else:
            segs.append({"lo": prev_hi + 1, "hi": hi,
                         "handle": "[removed]" if pk in blk else s["handle"]})
        if first_hi is None:
            first_hi = hi
        prev_hi, prev_pk = hi, pk
    return segs, first_hi

def vranges_cached():
    """Serialised /api/vranges with a TTL and an ETag, returned as (bytes, etag).

    This is ~99.9% of what /api/state used to ship (3,393,853 of 3,397,846 bytes at 38,507 entries) and
    it only changes when a range is verified — but the board polled it every 10 seconds, and it grows
    with the chain. Splitting it out takes the steady-state poll from ~313 KB gzipped to a few KB, and
    the ETag makes an unchanged index a 304 rather than a re-download.

    Single-flight, like state_cached (#265). It was a plain TTL cache, and on 2026-09-13 that took the
    board down for ~20 minutes: a rebuild had grown to ~25 s, so under ordinary traffic every request that
    arrived during one missed the cache and started its own. ~350 threads ended up rebuilding at once, each
    holding a connection, the -wal and a temp file, until the process hit its 1024 open files and nothing
    could open the database -- claims and submits included. Now one thread rebuilds and everyone else gets
    the previous index meanwhile; only a cold start waits."""
    def build():
        c = db()
        try:
            blk = blocked_pubkeys()      # same moderation list state() applies, so handles match exactly
            payload = {"vranges": build_vranges(c, blk), "range_size": RANGE_SIZE}
        finally:
            c.close()                    # explicitly: a connection must not depend on the GC to be released
        v = json.dumps(payload).encode()
        return v, '"' + hashlib.sha256(v).hexdigest()[:32] + '"'
    return _single_flight("vranges", VRANGES_TTL, build)

def _etag_matches(header, etag):
    """True when an If-None-Match header names this ETag, weak or strong.

    nginx gzips responses on the way out and turns the ETag into a WEAK one, `W/"..."`, and that is
    what a browser sends back. Comparing the raw header with the strong tag never matched, so every
    revalidation of /api/vranges re-downloaded the whole index. Measured 2026-09-13 through hazync.org:
    `W/"..."` answered 200 and 572 KB, the bare `"..."` answered 304."""
    if not header or not etag:
        return False
    want = etag[2:] if etag.startswith("W/") else etag
    for t in header.split(","):
        t = t.strip()
        if t == "*" or (t[2:] if t.startswith("W/") else t) == want:
            return True
    return False

def fold_ids_cached():
    """Ids of every verified range that is a FOLD (see fold_spans), cached like the index it is read from."""
    def build():
        c = db()
        try:
            return frozenset(rid for rid, _lo, _hi in fold_spans(c))
        finally:
            c.close()
    return _single_flight("fold:ids", VRANGES_TTL, build)

def known_handles_cached():
    """Every handle that has a verified range, moderation applied. Bounds the per-prover cache keys."""
    def build():
        c = db()
        try:
            blk = blocked_pubkeys()
            return frozenset(r["handle"] for r in c.execute("SELECT DISTINCT handle,pubkey FROM vranges")
                             if r["handle"] and (r["pubkey"] or "").lower() not in blk)
        finally:
            c.close()
    return _single_flight("handles", VRANGES_TTL, build)

def spine_segments_cached():
    return _single_flight("spine:segs:list", VRANGES_TTL, lambda: spine_segments()[0])

def block_status(prover=None):
    """Every block's furthest state, as runs, for the block map: [[lo, hi, status], ...] with 3 proven,
    4 folded and 5 anchored (the numbers blockmap.js already uses). A block in no run is open.

    The map used to build this from /api/vranges, every verified range with its handle (5.8 MB, 568 KB
    gzipped at 70k rows), on every load and every five minutes. Runs are a few thousand entries. Claims
    are not included: they change by the second and already ride on /api/state.

    With `prover`, only the ranges that prover PROVED -- not the folds they made -- so the map can light
    up one prover's blocks. A blocked contributor never matches."""
    tip = chain_tip()
    spine_hi = (spine_head() or {}).get("hi") or 0
    folds = fold_ids_cached()
    c = db()
    try:
        blk = blocked_pubkeys()
        if prover is None:
            rows = c.execute("SELECT id,lo,hi FROM vranges").fetchall()
        else:
            rows = [r for r in c.execute("SELECT id,lo,hi,pubkey FROM vranges WHERE handle=?", (prover,))
                    if (r["pubkey"] or "").lower() not in blk and r["id"] not in folds]
    finally:
        c.close()
    top = max([tip, spine_hi] + [r["hi"] for r in rows])
    st = bytearray(top + 1)
    # A fold outranks a single proof, and the genesis proof outranks both: proofs first, folds over them.
    for want_fold, code in ((False, b"\x03"), (True, b"\x04")):
        for r in rows:
            if (r["id"] in folds) == want_fold:
                lo, hi = max(1, r["lo"]), r["hi"]
                if hi >= lo:
                    st[lo:hi + 1] = code * (hi - lo + 1)
    if prover is None and spine_hi:
        st[1:spine_hi + 1] = b"\x05" * spine_hi
    runs = [[m.start(), m.end() - 1, m.group(1)[0]] for m in re.finditer(rb"([\x01-\xff])\1*", bytes(st))]
    return {"tip": tip, "spine_hi": spine_hi, "frontier": frontier_hi(), "prover": prover, "runs": runs}

def block_status_cached(prover=None):
    """(bytes, etag) for /api/blockstatus, single-flight like the index it replaces for the map."""
    def build():
        v = json.dumps(block_status(prover)).encode()
        return v, '"' + hashlib.sha256(v).hexdigest()[:32] + '"'
    return _single_flight("blockstatus" if prover is None else "blockstatus:p:" + prover, VRANGES_TTL, build)

def _parse_price_bands(raw):
    """SPONSOR_PRICE_BANDS: a JSON list of [lo, hi, usd_per_block], heights inclusive, the price a WHOLE
    number of dollars. Set but unparseable, empty, a zero, negative or fractional price, a band starting
    below block 1, or two bands that overlap all mean unpriced (None), and nothing can be sponsored. Unset
    means SPONSOR_PRICE_BANDS_DEFAULT."""
    if not raw:
        return None
    try:
        bands = json.loads(raw)
        out = []
        for b in bands:
            lo, hi, sats = b
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lo, hi, sats)):
                return None
            if lo < 1 or hi < lo or sats < 1:
                return None
            out.append((lo, hi, sats))
    except (ValueError, TypeError):
        return None
    out.sort()
    if not out or any(out[i][0] <= out[i - 1][1] for i in range(1, len(out))):
        return None
    return out

SPONSOR_OPEN = os.environ.get("SPONSOR_OPEN", "0") == "1"
SPONSOR_MAX_BLOCKS = int(os.environ.get("SPONSOR_MAX_BLOCKS", "1000"))
SPONSOR_NAME_MAX = 40                               # characters (code points); the site's form reads it from GET /api/sponsor
# The minimum per block, in whole dollars, by height (decided 2026-09-14, after the first sponsor bot trial).
# Measured on one RTX 4090 at $0.74 an hour, the slowest trial block cost $0.18 at 180k-200k (a 2.0 MB bundle)
# and $0.22 at 200k-230k (3.7 MB), 248 to 441 seconds per MB. At the slowest rate the heaviest 1% of blocks
# cost about $0.27 up to 200,000 and $0.57 from there to 230,000. Above 230,000 the only measured block is
# 966,256 ($1.39 of GPU time), and the bridge has built no bundles there yet: a sponsorship above them is held
# until it does, and the quote's `waiting` says how many blocks wait. Blocks above 1,000,000 are $6 each; the
# top band runs to SPONSOR_BAND_TOP, meaning "and above", and no block can be sponsored before it is mined.
# docs/SPONSORSHIP.md has the figures.
SPONSOR_BAND_TOP = 10 ** 9
SPONSOR_PRICE_BANDS_DEFAULT = [(1, 200000, 1), (200001, 400000, 2), (400001, 600000, 3),
                               (600001, 800000, 4), (800001, 1000000, 5), (1000001, SPONSOR_BAND_TOP, 6)]
SPONSOR_PRICE_BANDS = (_parse_price_bands(os.environ["SPONSOR_PRICE_BANDS"]) if "SPONSOR_PRICE_BANDS" in os.environ
                       else list(SPONSOR_PRICE_BANDS_DEFAULT))

def _parse_btc_usd(raw):
    """SPONSOR_BTC_USD: dollars per bitcoin, to turn a dollar minimum into sats. No default: a bitcoin price
    written into the code would be wrong within the day. Unset or not a positive finite number -> None."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return int(v) if v.is_integer() else v

SPONSOR_BTC_USD = _parse_btc_usd(os.environ.get("SPONSOR_BTC_USD"))
SPONSOR_PUBLIC = ("paid", "proving", "proven")     # the only statuses that ever show a sponsor's name
# A name is public only when BOTH hold: the status says paid, and what settled covers the minimum. Every
# query that returns a sponsor's name to anyone but the sponsor uses this, so an `underpaid` row, or a
# `paid` row set by hand below its minimum, never shows.
SPONSOR_PUBLIC_SQL = ("status IN ('paid','proving','proven') AND paid_sats IS NOT NULL"
                      " AND min_sats IS NOT NULL AND paid_sats >= min_sats")

# A HOLD (docs/SPONSORSHIP.md, "Holds"): a sponsorship paid at least its minimum and not yet proven keeps
# its blocks for the sponsor proving bot. No claim or pick ever offers a held block to anyone, and a proof of
# a held block is accepted only from a key registered for that sponsorship (_hold_refusal). The hold ends when the whole span is covered
# (submit() moves the sponsorship to `proven`) or it is cancelled or refunded. It has no expiry, so /api/state
# reports a hold on the frontier's next block once it is older than SPONSOR_HOLD_ALERT.
SPONSOR_HOLD_SQL = ("status IN ('paid','proving') AND paid_sats IS NOT NULL"
                    " AND min_sats IS NOT NULL AND paid_sats >= min_sats")
SPONSOR_HOLD_ALERT = int(os.environ.get("SPONSOR_HOLD_ALERT", str(6 * 3600)))

def _sponsor_holds(c, lo=None, hi=None):
    """Held sponsorships, oldest payment first; only those overlapping lo..hi when given."""
    q, args = "SELECT id,lo,hi,status,paid_at FROM sponsorships WHERE " + SPONSOR_HOLD_SQL, ()
    if lo is not None:
        q, args = q + " AND lo<=? AND hi>=?", (hi, lo)
    try:
        return c.execute(q + " ORDER BY paid_at ASC, id ASC", args).fetchall()
    except sqlite3.OperationalError:          # a database from before the table; init_db adds it on start
        return []

def _sponsor_mark_proven(c, lo, hi, now=None):
    """Every held sponsorship overlapping lo..hi whose WHOLE span is now covered by verified ranges becomes
    `proven`, which ends its hold. Covered by anyone counts. Called by submit() under the lock, on the
    connection that has just recorded the range, before it commits. Returns the ids it moved."""
    done = []
    for sp in _sponsor_holds(c, lo, hi):
        need = sp["lo"]
        for r in c.execute("SELECT lo, hi FROM vranges WHERE lo<=? AND hi>=? ORDER BY lo", (sp["hi"], sp["lo"])):
            if r["lo"] > need:
                break
            need = max(need, r["hi"] + 1)
            if need > sp["hi"]:
                break
        if need > sp["hi"]:
            c.execute("UPDATE sponsorships SET status='proven', proven_at=? WHERE id=? AND status IN ('paid','proving')",
                      (now or time.time(), sp["id"]))
            done.append(sp["id"])
    return done

def _hold_refusal(c, lo, hi, pubkey):
    """(sponsorship id, first block) if [lo..hi] would newly prove a block held for a sponsorship and `pubkey`
    is not a key registered for THAT sponsorship in sponsor_keys; else None.

    The sponsor paid for those blocks, so another prover must not take them, and their credit, by submitting
    them directly: claims never offer them (coverage_and_held), and this closes the other door. Only blocks
    not yet covered by verified ranges count, so a fold that re-expresses blocks already proven takes nothing."""
    for sp in _sponsor_holds(c, lo, hi):
        a, b = max(lo, sp["lo"]), min(hi, sp["hi"])
        covered = set()
        for r in c.execute("SELECT lo, hi FROM vranges WHERE lo<=? AND hi>=?", (b, a)):
            covered.update(range(max(a, r["lo"]), min(b, r["hi"]) + 1))
        first = next((h for h in range(a, b + 1) if h not in covered), None)
        if first is None:
            continue
        try:
            mine = c.execute("SELECT 1 FROM sponsor_keys WHERE pubkey=? AND sponsorship_id=?",
                             (str(pubkey).lower(), sp["id"])).fetchone() is not None
        except sqlite3.OperationalError:          # a database from before the table
            mine = False
        if not mine:
            return sp["id"], first
    return None

def _hold_message(held):
    return {"error": f"block {held[1]:,} is held for sponsorship #{held[0]}: only the sponsor's prover can prove it"}

def _is_public_sponsorship(r):
    return (r["status"] in SPONSOR_PUBLIC and r["paid_sats"] is not None and r["min_sats"] is not None
            and r["paid_sats"] >= r["min_sats"])

def sponsor_min_usd(lo, hi):
    """The minimum for blocks lo..hi in whole dollars: the sum of each block's band price, or None if any
    block is in no band. The bands are a deliberate OVERESTIMATE of compute, so paying the minimum means
    the blocks can be proven whatever cards cost that day; anyone may pay more."""
    if not SPONSOR_PRICE_BANDS:
        return None
    total, covered = 0, 0
    for blo, bhi, usd in SPONSOR_PRICE_BANDS:
        a, b = max(lo, blo), min(hi, bhi)
        if a <= b:
            total += (b - a + 1) * usd
            covered += b - a + 1
    return total if covered == hi - lo + 1 else None

def sponsor_min_sats(min_usd):
    """A dollar minimum in sats at SPONSOR_BTC_USD, rounded UP so it never falls short; None without both.
    Exact arithmetic: a float division would turn exactly 1,000 sats into 1,000.0000000001 and round up."""
    if min_usd is None or SPONSOR_BTC_USD is None:
        return None
    return math.ceil(Fraction(min_usd * 100_000_000) / Fraction(str(SPONSOR_BTC_USD)))

def _public_sponsor(c, n):
    """The sponsor shown on block n, if any. Only a sponsorship PAID AT LEAST ITS MINIMUM is shown: an
    unpaid request is text anyone can type, and showing it would let anyone put a name on any block."""
    try:
        r = c.execute("SELECT id,name,lo,hi,status FROM sponsorships WHERE lo<=? AND hi>=? AND "
                      + SPONSOR_PUBLIC_SQL + " ORDER BY paid_at ASC, id ASC LIMIT 1",
                      (n, n)).fetchone()
    except sqlite3.OperationalError:          # a database from before the table; init_db adds it on start
        return None
    return dict(id=r["id"], name=r["name"], lo=r["lo"], hi=r["hi"], status=r["status"]) if r else None

def block_detail(n):
    """Everything about one block, for the block map's pop-up and the explorer's block page: every proof
    that covers it (with who made it and whether it is a fold), who anchored it, a live claim, and a
    paid sponsor. Replaces downloading the whole index to look up one block."""
    tip = chain_tip()
    if n > tip:
        return 404, {"error": "not mined yet", "block": n, "tip": tip}
    spine_hi = (spine_head() or {}).get("hi") or 0
    fr = frontier_hi()
    folds = fold_ids_cached()
    now = time.time()
    c = db()
    try:
        blk = blocked_pubkeys()
        rows = c.execute("SELECT id,lo,hi,handle,pubkey,ts FROM vranges WHERE lo<=? AND hi>=?"
                         " ORDER BY (hi-lo) ASC, ts ASC", (n, n)).fetchall()
        claim = c.execute("SELECT lo,hi,handle,assignee,claimed_at,last_beat FROM ranges"
                          " WHERE status='claimed' AND lo<=? AND hi>=? ORDER BY lo LIMIT 1", (n, n)).fetchone()
        sponsor = _public_sponsor(c, n)
        hold = _sponsor_holds(c, n, n)
    finally:
        c.close()
    proofs = [{"lo": r["lo"], "hi": r["hi"], "ts": r["ts"], "fold": r["id"] in folds,
               "handle": r["handle"] if (r["pubkey"] or "").lower() not in blk else "[removed]",
               "proof": f"/api/proof/{r['id']}"
               if os.path.exists(os.path.join(PROOFS_DIR, f"proof_{r['id']}.bin")) else None}
              for r in rows]
    cl = None
    if claim:
        beat = int(now - (claim["last_beat"] or claim["claimed_at"] or now))
        cl = {"lo": claim["lo"], "hi": claim["hi"], "elapsed": int(now - (claim["claimed_at"] or now)),
              "stale": beat > CLAIM_TTL,
              "handle": claim["handle"] if (claim["assignee"] or "").lower() not in blk else "[removed]"}
    if n == 0:
        status = "genesis"
    elif n <= spine_hi:
        status = "spined"
    elif any(p["fold"] for p in proofs):
        status = "folded"
    elif proofs:
        status = "proved"
    elif cl and not cl["stale"]:
        status = "claimed"
    else:
        status = "open"
    anchored_by = None
    if 0 < n <= spine_hi:
        seg = next((sg for sg in spine_segments_cached() if sg["lo"] <= n <= sg["hi"]), None)
        anchored_by = seg["handle"] if seg else None
    return 200, {"block": n, "tip": tip, "frontier": fr, "spine_hi": spine_hi, "status": status,
                 "unbroken": 0 < n <= fr, "proofs": proofs, "claim": cl, "anchored_by": anchored_by,
                 "sponsor": sponsor,
                 # A paid sponsorship keeps this block for the sponsor bot: normal workers are never offered it.
                 "held": ({"sponsorship": hold[0]["id"], "since": int(now - (hold[0]["paid_at"] or now))}
                          if hold else None),
                 **({"note": GENESIS_MESSAGE} if n == 0 else {})}

# Control, formatting, surrogate and private-use characters: where a right-to-left override, a zero-width
# joiner that makes "Jo<ZWJ>hn" look like "John", or an invisible letter would hide. NOT unassigned (Cn):
# this Python's Unicode tables are older than browsers', so refusing Cn refused emoji newer than it (U+1FAE0
# is unassigned to Python 3.10) that the site's form, checking the same rule, had already accepted.
_SPONSOR_NAME_HIDDEN = ("Cc", "Cf", "Cs", "Co")

def _clean_sponsor_name(v):
    """1 to SPONSOR_NAME_MAX characters, counted in code points after inner whitespace is collapsed, with
    no hidden character. The site's form (assets/board.js nameCheck) applies exactly this rule, so a name
    it lets through is never refused here."""
    if not isinstance(v, str):
        return None
    t = " ".join(v.split())
    if not 1 <= len(t) <= SPONSOR_NAME_MAX or any(unicodedata.category(ch) in _SPONSOR_NAME_HIDDEN for ch in t):
        return None
    return t

def sponsor_info():
    return {"open": SPONSOR_OPEN, "max_blocks": SPONSOR_MAX_BLOCKS, "payments": False,
            "priced": bool(SPONSOR_PRICE_BANDS), "bands": [list(b) for b in SPONSOR_PRICE_BANDS or []],
            "btc_usd": SPONSOR_BTC_USD, "name_max": SPONSOR_NAME_MAX}

def _sponsor_span(lo, hi):
    """Validate a span to sponsor: (code, error) on a bad one, else (None, (lo, hi)). Shared by the quote
    and the request, so the form hears the same refusal whichever it asked first."""
    try:
        lo = int(lo)
        hi = int(lo if hi is None else hi)
    except (TypeError, ValueError):
        return 400, {"error": "lo and hi must be block heights"}
    tip = chain_tip()
    _genesis = genesis_refusal(lo, hi, what="Sponsor")
    if _genesis:
        return _genesis
    if lo < 1 or hi < lo or hi > tip:
        return 400, {"error": f"blocks must run from 1 to {tip}, lowest first"}
    if hi - lo + 1 > SPONSOR_MAX_BLOCKS:
        return 400, {"error": f"at most {SPONSOR_MAX_BLOCKS} blocks in one sponsorship"}
    if hi <= ((spine_head() or {}).get("hi") or 0):
        return 409, {"error": "those blocks are already proven and anchored"}
    # Every block must still be OPEN: not proven, not being proven right now, not already sponsored. Paying
    # for a block the chain already has, or that a worker is minutes from finishing, buys nothing; and two
    # sponsors cannot hold the same block. An unpaid request holds nothing, so it does not block anyone.
    now = time.time()
    c = db()
    try:
        pr = c.execute("SELECT lo FROM vranges WHERE lo<=? AND hi>=? ORDER BY lo LIMIT 1", (hi, lo)).fetchone()
        cl = c.execute("SELECT lo FROM ranges WHERE status='claimed' AND lo<=? AND hi>=?"
                       " AND COALESCE(last_beat, claimed_at) > ? AND claimed_at > ?"
                       " AND (last_beat IS NOT NULL OR claimed_at > ?) ORDER BY lo LIMIT 1",
                       (hi, lo, now - CLAIM_TTL, now - CLAIM_MAX, now - CLAIM_GRACE)).fetchone()
        sp = _sponsor_holds(c, lo, hi)
    finally:
        c.close()
    if pr:
        return 409, {"error": f"block {max(pr['lo'], lo):,} is already proven, so it cannot be sponsored"}
    if cl:
        return 409, {"error": f"block {cl['lo']:,} is being proven right now, so it cannot be sponsored"}
    if sp:
        return 409, {"error": f"block {max(sp[0]['lo'], lo):,} is already sponsored, so it cannot be sponsored again"}
    return None, (lo, hi)

def sponsor_quote(lo, hi):
    """GET /api/sponsor/quote?lo=&hi= -- the minimum for a span. Answers while sponsorship is closed too,
    so the form can show the minimum before anyone can pay it."""
    code, v = _sponsor_span(lo, hi)
    if code:
        return code, v
    lo, hi = v
    usd = sponsor_min_usd(lo, hi)
    # Blocks with nothing to prove from yet (no bridge bundle or witness): a sponsorship holds them until the
    # bridge builds them, and the form says so before anyone pays.
    waiting = sum(1 for h in range(lo, hi + 1) if bundle_path(h) is None)
    return 200, {"lo": lo, "hi": hi, "blocks": hi - lo + 1, "min_usd": usd, "min_sats": sponsor_min_sats(usd),
                 "btc_usd": SPONSOR_BTC_USD, "priced": usd is not None, "waiting": waiting}

def sponsor_request(body):
    """POST /api/sponsor {lo, hi, name, amount_sats}. Closed unless SPONSOR_OPEN=1, and even open it only
    RECORDS a request: payments are not connected, so nothing is charged, queued or proven
    (docs/SPONSORSHIP.md). The pledge must be at least the span's minimum. The answer carries the private
    status link's token, once: only its sha256 is kept."""
    if not SPONSOR_OPEN:
        return 503, {"error": "Sponsorship is not open yet.", "open": False}
    if not isinstance(body, dict):
        return 400, {"error": "expected a JSON object with lo, hi, name and amount_sats"}
    code, v = _sponsor_span(body.get("lo"), body.get("hi"))
    if code:
        return code, v
    lo, hi = v
    name = _clean_sponsor_name(body.get("name"))
    if not name:
        return 400, {"error": f"a name of 1 to {SPONSOR_NAME_MAX} visible characters is required"}
    min_usd = sponsor_min_usd(lo, hi)
    if min_usd is None:
        return 503, {"error": "No minimum is set for these blocks yet, so they cannot be sponsored."}
    min_sats = sponsor_min_sats(min_usd)
    if min_sats is None:
        return 503, {"error": "No bitcoin price is set to turn the minimum into sats, so these blocks cannot be sponsored yet."}
    amount = body.get("amount_sats")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        return 400, {"error": "amount_sats must be a whole number of sats", "min_sats": min_sats, "min_usd": min_usd}
    if amount < min_sats:
        return 400, {"error": f"the minimum for these blocks is ${min_usd}, {min_sats} sats", "min_sats": min_sats,
                     "min_usd": min_usd}
    token = secrets.token_urlsafe(24)
    c = db()
    try:
        cur = c.execute("INSERT INTO sponsorships(lo,hi,name,status,created_at,min_usd,min_sats,pledged_sats,token_hash)"
                        " VALUES(?,?,?,'requested',?,?,?,?,?)",
                        (lo, hi, name, time.time(), min_usd, min_sats, amount, hashlib.sha256(token.encode()).hexdigest()))
        c.commit()
        sid = cur.lastrowid
    finally:
        c.close()
    return 202, {"id": sid, "token": token, "status": "requested", "lo": lo, "hi": hi, "blocks": hi - lo + 1,
                 "name": name, "min_usd": min_usd, "min_sats": min_sats, "pledged_sats": amount,
                 "message": "Recorded. Payments are not connected yet, so nothing has been charged and nothing"
                            " is queued. Keep your status link: it is the only way back to this sponsorship."}

def _status_runs():
    """The block map's runs, from the same cache /api/blockstatus serves, with their upper ends for bisect."""
    runs = json.loads(block_status_cached()[0])["runs"]
    return runs, [r[1] for r in runs]

def _proven_in(lo, hi, runs):
    """Blocks in lo..hi that are proven, folded or anchored (every run is one of those)."""
    runs, ends = runs
    n, i = 0, bisect.bisect_left(ends, lo)
    while i < len(runs) and runs[i][0] <= hi:
        a, b, code = runs[i]
        if code >= 3:
            n += min(b, hi) - max(a, lo) + 1
        i += 1
    return n

def _queue_ahead(c, r):
    """Public paid sponsorships paid before this one, still waiting; None unless this one is waiting."""
    if r["status"] != "paid" or r["paid_at"] is None:
        return None
    return c.execute("SELECT COUNT(*) FROM sponsorships WHERE status='paid' AND " + SPONSOR_PUBLIC_SQL
                     + " AND (paid_at < ? OR (paid_at = ? AND id < ?))",
                     (r["paid_at"], r["paid_at"], r["id"])).fetchone()[0]

_SPONSOR_COLS = "id,lo,hi,name,status,created_at,min_usd,min_sats,pledged_sats,paid_sats,paid_at,proven_at"

def sponsor_status(token):
    """GET /api/sponsor/status/<token> -- one sponsorship, for whoever holds its private link."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token or ""):
        return 404, {"error": "no sponsorship with that link"}
    c = db()
    try:
        try:
            r = c.execute(f"SELECT {_SPONSOR_COLS} FROM sponsorships WHERE token_hash=?",
                          (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        except sqlite3.OperationalError:
            r = None
        if not r:
            return 404, {"error": "no sponsorship with that link"}
        ahead = _queue_ahead(c, r)
    finally:
        c.close()
    out = {k: r[k] for k in ("id", "lo", "hi", "name", "status", "min_usd", "min_sats", "pledged_sats", "paid_sats",
                             "created_at", "paid_at", "proven_at")}
    out.update(blocks=r["hi"] - r["lo"] + 1, proven_blocks=_proven_in(r["lo"], r["hi"], _status_runs()),
               queue_ahead=ahead, public=_is_public_sponsorship(r))
    return 200, out

def sponsors_public():
    """GET /api/sponsors -- every public sponsorship, newest payment first, for the sponsors page. Only
    what the block map would show anyway plus the amount: never the pledge, the invoice, the note or the
    link."""
    c = db()
    try:
        try:
            rows = c.execute(f"SELECT {_SPONSOR_COLS} FROM sponsorships WHERE {SPONSOR_PUBLIC_SQL}"
                             " ORDER BY paid_at DESC, id DESC").fetchall()
        except sqlite3.OperationalError:
            rows = []
        aheads = [_queue_ahead(c, r) for r in rows]
    finally:
        c.close()
    runs = _status_runs() if rows else None
    return {"sponsorships": [{"id": r["id"], "name": r["name"], "lo": r["lo"], "hi": r["hi"],
                              "blocks": r["hi"] - r["lo"] + 1, "status": r["status"],
                              "paid_sats": r["paid_sats"], "min_usd": r["min_usd"], "min_sats": r["min_sats"],
                              "paid_at": r["paid_at"],
                              "proven_at": r["proven_at"], "proven_blocks": _proven_in(r["lo"], r["hi"], runs),
                              "queue_ahead": ahead} for r, ahead in zip(rows, aheads)],
            "open": SPONSOR_OPEN, "priced": bool(SPONSOR_PRICE_BANDS)}

def state(slim=False):
    now = time.time()
    _tip_now = chain_tip()    # read once: the board must not report a pct and a tip from two scans
    c = db()
    proven = proven_count()   # distinct covered blocks (overlap-safe), not SUM(hi-lo+1) which double-counts
    folded = folded_count()   # blocks inside a range made by FOLDING -- see fold_spans
    blk = blocked_pubkeys()   # moderation takedown list — hide these pubkeys from the public board
    # board window: all verified + claimed, then a few open around the frontier
    fr = frontier_hi()
    # rolling window around the frontier: a little behind, then open blocks ahead (synthesised so the
    # board shows what's next to prove even before those range rows exist).
    start = max(0, (fr // RANGE_SIZE) - 1) * RANGE_SIZE
    existing = {r["id"]: r for r in c.execute("SELECT * FROM ranges WHERE lo >= ? ORDER BY lo LIMIT 60", (start,))}
    board = []
    for i in range(18):
        lo = start + i * RANGE_SIZE; hi = lo + RANGE_SIZE - 1
        if lo >= chain_tip(): break
        rid = f"{lo}-{hi}"; r = existing.get(rid)
        if r and r["status"] in ("claimed", "verified"):
            _h = r["handle"] if (r["assignee"] or "").lower() not in blk else "[removed]"
            b = {"id": rid, "lo": lo, "hi": hi, "status": r["status"], "handle": _h}
            if r["status"] == "claimed":
                b["elapsed"] = int(now - (r["claimed_at"] or now))
                b["beat"] = int(now - (r["last_beat"] or r["claimed_at"] or now))
                b["stale"] = b["beat"] > CLAIM_TTL
        else:
            b = {"id": rid, "lo": lo, "hi": hi, "status": "open", "handle": None}
        board.append(b)
    # DISTINCT blocks per contributor (interval-merge) — reconciles with the headline 'proven' by
    # construction; a stored per-submit counter can drift on overlapping submissions.
    _dbp = contributions_by_pubkey()
    _rmap = rotation_map()
    # Moderation has to follow rotations too, or a takedown is trivially escaped by rotating to a fresh
    # key: the blocked key's blocks would reappear on a head that is not itself on the list. rotate()
    # refuses when either key is blocked, but a key can be blocked AFTER it has rotated, so the read
    # path cannot rely on that. Blocking any key in a chain hides the head it resolves to.
    _blk_resolved = blk | {resolve_pubkey(b, _rmap) for b in blk}
    # One row per RESOLVED identity. Iterating `contributors` would emit a rotated-away key as well,
    # and rotate() guarantees the head has a row, so key off the resolved totals instead.
    _handles = {r["pubkey"]: r["handle"] for r in c.execute("SELECT pubkey,handle FROM contributors")}
    # #293: the release each contributor was last seen running, so they can notice their own worker is
    # stale without having to ask anyone. None until they run a CLI new enough to say.
    try:
        _versions = {r["pubkey"]: r["last_version"]
                     for r in c.execute("SELECT pubkey,last_version FROM contributors")}
    except Exception:
        _versions = {}
    # A contributor counts if they did ANY of the three. Someone who only folds or only anchors was
    # invisible here before, which is the whole point of splitting the kinds.
    ncontrib = sum(1 for v in _dbp.values() if v["proved"] or v["folded"] or v["anchored"])
    # Fold OPERATIONS chain-wide, the leaderboard's column summed. `folded` above counts BLOCKS inside a
    # fold; this counts folds, which is what "N of the proven - 1 folds it takes to make one proof" needs.
    # The site used to count them from /api/vranges, a full-index download per visitor.
    folds = sum(v["folded"] for v in _dbp.values())
    leaders = sorted(
        (dict(id=pk[:10], handle=_handles.get(pk), blocks=v["proved"],
              proved=v["proved"], folded=v["folded"], anchored=v["anchored"],
              version=_versions.get(pk))
         for pk, v in _dbp.items()
         if pk.lower() not in _blk_resolved and (v["proved"] or v["folded"] or v["anchored"])),
        # Ranked on blocks proved, the headline number; folds and absorptions break ties beneath it
        # rather than competing with it, since one fold is not worth one block and nothing here says
        # what it is worth. `blocks` is kept as an alias so an existing client does not break.
        key=lambda d: (d["proved"], d["folded"], d["anchored"]), reverse=True)[:8]
    recent = [dict(range=s["range_id"], handle=(s["handle"] if s["pubkey"].lower() not in blk else "[removed]"),
                   verified=bool(s["verified"]), ts=s["ts"], note=s["note"])
              # 40, not 8: the feed now carries three kinds of work (proved / folded / spine) and the
              # board filters them client-side. At 8 rows a single fast prover fills the window and
              # the other two kinds are invisible under every filter, which is the problem #114 was
              # about. Still a few KB.
              for s in c.execute("SELECT * FROM submissions ORDER BY ts DESC LIMIT 40")]
    # full verified + claimed lists so the client can browse/search/filter any block, not just the
    # frontier window (each is small: claims are few, verified ranges are RANGE_SIZE-coarse).
    vranges = build_vranges(c, blk) if not slim else []
    claims = []
    for r in c.execute("SELECT lo,hi,handle,assignee,claimed_at,last_beat FROM ranges WHERE status='claimed' ORDER BY lo"):
        beat = int(now - (r["last_beat"] or r["claimed_at"] or now))
        claims.append(dict(lo=r["lo"], hi=r["hi"],
                           handle=(r["handle"] if (r["assignee"] or "").lower() not in blk else "[removed]"),
                           elapsed=int(now - (r["claimed_at"] or now)), stale=beat > CLAIM_TTL))
    # Blocks parked after MAX_ATTEMPTS, plus how long the frontier has been stuck. Without this a stall
    # is invisible: the frontier is the lowest unproven block, so ONE bad block pins it while every other
    # signal stays green — `proven` keeps climbing as workers prove ahead of the gap, which is exactly
    # how a 45-minute stall went unnoticed on 2026-07-28.
    failed = [dict(id=r["id"], lo=r["lo"], hi=r["hi"], attempts=r["attempts"],
                   last_error=(r["last_error"] or "")[:200],
                   since=int(now - (r["last_failed_at"] or now)))
              for r in c.execute("SELECT id,lo,hi,attempts,last_error,last_failed_at FROM ranges "
                                 "WHERE status='failed' ORDER BY lo")]
    # Find what covers the next needed block by INTERVAL, not by id. Looking up id == str(fr+1) is the
    # same id-vs-interval mistake fixed in the claim path, and it reads exactly backwards once ranges
    # can be wide: with 38000-38999 claimed and being proved, the row whose id is "38000" is a distinct,
    # untouched single-block row, so the blocker reported "open, attempts 0, stalled_for 0" — i.e. it
    # said nothing is happening while a worker was 200 blocks into proving it.
    #
    # A LIVE range covering the block is the real answer; the bare single-block row is the fallback for
    # when nothing covers it (genuinely open, which is the interesting stall case).
    nb = fr + 1
    blocker = c.execute(
        "SELECT id,status,attempts,last_failed_at,claimed_at,verified_at,assignee,handle,last_beat FROM ranges "
        "WHERE lo <= ? AND hi >= ? AND status IN ('claimed','verified','failed') "
        "ORDER BY (hi-lo) ASC LIMIT 1", (nb, nb)).fetchone()
    if blocker is None:
        blocker = c.execute("SELECT id,status,attempts,last_failed_at,claimed_at,verified_at,assignee,handle,last_beat"
                            " FROM ranges WHERE id=?", (str(nb),)).fetchone()
    # #281: measure the stall from when the FRONTIER last moved, not from the blocking row's own
    # timestamp.
    #
    # Two separate reasons the old reading could not work. It exempted `verified` rows, so a range
    # sitting over the blocker reported 0 — even though a verified range covering fr+1 is a stall BY
    # DEFINITION: if it could advance the frontier, fr would already be past it. And the row is the
    # wrong clock anyway, because `claim()` does INSERT OR REPLACE on it: every time a worker takes
    # the blocking block the timestamp resets, so a blocker re-claimed every CLAIM_TTL would look
    # permanently fresh while nothing at all advanced.
    #
    # The frontier's own high-water mark has neither problem. It moves only on real progress, and
    # nothing else writes it.
    row = c.execute("SELECT v FROM meta WHERE k='frontier_mark'").fetchone()
    stalled_for = 0
    try:
        prev_hi, prev_ts = str(row["v"]).split(":", 1) if row else (None, None)
    except Exception:
        prev_hi, prev_ts = None, None          # malformed mark: re-stamp rather than report nonsense
    if prev_hi is None or int(prev_hi) != fr:
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('frontier_mark',?)", (f"{fr}:{now}",))
        c.commit()
    else:
        stalled_for = max(0, int(now - float(prev_ts)))

    # #285: `stalled_for` alone stops meaning "something is wrong" as the frontier climbs. Measured
    # across all 230,000 bundles on 2026-09-12, the median bundle grows 354x with height -- 6 KB near
    # genesis, 2.16 MB by block 220,000 -- and a 1.88 MB block is ~880 segments and ~52 minutes of
    # entirely healthy proving. Above roughly block 180,000 the MEDIAN block takes tens of minutes, so
    # a healthy board would sit at stalled_for 1,800-3,600 permanently and the number added to make a
    # frozen frontier visible would stop distinguishing one.
    #
    # So publish the judgement too, rather than leaving every consumer to infer it. The frontier is
    # merely WAITING when a live worker holds the blocker; it needs a human when:
    #   * a VERIFIED range covers it -- it can never seam onto the frontier (the #281 case);
    #   * nobody holds it and nobody has for longer than a claim cycle -- the work is not being done;
    #   * it has failed its way to MAX_ATTEMPTS.
    _st = blocker["status"] if blocker else "open"
    _att = (blocker["attempts"] if blocker else 0) or 0
    _hold = next(iter(_sponsor_holds(c, nb, nb)), None)
    if _st == "verified":
        _attn, _attn_why = True, ("a verified range covers this block but cannot seam onto the frontier; "
                                  "it will never advance until the block is re-proved")
    elif _att >= MAX_ATTEMPTS:
        _attn, _attn_why = True, f"the blocking range has failed {_att} times (MAX_ATTEMPTS={MAX_ATTEMPTS})"
    elif _st == "claimed":
        # ⛔ "a live worker is proving it" was the whole answer, and for block 39,413 it was the whole
        # answer for FIVE HOURS: claimed, re-claimed, heartbeating, zero submissions, needs_attention
        # false. The claim was live every time it was looked at; what nobody could see was that the
        # same worker kept taking it and never finishing.
        #
        # So say WHO holds it and WHAT THEY ARE RUNNING (#293), and stop calling it benign once the
        # frontier has sat through several claim cycles. 39,413 turned out to be #286 — assembling
        # 1,012 segment receipts took 757s against a 600s silence timeout that release removed — and
        # the holder's version is the single fact that would have said so without proving it again.
        _vrow = None
        try:
            if blocker["assignee"]:
                _vrow = c.execute("SELECT last_version FROM contributors WHERE pubkey=?",
                                  (blocker["assignee"],)).fetchone()
        except Exception:
            _vrow = None
        _ver = (_vrow["last_version"] if _vrow else None) or "unknown"
        _who = (blocker["handle"] or "an anonymous prover")
        # ⛔ But "over two claim cycles" alone cannot tell 39,413 from a block that is simply big. Block
        # 55,862 (3,261 segments) held the frontier 2h43m on 2026-09-13 under ONE claim — claimed once,
        # beating until 6 s before its only submission, verified 9,830 s after the claim — and was
        # flagged "may be failing on a bug already fixed" while running the latest release.
        #
        # What separates them is already in the row. `claim()` does INSERT OR REPLACE with
        # claimed_at=now, so a worker stuck in 39,413's kill-retry-reclaim loop carries a claim far
        # YOUNGER than the stall. A worker grinding through a big block carries one about as old as the
        # stall itself: it took the block at most one claim cycle after the frontier stopped. And the
        # worker only beats when a segment finishes (#256), so a HUNG prover's beat goes stale and its
        # claim lapses after CLAIM_TTL; a fresh beat means work is still landing. CLAIM_MAX still caps a
        # continuous hold outright. So a continuous claim with a live beat is waiting, not stuck.
        _claim_age = int(now - (blocker["claimed_at"] or now))
        _beat_age = int(now - (blocker["last_beat"] or blocker["claimed_at"] or now))
        _continuous = _claim_age >= stalled_for - CLAIM_TTL
        _live = _beat_age <= CLAIM_TTL
        if stalled_for > 2 * CLAIM_TTL and not (_continuous and _live):
            _attn = True
            if not _continuous:
                _attn_why = (f"{_who} has re-taken this block over {stalled_for}s — over two claim cycles — "
                             f"without ever submitting it (this claim is {_claim_age}s old); their worker "
                             f"is running hazync-worker/{_ver} and may be failing on a bug already fixed")
            else:
                _attn_why = (f"{_who} has held this block for {_claim_age}s but its last heartbeat was "
                             f"{_beat_age}s ago, so no proving progress is landing "
                             f"(hazync-worker/{_ver})")
        elif stalled_for > 2 * CLAIM_TTL:
            _attn = False
            _attn_why = (f"{_who} has held this block continuously for {_claim_age // 3600}h "
                         f"{_claim_age % 3600 // 60}m and is still heartbeating ({_beat_age}s ago, "
                         f"hazync-worker/{_ver}) — a large block, not a stall")
        else:
            _attn = False
            _attn_why = f"a live worker is proving it ({_who}, hazync-worker/{_ver})"
    elif _hold:
        # Normal workers are kept off a held block, so "nobody has held it for a claim cycle" would be a false
        # alarm; the real question is whether the sponsor bot is getting to it.
        _held_for = int(now - (_hold["paid_at"] or now))
        if _held_for > SPONSOR_HOLD_ALERT:
            _attn, _attn_why = True, (f"held for sponsorship #{_hold['id']} for {_held_for}s, longer than "
                                      f"SPONSOR_HOLD_ALERT={SPONSOR_HOLD_ALERT}; normal workers are kept off it, "
                                      f"so only the sponsor bot can move the frontier, and it has not")
        else:
            _attn, _attn_why = False, f"held for sponsorship #{_hold['id']}; the sponsor bot proves it"
    elif stalled_for > CLAIM_TTL:
        _attn, _attn_why = True, (f"nobody has held this block for {stalled_for}s, longer than a claim "
                                  f"cycle (CLAIM_TTL={CLAIM_TTL})")
    else:
        _attn, _attn_why = False, "open and waiting for a prover to take it"
    c.close()
    return {
        # spine_hi sits NEXT TO frontier deliberately. The spine is the only shippable artifact — the
        # single genesis-anchored proof /api/spine/proof serves and the README's 30-second demo
        # downloads — and it is driven by one serial job that, until hazync#74, nothing ran. A stalled
        # spine is INVISIBLE from every other signal: proven climbs, frontier climbs, every gate stays
        # green, and only this number quietly stops. Reporting it beside frontier makes the gap
        # (frontier - spine_hi) a thing you can see rather than something you have to notice.
        # None means no spine at all, which is different from a stale one and should read differently.
        "progress": {"proven": proven, "folded": folded, "folds": folds, "frontier": fr, "tip": _tip_now,
                     "pct": round(100.0*fr/_tip_now, 3) if _tip_now else 0, "contributors": ncontrib,
                     "spine_hi": (spine_head() or {}).get("hi")},
        "failed": failed,
        # `block` is the block the frontier needs next; `id` is the RANGE responsible for it, which is
        # not the same thing once ranges can be wide — reporting str(fr+1) as the id hid a claimed
        # 38000-38999 behind an untouched single-block row of the same name.
        #
        # #281: THIS KEY DID NOT EXIST. `blocker` and `stalled_for` were computed a few lines above and
        # then dropped on the floor — the comment describing them survived, the value never reached the
        # API. So the one signal built to make a frozen frontier visible could not be read by anything:
        # not the board, not a monitor, not a human with curl. The thirteen-hour freeze on 2026-09-11
        # went unreported for two compounding reasons: the `verified` exemption above returned 0, and
        # even that 0 was never published. A signal nobody can read is not a signal.
        "blocked": {"block": nb,
                    "id": blocker["id"] if blocker else None,
                    "status": blocker["status"] if blocker else "open",
                    "attempts": (blocker["attempts"] if blocker else 0) or 0,
                    "sponsorship": _hold["id"] if _hold else None,
                    "stalled_for": stalled_for,
                    "needs_attention": _attn, "why": _attn_why},
        "board": board, "leaderboard": leaders, "recent": recent,
        "vranges": vranges, "claims": claims, "range_size": RANGE_SIZE,
        "frontier_proof": frontier_proof(),
        "timeline": timeline(fr),
        "signatures": "ed25519" if HAVE_ED else "dev (no signature lib installed)",
        "verify_mode": VERIFY,
    }

def state_cached(slim=False):
    """Serialised /api/state with a short TTL. state() does full-table scans + a frontier walk on every
    call, so under an anonymous GET flood recomputing it per request is the cheapest way to pin the box.
    A ~1.5s cache collapses a burst into one recompute while keeping the board effectively live.

    Slim and full are cached SEPARATELY: they are different payloads, and sharing one slot would serve
    whichever was computed last to both callers."""
    # #265: single-flight. The old "a rare cold-start double-compute is harmless" stopped being rare once a
    # recompute took longer than STATE_TTL under load: every request missed and recomputed at once.
    return _single_flight("state:" + ("slim" if slim else "full"), STATE_TTL,
                          lambda: json.dumps(state(slim=slim)).encode())

_MID_CACHE = {"v": None}

def expected_method_id():
    # The guest image id this coordinator verifies against == HOST_BIN's method-id. Exposed via /api/meta
    # so a contributor can pre-flight `host method-id` BEFORE proving, instead of discovering a mismatch
    # only when their first submit is rejected. Cached — a binary's id never changes at runtime.
    if _MID_CACHE["v"] is None and HOST_BIN:
        try:
            r = subprocess.run([HOST_BIN, "method-id"], capture_output=True, text=True, timeout=30)
            _MID_CACHE["v"] = next((t for t in r.stdout.split()
                                    if len(t) == 64 and all(ch in "0123456789abcdef" for ch in t)), None)
        except Exception:
            pass
    return _MID_CACHE["v"]


def _claim_by_nonce(c, pk, nonce, now):
    """The live claim this exact claim request already made, if any (#268) -- see claim()."""
    if not nonce or not pk:
        return None
    r = c.execute("SELECT id FROM ranges WHERE status='claimed' AND assignee=? AND claim_nonce=?"
                  " AND COALESCE(last_beat, claimed_at) > ? AND claimed_at > ?",
                  (pk, nonce, now - CLAIM_TTL, now - CLAIM_MAX)).fetchone()
    return r["id"] if r else None

def claim(body):
    """Hand out the earliest block that is neither proven nor already claimed.

    Width is ONE block. That matters: the objections that removed allocation in #37 were all
    objections to WIDE claims — "one bad block halts everyone" and "width is a reliability bet" were
    about a 67-minute commitment to a 1000-block range, where any failure discarded the lot. At width
    1 a claim is a few seconds of GPU time, so a failure costs a few seconds and the block simply
    reopens.

    No heartbeat. A claim expires CLAIM_TTL seconds after it is taken, whether the worker is alive or
    not, so a worker that dies mid-block leaves nothing to reap — the block reopens by itself. That
    removes the machinery (expiry sweeps, retry counters, orphan detection) without removing the
    ordering benefit that made claims worth having.

    A claim is ADVISORY-ON-TOP: `submit` accepts any height regardless of who claimed it. So a bug
    here can waste effort but can never lock a contributor out, which is the property free-running had
    and the one worth keeping.
    """
    pk = body.get("pubkey", "")
    handle = clean_handle(body.get("handle"), handle_cap(pk))
    # A claim writes the handle to `contributors`, so it takes the same handle rules as a submit.
    if handle_refused(handle, pk): return 400, {"error": "that handle is reserved — please pick another"}
    nonce = str(body.get("nonce") or "")[:64] or None
    now = time.time()
    _blocker = frontier_hi() + 1        # read OUTSIDE the lock — see the note at its use below
    with _lock:
        c = db()
        # #268: a claim whose RESPONSE was lost (client or proxy timeout, a dropped connection) is
        # retried by the worker, and before this each retry took the next free block, orphaning the
        # first for a full CLAIM_TTL -- 19 blocks under the frontier in the 2026-09-11 lock-up. The
        # worker sends one nonce per claim and reuses it on every retry of THAT claim, so a retry gets
        # the block it was already given. A later claim carries a fresh nonce and still cannot re-take
        # its own block, so the 39,318 rule below is untouched. No nonce: exactly the old behaviour.
        again = _claim_by_nonce(c, pk, nonce, now)
        if again:
            c.close()
            return 200, {"ok": True, "range": again, "ttl": CLAIM_TTL,
                         "note": "claimed for %d minutes; submissions are accepted for any height regardless"
                                 % int(CLAIM_TTL / 60)}
        # CLAIM_OPEN_MAX: a key already holding that many live claims gets no more until one is proven or
        # lapses. Counted with the liveness `held` uses, so a finished or released claim frees its slot and a
        # slow prover that beats keeps its blocks. A retry of a claim this key already made was answered
        # above, so a lost response is never refused.
        if CLAIM_OPEN_MAX > 0 and pk:
            open_n = c.execute("SELECT COUNT(*) FROM ranges WHERE assignee=? AND " + LIVE_CLAIM_SQL,
                               (pk,) + _live_claim_args(now)).fetchone()[0]
            if open_n >= CLAIM_OPEN_MAX:
                c.close()
                return 429, {"error": f"this key already holds {open_n} claimed blocks that are not finished "
                                      f"(at most {CLAIM_OPEN_MAX}): prove one, or let one lapse, before claiming "
                                      f"another",
                             "open_claims": open_n, "max": CLAIM_OPEN_MAX}
        proven, held = coverage_and_held(c, now)
        # CLAIM_RETAKE_WAIT: the blocks this key's own never-beaten claims let lapse are held FOR THIS KEY, in both
        # the frontier re-offer and the scan below. Every other key is offered them as soon as the grace ends.
        if pk and CLAIM_RETAKE_WAIT > 0:
            held = held | {r["lo"] for r in c.execute(
                "SELECT lo FROM ranges WHERE assignee=? AND status='claimed' AND last_beat IS NULL AND claimed_at > ?",
                (pk, now - CLAIM_RETAKE_WAIT))}
        # A claim blocks EVERY worker for CLAIM_TTL, including the one that made it. That looks like a
        # bug — a worker locked out of retrying its own failed block — and on 2026-08-01 it was
        # "fixed" so a worker could re-pick its own claim. That was wrong, and reverted the same day.
        #
        # The block it was meant to unstick (39,318) does not fail fast: it HANGS the prover for over
        # an hour (its bundle is 1,098,218 bytes against ~4 KB for its neighbours — see the issue).
        # With self-reclaim allowed, the worker retried that same block forever and proved nothing
        # else. The hour-long lockout is not an oversight, it is the rate limit that keeps one bad
        # block from consuming a worker: the hole persists, but the fleet makes progress, which is the
        # trade #37 argued for in the first place.
        # A claim is held while the worker is ALIVE, not for a fixed wall-clock hour.
        #
        # Expiry used to be measured from when the claim was taken, with no heartbeat, on the
        # reasoning that at width 1 a claim is a few seconds of GPU. That holds for early blocks and
        # breaks for real ones: block 741,000 (670 inputs) is a MEASURED 3,275s = 55 min, which
        # finishes five minutes inside a 3600s expiry. Anything larger expired mid-prove, the
        # coordinator handed the same block to someone else, and two provers burned identical
        # GPU-hours — invisibly, because the loser's submission is discarded as "already proven".
        #
        # So: liveness from last_beat (refreshed by POST /api/beat while a prove is in flight), and
        # CLAIM_MAX as a hard ceiling so a wedged-but-beating worker cannot hold a block forever.
        # A worker that dies stops beating and the block reopens in CLAIM_TTL, exactly as before.
        # Workers that predate the beat send none, so COALESCE falls back to claimed_at and they keep
        # the old behaviour rather than breaking.
        _ceiling = provable_tip()          # what the bridge can serve, not a hardcoded chain height
        # #281: the block the CHAIN needs comes before the block the board merely lacks.
        #
        # `proven` above is COVERAGE, and coverage is a different question from "does the frontier
        # advance". A range can cover frontier+1 and still be unable to seam onto it: a bad-bounds
        # range (refused at submit since #283), or — still possible, and not fixable at submit — a
        # proof of the right HEIGHT against the wrong predecessor state. A fork, or a stale bundle
        # after a reorg. Width 1 makes that trivial to submit and nothing rejects it, because proving
        # out of order is the whole design and the coordinator cannot know which predecessor is real.
        #
        # Whenever that happens the scan below walks straight past the one block that would unstick
        # the board — it is "proven", after all — and the frontier stops for good while `proven` keeps
        # climbing. That is the 30,050 freeze, and it cost thirteen hours.
        #
        # If frontier+1 is COVERED and the frontier still has not moved past it, the cover cannot
        # seam: if it could, the frontier would already be beyond it. So hand that block out again.
        # `held` still applies, so this re-offers at most once per CLAIM_TTL — the same hour-long rate
        # limit that stops one bad block consuming a worker, and the reason this cannot become a
        # re-prove loop. A block proved twice costs one prove; a frontier frozen on a bad cover costs
        # everything above it.
        #
        # `_blocker` is read BEFORE the lock, deliberately: _frontier_chain scans every vrange on a
        # cache miss (41k rows on the live board) and holding the global write lock across that would
        # jam every claim, beat and submit — the #265 failure. A stale frontier here is harmless; the
        # worst case is re-offering a block that has just been seamed, which `held` already bounds.
        if (_blocker < _ceiling and _blocker in proven and _blocker not in held
                and witness_available(_blocker)):
            h = _blocker
        else:
            h = 1
            while h < _ceiling:
                if h not in proven and h not in held and witness_available(h):
                    break
                h += 1
            else:
                c.close()
                return 409, {"error": "nothing available to claim"}
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at,claim_nonce)"
                  " VALUES(?,?,?,'claimed',?,?,?,?)", (str(h), h, h, pk, handle, now, nonce))
        # A contributor may claim long before they ever submit — and if their worker cannot finish,
        # they never submit at all. That is the case this whole thing exists to diagnose, so the
        # version is recorded HERE as well, on a row that may not exist until their first proof.
        c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                  (pk, handle, now))
        note_client_version(c, pk, body)
        c.commit()
        c.close()
    return 200, {"ok": True, "range": str(h), "ttl": CLAIM_TTL,
                 "note": "claimed for %d minutes; submissions are accepted for any height regardless"
                         % int(CLAIM_TTL / 60)}


def beat(body):
    """Refresh a claim the caller already holds, so a long prove is not stolen out from under it.

    Deliberately the smallest thing that solves the problem: it moves one timestamp. None of the
    machinery #37 removed comes back — no expiry sweep, no retry counters, no orphan detection. A
    worker that stops beating simply stops holding the claim, which is the pre-existing behaviour.

    Only the assignee may beat their own claim, and it cannot resurrect an expired or verified one:
    a block that already reopened has been handed on, and quietly taking it back would produce the
    duplicate work this exists to prevent.
    """
    rid, pk = body.get("range"), body.get("pubkey", "")
    if not rid or not pk:
        return 400, {"error": "range and pubkey required"}
    if not parse_any_range(rid):
        return 400, {"error": "invalid range id"}

    # A beat used to be authenticated by ASSIGNEE MATCH alone, and pubkeys are public on the board — so
    # anyone could renew anyone else's claim and hold a block out of the reopen pool (audit #5, L-2).
    # A SIGNATURE IS NOW REQUIRED.
    #
    # Landed as a hard requirement rather than phased in, because this ships with a guest re-baseline:
    # every proof made against the old id is invalid, so every worker must take the new release anyway.
    # A protocol break costs nothing at exactly this moment, and phasing would have left the hole open
    # for a full release cycle for no benefit — an attacker just omits the field.
    #
    # The signed message is "<rid>:<ts>", not "<rid>". Signing the id alone leaves a captured beat
    # replayable forever: an attacker who saw one legitimate beat could keep the claim alive after the
    # holder ABANDONED it, which is the griefing case this is meant to stop. Binding a timestamp
    # collapses that to BEAT_SKEW seconds. CLAIM_MAX remains the outer bound in every case.
    sig, ts = body.get("sig", ""), body.get("ts")
    if not sig:
        return 401, {"error": "beat must be signed: sig over '<range>:<ts>' (ed25519)"}
    if not is_hex(pk, 32) or not is_hex(sig, 64):
        return 400, {"error": "pubkey must be 32-byte hex and sig 64-byte hex (ed25519)"}
    # ts is INTEGER unix seconds, and that is part of the protocol rather than a preference.
    #
    # The signed message is built by formatting ts, so client and server must render the SAME
    # characters or the signature cannot verify. An earlier revision accepted any number and used
    # float(), which silently coupled the wire format to Python's float repr: the reference worker
    # signs f"{time.time()}" and it agreed with itself, so it passed. Any other client sending the
    # obvious thing — a whole-number JSON timestamp, as JWT iat/exp do — signs "<rid>:1754305000",
    # the server rebuilds "<rid>:1754305000.0", and the beat is rejected 403 "signature does not
    # verify for that pubkey": an error blaming the KEY for what is a number-formatting mismatch,
    # on a public multi-operator board where third-party workers are the point. Found by testing a
    # non-Python client's encoding against a live coordinator, not by reading this back.
    #
    # Integer seconds removes the ambiguity outright, and BEAT_SKEW is 120s so sub-second precision
    # buys nothing. A non-integer is now a CLEAR 400 naming the canonical form, instead of a 403
    # pointing at the wrong thing.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return 400, {"error": "ts required (unix seconds), and must be a number"}
    if float(ts) != int(ts):
        return 400, {"error": "ts must be INTEGER unix seconds — the signed message is '<range>:<ts>' "
                              "with ts rendered as a whole number"}
    ts = int(ts)
    # Reject a beat from outside the window in BOTH directions. A far-future ts would otherwise be a
    # signature that stays valid indefinitely — the replay hole reintroduced by the caller's clock.
    if abs(time.time() - ts) > BEAT_SKEW:
        return 400, {"error": f"beat timestamp outside +/-{BEAT_SKEW}s — check your clock"}
    if not verify_sig(pk, sig, f"{rid}:{ts}".encode()):
        return 403, {"error": "beat signature does not verify for that pubkey"}
    now = time.time()
    with _lock:
        c = db()
        r = c.execute("SELECT status, assignee, claimed_at FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "claimed" or (r["assignee"] or "") != pk:
            c.close()
            return 409, {"error": "not your claim (or it has expired and been handed on)"}
        if (r["claimed_at"] or now) < now - CLAIM_MAX:
            c.close()
            return 409, {"error": "claim held past CLAIM_MAX — release it and re-claim"}
        c.execute("UPDATE ranges SET last_beat=? WHERE id=?", (now, rid))
        c.commit()
        c.close()
    return 200, {"ok": True, "held_for": CLAIM_TTL}

# ---- heartbeat rejection log ---------------------------------------------------------------------
# The first third-party contributor (bip-448, 2026-09-11) sent 404 beats over ten hours and every one
# was rejected 409 -- and nothing on this side recorded why. log_message() above silences the access
# log, beat() never sees the client address, and the 409 text cannot say whether the range does not
# exist, was never theirs, has been verified, or was theirs and expired. Diagnosing it took nginx logs
# on another box and guesswork about the one question that mattered: WHICH claim did the worker think
# it held? So every rejected beat is logged with the caller, the reason, and -- for a 409 -- what the
# database actually holds for that range. The API response is unchanged.
#
# Identical rejections (same ip, pubkey, range, code, reason) are logged once per BEAT_LOG_WINDOW
# seconds with a count of the ones suppressed in between: a stuck worker beating every 90 s must not
# bury the journal, and neither can someone replaying bad beats inside the rate limit.
BEAT_LOG_WINDOW = int(os.environ.get("BEAT_LOG_WINDOW", "600"))
_beat_log_seen = {}                 # key -> [last_logged_ts, suppressed_since]
_beat_log_lock = threading.Lock()

def _beat_range_state(rid, pk):
    """What the DB holds for a beaten range, relative to the caller: the fact a 409 cannot carry."""
    try:
        c = db()
        r = c.execute("SELECT status, assignee, claimed_at, last_beat FROM ranges WHERE id=?",
                      (rid,)).fetchone()
        who = c.execute("SELECT handle FROM contributors WHERE pubkey=?", (pk,)).fetchone()
        c.close()
    except Exception as e:                       # a logging aid must never break the request
        return f"state=<db error: {e}>"
    tag = f"handle={who['handle']!r} " if who else "handle=<not a contributor> "
    if not r:
        return tag + "state=no-such-range"
    now = time.time()
    a = r["assignee"] or ""
    holder = "caller" if a == pk else (a[:16] or "none")
    age = lambda t: f"{now - t:.0f}s ago" if t else "never"
    return (tag + f"state={r['status']} assignee={holder} claimed={age(r['claimed_at'])} "
            f"last_beat={age(r['last_beat'])}")

def log_beat_rejection(ip, body, code, obj):
    if not isinstance(body, dict):
        body = {}
    rid = str(body.get("range", ""))[:48]
    pk = str(body.get("pubkey", ""))[:64]
    reason = str((obj or {}).get("error", ""))[:120]
    key = (ip, pk, rid, code, reason)
    now = time.time()
    with _beat_log_lock:
        e = _beat_log_seen.get(key)
        if e and now - e[0] < BEAT_LOG_WINDOW:
            e[1] += 1
            return False
        suppressed = e[1] if e else 0
        _beat_log_seen[key] = [now, 0]
        if len(_beat_log_seen) > 10000:          # bound memory against key churn
            for k in [k for k, v in _beat_log_seen.items() if now - v[0] >= BEAT_LOG_WINDOW]:
                del _beat_log_seen[k]
    state = _beat_range_state(rid, pk) if code == 409 else ""
    more = f" (+{suppressed} identical since the last line)" if suppressed else ""
    print(f"[beat-rejected] {code} ip={ip} pubkey={pk[:16]} range={rid!r} ts={body.get('ts')!r} "
          f"reason={reason!r} {state}{more}".rstrip(), flush=True)
    return True

def witness_available(blk):
    """True if a witness for `blk` can be served. Free-running proving needs arbitrary heights, and
    the bridge already provides them — the lookup is a direct file path with no frontier window."""
    for f in ([os.path.join(BRIDGE_DIR, f"bundle_{blk}.json")] if BRIDGE_DIR else []) \
             + [os.path.join(WITNESS, f"block_{blk}.json")]:
        if os.path.exists(f):
            return True
    return False


def _overlapping_vrange(c, lo, hi):
    """The lowest verified range sharing at least one block with [lo..hi], or None."""
    return c.execute("SELECT id,lo,hi FROM vranges WHERE lo <= ? AND hi >= ? ORDER BY lo LIMIT 1",
                     (hi, lo)).fetchone()


def _tiled_by_verified(c, lo, hi):
    """True if verified ranges lying INSIDE [lo..hi] tile it exactly, leaving no gap (H9 contiguity).

    This is the "genuine fold" test. A fold re-expresses work the board has already verified, so its
    span is always tiled by its own children. A leaf proof of fresh territory is not tiled at all, and
    does not need to be. The shape that is NEITHER — a wide range starting INSIDE existing coverage
    but not backed by it — is the one that can never seam, and this is what separates it from the
    other two.

    Height contiguity only. The full boundary seam (in_bhash/out_bhash) is enforced where it actually
    decides something, in `_frontier_chain`; requiring it here would reject a legitimate re-fold for
    no gain, since the receipt itself has already been STARK-verified for exactly this [lo..hi].
    """
    rows = [dict(r) for r in c.execute(
        "SELECT lo,hi FROM vranges WHERE lo >= ? AND hi <= ? ORDER BY lo", (lo, hi)).fetchall()]
    reach = lo - 1
    while reach < hi:
        nxt = max((r["hi"] for r in rows if r["lo"] <= reach + 1 and r["hi"] > reach), default=None)
        if nxt is None:
            return False
        reach = nxt
    return True


def submit(body):
    rid, pk = body.get("range"), body.get("pubkey", "")
    sig, receipt_b64 = body.get("sig", ""), body.get("receipt", "")
    handle = clean_handle(body.get("handle"), handle_cap(pk))
    if not (rid and pk and receipt_b64): return 400, {"error": "range, pubkey, receipt required"}
    if handle_refused(handle, pk): return 400, {"error": "that handle is reserved — please pick another"}
    if not parse_any_range(rid): return 400, {"error": "invalid range id"}
    _genesis = genesis_refusal(*parse_any_range(rid))    # before any row, signature or verification
    if _genesis: return _genesis
    # #281: refuse a wide range that starts INSIDE existing coverage without being backed by it.
    #
    # `_frontier_chain` needs `prev.hi + 1 == lo` EXACTLY (H9), so such a range can never join the
    # genesis chain. Accepting it is worse than useless: the blocks it covers then look proven to
    # `claim()`, which is coverage-based, so the gap is never handed out again and the frontier stops
    # permanently. That is how [30000..30050] followed by [30050..30100] — inclusive bounds, one block
    # of overlap — froze the board at 30,050 for thirteen hours on 2026-09-11, while `proven` kept
    # climbing and `stalled_for` reported 0 because a VERIFIED row sat over the blocker.
    #
    # #281, the structural half: A WIDE RANGE MUST BE A FOLD. Only a single block may introduce new
    # coverage; anything wider has to be backed by ranges already verified, tiling it exactly.
    #
    # #283 allowed a second shape -- a wide range over fresh territory -- and that is what made bad
    # bounds expressible at all. It cannot produce the 30,050 freeze on its own (a fresh range above
    # the frontier leaves an ordinary hole, which claim() hands out), but it lets `proven` count blocks
    # no single proof ever covered, so coverage and per-block work drift apart and every consumer of
    # coverage has to be defensive about it. With this rule they do not: covered == proved, one block
    # at a time, and the fold tree only ever re-expresses what is already there.
    #
    # The worker submits its blocks as it proves them (`submit_leaves`), so a wide `hazync run lo-hi`
    # still works end to end -- the leaves land first and the fold that follows is backed by them.
    #
    # ⚠ A worker OLDER than this sends the wide range alone and will be refused. That is a protocol
    # break, taken deliberately and with a message that says exactly what to do, rather than leaving a
    # shape whose only legitimate use is one the current client no longer needs.
    #
    # No genesis-seed exemption: `[0..hi]` used to skip this rule outright (`_lo > 0`), which let it add
    # `hi` blocks of coverage with no per-block receipt beneath it -- the one G1 hole #281 left. Block 0
    # is now refused above (genesis_refusal), before this rule is ever reached.
    _lo, _hi = parse_any_range(rid)
    if _hi > _lo:                      # width 1 introduces coverage; wider must be a fold
        with _lock:
            c = db()
            _backed = _tiled_by_verified(c, _lo, _hi)
            _clash = None if _backed else _overlapping_vrange(c, _lo, _hi)
            c.close()
        if not _backed:
            _msg = (f"range [{_lo}..{_hi}] is {_hi - _lo + 1} blocks wide but the board does not hold "
                    f"proofs for all of them, so this is not a fold. Submit each block on its own "
                    f"first, then the range that folds them -- `hazync run {_lo}-{_hi}` does that for "
                    f"you from v0.21.4. Only a single block may cover ground nothing has covered yet.")
            if _clash and _clash["lo"] <= _lo <= _clash["hi"]:
                _msg += (f" Note [{_clash['lo']}..{_clash['hi']}] is already verified: start at "
                         f"{_clash['hi'] + 1}, not {_lo} — range bounds are INCLUSIVE.")
            return 409, {"error": _msg}
    if HAVE_ED and not is_hex(pk, 32): return 400, {"error": "pubkey must be 32-byte hex (ed25519)"}
    if HAVE_ED and not is_hex(sig, 64): return 400, {"error": "sig must be 64-byte hex (ed25519)"}
    if len(receipt_b64) > MAX_BODY: return 413, {"error": "receipt too large"}
    try: receipt = base64.b64decode(receipt_b64)
    except Exception: return 400, {"error": "receipt must be base64"}
    sha = hashlib.sha256(receipt).hexdigest()
    # 1. cheap pre-check under the lock, then RELEASE it — the STARK verification below can take up to
    #    120s, and holding the global write lock across it would stall every claim/heartbeat/submit and
    #    reap honest provers as stale. Verify lock-free; re-acquire only to commit.
    with _lock:
        c = db()
        r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        c.close()
    if not r:
        # FREE-RUNNING: a submission does not require a prior claim. There is no allocation any more, so
        # the row is created on demand from the (already validated) range id. The receipt still has to
        # prove exactly this [lo..hi] — verify_receipt checks that — so an invented id buys nothing
        # beyond a row that then fails verification.
        lo, hi = parse_any_range(rid)
        with _lock:
            c = db()
            c.execute("INSERT OR IGNORE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'open')", (rid, lo, hi))
            c.commit()
            r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
            c.close()
    if r["status"] == "verified": return 409, {"error": "already proven"}
    # A held block is proven only by its sponsorship's registered key (_hold_refusal). Checked here, before
    # the expensive verification, and again when committing, in case a hold started in between.
    with _lock:
        c = db()
        _held = _hold_refusal(c, int(r["lo"]), int(r["hi"]), pk)
        c.close()
    if _held:
        return 403, _hold_message(_held)
    # 2. expensive verification OUTSIDE the lock (concurrent submits for different ranges run in parallel),
    #    but bounded by _verify_sem so a burst can't spawn unlimited STARK verifications and OOM the box.
    with _verify_sem:
        sig_ok = verify_sig(pk, sig, receipt)
        rcpt_ok, note, meta = verify_receipt(receipt, r) if sig_ok else (False, "signature invalid", None)
    ok = sig_ok and rcpt_ok
    # 3. commit under the lock, re-checking status so a racing submit for the same range can't double-credit
    with _lock:
        c = db()
        r2 = c.execute("SELECT status FROM ranges WHERE id=?", (rid,)).fetchone()
        if r2 and r2["status"] == "verified":
            c.close()
            return 409, {"error": "already proven"}   # another submit won the race while we were verifying
        _held = _hold_refusal(c, int(r["lo"]), int(r["hi"]), pk)
        if _held:
            c.close()
            return 403, _hold_message(_held)
        c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
                  " VALUES(?,?,?,?,?,?,?,?)", (rid, pk, handle, sha, sig, int(ok), note, time.time()))
        if ok:
            c.execute("UPDATE ranges SET status='verified', receipt_sha=?, verified_at=? WHERE id=?",
                      (sha, time.time(), rid))
            # Record the PROVEN [lo..hi] (from the receipt), not the claimed range: for the genesis seed
            # the claim is [0..999] but the receipt proves [1..999], and the frontier chain needs lo==1.
            v_lo, v_hi = int(meta.get("lo", r["lo"])), int(meta.get("hi", r["hi"]))
            c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work,"
                      "in_bhash,out_bhash)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, v_lo, v_hi, meta["in_tip"], meta["out_tip"], pk, handle, time.time(),
                       meta.get("out_leaves", 0), str(meta.get("range_work", "0")),
                       str(meta.get("in_bhash", "")), str(meta.get("out_bhash", ""))))
            c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                      (pk, handle, time.time()))
            c.execute("UPDATE contributors SET blocks=blocks+?, handle=? WHERE pubkey=?",
                      (v_hi-v_lo+1, handle, pk))
            note_client_version(c, pk, body)
            try:                                          # keep the receipt so anyone can re-verify it
                os.makedirs(PROOFS_DIR, exist_ok=True)
                with open(os.path.join(PROOFS_DIR, f"proof_{rid}.bin"), "wb") as pf:
                    pf.write(receipt)
            except Exception:
                pass
            _sponsor_mark_proven(c, v_lo, v_hi)           # a sponsorship now fully covered ends its hold
        c.commit(); c.close()
        _frontier_invalidate()        # #265: a new verified range can move the frontier
    # `"ok": true` means the receipt verified and was accepted for THIS range — it does NOT mean the
    # range is genesis-anchored, and a client that reads it as "this proves the chain from genesis"
    # is wrong for every mid-chain receipt (which is most of them). Report the distinction instead of
    # leaving it to be inferred (#59). Fields are additive; older clients ignore them.
    resp = {"ok": ok, "range": rid, "receipt_sha": sha,
            "signature": "valid" if sig_ok else "invalid", "note": note}
    if ok and meta:
        resp.update({"anchored": bool(meta.get("anchored", False)),
                     "lo": int(meta.get("lo", r["lo"])), "hi": int(meta.get("hi", r["hi"])),
                     "in_bhash": str(meta.get("in_bhash", "")),
                     "out_bhash": str(meta.get("out_bhash", ""))})
    return (200 if ok else 422), resp

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, ctype="application/json", raw=None, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if raw is not None: self.wfile.write(raw)
        elif obj is not None: self.wfile.write(json.dumps(obj).encode())
    def do_OPTIONS(self): self._send(204)
    def log_message(self, *a): pass
    def _client_ip(self):
        # Trust X-Forwarded-For ONLY when the direct peer is a configured reverse proxy; otherwise a
        # client could forge it to bypass the rate limit and bloat the rate map. Falls back to the peer.
        peer = self.client_address[0]
        xff = self.headers.get("X-Forwarded-For")
        if xff and peer in TRUSTED_PROXIES:
            return xff.split(",")[0].strip() or peer
        return peer
    def _body(self):
        try: n = int(self.headers.get("Content-Length", 0))
        except Exception: n = 0
        if n > MAX_BODY: self.rfile.read(min(n, MAX_BODY)); return None   # oversized — signal 413
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def do_GET(self):
        p = urlparse(self.path).path
        # Read rate limit on the API (defence-in-depth with the nginx limit_req; the box may also be hit
        # directly). Static assets are cheap and left unlimited.
        if p.startswith("/api/") and not rate_ok(self._client_ip(), "r", RATE_MAX_GET):
            return self._send(429, {"error": "rate limit — slow down"})
        if p == "/api/state":
            # ?slim=1 omits vranges — the board polls this every 10s and fetches /api/vranges only when
            # progress moves. Default keeps vranges so existing clients are unaffected (#35).
            if parse_qs(urlparse(self.path).query).get("slim", ["0"])[0] not in ("0", "", "false"):
                return self._send(200, raw=state_cached(slim=True), ctype="application/json")
            return self._send(200, raw=state_cached(), ctype="application/json")
        if p == "/api/vranges":
            raw, etag = vranges_cached()
            if _etag_matches(self.headers.get("If-None-Match"), etag):
                return self._send(304, raw=b"", headers={"ETag": etag, "Cache-Control": "no-cache"})
            return self._send(200, raw=raw, ctype="application/json",
                              headers={"ETag": etag, "Cache-Control": "no-cache"})
        if p == "/api/blockstatus":                        # every block's state as runs, for the block map
            who = parse_qs(urlparse(self.path).query).get("prover", [None])[0]
            if who is not None and who not in known_handles_cached():
                return self._send(404, {"error": "no prover by that name"})
            raw, etag = block_status_cached(who)
            if _etag_matches(self.headers.get("If-None-Match"), etag):
                return self._send(304, raw=b"", headers={"ETag": etag, "Cache-Control": "no-cache"})
            return self._send(200, raw=raw, ctype="application/json",
                              headers={"ETag": etag, "Cache-Control": "no-cache"})
        if p.startswith("/api/block/"):                    # everything about one block
            seg = p.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[0-9]{1,9}", seg):
                return self._send(400, {"error": "a block height, for example /api/block/170"})
            code, obj = block_detail(int(seg))
            return self._send(code, obj)
        if p == "/api/sponsor":                            # is sponsorship open; see docs/SPONSORSHIP.md
            return self._send(200, sponsor_info())
        if p == "/api/sponsor/quote":                      # the minimum for a span
            q = parse_qs(urlparse(self.path).query)
            code, obj = sponsor_quote(q.get("lo", [None])[0], q.get("hi", [None])[0])
            return self._send(code, obj)
        if p.startswith("/api/sponsor/status/"):           # one sponsorship, by its private link
            code, obj = sponsor_status(p[len("/api/sponsor/status/"):])
            return self._send(code, obj)
        if p == "/api/sponsors":                           # the public table of paid sponsorships
            return self._send(200, sponsors_public())
        if p == "/api/pick": code, obj = pick(None); return self._send(code, obj)
        if p == "/api/meta":                               # pre-flight: expected guest id + frontier
            return self._send(200, {"method_id": expected_method_id(), "frontier": frontier_hi(),
                                    "reproduce": "reproduce/METHOD_ID",
                                    "source_sha256": source_sha256()})
        if p == "/api/foldable":                           # adjacent pairs whose fold does not exist yet
            try: n = max(1, min(32, int(parse_qs(urlparse(self.path).query).get("limit", ["8"])[0])))
            except Exception: n = 8
            pairs = foldable(n)
            return self._send(200, {"pairs": pairs, "count": len(pairs)})
        if p == "/api/spine":                              # the headline artifact: genesis -> N in one receipt
            head = spine_head()
            if not head:
                return self._send(404, {"error": "no spine yet — nothing has been folded from genesis",
                                        "hint": "extend one with `host extend-spine` and POST it here"})
            return self._send(200, head)
        if p == "/api/spine/segments":                     # who absorbed each block into the spine (#244)
            return self._send(200, raw=_single_flight(
                "spine:segments", VRANGES_TTL,
                lambda: (lambda sg: json.dumps(
                    {"segments": sg[0], "first_advance_hi": sg[1],
                     "hi": (spine_head() or {}).get("hi")}).encode())(spine_segments())),
                ctype="application/json")
        if p == "/api/spine/proof":                        # the receipt itself; check it with `hazync-verify`
            f = os.path.join(SPINE_DIR, "spine.bin")
            if os.path.exists(f):
                # Served name, not the name at rest (#278). On disk this stays spine.bin: check-retention.py
                # parses the stored prefix/suffix, and a user never sees the on-disk name anyway.
                head = spine_head()
                hi = head.get("hi") if head else None
                name = f"hazync-spine-1-{int(hi)}.hzk" if isinstance(hi, int) else "hazync-spine.hzk"
                return self._send(200, raw=open(f, "rb").read(), ctype="application/octet-stream",
                                  headers={"Content-Disposition": f'attachment; filename="{name}"'})
            return self._send(404, {"error": "no spine yet"})
        if p.startswith("/api/proof/"):                    # download a verified proof receipt (re-verify with `host verify-any`)
            rid = p.rsplit("/", 1)[-1]
            rng = parse_any_range(rid)
            if rng:
                f = os.path.join(PROOFS_DIR, f"proof_{rid}.bin")
                if os.path.exists(f):
                    # `curl -O` otherwise names the file after the URL path -- i.e. "1", no extension at
                    # all. The name is built from the PARSED (lo, hi), never from the raw path segment:
                    # a header is not a path, and parse_any_range's shape check is not a header-injection
                    # check. Ints cannot carry CR/LF (#278).
                    lo, hi = rng
                    name = f"hazync-{lo}.hzk" if lo == hi else f"hazync-{lo}-{hi}.hzk"
                    return self._send(200, raw=open(f, "rb").read(), ctype="application/octet-stream",
                                      headers={"Content-Disposition": f'attachment; filename="{name}"'})
            if rng == (0, 0):
                return self._send(404, {"error": GENESIS_MESSAGE, "genesis": True})
            return self._send(404, {"error": "proof not available"})
        if p == "/api/witnesses":
            # Bulk bundle sync (#69). Seeding a new coordinator from a peer is ~220,000 bundles; with
            # only /api/witness/<n> that is 220,000 requests, which is why nobody has done it.
            #
            # STREAMED, never buffered. One RANGE_SIZE chunk is a few hundred MB and the whole set is
            # ~73 GB — building an archive in memory would OOM the coordinator on the first request.
            # `tarfile` in "w|" mode writes straight to the socket and never seeks.
            #
            # TAR SPECIFICALLY, and not for convenience. This server speaks HTTP/1.0, so a response
            # with no Content-Length ends at connection close — which makes a TRUNCATED transfer look
            # exactly like a complete one. A tar ends with two zero blocks, so a client that parses the
            # archive to completion has proof it received all of it. A bare concatenation would not.
            q = parse_qs(urlparse(self.path).query)
            def _int(name, default=None):
                v = q.get(name, [None])[0]
                if v is None: return default
                return int(v) if v.lstrip("-").isdigit() else None
            heights, missing, err = bulk_plan(_int("from"), _int("count", BULK_MAX))
            if err:
                return self._send(400, {"error": err})
            manifest = json.dumps({
                "from": _int("from"), "count": _int("count", BULK_MAX),
                "served": heights, "missing": missing,
                # A client compares this against what it extracted. `missing` is reported rather than
                # skipped silently: a gap in the bridge's output and the end of the chain are different
                # facts, and a syncing peer must not read one as the other.
                "note": "verify the archive parses to its end-of-archive marker; a truncated stream is "
                        "otherwise indistinguishable from a complete one over HTTP/1.0",
            }, indent=1).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                with tarfile.open(fileobj=self.wfile, mode="w|") as tf:
                    ti = tarfile.TarInfo("MANIFEST.json"); ti.size = len(manifest); ti.mtime = 0
                    tf.addfile(ti, io.BytesIO(manifest))
                    for h in heights:
                        f = bundle_path(h)
                        if not f:            # raced with a bridge rotation between plan and send
                            continue
                        ti = tarfile.TarInfo(os.path.basename(f))
                        ti.size = os.path.getsize(f); ti.mtime = 0
                        with open(f, "rb") as fh:
                            tf.addfile(ti, fh)
            except (BrokenPipeError, ConnectionResetError):
                # The client walked away mid-chunk. Normal for a resumable sync; not an error here, and
                # letting it propagate would spam the log with tracebacks for ordinary behaviour.
                pass
            return
        if p.startswith("/api/witness/"):
            seg = p.rsplit("/", 1)[-1]
            blk = int(seg) if seg.isdigit() else (parse_range(seg) or [None])[0]  # block number or range id
            if blk is not None:
                f = bundle_path(blk)
                if f:
                    return self._send(200, raw=open(f, "rb").read())
            return self._send(404, {"error": "witness not available"})
        # static frontend
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        webroot = os.path.abspath(WEB)
        fp = os.path.abspath(os.path.join(WEB, rel))
        # Contain to WEB with a separator boundary — a bare startswith(webroot) would also accept a
        # sibling like <web>XYZ/secret whose path merely shares the "web" prefix.
        if (fp == webroot or fp.startswith(webroot + os.sep)) and os.path.isfile(fp):
            ct = "text/html" if fp.endswith(".html") else "text/plain"
            return self._send(200, raw=open(fp, "rb").read(), ctype=ct)
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        p = urlparse(self.path).path
        # Allocation endpoints are GONE (#37): no claim, no heartbeat, no release. Proving is
        # unallocated, so there is nothing to lease, keep alive, or hand back.
        if p not in ("/api/submit", "/api/claim", "/api/spine", "/api/beat", "/api/rotate", "/api/sponsor"):
            return self._send(404, {"error": "not found"})
        if not rate_ok(self._client_ip()):
            return self._send(429, {"error": "rate limit — slow down"})
        body = self._body()
        if body is None:
            return self._send(413, {"error": "request body too large"})
        # #293: the client's release rides in on the User-Agent. Injected into the body rather than
        # threaded through five signatures — the body is already where the caller's identity lives,
        # and a client that sends nothing simply has no key here.
        ua = self.headers.get("User-Agent") or ""
        if isinstance(body, dict) and ua.startswith("hazync-worker/"):
            body["_client_version"] = ua[len("hazync-worker/"):][:32]
        fn = {"/api/submit": submit, "/api/claim": claim, "/api/spine": submit_spine,
              "/api/beat": beat, "/api/rotate": rotate, "/api/sponsor": sponsor_request}[p]
        code, obj = fn(body)
        if p == "/api/beat" and code != 200:
            try:
                log_beat_rejection(self._client_ip(), body, code, obj)
            except Exception:
                pass                             # never let the log line cost the caller a response
        return self._send(code, obj)

def install_stack_dump():
    """`kill -USR1 <pid>` writes every thread's stack to stderr (the journal) and keeps serving.

    #265 recurred on 2026-09-11 17:41 UTC: the listener stopped accepting (the kernel logged SYN
    cookies on 8899) for 11 minutes, with nothing in the journal, and the restart that cleared it
    also destroyed the only evidence of why. This box has no pip, so py-spy is not an option. Take
    the dump BEFORE restarting a wedged coordinator:
        kill -USR1 $(systemctl show -p MainPID --value hazync-coordinator)
        journalctl -u hazync-coordinator --since -2min
    """
    import faulthandler, signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)

if __name__ == "__main__":
    install_stack_dump()
    print(f"[hazync-coordinator] open-file limit {raise_open_file_limit()}", flush=True)
    init_db()
    # Fail closed at startup: never serve on a public interface while the STARK check or signatures are
    # in a permissive/dev mode — a misconfigured redeploy would otherwise credit the public board for
    # unverified or unsigned receipts. Loopback (behind a trusted proxy) is always allowed.
    insecure = []
    if VERIFY != "real": insecure.append(f"VERIFY_MODE={VERIFY}")
    if os.environ.get("COORD_ALLOW_MOCK"): insecure.append("COORD_ALLOW_MOCK set")
    if not HAVE_ED: insecure.append("no ed25519 signature lib")
    if os.environ.get("COORD_ALLOW_UNSIGNED"): insecure.append("COORD_ALLOW_UNSIGNED set")
    public = BIND not in ("127.0.0.1", "::1", "localhost")
    if public and insecure and not os.environ.get("COORD_ALLOW_PUBLIC_INSECURE"):
        raise SystemExit(f"[hazync-coordinator] refusing to bind public interface {BIND}:{PORT} in an "
                         f"insecure mode ({', '.join(insecure)}). Fix the config, bind 127.0.0.1 behind a "
                         f"proxy, or set COORD_ALLOW_PUBLIC_INSECURE=1 to override (not for production).")
    print(f"[hazync-coordinator] :{PORT}  db={DB}  verify={VERIFY}  sigs={'ed25519' if HAVE_ED else 'dev'}")
    print(f"  dashboard  http://localhost:{PORT}/")
    print(f"  api        GET /api/state · POST /api/claim · POST /api/submit · GET /api/witness/<h> · GET /api/witnesses?from=&count=")

    # hazync#69 — actually RUN the peer sync.
    #
    # `sync_from_peers` has existed, been hardened (audit #3 F-4) and been tested for some time while
    # nothing ever called it: its own comment says "code that is not wired up yet and will be". A
    # federation feature that is never invoked federates nothing, and the tests passed throughout
    # because they call the function directly — which is exactly the shape of a check that cannot fail.
    #
    # Starts ONLY when peers are configured, so a solo coordinator is byte-for-byte unaffected: no
    # thread, no timer, no network. The loop can never kill the server — a peer being down, slow or
    # hostile must not take the board offline, and `sync_from_peers` already declines to raise.
    if PEERS:
        PEER_SYNC_INTERVAL = int(os.environ.get("PEER_SYNC_INTERVAL", "300"))

        def _peer_sync_loop():
            while True:
                try:
                    r = sync_from_peers()
                    if r.get("adopted") or r.get("rejected"):
                        print(f"[peer-sync] adopted={r['adopted']} rejected={r['rejected']} "
                              f"peers={r['peers']}", flush=True)
                except Exception as e:                      # noqa: BLE001 — never let this thread die
                    print(f"[peer-sync] pass failed, will retry: {e}", flush=True)
                time.sleep(PEER_SYNC_INTERVAL)

        threading.Thread(target=_peer_sync_loop, daemon=True, name="peer-sync").start()
        print(f"  peer-sync  every {PEER_SYNC_INTERVAL}s from {len(PEERS)} peer(s): {', '.join(PEERS)}")

    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
