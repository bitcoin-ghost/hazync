#!/usr/bin/env python3
"""The worker attach loop is ENDED, and the thing that ends it is actually called (hazync#463).

⛔ WHY THIS EXISTS. `FleetRunner.stop_auto_attach` documented itself as "Called when the run is done
with them" and had **no caller anywhere in the repo**. The loop it ends polls `/dev/tcp` once a
second for up to 86,400 iterations; the driver terminated the pods seconds later, so nobody ever saw
it — until `--keep`, or a card released without being terminated.

This is the `bind_verdict` shape (hazync#252/#445): a docstring describing behaviour that never
happens. So three separate things are pinned here, because each can be true while the others are not:

  1. the SHIPPED script really does stop on the file  — run it, for real, and time it
  2. `stop_auto_attach` really does create that file  — on workers, never on the aggregate
  3. the teardown really does call it, before release — read out of tip_smoke's own AST

  python3 test_attach_lifecycle.py            # must PASS
  python3 test_attach_lifecycle.py --control  # the guard and the call removed; MUST FAIL
"""
import ast
import os
import subprocess
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_driver as td      # noqa: E402
import tip_runner as trn     # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── 1. the shipped loop really exits on the file ─────────────────────────────────────────────────
# ⚠ THE SCRIPT UNDER TEST IS THE ONE THAT SHIPS. It is extracted from `worker_attach_script`'s
# heredoc rather than retyped here; the ONLY edit is /workspace -> a temp dir, because the test has
# no /workspace and a test that writes there would be testing this machine, not the script.
def shipped_loop_body(work, drop_guard=False):
    s = trn.worker_attach_script({})
    body = s.split("<<'EOS'\n", 1)[1].split("\nEOS\n", 1)[0]
    if drop_guard:
        body = body.replace(f"  [ -f {trn.ATTACH_STOP} ] && exit 0\n", "")
    return body.replace("/workspace", work)


def run_loop_and_stop(drop_guard=False, patience=12.0):
    """Start the real loop against an unreachable aggregate, then ask it to stop. Returns seconds,
    or None if it was still running after `patience`."""
    work = tempfile.mkdtemp(prefix="attach_")
    path = os.path.join(work, "autoattach.sh")
    with open(path, "w") as fh:
        fh.write(shipped_loop_body(work, drop_guard=drop_guard))
    os.chmod(path, 0o755)
    # 127.0.0.1:1 refuses instantly, so an iteration costs its `sleep 1` and nothing else.
    p = subprocess.Popen(["bash", path, "127.0.0.1:1", "w0"], cwd=work,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)                    # past the loop's own `rm -f`, which would delete an early touch
        open(os.path.join(work, "attach.stop"), "w").close()
        t0 = time.time()
        while time.time() - t0 < patience:
            if p.poll() is not None:
                return time.time() - t0, p.returncode
            time.sleep(0.2)
        return None, None
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


took, rc = run_loop_and_stop(drop_guard=CONTROL)
check(took is not None and rc == 0,
      f"the shipped attach loop exits 0 when attach.stop appears (took={took}, rc={rc})")
check(took is not None and took < 6.0,
      f"and exits PROMPTLY — a stop noticed an hour later is not a stop (took={took})")

# 2. It must not exit on its own. Otherwise check 1 proves nothing: a loop that dies anyway would
#    pass it. This is the positive control that lives in the suite rather than behind --control.
work = tempfile.mkdtemp(prefix="attach_")
path = os.path.join(work, "autoattach.sh")
open(path, "w").write(shipped_loop_body(work))
p = subprocess.Popen(["bash", path, "127.0.0.1:1", "w0"], cwd=work,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(4.0)
still_running = p.poll() is None
p.kill(); p.wait(timeout=5)
check(still_running, "and does NOT exit while no stop file exists — so check 1 is measuring the file")


# ── 3. stop_auto_attach writes that exact file, on the workers only ──────────────────────────────
class FakeSSH:
    def __init__(self, raise_on=()):
        self.cmds, self.raise_on = [], set(raise_on)

    def run(self, card, body, env=None, timeout=None):
        if card.cid in self.raise_on:
            raise OSError("connection refused (a pod on its way out)")
        self.cmds.append((card.cid, body))
        return "OK\n"


A = td.Card("a", "10.0.0.1", 22)
B = td.Card("b", "10.0.0.2", 22)
AGG = td.Card("agg", "10.0.0.9", 22)


def runner(ssh):
    return trn.FleetRunner(ssh, AGG, stage_dir=tempfile.mkdtemp(prefix="stage_"))


ssh = FakeSSH()
runner(ssh).stop_auto_attach({0: A, 1: B, 2: AGG})
touched = {cid for cid, b in ssh.cmds if trn.ATTACH_STOP in b}
check(touched == {"a", "b"},
      f"both workers are told to stop and the aggregate is not (touched={sorted(touched)})")
check(all(trn.ATTACH_STOP in b for _, b in ssh.cmds),
      "the path touched is the SAME constant the loop watches — not a near-miss nobody reads")

# ⛔ A DYING POD REFUSES CONNECTIONS, AND IT IS ALWAYS THE FIRST ONE SOMETIMES. If one raise ended the
# loop, the cards skipped would be precisely the ones left polling.
ssh = FakeSSH(raise_on={"a"})
runner(ssh).stop_auto_attach({0: A, 1: B})
check({cid for cid, _ in ssh.cmds} == {"b"},
      "a card that cannot be reached does not stop the cards after it")


# ── 4. the teardown calls it, in the finally, BEFORE the pods are released ───────────────────────
# ⛔ READ OUT OF tip_smoke's OWN AST. Grepping for the name would pass on a call sitting in dead code
# or after termination, which is the same no-op wearing a different costume.
src = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
if CONTROL:
    # the mutation is the ORIGINAL BUG: the function exists, documents a caller, and has none
    src = src.replace("runner.stop_auto_attach(assignment)", "pass  # CONTROL: caller removed")

tree = ast.parse(src)


def names_called(node):
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


in_finally = False
before_release = False
for t in ast.walk(tree):
    if not isinstance(t, ast.Try) or not t.finalbody:
        continue
    stops = [i for i, st in enumerate(t.finalbody) if "stop_auto_attach" in names_called(st)]
    rels = [i for i, st in enumerate(t.finalbody) if "terminate_confirmed" in names_called(st)]
    if stops:
        in_finally = True
        if rels and min(stops) < min(rels):
            before_release = True

check(in_finally, "the smoke driver's teardown `finally` calls stop_auto_attach")
check(before_release,
      "and calls it BEFORE terminate_confirmed — after release it would be touching a dead pod")


EXPECTED_CONTROL_FAILURES = {
    "the smoke driver's teardown `finally` calls stop_auto_attach",
    "and calls it BEFORE terminate_confirmed — after release it would be touching a dead pod",
}

print()
if CONTROL:
    got = {f for f in fails}
    # The timed checks carry live numbers in their text, so match them by prefix.
    loop_failed = any(f.startswith("the shipped attach loop exits 0") for f in got)
    got = {f for f in got if not f.startswith(("the shipped attach loop exits 0", "and exits PROMPTLY"))}
    if loop_failed and got == EXPECTED_CONTROL_FAILURES:
        print("CONTROL OK — with the guard line and the caller removed, the loop never stopped and "
              "both teardown assertions failed:")
        for f in sorted(EXPECTED_CONTROL_FAILURES):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — the mutations did not produce the expected failures.")
    print(f"  loop-stop assertion failed: {loop_failed} (expected True)")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES):
        print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("the attach loop is ended, by a caller that exists, before the cards go away")
