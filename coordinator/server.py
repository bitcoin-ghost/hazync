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
HOST_BIN   = os.environ.get("HAZYNC_HOST", "")
VERIFY     = os.environ.get("VERIFY_MODE", "mock" if not HOST_BIN else "real")
STATE_DIR  = os.environ.get("COORD_STATE", os.path.join(os.path.dirname(__file__), "state"))
PROOFS_DIR = os.environ.get("COORD_PROOFS", os.path.join(os.path.dirname(__file__), "proofs"))  # kept, downloadable
GENESIS_TIP = os.environ.get("GENESIS_TIP", "6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000")
CLAIM_TTL  = int(os.environ.get("CLAIM_TTL", "1800"))    # auto-release a claim after no heartbeat this long
CLAIM_MAX  = int(os.environ.get("CLAIM_MAX", "86400"))   # hard cap: release a claim after this long regardless
MAX_BODY   = int(os.environ.get("MAX_BODY", str(8 << 20)))   # reject POST bodies larger than this (8 MiB)
MAX_HANDLE = int(os.environ.get("MAX_HANDLE", "48"))         # cap contributor handle length
RATE_MAX   = int(os.environ.get("RATE_MAX", "120"))          # max writes per IP per window
RATE_WINDOW= int(os.environ.get("RATE_WINDOW", "60"))        # rate-limit window (seconds)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    HAVE_ED = True
except Exception:
    HAVE_ED = False

_lock = threading.Lock()
_rate = {}          # ip -> [timestamps] sliding window, guarded by _rate_lock
_rate_lock = threading.Lock()

def rate_ok(ip):
    """Sliding-window per-IP write limiter. True if this write is within budget."""
    now = time.time()
    with _rate_lock:
        q = [t for t in _rate.get(ip, ()) if now - t < RATE_WINDOW]
        if len(q) >= RATE_MAX:
            _rate[ip] = q
            return False
        q.append(now); _rate[ip] = q
        return True

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
    for col in ("out_leaves INTEGER", "range_work TEXT",
                "in_bhash TEXT", "out_bhash TEXT"):  # H7/S1: full-boundary continuity digest
        try: c.execute(f"ALTER TABLE vranges ADD COLUMN {col}")
        except Exception: pass
    c.commit(); c.close()

def reap():
    """Free stale claims: no heartbeat for CLAIM_TTL, or held longer than CLAIM_MAX. Lazy — called on
    each state()/claim(), so an abandoned claim returns to the pool within a poll interval."""
    now = time.time()
    c = db()
    c.execute("UPDATE ranges SET status='open', assignee=NULL, handle=NULL, claimed_at=NULL, last_beat=NULL "
              "WHERE status='claimed' AND ("
              " (last_beat IS NOT NULL AND ?-last_beat > ?) OR"
              " (claimed_at IS NOT NULL AND ?-claimed_at > ?) )",
              (now, CLAIM_TTL, now, CLAIM_MAX))
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

def pick(body):
    """Suggest the next open block/range after the genesis frontier — the 'just give me something' pick."""
    reap()
    fr = frontier_hi()
    c = db()
    taken = set(r["id"] for r in c.execute("SELECT id FROM ranges WHERE status IN ('claimed','verified')"))
    c.close()
    k = max(1, fr + 1)
    for _ in range(200000):
        lo = (k // RANGE_SIZE) * RANGE_SIZE
        hi = lo + RANGE_SIZE - 1
        rid = f"{lo}-{hi}"
        if hi >= TIP:
            break
        if lo >= 1 and rid not in taken:
            return 200, {"range": rid, "lo": lo, "hi": hi, "cmd": f"hazync run {rid}"}
        k = hi + 1
    return 404, {"error": "no open range available"}

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
            return False, "receipt rejected (not a valid proof): " + (r.stdout + r.stderr).decode(errors="replace")[-160:], None
        kv = dict(t.split("=", 1) for t in line[len("RANGE-OK"):].split() if "=" in t)
        lo, hi = int(kv["lo"]), int(kv["hi"])
        if lo != rng["lo"] or hi != rng["hi"]:
            return False, f"receipt proves [{lo}..{hi}], not the claimed [{rng['lo']}..{rng['hi']}]", None
        return True, f"range [{lo}..{hi}] VERIFIED", {"in_tip": kv["in_tip"], "out_tip": kv["out_tip"],
                "out_leaves": int(kv.get("out_leaves", 0)), "range_work": kv.get("range_work", "0"),
                "in_bhash": kv.get("in_bhash", ""), "out_bhash": kv.get("out_bhash", "")}
    except Exception as e:
        return False, f"verify error: {e}", None
    finally:
        try: os.remove(tmp)
        except Exception: pass

def _frontier_chain():
    """Walk verified ranges from genesis, chaining on the FULL boundary — tip-hash AND difficulty/MTP
    continuity (H7: out_nbits/out_epoch of range k must equal in_nbits/in_epoch of range k+1). Tip-hash
    equality alone does not bind difficulty across a seam, so a range could otherwise claim an easier
    in_nbits and be mined cheaper. The genesis-connecting range's in-boundary is pinned by `verify-any`
    (assert_genesis_in_boundary). Returns (hi, tip_hash, cum_work, leaves)."""
    c = db()
    rows = c.execute("SELECT lo,hi,in_tip,out_tip,out_leaves,range_work,in_bhash,out_bhash "
                     "FROM vranges ORDER BY lo, ts").fetchall()  # F2: deterministic order, not rowid
    c.close()
    by_in = {}
    for r in rows:
        by_in.setdefault(r["in_tip"], r)  # first-wins is now safe: a chained range must match the full boundary digest
    tip, hi, seen = GENESIS_TIP, 0, set()
    cum_work, leaves, tip_hash = 0, 0, GENESIS_TIP
    prev_bhash = None  # None at genesis: verify-any pinned that range's full in-boundary
    prev_hi = 0        # H9: height cursor — the genesis-connecting range must be [1..], then contiguous
    while tip in by_in and tip not in seen:
        r = by_in[tip]
        if not r["in_bhash"]:
            break  # F3: no boundary digest (pre-migration / NULL) — not chainable
        if r["lo"] != prev_hi + 1:
            break  # H9: HEIGHT must be contiguous from genesis (lo==1 first, then hi(k)+1). boundary_digest
                   # binds the UTXO/difficulty/MTP state but NOT height, so a block mined onto the real tip
                   # yet labelled with a false (low) height — claiming a larger subsidy and weaker script
                   # flags — has a valid tip/boundary and would otherwise splice in. The guest fold_range
                   # enforces this same adjacency (l.hi+1==rr.lo); the coordinator seam must match it.
        if prev_bhash is not None and str(r["in_bhash"]) != str(prev_bhash):
            break  # S1/F1: full-boundary discontinuity (UTXO roots / difficulty / MTP) — do not chain
        seen.add(tip)
        hi = r["hi"]; tip_hash = r["out_tip"]
        try: cum_work += int(r["range_work"] or 0)
        except Exception: pass
        leaves = r["out_leaves"] or 0
        prev_bhash = r["out_bhash"]; prev_hi = r["hi"]
        tip = r["out_tip"]
    return hi, tip_hash, cum_work, leaves

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
            b = {"id": rid, "lo": lo, "hi": hi, "status": r["status"], "handle": r["handle"]}
            if r["status"] == "claimed":
                b["elapsed"] = int(now - (r["claimed_at"] or now))
                b["beat"] = int(now - (r["last_beat"] or r["claimed_at"] or now))
                b["stale"] = b["beat"] > CLAIM_TTL // 2
        else:
            b = {"id": rid, "lo": lo, "hi": hi, "status": "open", "handle": None}
        board.append(b)
    leaders = [dict(id=x["pubkey"][:10], handle=x["handle"], blocks=x["blocks"])
               for x in c.execute("SELECT * FROM contributors ORDER BY blocks DESC LIMIT 8")]
    recent = [dict(range=s["range_id"], handle=s["handle"], verified=bool(s["verified"]),
                   ts=s["ts"], note=s["note"])
              for s in c.execute("SELECT * FROM submissions ORDER BY ts DESC LIMIT 8")]
    # full verified + claimed lists so the client can browse/search/filter any block, not just the
    # frontier window (each is small: claims are few, verified ranges are RANGE_SIZE-coarse).
    vranges = []
    for r in c.execute("SELECT id,lo,hi,handle FROM vranges ORDER BY lo"):
        v = dict(lo=r["lo"], hi=r["hi"], handle=r["handle"])
        if os.path.exists(os.path.join(PROOFS_DIR, f"proof_{r['id']}.bin")):
            v["proof"] = f"/api/proof/{r['id']}"      # downloadable receipt, re-verifiable by anyone
        vranges.append(v)
    claims = []
    for r in c.execute("SELECT lo,hi,handle,claimed_at,last_beat FROM ranges WHERE status='claimed' ORDER BY lo"):
        beat = int(now - (r["last_beat"] or r["claimed_at"] or now))
        claims.append(dict(lo=r["lo"], hi=r["hi"], handle=r["handle"],
                           elapsed=int(now - (r["claimed_at"] or now)), stale=beat > CLAIM_TTL // 2))
    c.close()
    return {
        "progress": {"proven": proven, "frontier": fr, "tip": TIP,
                     "pct": round(100.0*fr/TIP, 3) if TIP else 0, "contributors": ncontrib},
        "board": board, "leaderboard": leaders, "recent": recent,
        "vranges": vranges, "claims": claims, "range_size": RANGE_SIZE,
        "frontier_proof": frontier_proof(),
        "timeline": timeline(fr),
        "signatures": "ed25519" if HAVE_ED else "dev (no signature lib installed)",
        "verify_mode": VERIFY,
    }

def claim(body):
    rid, pk, handle = body.get("range"), body.get("pubkey", ""), clean_handle(body.get("handle"))
    if not rid or not pk: return 400, {"error": "range and pubkey required"}
    if HAVE_ED and not is_hex(pk, 32): return 400, {"error": "pubkey must be 32-byte hex (ed25519)"}
    if not parse_range(rid): return 400, {"error": "invalid range id"}
    reap()
    now = time.time()
    with _lock:
        c = db()
        r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r:
            pr = parse_range(rid)                       # pick-any: auto-create a valid range on demand
            if not pr: c.close(); return 400, {"error": "invalid range id"}
            c.execute("INSERT INTO ranges(id,lo,hi) VALUES(?,?,?)", (rid, pr[0], pr[1]))
            r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        if r["status"] == "verified": c.close(); return 409, {"error": "already proven"}
        if r["status"] == "claimed" and r["assignee"] != pk:
            # locked to someone else and still alive (reap() already freed stale ones)
            since = int((now - (r["last_beat"] or r["claimed_at"] or now)) / 60)
            c.close()
            return 409, {"error": f"locked — being proved by {r['handle']} ({since}m active)"}
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

def submit(body):
    rid, pk = body.get("range"), body.get("pubkey", "")
    sig, receipt_b64 = body.get("sig", ""), body.get("receipt", "")
    handle = clean_handle(body.get("handle"))
    if not (rid and pk and receipt_b64): return 400, {"error": "range, pubkey, receipt required"}
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
    # 2. expensive verification OUTSIDE the lock (concurrent submits for different ranges now run in parallel)
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
            c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work,"
                      "in_bhash,out_bhash)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, r["lo"], r["hi"], meta["in_tip"], meta["out_tip"], pk, handle, time.time(),
                       meta.get("out_leaves", 0), str(meta.get("range_work", "0")),
                       str(meta.get("in_bhash", "")), str(meta.get("out_bhash", ""))))
            c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                      (pk, handle, time.time()))
            c.execute("UPDATE contributors SET blocks=blocks+?, handle=? WHERE pubkey=?",
                      (r["hi"]-r["lo"]+1, handle, pk))
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
        xff = self.headers.get("X-Forwarded-For")
        return xff.split(",")[0].strip() if xff else self.client_address[0]
    def _body(self):
        try: n = int(self.headers.get("Content-Length", 0))
        except Exception: n = 0
        if n > MAX_BODY: self.rfile.read(min(n, MAX_BODY)); return None   # oversized — signal 413
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/state": return self._send(200, state())
        if p == "/api/pick": code, obj = pick(None); return self._send(code, obj)
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
                f = os.path.join(WITNESS, f"block_{blk}.json")     # per-block witness (bridge output)
                if os.path.exists(f):
                    return self._send(200, raw=open(f, "rb").read())
            return self._send(404, {"error": "witness not available"})
        # static frontend
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        fp = os.path.normpath(os.path.join(WEB, rel))
        if fp.startswith(os.path.abspath(WEB)) and os.path.isfile(fp):
            ct = "text/html" if fp.endswith(".html") else "text/plain"
            return self._send(200, raw=open(fp, "rb").read(), ctype=ct)
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        p = urlparse(self.path).path
        if p not in ("/api/claim", "/api/heartbeat", "/api/submit"):
            return self._send(404, {"error": "not found"})
        if not rate_ok(self._client_ip()):
            return self._send(429, {"error": "rate limit — slow down"})
        body = self._body()
        if body is None:
            return self._send(413, {"error": "request body too large"})
        fn = {"/api/claim": claim, "/api/heartbeat": heartbeat, "/api/submit": submit}[p]
        code, obj = fn(body)
        return self._send(code, obj)

if __name__ == "__main__":
    init_db()
    print(f"[hazync-coordinator] :{PORT}  db={DB}  verify={VERIFY}  sigs={'ed25519' if HAVE_ED else 'dev'}")
    print(f"  dashboard  http://localhost:{PORT}/")
    print(f"  api        GET /api/state · POST /api/claim · POST /api/submit · GET /api/witness/<range>")
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
