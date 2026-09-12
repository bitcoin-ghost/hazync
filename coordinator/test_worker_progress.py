#!/usr/bin/env python3
"""The worker watches a prove for PROGRESS, not silence (#256).

A stuck block used to cost up to 4 x 90 min (the timeout walked the out-of-memory segment ladder), with
a timer-driven heartbeat keeping the claim alive the whole time. Killing on silence is not the answer:
healthy provers on block 39,318 were killed for being quiet. Fake hosts print the real progress format.

  python3 test_worker_progress.py            # must PASS
  python3 test_worker_progress.py --control  # progress detection broken; MUST FAIL
"""
import importlib.machinery
import importlib.util
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_prog_")
os.environ.update(HAZYNC_STALL_MIN="1.5", HAZYNC_FIRST_PROGRESS="2", HAZYNC_TICK="0.3",
                  HAZYNC_ASSEMBLY_MIN="2")
CONTROL = "--control" in sys.argv

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)
hz._seg_ladder = lambda env: (None, 20, 19, 18)
if CONTROL:
    hz._PROGRESS_RE = re.compile(r"NEVER-MATCHES")
    hz._SEG_N_RE = re.compile(r"NEVER-MATCHES-EITHER")   # assembly is never detected -> pre-#256 behaviour
    print("CONTROL: progress lines are not recognised -- the checks below MUST fail")

T = tempfile.mkdtemp(prefix="hz_fakeprog_")
beats = []
hz.post = lambda path, body: beats.append(body) or {"ok": True}


class _Sk:
    def sign(self, m):
        return b"\x00" * 64


def fake(name, body):
    """A stand-in host. Records each invocation's HAZYNC_SEG_PO2 (or 'unset') in <name>.runs."""
    p = os.path.join(T, name)
    with open(p, "w") as f:
        f.write(f"#!/bin/bash\necho \"${{HAZYNC_SEG_PO2:-unset}}\" >> {p}.runs\n{body}\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


def seg_lines(n, gap, then="exit 0"):
    return (f'echo "range [5..5] (bridge): executed, {n} segments at po2 21 -- proving"\n'
            f'for k in $(seq 1 {n}); do sleep {gap}; echo "    segment $k/{n}  1s elapsed, ~1s left"; done\n{then}')


def runs(p):
    return [l.strip() for l in open(p + ".runs")] if os.path.exists(p + ".runs") else []


def attempt(host, argv=("prove-range-bridge", "5"), beat=None):
    hz.HOSTBIN = host
    work = tempfile.mkdtemp(prefix="hz_w_")
    try:
        hz._run_with_seg_retry(list(argv), dict(os.environ), work, "block 5",
                               _beat_range=beat, _beat_pk="ab" * 32, _beat_sk=_Sk())
        return None
    except SystemExit as e:
        return e.code


fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


h = fake("steady", seg_lines(10, 0.05))
code = attempt(h)
check(code is None and len(runs(h)) == 1, f"steady prove completes on the first attempt (code={code!r}, runs={runs(h)})")

h = fake("slow_steady", seg_lines(4, 1.0))
code = attempt(h)
check(code is None and len(runs(h)) == 1,
      f"slow but steady (1 s/segment, stall floor 1.5 s) is NOT killed -- the block 39,318 regression (runs={runs(h)})")

# This one hangs after its LAST segment, so since the assembly fix it is classified as a wedged
# ASSEMBLY rather than a missing-progress stall -- which is what it actually is. It is still killed,
# still retried once, and the message still names how far it got; only the phase name changed.
# The trade is deliberate: a genuine post-segment hang now takes HAZYNC_ASSEMBLY_MIN to detect instead
# of the progress window, and in exchange a healthy silent assembly is no longer killed at all.
h = fake("hang", seg_lines(2, 0.05, then="sleep 1000"))
beats.clear()
code = attempt(h, beat="5")
check(isinstance(code, str) and "gave up after one retry" in code and "2 segment receipts" in code,
      f"hangs after the LAST segment -> killed as a wedged assembly, naming the phase ({str(code)[:110]!r})")
check(runs(h) == ["unset", "unset"], f"...retried ONCE at the SAME segment size, ladder not walked (runs={runs(h)})")
check(0 < len(beats) <= 4, f"...heartbeats only while segments were finishing, none during the stall (beats={len(beats)})")

h = fake("silent", "sleep 1000")
code = attempt(h)
check(isinstance(code, str) and "no segment finished within" in code and runs(h) == ["unset", "unset"],
      f"silent from the start -> killed at HAZYNC_FIRST_PROGRESS, retried once ({str(code)[:70]!r})")

h = fake("fold", "sleep 3; exit 0")
code = attempt(h, argv=("fold-range", "a", "b", "m"))
check(code is None and len(runs(h)) == 1, f"fold-range prints no progress and is NOT killed by the window (code={code!r})")

h = fake("oom", 'echo "memory allocation of 268435456 bytes failed" >&2; exit 101')
code = attempt(h)
check(runs(h) == ["unset", "20", "19", "18"], f"an ordinary failure still walks the ladder (runs={runs(h)})")

# ---- the ASSEMBLY phase (block 39,318, 2026-09-12) -------------------------------------------------
# All segments prove, then the host lifts and joins them into one receipt and — on a host without the
# assembly heartbeat — says NOTHING while it does. The progress window used to kill it there: 880/880
# proved in 3,102 s, silence, killed at 600 s, retried, killed, re-claimed, forever. Assembly scales
# with segment count, so above ~600 segments every block died this way and the frontier could not pass.
h = fake("assembling", seg_lines(4, 0.05, then="sleep 1.2; exit 0"))
code = attempt(h)
check(code is None and len(runs(h)) == 1,
      f"a SILENT assembly after the last segment is NOT killed -- the block 39,318 loop (code={code!r}, runs={runs(h)})")

# It is bounded, not unbounded: a host that never returns still dies, and says which phase it died in.
h = fake("assembly_wedged", seg_lines(4, 0.05, then="sleep 1000"))
code = attempt(h)
check(isinstance(code, str) and "assembly (lift + join of 4 segment receipts)" in code,
      f"...but a wedged assembly is still killed, naming the phase ({str(code)[:100]!r})")

# A host that DOES report assembly progress is watched normally, like any other phase.
h = fake("assembly_reports",
         seg_lines(4, 0.05, then='for k in 1 2 3 4 5 6; do sleep 0.4; echo "    assembling 4 segment receipts (lift + join)  ${k}s elapsed"; done; exit 0'))
code = attempt(h)
check(code is None and len(runs(h)) == 1,
      f"a host that reports assembly progress completes on the first attempt (code={code!r}, runs={runs(h)})")

# The budget must not resurrect the thing #256 fixed: a stall BEFORE the last segment still dies fast.
h = fake("midway_hang", seg_lines(2, 0.05, then="sleep 1000").replace("segment $k/2", "segment $k/9"))
code = attempt(h)
check(isinstance(code, str) and "no progress for" in code,
      f"a hang BEFORE the last segment is still caught by the progress window ({str(code)[:80]!r})")

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
