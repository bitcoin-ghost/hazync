#!/usr/bin/env python3
"""A claim whose response is lost does not orphan a block (#268).

POST /api/claim commits the claim and then answers. If the answer never arrives, the worker retries,
and before #268 every retry took the NEXT free block and left the first `claimed` for a full
CLAIM_TTL: 19 blocks under the frontier in the 2026-09-11 lock-up, attempts=0, never beaten.

End to end: the REAL HTTP handler on a loopback port, and the REAL `hazync run` claim path pointed at
it. The first claim request is committed by the server and then its connection is dropped before any
response -- what a proxy or client timeout looks like from the worker. Also checks the server rules
directly: a retry of the same claim gets the same block, but a fresh claim still cannot re-take its
own block (the 39,318 rule), and a different key cannot borrow someone else's nonce.

  python3 test_claim_nonce.py            # must PASS
  python3 test_claim_nonce.py --control  # the worker sends no nonce (the pre-#268 client); MUST FAIL
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import threading
import time

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_worker_")
os.environ.setdefault("COORD_WEB", os.path.dirname(os.path.abspath(__file__)))
CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, HERE)
import server  # noqa: E402
server.init_db()
server.provable_tip = lambda: 1_000
server.witness_available = lambda h: True

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


def claimed_rows():
    c = server.db()
    rows = [(r["lo"], r["assignee"]) for r in c.execute(
        "SELECT lo, assignee FROM ranges WHERE status='claimed' ORDER BY lo")]
    c.close()
    return rows


def reset():
    c = server.db()
    c.execute("DELETE FROM ranges WHERE status='claimed'")
    c.commit()
    c.close()


PK_A, PK_B = "aa" * 32, "bb" * 32

# ---- server rules, called directly ----
if not CONTROL:
    r1 = server.claim({"pubkey": PK_A, "handle": "a", "nonce": "n1"})[1]["range"]
    r2 = server.claim({"pubkey": PK_A, "handle": "a", "nonce": "n1"})[1]["range"]
    check(r1 == r2, f"a retry of the same claim gets the same block ({r1} then {r2})")
    r3 = server.claim({"pubkey": PK_A, "handle": "a", "nonce": "n2"})[1]["range"]
    check(r3 != r1, f"a fresh claim by the same key cannot re-take its own block ({r3} vs {r1})")
    r4 = server.claim({"pubkey": PK_B, "handle": "b", "nonce": "n1"})[1]["range"]
    check(r4 not in (r1, r3), f"another key cannot borrow a nonce to take someone's block ({r4})")
    r5 = server.claim({"pubkey": PK_A, "handle": "a"})[1]["range"]
    r6 = server.claim({"pubkey": PK_A, "handle": "a"})[1]["range"]
    check(r5 != r6, f"no nonce: the old behaviour, every claim is a new block ({r5}, {r6})")
    c = server.db()
    c.execute("UPDATE ranges SET claimed_at=? WHERE id=?", (time.time() - server.CLAIM_TTL - 60, r1))
    c.commit()
    c.close()
    check(server._claim_by_nonce(server.db(), PK_A, "n1", time.time()) is None,
          "an expired claim is not handed back by its nonce")
    reset()

# ---- end to end: the real worker against the real handler, first response lost ----
_real_claim = server.claim
calls = []


def _lossy_claim(body):
    calls.append(dict(body))
    out = _real_claim(body)                      # committed on the server...
    if len(calls) == 1:
        raise ConnectionAbortedError("test: response lost after commit")   # ...answer never sent
    return out


server.claim = _lossy_claim
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
httpd.handle_error = lambda *a: None            # the dropped connection is the point; keep stderr quiet
threading.Thread(target=httpd.serve_forever, daemon=True).start()
os.environ["COORD_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}"

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)


class _Sk:
    def sign(self, m):
        return b"\x00" * 64


got = []
hz.HOSTBIN = sys.executable
hz.identity = lambda: (_Sk(), PK_A, "tester")
hz.cmd_prove = lambda args, claimed=False: got.append(args[0])
hz.cmd_submit = lambda args: None
hz.time.sleep = lambda s: None                   # the retry backoff, not what is under test

if CONTROL:
    _post = hz.post
    hz.post = lambda path, body: _post(path, {k: v for k, v in body.items() if k != "nonce"})
    print("CONTROL: the worker sends no nonce -- the checks below MUST fail")

hz.cmd_run([])
rows = claimed_rows()
check(len(calls) == 2, f"the lost response was retried ({len(calls)} claim requests)")
check(len(rows) == 1, f"exactly one block is claimed, none orphaned (claimed: {[r[0] for r in rows]})")
check(len(got) == 1 and rows and str(rows[0][0]) == got[0],
      f"the worker proves the block the server committed (proves {got}, claimed {[r[0] for r in rows]})")

httpd.shutdown()
print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
