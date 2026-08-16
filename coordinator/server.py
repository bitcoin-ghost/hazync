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
import os, json, sqlite3, hashlib, subprocess, base64, time, threading, tarfile, io
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
# A claim expires this long after it is TAKEN — not after a heartbeat stops, because there are no
# heartbeats. A worker that dies mid-block leaves nothing to reap; the block simply reopens on its own.
CLAIM_TTL  = int(os.environ.get("CLAIM_TTL", "3600"))    # 1 hour, then anyone may take it
CLAIM_MAX  = int(os.environ.get("CLAIM_MAX", "86400"))   # hard cap: release a claim after this long regardless
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
_state_cache = {}                      # short-TTL cache of the serialised /api/state, per slim/full, guarded by _state_lock
# vranges is served separately (#35): it is ~99.9% of the old /api/state payload and changes only when a
# range is verified, so a 10s cache + ETag turns a re-poll into a 304 instead of re-shipping the index.
_vranges_cache = {"t": 0.0, "v": None, "etag": None}
VRANGES_TTL = float(os.environ.get("VRANGES_CACHE_TTL", "10"))
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

def clean_handle(h):
    """A display handle: printable, trimmed, length-capped, and stripped of HTML-significant characters
    (< > & " ') so it is safe to render on the public dashboard. This is the single server-side choke
    point (CLI, API, and any future consumer all pass through it); the dashboard also escapes at every
    render sink, so the two layers are defence-in-depth against stored XSS."""
    h = "".join(ch for ch in str(h or "anon")
                if ch.isprintable() and ch not in "<>&\"'").strip()
    return (h[:MAX_HANDLE] or "anon")

def is_hex(s, nbytes):
    """True if s is exactly nbytes of lowercase/upper hex (ed25519 pubkey=32, sig=64)."""
    try:
        return isinstance(s, str) and len(s) == nbytes * 2 and bytes.fromhex(s) is not None
    except Exception:
        return False

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.executescript("""
      CREATE TABLE IF NOT EXISTS ranges(
        id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER,
        status TEXT DEFAULT 'open',            -- open | claimed | verified
        assignee TEXT, handle TEXT,
        receipt_sha TEXT, claimed_at REAL, verified_at REAL, last_beat REAL);
      CREATE TABLE IF NOT EXISTS contributors(
        pubkey TEXT PRIMARY KEY, handle TEXT, blocks INTEGER DEFAULT 0, first_seen REAL);
      CREATE TABLE IF NOT EXISTS submissions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, range_id TEXT, pubkey TEXT, handle TEXT,
        receipt_sha TEXT, sig TEXT, verified INTEGER, note TEXT, ts REAL);
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS vranges(
        id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, in_tip TEXT, out_tip TEXT,
        pubkey TEXT, handle TEXT, ts REAL, out_leaves INTEGER, range_work TEXT);
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
                "last_failed_at REAL", "last_assignee TEXT"):   # failure tracking
        try: c.execute(f"ALTER TABLE ranges ADD COLUMN {col}")
        except Exception: pass
    for col in ("out_leaves INTEGER", "range_work TEXT",
                "in_bhash TEXT", "out_bhash TEXT"):  # H7/S1: full-boundary continuity digest
        try: c.execute(f"ALTER TABLE vranges ADD COLUMN {col}")
        except Exception: pass
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
            try:
                os.makedirs(PROOFS_DIR, exist_ok=True)
                with open(os.path.join(PROOFS_DIR, f"proof_{rid}.bin"), "wb") as pf:
                    pf.write(receipt)
            except Exception:
                pass
            have.add(rid)
            adopted += 1
    return {"adopted": adopted, "rejected": rejected, "peers": len(PEERS)}

def pick(body):
    """Suggest the next open BLOCK after the frontier. Per-block is the DEFAULT proving unit: one block
    per `hazync run` — no fold, low memory, and it matches the board's per-block proofs (so `/api/proof/<n>`
    stays valid). Block 1 pins to genesis. A bigger aligned chunk is opt-in via `hazync run <lo>-<hi>`."""
    fr = frontier_hi()
    c = db()
    taken = set(r["id"] for r in c.execute("SELECT id FROM ranges WHERE status IN ('claimed','verified')"))
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
            rid = str(n)
            if rid not in taken and n not in peers and not (avoid_busy and n in busy) \
               and witness_available(n):
                return 200, {"range": rid, "lo": n, "hi": n, "cmd": f"hazync run {rid}"}
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

def submit_spine(body):
    """Accept an extended spine. Monotonic: a head that does not advance is refused."""
    pk, sig = body.get("pubkey", ""), body.get("sig", "")
    receipt_b64, handle = body.get("receipt", ""), clean_handle(body.get("handle"))
    if not receipt_b64: return 400, {"error": "receipt required"}
    if handle_reserved(handle): return 400, {"error": "that handle is reserved — please pick another"}
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
    """
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
    return _frontier_chain()[0]

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

def distinct_blocks_by_pubkey():
    """Per-contributor DISTINCT blocks proven, computed the SAME way as proven_count (interval-merge) so
    the leaderboard always reconciles with the headline 'proven' number. A stored per-submit counter can
    drift (e.g. a block proved both as a single and inside an overlapping range double-counts); deriving
    from vranges makes that impossible. Cheap: vranges are RANGE_SIZE-coarse + a few singles."""
    c = db()
    rows = c.execute("SELECT pubkey,lo,hi FROM vranges ORDER BY pubkey,lo,hi").fetchall()
    c.close()
    out, cur_pk, cur_lo, cur_hi, tot = {}, None, None, None, 0
    def flush():
        if cur_pk is not None:
            out[cur_pk] = out.get(cur_pk, 0) + tot
    for r in rows:
        pk = r["pubkey"]
        if pk != cur_pk:
            if cur_hi is not None: out[cur_pk] = out.get(cur_pk, 0) + (cur_hi - cur_lo + 1)
            cur_pk, cur_lo, cur_hi = pk, r["lo"], r["hi"]
            continue
        if r["lo"] > cur_hi + 1:
            out[cur_pk] = out.get(cur_pk, 0) + (cur_hi - cur_lo + 1)
            cur_lo, cur_hi = r["lo"], r["hi"]
        else:
            cur_hi = max(cur_hi, r["hi"])
    if cur_hi is not None: out[cur_pk] = out.get(cur_pk, 0) + (cur_hi - cur_lo + 1)
    return out

def frontier_proof():
    """The genesis-anchored frontier as a chain-state (the real committed proof output the hero panel
    shows). Empty (height 0) until the first genesis-anchored proof lands."""
    hi, tip_hash, cum_work, leaves = _frontier_chain()
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
    out = []
    for r in c.execute("SELECT id,lo,hi,handle,pubkey FROM vranges ORDER BY lo"):
        v = dict(lo=r["lo"], hi=r["hi"],
                 handle=(r["handle"] if (r["pubkey"] or "").lower() not in blk else "[removed]"))
        if os.path.exists(os.path.join(PROOFS_DIR, f"proof_{r['id']}.bin")):
            v["proof"] = f"/api/proof/{r['id']}"      # downloadable receipt, re-verifiable by anyone
        out.append(v)
    return out

def vranges_cached():
    """Serialised /api/vranges with a TTL and an ETag, returned as (bytes, etag).

    This is ~99.9% of what /api/state used to ship (3,393,853 of 3,397,846 bytes at 38,507 entries) and
    it only changes when a range is verified — but the board polled it every 10 seconds, and it grows
    with the chain. Splitting it out takes the steady-state poll from ~313 KB gzipped to a few KB, and
    the ETag makes an unchanged index a 304 rather than a re-download."""
    now = time.time()
    with _state_lock:
        if _vranges_cache["v"] is not None and now - _vranges_cache["t"] < VRANGES_TTL:
            return _vranges_cache["v"], _vranges_cache["etag"]
    c = db()
    blk = blocked_pubkeys()          # same moderation list state() applies, so handles match exactly
    payload = {"vranges": build_vranges(c, blk), "range_size": RANGE_SIZE}
    v = json.dumps(payload).encode()
    etag = '"' + hashlib.sha256(v).hexdigest()[:32] + '"'
    with _state_lock:
        _vranges_cache["t"] = time.time(); _vranges_cache["v"] = v; _vranges_cache["etag"] = etag
    return v, etag

def state(slim=False):
    now = time.time()
    _tip_now = chain_tip()    # read once: the board must not report a pct and a tip from two scans
    c = db()
    proven = proven_count()   # distinct covered blocks (overlap-safe), not SUM(hi-lo+1) which double-counts
    ncontrib = c.execute("SELECT COUNT(*) FROM contributors WHERE blocks>0").fetchone()[0]
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
    _dbp = distinct_blocks_by_pubkey()
    leaders = sorted(
        (dict(id=x["pubkey"][:10], handle=x["handle"], blocks=_dbp.get(x["pubkey"], 0))
         for x in c.execute("SELECT * FROM contributors")
         if x["pubkey"].lower() not in blk and _dbp.get(x["pubkey"], 0) > 0),
        key=lambda d: d["blocks"], reverse=True)[:8]
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
        "SELECT id,status,attempts,last_failed_at,claimed_at FROM ranges "
        "WHERE lo <= ? AND hi >= ? AND status IN ('claimed','verified','failed') "
        "ORDER BY (hi-lo) ASC LIMIT 1", (nb, nb)).fetchone()
    if blocker is None:
        blocker = c.execute("SELECT id,status,attempts,last_failed_at,claimed_at FROM ranges WHERE id=?",
                            (str(nb),)).fetchone()
    stalled_for = 0
    if blocker is not None and blocker["status"] != "verified":
        mark = blocker["last_failed_at"] or blocker["claimed_at"]
        stalled_for = int(now - mark) if mark else 0
    c.close()
    return {
        # spine_hi sits NEXT TO frontier deliberately. The spine is the only shippable artifact — the
        # single genesis-anchored proof /api/spine/proof serves and the README's 30-second demo
        # downloads — and it is driven by one serial job that, until hazync#74, nothing ran. A stalled
        # spine is INVISIBLE from every other signal: proven climbs, frontier climbs, every gate stays
        # green, and only this number quietly stops. Reporting it beside frontier makes the gap
        # (frontier - spine_hi) a thing you can see rather than something you have to notice.
        # None means no spine at all, which is different from a stale one and should read differently.
        "progress": {"proven": proven, "frontier": fr, "tip": _tip_now,
                     "pct": round(100.0*fr/_tip_now, 3) if _tip_now else 0, "contributors": ncontrib,
                     "spine_hi": (spine_head() or {}).get("hi")},
        "failed": failed,
        # `block` is the block the frontier needs next; `id` is the RANGE responsible for it, which is
        # not the same thing once ranges can be wide — reporting str(fr+1) as the id hid a claimed
        # 38000-38999 behind an untouched single-block row of the same name.
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
    key = "slim" if slim else "full"
    now = time.time()
    with _state_lock:
        e = _state_cache.get(key)
        if e is not None and now - e["t"] < STATE_TTL:
            return e["v"]
    v = json.dumps(state(slim=slim)).encode()   # compute outside the lock; a rare cold-start double-compute is harmless
    with _state_lock:
        _state_cache[key] = {"t": time.time(), "v": v}
    return v

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
    handle = clean_handle(body.get("handle"))
    now = time.time()
    with _lock:
        c = db()
        proven = set()
        for row in c.execute("SELECT lo, hi FROM vranges"):
            proven.update(range(row["lo"], row["hi"] + 1))
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
        held = {r["lo"] for r in c.execute(
            "SELECT lo FROM ranges WHERE status='claimed'"
            " AND COALESCE(last_beat, claimed_at) > ? AND claimed_at > ?",
            (now - CLAIM_TTL, now - CLAIM_MAX))}
        h = 1
        _ceiling = provable_tip()          # what the bridge can serve, not a hardcoded chain height
        while h < _ceiling:
            if h not in proven and h not in held and witness_available(h):
                break
            h += 1
        else:
            c.close()
            return 409, {"error": "nothing available to claim"}
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at)"
                  " VALUES(?,?,?,'claimed',?,?,?)", (str(h), h, h, pk, handle, now))
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

def witness_available(blk):
    """True if a witness for `blk` can be served. Free-running proving needs arbitrary heights, and
    the bridge already provides them — the lookup is a direct file path with no frontier window."""
    for f in ([os.path.join(BRIDGE_DIR, f"bundle_{blk}.json")] if BRIDGE_DIR else []) \
             + [os.path.join(WITNESS, f"block_{blk}.json")]:
        if os.path.exists(f):
            return True
    return False


def submit(body):
    rid, pk = body.get("range"), body.get("pubkey", "")
    sig, receipt_b64 = body.get("sig", ""), body.get("receipt", "")
    handle = clean_handle(body.get("handle"))
    if not (rid and pk and receipt_b64): return 400, {"error": "range, pubkey, receipt required"}
    if handle_reserved(handle): return 400, {"error": "that handle is reserved — please pick another"}
    if not parse_any_range(rid): return 400, {"error": "invalid range id"}
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
            try:                                          # keep the receipt so anyone can re-verify it
                os.makedirs(PROOFS_DIR, exist_ok=True)
                with open(os.path.join(PROOFS_DIR, f"proof_{rid}.bin"), "wb") as pf:
                    pf.write(receipt)
            except Exception:
                pass
        c.commit(); c.close()
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
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, raw=b"", headers={"ETag": etag, "Cache-Control": "no-cache"})
            return self._send(200, raw=raw, ctype="application/json",
                              headers={"ETag": etag, "Cache-Control": "no-cache"})
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
        if p == "/api/spine/proof":                        # the receipt itself; check it with `hazync-verify`
            f = os.path.join(SPINE_DIR, "spine.bin")
            if os.path.exists(f):
                return self._send(200, raw=open(f, "rb").read(), ctype="application/octet-stream")
            return self._send(404, {"error": "no spine yet"})
        if p.startswith("/api/proof/"):                    # download a verified proof receipt (re-verify with `host verify-any`)
            rid = p.rsplit("/", 1)[-1]
            if parse_any_range(rid):
                f = os.path.join(PROOFS_DIR, f"proof_{rid}.bin")
                if os.path.exists(f):
                    return self._send(200, raw=open(f, "rb").read(), ctype="application/octet-stream")
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
        if p not in ("/api/submit", "/api/claim", "/api/spine", "/api/beat"):
            return self._send(404, {"error": "not found"})
        if not rate_ok(self._client_ip()):
            return self._send(429, {"error": "rate limit — slow down"})
        body = self._body()
        if body is None:
            return self._send(413, {"error": "request body too large"})
        fn = {"/api/submit": submit, "/api/claim": claim, "/api/spine": submit_spine,
              "/api/beat": beat}[p]
        code, obj = fn(body)
        return self._send(code, obj)

if __name__ == "__main__":
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
