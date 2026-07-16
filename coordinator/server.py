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
        receipt_sha TEXT, claimed_at REAL, verified_at REAL);
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
    c.commit(); c.close()

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

def state():
    c = db()
    proven = c.execute("SELECT COALESCE(SUM(hi-lo+1),0) FROM vranges").fetchone()[0]
    ncontrib = c.execute("SELECT COUNT(*) FROM contributors WHERE blocks>0").fetchone()[0]
    # board window: all verified + claimed, then a few open around the frontier
    board = []
    for r in c.execute("SELECT * FROM ranges ORDER BY lo LIMIT 18"):
        board.append({"id": r["id"], "lo": r["lo"], "hi": r["hi"], "status": r["status"],
                      "handle": r["handle"]})
    leaders = [dict(id=x["pubkey"][:10], handle=x["handle"], blocks=x["blocks"])
               for x in c.execute("SELECT * FROM contributors ORDER BY blocks DESC LIMIT 8")]
    recent = [dict(range=s["range_id"], handle=s["handle"], verified=bool(s["verified"]),
                   ts=s["ts"], note=s["note"])
              for s in c.execute("SELECT * FROM submissions ORDER BY ts DESC LIMIT 8")]
    c.close()
    fr = frontier_hi()
    return {
        "progress": {"proven": proven, "frontier": fr, "tip": TIP,
                     "pct": round(100.0*fr/TIP, 3) if TIP else 0, "contributors": ncontrib},
        "board": board, "leaderboard": leaders, "recent": recent,
        "signatures": "ed25519" if HAVE_ED else "dev (no signature lib installed)",
        "verify_mode": VERIFY,
    }

def claim(body):
    rid, pk, handle = body.get("range"), body.get("pubkey", ""), body.get("handle", "anon")
    if not rid or not pk: return 400, {"error": "range and pubkey required"}
    with _lock:
        c = db()
        r = c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()
        if not r: c.close(); return 404, {"error": "no such range"}
        if r["status"] == "verified": c.close(); return 409, {"error": "already proven"}
        c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
                  (pk, handle, time.time()))
        c.execute("UPDATE contributors SET handle=? WHERE pubkey=?", (handle, pk))
        c.execute("UPDATE ranges SET status='claimed', assignee=?, handle=?, claimed_at=? WHERE id=?",
                  (pk, handle, time.time(), rid))
        c.commit(); c.close()
    wit = os.path.join(WITNESS, f"witness_{rid}.json")
    return 200, {"ok": True, "range": rid,
                 "witness": f"/api/witness/{rid}" if os.path.exists(wit) else None,
                 "cmd": f"hazync prove {rid}"}

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
        if p.startswith("/api/witness/"):
            rid = p.rsplit("/", 1)[-1]
            f = os.path.join(WITNESS, f"witness_{rid}.json")
            if os.path.exists(f):
                return self._send(200, raw=open(f, "rb").read())
            return self._send(404, {"error": "witness not available for this range"})
        # static frontend
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        fp = os.path.normpath(os.path.join(WEB, rel))
        if fp.startswith(os.path.abspath(WEB)) and os.path.isfile(fp):
            ct = "text/html" if fp.endswith(".html") else "text/plain"
            return self._send(200, raw=open(fp, "rb").read(), ctype=ct)
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/claim":  code, obj = claim(self._body());  return self._send(code, obj)
        if p == "/api/submit": code, obj = submit(self._body()); return self._send(code, obj)
        return self._send(404, {"error": "not found"})

if __name__ == "__main__":
    init_db()
    print(f"[hazync-coordinator] :{PORT}  db={DB}  verify={VERIFY}  sigs={'ed25519' if HAVE_ED else 'dev'}")
    print(f"  dashboard  http://localhost:{PORT}/")
    print(f"  api        GET /api/state · POST /api/claim · POST /api/submit · GET /api/witness/<range>")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
