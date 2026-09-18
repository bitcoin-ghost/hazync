#!/usr/bin/env python3
"""`hazync run --distributed` proves ONE board block across many cards (mode 6, #367).

WHY THIS EXISTS. The distributed path cannot reuse `_prove_watched`/`_run_with_seg_retry`, and every
reason is a way to lose a block:

  * `_run_with_seg_retry` takes `gpu_lock`, whose contract is "one GPU job at a time, per box". In
    mode 6 this process does NOT prove — it serves, while N local workers each hold one card. Holding
    that lock would let the server block its own workers.
  * `_prove_watched` arms its stall watchdog only for `argv[0] in ("prove-range","prove-range-bridge")`,
    so `seg-serve` would run with NO progress window at all and a wedged fleet would sit until
    HAZYNC_PROVE_TIMEOUT (6 h by default).
  * the run owns a server AND N worker processes. `main()` installs no signal handlers, on the stated
    grounds that free-running proving "has nothing to hand back" — true of a single-card prove, false
    here, where leaving children behind wedges the box for the next run.

So the beat, the watchdog and the teardown are hand-rolled, and this suite is what says they work.

⚠ THE POINT IS THE BEHAVIOUR, NOT THE STRINGS. These assertions drive the real `_prove_distributed`
with a fake host binary emitting real seg-serve output, and check what it DID: beats POSTed, workers
started on the right cards, no orphans left, a silent fleet killed. A test that only matched log lines
would pass with the beat wired to nothing.

⚠ AND THE BEAT MUST STAY CLAIM-GATED. Scenario 2 is the #251 guard: an explicit `run <id> --distributed`
holds no claim, so it must beat NOTHING. Beating an unclaimed range is 400+ doomed 409s, which is how a
healthy run once looked like a worker hammering the coordinator.

WHAT THIS DOES NOT COVER: no real proving, no GPU, no network, no receipt bytes. The fake host writes
the lines the real one writes; what is checked is this module's reaction to them.

Usage:
  python3 test_distributed_run.py            # assertions; exit 0 on success
  python3 test_distributed_run.py --control  # teardown neutered; MUST fail
"""
import importlib.machinery
import importlib.util
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_dist_")
# Short windows so the suite runs in seconds; the real defaults are 600 s / 6 h / 60 s.
#
# ⛔ HAZYNC_TICK MATTERS AS MUCH AS THE OTHERS. The beat ticker waits a whole tick before its first
# check, so at the 60 s default a fixture that finishes in two seconds records ZERO beats and the beat
# assertion fails for a reason that has nothing to do with the beat being wired. test_mode6_progress
# sets it for the same reason; leaving it out cost one wrong diagnosis here.
os.environ.update(HAZYNC_STALL_MIN="3", HAZYNC_PROVE_TIMEOUT="60", HAZYNC_TICK="0.3")
CONTROL = "--control" in sys.argv

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)

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


hz.identity = lambda: (FakeKey(), "pk", "tester")

# The exact lines the real seg-serve emits, from the runs on 2026-09-18.
SERVE_LINES = [
    "=== segment coordinator (push): RANGE [230000..230000] from bridge bundle, po2 21 ===",
    "  listening on 127.0.0.1:9110",
    "    32/71 segments  60s elapsed, ~73s left",
    "    joins 25/71",
    "    joins 71/71",
    ">>> PUSH-TRANSPORT RECEIPT VERIFIED against METHOD_ID",
]


def fake_host(lines, sleep_between=0.25, hang=False, touch=None):
    """A host binary that prints `lines`, optionally writes `touch`, then exits 0 (or hangs).

    `seg-connect` invocations record the card they were given, so the test can prove each worker was
    pinned to a distinct device rather than all landing on card 0.
    """
    d = tempfile.mkdtemp(prefix="hz_fakehost_")
    p = os.path.join(d, "host")
    body = "#!/usr/bin/env python3\nimport sys, os, time\n"
    body += "if 'seg-connect' in sys.argv:\n"
    body += f"    open(os.path.join({d!r}, 'worker_' + os.environ.get('HAZYNC_WORKER_ID','?')), 'w')" \
            ".write(os.environ.get('CUDA_VISIBLE_DEVICES','none'))\n"
    body += "    time.sleep(300)\n    sys.exit(0)\n"
    for ln in lines:
        body += f"print({ln!r}, flush=True)\ntime.sleep({sleep_between})\n"
    if touch:
        body += f"open({touch!r}, 'w').write('receipt')\n"
    if hang:
        body += "time.sleep(300)\n"
    body += "sys.exit(0)\n"
    open(p, "w").write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p, d


def live_children():
    """PIDs of our surviving fake hosts. Read from ps, NOT from a count we kept ourselves.

    ⛔ A self-reported count would pass even if teardown never ran. And the pattern must not appear in
    this process's own command line, or ps matches the test itself — the trap that made a clean box
    report two live seg-serves on 2026-09-18.
    """
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    needle = "hz_fakehost_"
    return [l.split()[0] for l in out.splitlines()
            if needle in l and "ps -eo" not in l and "test_distributed" not in l]


print("1. a claimed distributed run beats its claim while segments land")
before = live_children()
hostbin, hostdir = fake_host(SERVE_LINES, touch=None)
hz.HOSTBIN = hostbin
work = tempfile.mkdtemp(prefix="hz_work_")
beats.clear()
if CONTROL:
    # The control neuters teardown: the run still "succeeds", so only a test that checks for ORPHANS
    # notices. That is the assertion this suite exists for.
    hz.os.killpg = lambda *a, **k: None
try:
    hz._prove_distributed("230000", work, dict(os.environ), True, 9110, 2)
    ran = True
except SystemExit as e:
    ran, why = False, str(e)
    print(f"       (exited: {why[:100]})")
check(ran, "a healthy distributed run completes")
check(len(beats) > 0, f"beats were POSTed while proving ({len(beats)} sent)")
check(all(b == "230000" for b in beats), "every beat names the claimed range")

print("2. an UNCLAIMED run beats nothing (#251: 400+ doomed 409s otherwise)")
beats.clear()
hostbin2, hostdir2 = fake_host(SERVE_LINES)
hz.HOSTBIN = hostbin2
work2 = tempfile.mkdtemp(prefix="hz_work_")
try:
    hz._prove_distributed("230000", work2, dict(os.environ), False, 9111, 0)
except SystemExit:
    pass
check(beats == [], f"no claim, no beats (sent {len(beats)})")

print("3. workers are pinned one per card, and --workers=0 starts none")
check(sorted(open(os.path.join(hostdir, f)).read()
             for f in os.listdir(hostdir) if f.startswith("worker_")) == ["0", "1"],
      "two workers were pinned to CUDA_VISIBLE_DEVICES 0 and 1")
check(not [f for f in os.listdir(hostdir2) if f.startswith("worker_")],
      "--workers=0 started no local workers (cards join from outside)")

print("4. a silent fleet is killed, not waited on for HAZYNC_PROVE_TIMEOUT")
hostbin3, _ = fake_host(["    32/71 segments  60s elapsed, ~73s left"], sleep_between=0.1, hang=True)
hz.HOSTBIN = hostbin3
work3 = tempfile.mkdtemp(prefix="hz_work_")
t0 = time.time()
try:
    hz._prove_distributed("230000", work3, dict(os.environ), False, 9112, 0)
    stalled = False
except SystemExit as e:
    stalled = "no progress" in str(e)
el = time.time() - t0
check(stalled, "a run that goes quiet after progress is killed by the stall window")
check(el < 45, f"...and killed on HAZYNC_STALL_MIN (3 s), not the 60 s outer bound ({el:.0f}s)")

print("5. ⛔ no orphans: the server and every worker are gone afterwards")
time.sleep(1)
left = [p for p in live_children() if p not in before]
check(not left, f"no fake-host processes survived teardown (left: {len(left)})")

print("6. MODE=distributed passes the flag (continuous mode\'s whole contract)")
# ⛔ THE FLAG CANNOT RIDE IN $job. run-workers.sh interpolates it as ONE shell word, so
# job="run --distributed" reaches the CLI as a single argument and main()'s dispatch dict looks up
# "run --distributed" and reports an unknown command. It needs its own variable, appended UNQUOTED.
# Without this assertion MODE=distributed is ACCEPTED and silently behaves as plain prove mode --
# worse than rejecting it, because the operator sees workers running and no distribution happening.
import subprocess as _sp
_probe = r'''
job="run"; job_args="--distributed"
bash -c 'set -- '"$job"' '"$job_args"'; printf "%s|" "$#"; for a in "$@"; do printf "[%s]" "$a"; done'
'''
_out = _sp.run(["bash", "-c", _probe], capture_output=True, text=True).stdout.strip()
check(_out == "2|[run][--distributed]", f"MODE=distributed yields argv: run --distributed (got {_out!r})")
_probe0 = _probe.replace('job_args="--distributed"', 'job_args=""')
_out0 = _sp.run(["bash", "-c", _probe0], capture_output=True, text=True).stdout.strip()
check(_out0 == "1|[run]", f"...and an empty job_args leaves other modes untouched (got {_out0!r})")

print("7. mode 6 is ONE block")
try:
    hz._prove_distributed("100-200", tempfile.mkdtemp(), dict(os.environ), False, 9113, 0)
    one_block = False
except SystemExit as e:
    one_block = "ONE board block" in str(e)
check(one_block, "a multi-block range is refused before anything is started")

print(f"\n{'CONTROL: ' if CONTROL else ''}{len(fails)} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
