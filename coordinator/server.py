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
DB         = os.environ.get("COORD_DB", "coordinator.db")
WEB        = os.environ.get("COORD_WEB", os.path.join(os.path.dirname(__file__), "web"))
TIP        = int(os.environ.get("TIP_HEIGHT", "958301"))
RANGE_SIZE = int(os.environ.get("RANGE_SIZE", "1000"))
SEED       = int(os.environ.get("SEED_RANGES", "60"))
WITNESS    = os.environ.get("WITNESS_DIR", os.path.join(os.path.dirname(__file__), "witnesses"))
HOST_BIN   = os.environ.get("HAZYNC_HOST", "")
VERIFY     = os.environ.get("VERIFY_MODE", "mock" if not HOST_BIN else "real")
STATE_DIR  = os.environ.get("COORD_STATE", os.path.join(os.path.dirname(__file__), "state"))
GENESIS_TIP = os.environ.get("GENESIS_TIP", "6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000")
CLAIM_TTL  = int(os.environ.get("CLAIM_TTL", "1800"))    # auto-release a claim after no heartbeat this long
CLAIM_MAX  = int(os.environ.get("CLAIM_MAX", "86400"))   # hard cap: release a claim after this long regardless

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    HAVE_ED = True
except Exception:
    HAVE_ED = False

_lock = threading.Lock()

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
        pubkey TEXT, handle TEXT, ts REAL);
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
    """Validate a range id 'lo-hi': aligned to RANGE_SIZE, correct size, within [0, TIP). Returns (lo,hi)."""
    try:
        lo, hi = (int(x) for x in rid.split("-"))
    except Exception:
        return None
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
    """ed25519 signature over the receipt bytes. Dev mode (no lib) accepts and flags."""
    if not HAVE_ED:
        return True  # dev mode — see /api/state.signatures
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
        return True, "mock-verified (VERIFY_MODE=mock)", {"in_tip": "mock:%d" % rng["lo"], "out_tip": "mock:%d" % rng["hi"]}
    if not HOST_BIN or not os.path.exists(HOST_BIN):
        return False, "no HAZYNC_HOST binary configured for real verification", None
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, f"in_{rng['id']}.bin")
    with open(tmp, "wb") as f:
        f.write(receipt)
    try:
        r = subprocess.run([HOST_BIN, "verify-any", tmp], capture_output=True, timeout=120)
        out = (r.stdout + r.stderr).decode(errors="replace")
        if r.returncode != 0 or "RANGE-OK" not in out:
            return False, "receipt rejected (not a valid proof): " + out[-160:], None
        kv = dict(t.split("=", 1) for t in out.split("RANGE-OK", 1)[1].split() if "=" in t)
        lo, hi = int(kv["lo"]), int(kv["hi"])
        if lo != rng["lo"] or hi != rng["hi"]:
            return False, f"receipt proves [{lo}..{hi}], not the claimed [{rng['lo']}..{rng['hi']}]", None
        return True, f"range [{lo}..{hi}] VERIFIED", {"in_tip": kv["in_tip"], "out_tip": kv["out_tip"]}
    except Exception as e:
        return False, f"verify error: {e}", None
    finally:
        try: os.remove(tmp)
        except Exception: pass

def frontier_hi():
    """Highest block covered by a contiguous chain of verified ranges from genesis (tip-matched)."""
    c = db()
    rows = c.execute("SELECT lo,hi,in_tip,out_tip FROM vranges").fetchall()
    c.close()
    by_in = {}
    for r in rows:
        by_in.setdefault(r["in_tip"], r)  # first wins on ties
    tip, hi, seen = GENESIS_TIP, 0, set()
    while tip in by_in and tip not in seen:
        seen.add(tip); r = by_in[tip]; hi = r["hi"]; tip = r["out_tip"]
    return hi

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
    proven = c.execute("SELECT COALESCE(SUM(hi-lo+1),0) FROM vranges").fetchone()[0]
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
    c.close()
    return {
        "progress": {"proven": proven, "frontier": fr, "tip": TIP,
                     "pct": round(100.0*fr/TIP, 3) if TIP else 0, "contributors": ncontrib},
        "board": board, "leaderboard": leaders, "recent": recent,
        "timeline": timeline(fr),
        "signatures": "ed25519" if HAVE_ED else "dev (no signature lib installed)",
        "verify_mode": VERIFY,
    }

def claim(body):
    rid, pk, handle = body.get("range"), body.get("pubkey", ""), body.get("handle", "anon")
    if not rid or not pk: return 400, {"error": "range and pubkey required"}
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
    handle = body.get("handle", "anon")
    if not (rid and pk and receipt_b64): return 400, {"error": "range, pubkey, receipt required"}
    try: receipt = base64.b64decode(receipt_b64)
    except Exception: return 400, {"error": "receipt must be base64"}
    sha = hashlib.sha256(receipt).hexdigest()
    with _lock:
        c = db()
        r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r: c.close(); return 404, {"error": "no such range"}
        if r["status"] == "verified": c.close(); return 409, {"error": "already proven"}
        sig_ok = verify_sig(pk, sig, receipt)
        rcpt_ok, note, meta = verify_receipt(receipt, r) if sig_ok else (False, "signature invalid", None)
        ok = sig_ok and rcpt_ok
        c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
                  " VALUES(?,?,?,?,?,?,?,?)", (rid, pk, handle, sha, sig, int(ok), note, time.time()))
        if ok:
            c.execute("UPDATE ranges SET status='verified', receipt_sha=?, verified_at=? WHERE id=?",
                      (sha, time.time(), rid))
            c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (rid, r["lo"], r["hi"], meta["in_tip"], meta["out_tip"], pk, handle, time.time()))
            c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                      (pk, handle, time.time()))
            c.execute("UPDATE contributors SET blocks=blocks+?, handle=? WHERE pubkey=?",
                      (r["hi"]-r["lo"]+1, handle, pk))
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
    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/state": return self._send(200, state())
        if p == "/api/pick": code, obj = pick(None); return self._send(code, obj)
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
        if p == "/api/claim":     code, obj = claim(self._body());     return self._send(code, obj)
        if p == "/api/heartbeat": code, obj = heartbeat(self._body()); return self._send(code, obj)
        if p == "/api/submit": code, obj = submit(self._body()); return self._send(code, obj)
        return self._send(404, {"error": "not found"})

if __name__ == "__main__":
    init_db()
    print(f"[hazync-coordinator] :{PORT}  db={DB}  verify={VERIFY}  sigs={'ed25519' if HAVE_ED else 'dev'}")
    print(f"  dashboard  http://localhost:{PORT}/")
    print(f"  api        GET /api/state · POST /api/claim · POST /api/submit · GET /api/witness/<range>")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
