#!/usr/bin/env python3
"""Tests for tip_driver.py — the tip rig's SSH layer (Phase 5 ⑧).

WHY THIS EXISTS. The single most destructive bug in the four 966,256 runs was not in the proving: it was
a remote probe written in double quotes, so its command substitutions expanded on the ORCHESTRATOR
before ssh ran. The remote command became a constant, no card's log size ever appeared to change, and
every healthy card was declared stalled at the same instant. It killed a chunk five minutes into run 3.

The fix is structural — build argv lists and never invoke a local shell — so the test is structural too:
the probe body must arrive at the remote end **byte for byte**, with its `$(...)` intact.

No network and no fleet: `subprocess.run` is intercepted and the argv inspected.

  python3 test_tip_driver.py            # assertions; exit 0 on success
  python3 test_tip_driver.py --control  # the body is pre-expanded locally; MUST fail
"""
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_driver as td  # noqa: E402
import tip_fleet as tf   # noqa: E402

fails = []
calls = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def fake_run(cmd, **kw):
    calls.append(cmd)
    if CONTROL:
        # THE BUG, REINTRODUCED: pretend the orchestrator's shell got at the body first. Every
        # substitution collapses to nothing, which is exactly what the remote saw in run 3.
        cmd = [c.replace("$(stat -c%s \"$RDIR/prove.log\" 2>/dev/null || echo 0)", "")
                .replace("$(pgrep -cf hazync-host-cuda)", "") for c in cmd]
        calls[-1] = cmd
    return FakeCompleted(stdout="DONE\n")


subprocess.run = fake_run

card = td.Card("c1", "10.0.0.1", 2222, loc="US")
runner = td.SSHRunner(key="/dev/null")

# ── 1. the probe body survives to the remote end, unexpanded ───────────────────────────────────────
calls.clear()
runner.run(card, tf.probe_body(), {"RDIR": "/workspace", "CHUNK": "4"})
argv = calls[-1]
joined = " ".join(argv)

check(not any(c is None for c in argv), "argv is a list of strings — no shell is involved locally")
check("$(stat" in joined, "the probe reaches ssh with `$(stat …)` INTACT")
check("$(pgrep" in joined, "the probe reaches ssh with `$(pgrep …)` INTACT")
check("RDIR=/workspace" in joined and "CHUNK=4" in joined,
      "RDIR and CHUNK are passed as assignments, not interpolated into the body")

# ⛔ The body must appear exactly once and unmodified — the whole point of the argv form.
check(tf.probe_body() in joined.replace("'\\''", "'"),
      "the body arrives byte for byte")

# ── 2. shell=True is never used ────────────────────────────────────────────────────────────────────
# A shell=True call would make every guard above meaningless, so it is asserted directly -- against
# the PARSED code, not the text. The module's own docstring explains why shell=True is never used, and
# a grep would match that sentence and pass while the code did the opposite.
import ast as _ast  # noqa: E402
_tree = _ast.parse(open(os.path.join(HERE, "tip_driver.py")).read())
_shell_true = [n for n in _ast.walk(_tree) if isinstance(n, _ast.Call)
               for kw in n.keywords
               if kw.arg == "shell" and getattr(kw.value, "value", False) is True]
check(not _shell_true, "no call in tip_driver passes shell=True (checked by AST, not grep)")

# ── 3. ssh hygiene: no stdin, batch mode, a timeout, and host keys checked ─────────────────────────
check("-n" in argv, "ssh is given -n: a password prompt in a parallel fan-out would hang the fleet")
check("BatchMode=yes" in joined, "ssh runs in batch mode")
check("StrictHostKeyChecking=yes" in joined,
      "host keys are checked — a rented fleet with an unknown key is a reason to stop")

# ── 4. a failed call is UNREACHABLE, never a stall ─────────────────────────────────────────────────
def boom(cmd, **kw):
    calls.append(cmd)
    raise subprocess.TimeoutExpired(cmd, 1)


subprocess.run = boom
check(runner.run(card, "echo hi") is None, "a timed-out call returns None")
check(tf.card_state(runner.run(card, "echo hi")) == tf.UNREACHABLE,
      "and None reads as UNREACHABLE, which never triggers a recovery")
subprocess.run = fake_run

# ── 5. a non-zero exit is not a reply ──────────────────────────────────────────────────────────────
subprocess.run = lambda cmd, **kw: FakeCompleted(stdout="rubbish", returncode=255)
check(runner.run(card, "echo hi") is None, "a non-zero ssh exit yields None, not its stdout")
subprocess.run = fake_run

# ── 6. fetch: scp exiting 0 is not evidence the file arrived ───────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="tipdrv_")
missing = os.path.join(tmp, "absent.bin")
check(not runner.fetch(card, "/workspace/chunk_0.bin", missing),
      "a copy that produced no local file is a FAILURE even though scp exited 0")
empty = os.path.join(tmp, "empty.bin")
open(empty, "w").close()
check(not runner.fetch(card, "/workspace/chunk_0.bin", empty),
      "a zero-byte local file is a FAILURE — this is the silent scp drop")
good = os.path.join(tmp, "good.bin")
open(good, "w").write("x" * 10)
check(runner.fetch(card, "/workspace/chunk_0.bin", good), "a non-empty local file is a success")

# ── 7. kills go via a script, so the pattern is never in the argv being matched ────────────────────
calls.clear()
runner.kill_provers(card)
killargv = " ".join(calls[-1])
check("hzkill.sh" in killargv, "the kill is written to a script and run by path")
check("pkill" not in killargv,
      "`pkill` never appears in the command line — it would match the ssh session carrying it")

# ── 8. probe_all resolves the reassigned directory LOCALLY ─────────────────────────────────────────
calls.clear()
cards = {0: td.Card("a", "10.0.0.1", 22), 1: td.Card("b", "10.0.0.2", 22)}
td.probe_all(runner, cards, reassigned={1})
seen = " ".join(" ".join(c) for c in calls)
check("RDIR=/workspace/re1" in seen, "a reassigned chunk is probed in its OWN directory")
check("RDIR=/workspace CHUNK=0" in seen or "CHUNK=0 RDIR=/workspace" in seen,
      "an unmoved chunk is probed in the base directory")
check("_reassigned" not in seen,
      "the orchestrator's bookkeeping path never appears in a remote command")

EXPECTED_CONTROL_FAILURES = {
    "the probe reaches ssh with `$(stat …)` INTACT",
    "the probe reaches ssh with `$(pgrep …)` INTACT",
    "the body arrives byte for byte",
}

print()
if CONTROL:
    got = set(fails)
    if EXPECTED_CONTROL_FAILURES <= got:
        print(f"CONTROL OK — the body was expanded locally and the {len(EXPECTED_CONTROL_FAILURES)} "
              "assertion(s) that detect it failed, as they must:")
        for f in sorted(EXPECTED_CONTROL_FAILURES & got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — local expansion went undetected.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
