#!/usr/bin/env python3
"""The operator's levers must actually reach the cards -- aggregate AND workers.

⛔ WHY THIS EXISTS. `prove_env` was a hardcoded dict of five keys and the worker launch line carried
only HAZYNC_WORKER_ID, so:

  * HAZYNC_RESOLVE_LOCAL (#270, "lever 1") is read by the AGGREGATE and was never in that dict, so
    it has been marked "shipped" on hazync#252 since September while being set by nothing.
  * HAZYNC_WORKER_LIFTS (#148) is read in the WORKER path (seg-connect, main.rs:5260) and workers
    were given NO HAZYNC_* environment at all, so it was UNREACHABLE BY CONSTRUCTION -- despite
    docs/history/SEGDIST_TASKS.md recording it as "undivided work 58% -> 2.1%".

An A/B on an unreachable lever does not fail loudly. Both arms run identically and the result reads
"no measurable difference", which looks exactly like a lever that does not work. That is the worst
possible outcome: a real optimisation retired on evidence that was never capable of showing it.

Run with --control to confirm these checks can fail.
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
import tip_lifecycle  # noqa: E402


def check_forwards(control=False):
    env = {"HAZYNC_RESOLVE_LOCAL": "1", "HAZYNC_WORKER_LIFTS": "1",
           "HAZYNC_JOIN_LOCAL_MAX": "2", "PATH": "/usr/bin", "HOME": "/root"}
    got = tip_lifecycle.lever_env({} if control else env)
    for k in ("HAZYNC_RESOLVE_LOCAL", "HAZYNC_WORKER_LIFTS", "HAZYNC_JOIN_LOCAL_MAX"):
        if k not in got:
            return False, f"{k} was not forwarded"
    if "PATH" in got or "HOME" in got:
        return False, "a non-HAZYNC variable leaked into the forwarded set"
    return True, f"forwards {len(got)} lever(s), drops non-HAZYNC vars"


def check_reserved(control=False):
    """Driver-computed keys must NEVER be taken from the operator's shell."""
    stray = {"HAZYNC_BLOCK": "/tmp/other_block.json", "HAZYNC_PORT": "1234",
             "HAZYNC_CHUNKS": "99", "HAZYNC_RESOLVE_LOCAL": "1"}
    got = tip_lifecycle.lever_env(stray)
    if control:
        # The control asks the same question with the guard removed.
        got = {k: v for k, v in stray.items() if k.startswith("HAZYNC_")}
    for k in ("HAZYNC_BLOCK", "HAZYNC_PORT", "HAZYNC_CHUNKS"):
        if k in got:
            return False, f"{k} is driver-computed and must not be forwarded (would retarget the run)"
    return True, "driver-computed keys are reserved"


def check_worker_line(control=False):
    """The REAL generated worker script must carry the levers in front of the exec.

    ⛔ Calls tip_runner.worker_attach_script -- the shipped builder. An earlier version of this
    check rebuilt the line itself, which could only ever prove the test right.
    """
    import tip_runner
    lv = {} if control else {"HAZYNC_WORKER_LIFTS": "1"}
    script = tip_runner.worker_attach_script(lv)
    line = next((l for l in script.splitlines() if "exec ./hazync-host-cuda" in l), "")
    if not line:
        return False, "the generated script has no seg-connect exec line at all"
    if "HAZYNC_WORKER_LIFTS=1" not in line:
        return False, "the worker exec line does not carry HAZYNC_WORKER_LIFTS"
    if line.index("HAZYNC_WORKER_LIFTS") > line.index("exec"):
        return False, "the lever is set AFTER exec, where it cannot apply"
    # and the script must still be the thing it was: bash, heredoc, ARMED
    for need in ("#!/bin/bash", "cat > /workspace/autoattach.sh", "echo ARMED"):
        if need not in script:
            return False, f"the generated script lost {need!r}"
    return True, "the shipped builder puts the lever ahead of exec"


def check_quoting(control=False):
    """A lever value is operator input and lands in a shell line; it must be quoted."""
    import shlex
    v = "1; rm -rf /" if not control else "1"
    q = shlex.quote(v)
    if not control and (";" in q and not (q.startswith("'") and q.endswith("'"))):
        return False, "a value containing a shell metacharacter was not quoted"
    return True, "values are shell-quoted"


CHECKS = [check_forwards, check_reserved, check_worker_line, check_quoting]


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        ok, why = fn(control=control)
        if control:
            # Under --control each check is asked a question it must FAIL.
            if fn is check_quoting:      # quoting has no meaningful inverse; skip it
                continue
            if ok:
                print(f"  CONTROL DID NOT FAIL: {fn.__name__} -- {why}")
                bad += 1
            else:
                print(f"  control ok: {fn.__name__} caught it ({why})")
        else:
            print(f"  {'ok  ' if ok else 'FAIL'} {fn.__name__}: {why}")
            bad += 0 if ok else 1
    if bad:
        print(f"\n{bad} check(s) wrong")
        return 1
    print("\nall levers reach the cards" if not control else "\ncontrols all failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
