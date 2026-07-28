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
import os, json, sqlite3, hashlib, subprocess, base64, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT       = int(os.environ.get("COORD_PORT", "8899"))
BIND       = os.environ.get("COORD_BIND", "0.0.0.0")   # set to 127.0.0.1 when behind a reverse proxy
DB         = os.environ.get("COORD_DB", "coordinator.db")
WEB        = os.environ.get("COORD_WEB", os.path.join(os.path.dirname(__file__), "web"))
TIP        = int(os.environ.get("TIP_HEIGHT", "958301"))
RANGE_SIZE = int(os.environ.get("RANGE_SIZE", "1000"))
SEED       = int(os.environ.get("SEED_RANGES", "60"))
WITNESS    = os.environ.get("WITNESS_DIR", os.path.join(os.path.dirname(__file__), "witnesses"))
BRIDGE_DIR = os.environ.get("HAZYNC_BRIDGE_OUT", "")   # archive-node bundle dir (co-located); serves bundle_<n>.json
HOST_BIN   = os.environ.get("HAZYNC_HOST", "")
VERIFY     = os.environ.get("VERIFY_MODE", "mock" if not HOST_BIN else "real")
STATE_DIR  = os.environ.get("COORD_STATE", os.path.join(os.path.dirname(__file__), "state"))
PROOFS_DIR = os.environ.get("COORD_PROOFS", os.path.join(os.path.dirname(__file__), "proofs"))  # kept, downloadable
GENESIS_TIP = os.environ.get("GENESIS_TIP", "6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000")
CLAIM_TTL  = int(os.environ.get("CLAIM_TTL", "1800"))    # auto-release a claim after no heartbeat this long
CLAIM_MAX  = int(os.environ.get("CLAIM_MAX", "86400"))   # hard cap: release a claim after this long regardless
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
SERVE_WIDE = os.environ.get("SERVE_WIDE", "0") == "1"    # claim-next hands out RANGE_SIZE chunks, not single blocks (#28)
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
_state_cache = {"t": 0.0, "v": None}   # short-TTL cache of the serialised /api/state, guarded by _state_lock
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

def reap():
    """Free stale claims: no heartbeat for CLAIM_TTL, or held longer than CLAIM_MAX. Lazy — called on
    each state()/claim(), so an abandoned claim returns to the pool within a poll interval.

    An abandoned claim is a FAILED ATTEMPT, not a fresh start: the old code reset straight back to
    'open', so a block that could not be proved was indistinguishable from one never tried, and could
    loop claim -> fail -> reap -> claim forever while the contiguous frontier sat pinned behind it and
    nothing anywhere said so. Attempts are now counted and a block is parked as 'failed' once it has
    burnt MAX_ATTEMPTS, so the pool stops spending GPU-hours re-failing it."""
    now = time.time()
    c = db()
    stale = c.execute(
        "SELECT id, handle, COALESCE(attempts,0) AS n FROM ranges WHERE status='claimed' AND ("
        " (last_beat IS NOT NULL AND ?-last_beat > ?) OR"
        " (claimed_at IS NOT NULL AND ?-claimed_at > ?) )",
        (now, CLAIM_TTL, now, CLAIM_MAX)).fetchall()
    for r in stale:
        n = r["n"] + 1
        parked = n >= MAX_ATTEMPTS
        c.execute("UPDATE ranges SET status=?, attempts=?, last_assignee=assignee, last_failed_at=?, "
                  "last_error=?, assignee=NULL, handle=NULL, claimed_at=NULL, last_beat=NULL WHERE id=?",
                  ("failed" if parked else "open", n, now,
                   "claim abandoned (no heartbeat within CLAIM_TTL, or held past CLAIM_MAX)", r["id"]))
        if parked:
            print(f"[reap] range {r['id']} PARKED as failed after {n} attempts "
                  f"(last holder {r['handle']}) — it will not be served again until reset", flush=True)
        else:
            print(f"[reap] range {r['id']} reopened, attempt {n}/{MAX_ATTEMPTS} "
                  f"(last holder {r['handle']})", flush=True)
    c.commit(); c.close()

def parse_range(rid):
    """Validate a claim id. Two accepted forms:
         'n'      → a single block n (any n in [0, TIP)) — 'I just want to do one block'.
         'lo-hi'  → a range, must be RANGE_SIZE-aligned and exactly RANGE_SIZE long.
       Aligned ranges and single blocks are the only shapes allowed, so two different claim
       ids can never partially overlap (no double-claim ambiguity). Returns (lo, hi)."""
    try:
        parts = [int(x) for x in str(rid).split("-")]
    except Exception:
        return None
    if len(parts) == 1:                                  # single block
        n = parts[0]
        return (n, n) if 0 <= n < TIP else None
    if len(parts) != 2:
        return None
    lo, hi = parts
    if hi - lo + 1 != RANGE_SIZE or lo % RANGE_SIZE != 0 or lo < 0 or hi >= TIP:
        return None
    return lo, hi

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

def pick(body):
    """Suggest the next open BLOCK after the frontier. Per-block is the DEFAULT proving unit: one block
    per `hazync run` — no fold, low memory, and it matches the board's per-block proofs (so `/api/proof/<n>`
    stays valid). Block 1 pins to genesis. A bigger aligned chunk is opt-in via `hazync run <lo>-<hi>`."""
    reap()
    fr = frontier_hi()
    c = db()
    taken = set(r["id"] for r in c.execute("SELECT id FROM ranges WHERE status IN ('claimed','verified')"))
    c.close()
    n = max(1, fr + 1)
    for _ in range(2_000_000):
        if n >= TIP:
            break
        rid = str(n)
        if rid not in taken:
            return 200, {"range": rid, "lo": n, "hi": n, "cmd": f"hazync run {rid}"}
        n += 1
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
        return True, "mock-verified (VERIFY_MODE=mock)", {"in_tip": "mock:%d" % rng["lo"], "out_tip": "mock:%d" % rng["hi"], "out_leaves": 0, "range_work": "0", "in_bhash": "0", "out_bhash": "0"}
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
        return True, f"range [{lo}..{hi}] VERIFIED", {"lo": lo, "hi": hi, "in_tip": kv["in_tip"], "out_tip": kv["out_tip"],
                "out_leaves": int(kv.get("out_leaves", 0)), "range_work": kv.get("range_work", "0"),
                "in_bhash": kv.get("in_bhash", ""), "out_bhash": kv.get("out_bhash", "")}
    except Exception as e:
        return False, f"verify error: {e}", None
    finally:
        try: os.remove(tmp)
        except Exception: pass

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
        if r["in_tip"] == GENESIS_TIP and r["lo"] == 1:
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
    bps = TIP / segs if segs else TIP
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

def state():
    reap()
    now = time.time()
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
        if lo >= TIP: break
        rid = f"{lo}-{hi}"; r = existing.get(rid)
        if r and r["status"] in ("claimed", "verified"):
            _h = r["handle"] if (r["assignee"] or "").lower() not in blk else "[removed]"
            b = {"id": rid, "lo": lo, "hi": hi, "status": r["status"], "handle": _h}
            if r["status"] == "claimed":
                b["elapsed"] = int(now - (r["claimed_at"] or now))
                b["beat"] = int(now - (r["last_beat"] or r["claimed_at"] or now))
                b["stale"] = b["beat"] > CLAIM_TTL // 2
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
              for s in c.execute("SELECT * FROM submissions ORDER BY ts DESC LIMIT 8")]
    # full verified + claimed lists so the client can browse/search/filter any block, not just the
    # frontier window (each is small: claims are few, verified ranges are RANGE_SIZE-coarse).
    vranges = []
    for r in c.execute("SELECT id,lo,hi,handle,pubkey FROM vranges ORDER BY lo"):
        v = dict(lo=r["lo"], hi=r["hi"],
                 handle=(r["handle"] if (r["pubkey"] or "").lower() not in blk else "[removed]"))
        if os.path.exists(os.path.join(PROOFS_DIR, f"proof_{r['id']}.bin")):
            v["proof"] = f"/api/proof/{r['id']}"      # downloadable receipt, re-verifiable by anyone
        vranges.append(v)
    claims = []
    for r in c.execute("SELECT lo,hi,handle,assignee,claimed_at,last_beat FROM ranges WHERE status='claimed' ORDER BY lo"):
        beat = int(now - (r["last_beat"] or r["claimed_at"] or now))
        claims.append(dict(lo=r["lo"], hi=r["hi"],
                           handle=(r["handle"] if (r["assignee"] or "").lower() not in blk else "[removed]"),
                           elapsed=int(now - (r["claimed_at"] or now)), stale=beat > CLAIM_TTL // 2))
    # Blocks parked after MAX_ATTEMPTS, plus how long the frontier has been stuck. Without this a stall
    # is invisible: the frontier is the lowest unproven block, so ONE bad block pins it while every other
    # signal stays green — `proven` keeps climbing as workers prove ahead of the gap, which is exactly
    # how a 45-minute stall went unnoticed on 2026-07-28.
    failed = [dict(id=r["id"], lo=r["lo"], hi=r["hi"], attempts=r["attempts"],
                   last_error=(r["last_error"] or "")[:200],
                   since=int(now - (r["last_failed_at"] or now)))
              for r in c.execute("SELECT id,lo,hi,attempts,last_error,last_failed_at FROM ranges "
                                 "WHERE status='failed' ORDER BY lo")]
    blocker = c.execute("SELECT id,status,attempts,last_failed_at,claimed_at FROM ranges WHERE id=?",
                        (str(fr + 1),)).fetchone()
    stalled_for = 0
    if blocker is not None and blocker["status"] != "verified":
        mark = blocker["last_failed_at"] or blocker["claimed_at"]
        stalled_for = int(now - mark) if mark else 0
    c.close()
    return {
        "progress": {"proven": proven, "frontier": fr, "tip": TIP,
                     "pct": round(100.0*fr/TIP, 3) if TIP else 0, "contributors": ncontrib},
        "failed": failed,
        "frontier_blocker": {"id": str(fr + 1),
                             "status": (blocker["status"] if blocker is not None else "open"),
                             "attempts": (blocker["attempts"] if blocker is not None else 0) or 0,
                             "stalled_for": stalled_for},
        "board": board, "leaderboard": leaders, "recent": recent,
        "vranges": vranges, "claims": claims, "range_size": RANGE_SIZE,
        "frontier_proof": frontier_proof(),
        "timeline": timeline(fr),
        "signatures": "ed25519" if HAVE_ED else "dev (no signature lib installed)",
        "verify_mode": VERIFY,
    }

def state_cached():
    """Serialised /api/state with a short TTL. state() does full-table scans + a frontier walk on every
    call, so under an anonymous GET flood recomputing it per request is the cheapest way to pin the box.
    A ~1.5s cache collapses a burst into one recompute while keeping the board effectively live."""
    now = time.time()
    with _state_lock:
        if _state_cache["v"] is not None and now - _state_cache["t"] < STATE_TTL:
            return _state_cache["v"]
    v = json.dumps(state()).encode()   # compute outside the lock; a rare cold-start double-compute is harmless
    with _state_lock:
        _state_cache["t"] = time.time(); _state_cache["v"] = v
    return v

def claim(body):
    rid, pk, handle = body.get("range"), body.get("pubkey", ""), clean_handle(body.get("handle"))
    if not pk: return 400, {"error": "pubkey required"}
    if HAVE_ED and not is_hex(pk, 32): return 400, {"error": "pubkey must be 32-byte hex (ed25519)"}
    if handle_reserved(handle): return 400, {"error": "that handle is reserved — please pick another"}
    want_next = (not rid) or rid == "next"
    if not want_next and not parse_range(rid): return 400, {"error": "invalid range id"}
    reap()
    now = time.time()
    with _lock:
        c = db()
        if want_next:
            # Atomic claim-next: pick the next open BLOCK after the frontier AND claim it under the lock, so
            # two parallel workers can never grab the same one (the old client-side pick-then-claim left a
            # window where both did). Per-block is the default proving unit (no fold; matches the board's
            # per-block proofs). Block 1 pins to genesis; a bigger aligned chunk is opt-in via `run <lo>-<hi>`.
            fr = frontier_hi()
            # 'failed' is excluded too: a parked range must not be handed straight back out by
            # claim-next, or MAX_ATTEMPTS achieves nothing. It stays claimable by EXPLICIT id, which is
            # the operator's deliberate retry path.
            # Find the first genuinely FREE block by walking INTERVALS, not ids. 'failed' counts as live
            # so a parked range is not handed straight back out (or MAX_ATTEMPTS achieves nothing); it
            # stays claimable by EXPLICIT id, which is the operator's retry path.
            #
            # Ids are not sufficient once ranges can be wide: "id 3000 is not taken" does not mean block
            # 3000 is free, because 3000-3999 covers it under a different id. Scanning by id handed out
            # a block that overlapping() then rejected, so a second worker got a 409 instead of work.
            live = sorted((row["lo"], row["hi"]) for row in c.execute(
                "SELECT lo,hi FROM ranges WHERE status IN ('claimed','verified','failed')"))
            n = max(1, fr + 1)
            for lo_, hi_ in live:
                if hi_ < n: continue                              # entirely below the search point
                if lo_ > n: break                                 # gap: n is free
                n = hi_ + 1                                       # n is covered — jump past this range
            rid = str(n) if n < TIP else None

            if SERVE_WIDE and rid is not None:
                # Upgrade that single block to a whole RANGE_SIZE-aligned chunk when it starts exactly on
                # a boundary and the chunk is entirely free. `hazync prove` folds a multi-block range
                # locally before submitting, so ~99% of the fold work happens DISTRIBUTED across every
                # worker and overlapped with proving, instead of being deferred into one serial
                # exclusive-GPU pass at the end (#28): ~28h of paused proving becomes a final board fold
                # of ~34 receipts instead of ~34,000.
                #
                # Anchoring on the FIRST FREE BLOCK (rather than scanning ahead for any free chunk) is
                # what keeps this safe. Scanning ahead would skip a partially-covered region and orphan
                # it — nothing would ever claim those blocks, and since the frontier is the lowest
                # unproven block it would stall there forever while workers proved chunks far ahead.
                # Anchoring here also lets several workers take CONSECUTIVE chunks: once one claims
                # [n, n+999], the next worker's first free block is n+1000, still aligned.
                #
                # Mid-re-baseline the board holds tens of thousands of single-block receipts, so chunks
                # over that region are not free and it degrades to per-block automatically.
                # No separate "is this id already taken?" check: if the chunk id were live its interval
                # would be in `live`, so n would have jumped past it. n == base implies the id is free.
                if n % RANGE_SIZE == 0 and n + RANGE_SIZE - 1 < TIP:
                    cand = f"{n}-{n + RANGE_SIZE - 1}"
                    if not overlapping(c, n, n + RANGE_SIZE - 1, cand):
                        rid = cand
            if rid is None: c.close(); return 404, {"error": "no open block available"}
        r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r:
            pr = parse_range(rid)                       # pick-any: auto-create a valid range on demand
            if not pr: c.close(); return 400, {"error": "invalid range id"}
            c.execute("INSERT INTO ranges(id,lo,hi) VALUES(?,?,?)", (rid, pr[0], pr[1]))
            r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        if r["status"] == "verified": c.close(); return 409, {"error": "already proven"}
        if r["status"] == "failed":
            # Claiming a parked range BY EXPLICIT ID is how an operator retries it — typically after
            # fixing whatever actually broke (in practice: GPU oversubscription, not the block). Reset
            # the counter so the retry gets a full budget rather than instantly re-parking.
            print(f"[claim] range {rid} was parked as failed after {r['attempts']} attempts; "
                  f"{handle} is retrying it explicitly — counter reset", flush=True)
            c.execute("UPDATE ranges SET attempts=0 WHERE id=?", (rid,))
        if r["status"] == "claimed" and r["assignee"] != pk:
            # locked to someone else and still alive (reap() already freed stale ones)
            since = int((now - (r["last_beat"] or r["claimed_at"] or now)) / 60)
            c.close()
            return 409, {"error": f"locked — being proved by {r['handle']} ({since}m active)"}
        # Interval overlap, not just id equality — see overlapping(). Two different ids can cover the
        # same blocks (a single block inside an aligned range), which would duplicate proving work and
        # race on submission. Checked under the same lock that grants the claim, so it cannot be raced.
        clash = overlapping(c, r["lo"], r["hi"], rid)
        if clash:
            c.close()
            return 409, {"error": f"overlaps {len(clash)} live range(s) already claimed/proven: "
                                  f"{', '.join(clash[:5])}{' …' if len(clash) > 5 else ''}"}
        c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                  (pk, handle, now))
        c.execute("UPDATE contributors SET handle=? WHERE pubkey=?", (handle, pk))
        c.execute("UPDATE ranges SET status='claimed', assignee=?, handle=?, claimed_at=?, last_beat=? WHERE id=?",
                  (pk, handle, now, now, rid))
        c.commit(); c.close()
    wit = os.path.join(WITNESS, f"block_{r['lo']}.json")
    return 200, {"ok": True, "range": rid,
                 "witness": f"/api/witness/{rid}" if os.path.exists(wit) else None,
                 "cmd": f"hazync prove {rid}", "heartbeat_ttl": CLAIM_TTL}

def heartbeat(body):
    rid, pk = body.get("range"), body.get("pubkey", "")
    if not rid or not pk: return 400, {"error": "range and pubkey required"}
    if HAVE_ED and not is_hex(pk, 32): return 400, {"error": "pubkey must be 32-byte hex (ed25519)"}
    reap()
    with _lock:
        c = db()
        r = c.execute("SELECT status, assignee FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r: c.close(); return 404, {"error": "no such range"}
        if r["status"] != "claimed" or r["assignee"] != pk:
            st = r["status"] if r else None
            c.close()
            return 409, {"ok": False, "error": "you no longer hold this claim (expired or reassigned)", "status": st}
        c.execute("UPDATE ranges SET last_beat=? WHERE id=?", (time.time(), rid))
        c.commit(); c.close()
    return 200, {"ok": True, "heartbeat_ttl": CLAIM_TTL}

def release(body):
    # Voluntarily hand a claim back to the pool — called by `hazync run` when a prove/submit fails, so a
    # failed block reopens in seconds instead of waiting out the CLAIM_TTL reap. Idempotent and ownership-
    # checked: only the holder frees a still-claimed range; anything else (already reaped, verified, not
    # ours) is a no-op success so the client never errors on cleanup.
    rid, pk = body.get("range"), body.get("pubkey", "")
    if not rid or not pk: return 400, {"error": "range and pubkey required"}
    if HAVE_ED and not is_hex(pk, 32): return 400, {"error": "pubkey must be 32-byte hex (ed25519)"}
    with _lock:
        c = db()
        r = c.execute("SELECT status, assignee FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r: c.close(); return 404, {"error": "no such range"}
        if r["status"] != "claimed" or r["assignee"] != pk:
            c.close(); return 200, {"ok": True, "noop": True}   # already freed/verified/not ours
        # A voluntary release only ever follows a failed prove/submit, so it counts as an attempt for
        # exactly the same reason a reap does. Without this, a client that dutifully releases on every
        # failure would loop forever and never trip MAX_ATTEMPTS, while a client that crashed would.
        err = str(body.get("error") or "released by holder after a failed prove/submit")[:500]
        row = c.execute("SELECT COALESCE(attempts,0) AS a, COALESCE(env_failures,0) AS e "
                        "FROM ranges WHERE id=?", (rid,)).fetchone()
        # An OOM says the box was full, not that the block is bad — count it separately, against a
        # looser cap, so a capacity incident cannot park perfectly provable blocks.
        env = is_env_failure(err)
        n, ef = (row["a"], row["e"] + 1) if env else (row["a"] + 1, row["e"])
        parked = (ef >= MAX_ENV_FAILURES) if env else (n >= MAX_ATTEMPTS)
        c.execute("UPDATE ranges SET status=?, attempts=?, env_failures=?, last_assignee=assignee, "
                  "last_failed_at=?, last_error=?, assignee=NULL, handle=NULL, claimed_at=NULL, "
                  "last_beat=NULL WHERE id=?",
                  ("failed" if parked else "open", n, ef, time.time(), err, rid))
        c.commit(); c.close()
    if parked:
        why = (f"{ef} environmental failures (capacity — check GPU memory/worker count, NOT the block)"
               if env else f"{n} attempts")
        print(f"[release] range {rid} PARKED as failed after {why}: {err}", flush=True)
    return 200, {"ok": True, "released": rid, "attempts": n, "env_failures": ef,
                 "environmental": env, "parked": parked}

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

def submit(body):
    rid, pk = body.get("range"), body.get("pubkey", "")
    sig, receipt_b64 = body.get("sig", ""), body.get("receipt", "")
    handle = clean_handle(body.get("handle"))
    if not (rid and pk and receipt_b64): return 400, {"error": "range, pubkey, receipt required"}
    if handle_reserved(handle): return 400, {"error": "that handle is reserved — please pick another"}
    if not parse_range(rid): return 400, {"error": "invalid range id"}
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
    if not r: return 404, {"error": "no such range"}
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
    return (200 if ok else 422), {"ok": ok, "range": rid, "receipt_sha": sha,
                                  "signature": "valid" if sig_ok else "invalid", "note": note}

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, ctype="application/json", raw=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
            return self._send(200, raw=state_cached(), ctype="application/json")
        if p == "/api/pick": code, obj = pick(None); return self._send(code, obj)
        if p == "/api/meta":                               # pre-flight: expected guest id + frontier
            return self._send(200, {"method_id": expected_method_id(), "frontier": frontier_hi(),
                                    "reproduce": "reproduce/METHOD_ID"})
        if p.startswith("/api/proof/"):                    # download a verified proof receipt (re-verify with `host verify-any`)
            rid = p.rsplit("/", 1)[-1]
            if parse_range(rid):
                f = os.path.join(PROOFS_DIR, f"proof_{rid}.bin")
                if os.path.exists(f):
                    return self._send(200, raw=open(f, "rb").read(), ctype="application/octet-stream")
            return self._send(404, {"error": "proof not available"})
        if p.startswith("/api/witness/"):
            seg = p.rsplit("/", 1)[-1]
            blk = int(seg) if seg.isdigit() else (parse_range(seg) or [None])[0]  # block number or range id
            if blk is not None:
                # Prefer the archive-node bridge bundle (in-boundary + real root_prev + inclusion proofs,
                # provable with NO replay); fall back to the legacy per-block witness for old provers.
                for f in ([os.path.join(BRIDGE_DIR, f"bundle_{blk}.json")] if BRIDGE_DIR else []) \
                         + [os.path.join(WITNESS, f"block_{blk}.json")]:
                    if os.path.exists(f):
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
        if p not in ("/api/claim", "/api/heartbeat", "/api/submit", "/api/release"):
            return self._send(404, {"error": "not found"})
        if not rate_ok(self._client_ip()):
            return self._send(429, {"error": "rate limit — slow down"})
        body = self._body()
        if body is None:
            return self._send(413, {"error": "request body too large"})
        fn = {"/api/claim": claim, "/api/heartbeat": heartbeat, "/api/submit": submit,
              "/api/release": release}[p]
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
    print(f"  api        GET /api/state · POST /api/claim · POST /api/submit · GET /api/witness/<range>")
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
