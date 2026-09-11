#!/usr/bin/env python3
"""The worker heartbeats ONLY a range it claimed (#251).

`hazync run <range>` and `hazync prove <range>` hold no claim. Before #251 their prove ticker
heartbeated the range anyway, and the coordinator rejected every beat 409: 400+ of them from the
first outside contributor (bip-448, 2026-09-11), which made a healthy long run look like a stuck
worker hammering the coordinator. Only the no-argument `hazync run`, which claims first, may beat.

Loads the real CLI and stubs only the network, the GPU and the fold. What is checked is the
`_beat_range` that cmd_run / cmd_prove hand to the prove loop.

  python3 test_worker_beat.py            # must PASS
  python3 test_worker_beat.py --control  # restores the old always-beat behaviour; MUST FAIL
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_worker_")
CONTROL = "--control" in sys.argv

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)

beats = []          # the _beat_range each prove attempt was given
claims = []


class _Sk:
    def sign(self, m):
        return b"\x00" * 64


hz.HOSTBIN = sys.executable                     # any existing file satisfies the path checks
hz.RCPTDIR = hz.pathlib.Path(tempfile.mkdtemp(prefix="hz_rcpt_"))
hz.identity = lambda: (_Sk(), "ab" * 32, "tester")
hz.witness_in_window = lambda blk: True
hz.fetch_bundles = lambda lo, hi: True
hz._fold_and_save = lambda parts, out, work, env: None
hz.cmd_submit = lambda args: None


def _post(path, body):
    if path == "/api/claim":
        claims.append(body)
        return {"ok": True, "range": "777", "ttl": 3600}
    raise AssertionError(f"unexpected POST {path}")


hz.post = _post
hz._run_with_seg_retry = lambda argv, env, work, label, _beat_range=None, _beat_pk=None, _beat_sk=None: \
    beats.append(_beat_range)

if CONTROL:
    _orig = hz.cmd_prove
    hz.cmd_prove = lambda args, claimed=False: _orig(args, claimed=True)   # the pre-#251 behaviour
    print("CONTROL: every prove heartbeats its range -- the checks below MUST fail")

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


beats.clear(); claims.clear()
hz.cmd_run(["5-6"])
check(claims == [], "`run 5-6` (explicit range) does not claim")
check(beats == [None, None], f"`run 5-6` heartbeats nothing (got {beats})")

beats.clear(); claims.clear()
hz.cmd_prove(["9"])
check(beats == [None], f"`prove 9` heartbeats nothing (got {beats})")

beats.clear(); claims.clear()
hz.cmd_run([])
check(len(claims) == 1, "`run` (no argument) claims")
check(beats == ["777"], f"`run` heartbeats exactly the range it claimed (got {beats})")

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
