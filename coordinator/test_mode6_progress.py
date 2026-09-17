#!/usr/bin/env python3
"""A DISTRIBUTED (mode-6) prove must beat its claim, or it loses the block 10 minutes in (#361/#364).

WHY THIS EXISTS. `hazync` watches a prove for PROGRESS and beats the claim only when progress happened
since the last beat (#256) — a timer alone kept a HUNG prover's claim alive for hours. That gate reads
`_PROGRESS_RE`, and until now the pattern only knew what a SINGLE-CARD prove prints:

    segment 91/2352                                  <- singular, number LAST

A distributed run (`HAZYNC_RANGE=<n> host seg-serve`) prints neither of those shapes:

         91/2352 segments  124s elapsed, ~3071s left <- plural, number FIRST
         joins 25/2352

So `st["n"]` never rose, the beat never fired, and a claim that has NEVER beaten is released after
CLAIM_GRACE — 600 s (#296). Measured on the first mode-6 run, block 74,928 took 3,694 s across three
cards (docs/history/BENCH_MODE6_3xRTX4090_2026-09-17.md): the operator would have lost the claim at
ten minutes and had their block handed to someone else while they were still proving it.

⚠ THE POINT IS THE BEAT, NOT THE REGEX. These assertions drive `_prove_watched` with a fake host that
emits real seg-serve output and check that BEATS ARE SENT. A test that only matched the pattern would
pass even if the beat were wired wrong.

⚠ AND IT MUST STAY PROGRESS-GATED. Scenario 3 is the #256 regression guard: a host that prints nothing
must send NO beats, however long it runs. Beating on a timer is the failure #360 was just untangled
from, and it would keep a wedged distributed run's claim alive for CLAIM_MAX.

WHAT THIS DOES NOT COVER: no real proving, no GPU, no network. The fake host writes the exact lines the
real one does; what is checked is the watcher's reaction to them.

Usage:
  python3 test_mode6_progress.py            # assertions; exit 0 on success
  python3 test_mode6_progress.py --control  # restores the old pattern; MUST fail
"""
import importlib.machinery
import importlib.util
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_mode6_")
# Short windows so the suite runs in seconds; the real defaults are 600 s / 1800 s.
os.environ.update(HAZYNC_STALL_MIN="4", HAZYNC_FIRST_PROGRESS="6", HAZYNC_TICK="0.3")
CONTROL = "--control" in sys.argv

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)

if CONTROL:
    # Exactly the pattern before #361's follow-up: it knows only the single-card shapes.
    hz._PROGRESS_RE = re.compile(r"(segment \d+/\d+|executed, \d+ segments|assembling \d+ segment receipts)")

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


beats = []


def fake_post(path, body):
    if path == "/api/beat":
        beats.append(body.get("range"))
    return {"ok": True}


hz.post = fake_post


class FakeKey:
    def sign(self, b):
        class S:
            def hex(self_inner):
                return "00" * 64
        return S()


def fake_host(lines, sleep_between=0.4):
    """A host binary that prints `lines` with a pause between, then exits 0."""
    d = tempfile.mkdtemp(prefix="hz_fakehost_")
    p = os.path.join(d, "host")
    body = "#!/usr/bin/env python3\nimport sys, time\n"
    for ln in lines:
        body += f"print({ln!r}, flush=True)\ntime.sleep({sleep_between})\n"
    body += "sys.exit(0)\n"
    open(p, "w").write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


def run(lines, claimed_range="74928", sleep_between=0.4):
    beats.clear()
    hz.HOSTBIN = fake_host(lines, sleep_between)
    work = tempfile.mkdtemp(prefix="hz_work_")
    rc, err, stalled, why = hz._prove_watched(
        ["seg-serve"], dict(os.environ), work, "mode-6 test",
        _beat_range=claimed_range, _beat_pk="pk", _beat_sk=FakeKey())
    return rc, stalled, why, list(beats)


# The exact lines the real seg-serve emits, taken from the validated run's coordinator log.
SEGMENTS = [
    "=== segment coordinator (push): RANGE [74928..74928] from bridge bundle, po2 21 ===",
    "     91/2352 segments  124s elapsed, ~3071s left",
    "     208/2352 segments  239s elapsed, ~2468s left",
    "     325/2352 segments  356s elapsed, ~2219s left",
]
JOINS = [
    "     joins 25/2352",
    "     joins 50/2352",
    "     joins 75/2352",
]

print("1. the SEGMENT-streaming phase beats the claim")
rc, stalled, why, b = run(SEGMENTS)
check(rc == 0 and not stalled, f"a healthy distributed prove is not killed (rc={rc} stalled={stalled} {why})")
check(len(b) >= 1, f"beats were sent during the segment phase (got {len(b)})")
check(all(x == "74928" for x in b), "and every beat names the claimed range")

print("2. the JOIN/assembly phase also beats (it is a quarter of the run)")
rc, stalled, why, b = run(SEGMENTS + JOINS)
check(rc == 0 and not stalled, f"assembly is not killed (rc={rc} stalled={stalled} {why})")
check(len(b) >= 2, f"beats continue through the join tree (got {len(b)})")

print("3. #256 REGRESSION GUARD: a silent host must send NO beats")
rc, stalled, why, b = run([
    "=== segment coordinator (push): RANGE [74928..74928] from bridge bundle, po2 21 ===",
    "  listening on 0.0.0.0:9110",
    "  worker connected from 127.0.0.1:44796",
], sleep_between=1.2)
check(len(b) == 0, f"a host that reports no progress beats nothing (got {len(b)})")

print("4. an unclaimed run beats nothing, whatever it prints (#251)")
rc, stalled, why, b = run(SEGMENTS + JOINS, claimed_range=None)
check(len(b) == 0, f"no claim means no beats (got {len(b)})")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK: the old pattern fails {len(fails)} assertion(s), as it must")
        sys.exit(0)
    print("CONTROL BROKEN: the old pattern passed everything — this suite proves nothing")
    sys.exit(1)

if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    sys.exit(1)
print("all assertions passed")
