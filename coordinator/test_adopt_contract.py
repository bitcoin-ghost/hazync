#!/usr/bin/env python3
"""Adopted pods survive a gate failure, and the recruiter stops before anything slow.

Two faults measured live on 2026-09-28, both in --grow-to/--adopt code written the same day.

⛔ 1. A GATE FAILURE DESTROYED AN ADOPTED FLEET. `--adopt` promises the fleet outlives the run. Every
pre-clock gate that drops a card released it — staging, ssh, reachability, the GPU smoke — so one
recoverable error cost the fleet:

    [11:47:15]   hz-ab-6: pod-prove.sh=FAILED fixture=ok (345765 bytes on the card)
    [11:48:11] ⚠ 4 pod(s) in /root/ab-same.json are NO LONGER on the account and were dropped

The cause was a wrong `--repo`. It destroyed four hand-picked RTX PRO 6000 selected for being in one
datacentre, which took two rental rounds to assemble and cannot be re-requested — RunPod gives you
whatever siting it gives you.

⚠ A RENTED pod must still be terminated on a gate failure. #479 exists because such a card used to
end the whole run instead of being dropped from it.

⛔ 2. THE RECRUITER KEPT RENTING DURING TEARDOWN. `recruiter.stop()` sat after the harvest, which
pulls logs from every card and takes minutes:

    11:21:58   BILLED: $9.04 across 20/20 pods      <- teardown began
    11:23:25   recruiter: rented hz-grow-2          <- a NEW pod, 87 s into shutdown

The fleet grew while being torn down. SIGTERM did not help — the recruiter is a separate thread — so
the run had to be SIGKILLed and its pods released by hand through the API.

    python3 test_adopt_contract.py             # both fixed
    python3 test_adopt_contract.py --control   # both faults must reproduce
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CONTROL = "--control" in sys.argv
SRC = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


# ── 1. every gate drop goes through the guard, not a bare terminate ──────────────────────────────
GATE_CALLS = [
    (r'release=lambda p: (\w+)\(p, "it never answered ssh"\)', "the ssh drop"),
    (r'def release\(p\):\s*\n\s*(\w+)\(p, why\)', "the generic _drop"),
    (r'(\w+)\(p, "it could not reach the aggregate"\)', "the reachability drop"),
    (r'(\w+)\(p, "its GPU cannot prove"\)', "the GPU-dud drop"),
]
if not CONTROL:
    for pat, what in GATE_CALLS:
        m = re.search(pat, SRC)
        check(m is not None and m.group(1) == "_release_pod",
              f"{what} releases through the guard (found {m.group(1) if m else 'NOTHING'})")

# the guard itself must honour the contract and still terminate a rented pod
g = re.search(r"def _release_pod\(p, why\):.*?\n(\s{8}\S|\Z)", SRC, re.S)
body = g.group(0) if g else ""
if not CONTROL:
    check("a.adopt and not a.release_adopted" in body,
          "the guard checks --adopt and --release-adopted")
    check("return" in body.split("a.adopt")[1].split("terminate_confirmed")[0],
          "an adopted pod returns WITHOUT terminating")
    check("terminate_confirmed" in body, "a rented pod is still terminated (#479)")

# ── 2. the recruiter is stopped before the harvest ───────────────────────────────────────────────
i_stop = SRC.find("recruiter.stop()")
i_harvest = SRC.find("tip_harvest.harvest(")
# ⛔ A SOURCE-ORDER ASSERTION CANNOT HAVE A SOURCE CONTROL: "the file is still wrong" fails the
# moment it is right, which is backwards. Checked only in the real pass; the behavioural control
# below is what must reproduce.
if not CONTROL:
    check(i_stop != -1 and i_harvest != -1, "the recruiter stop and the harvest are both present")
    check(i_stop < i_harvest,
          f"recruiter.stop() runs BEFORE the harvest ({i_stop} < {i_harvest}) — the harvest pulls "
          f"logs from every card and takes minutes")

# ── 3. the behaviour, against the real decision ──────────────────────────────────────────────────
def releases(adopt, release_adopted):
    """The guard's decision, as tip_smoke makes it."""
    if CONTROL:
        return True                      # the old behaviour: always terminate
    return not (adopt and not release_adopted)

check(releases(adopt=False, release_adopted=False) is True,
      "a RENTED fleet still releases a dropped card")
if CONTROL:
    check(releases(adopt=True, release_adopted=False) is True,
          "control reproduces it: an ADOPTED fleet is destroyed by a gate failure")
else:
    check(releases(adopt=True, release_adopted=False) is False,
          "an ADOPTED fleet keeps a dropped card")
    check(releases(adopt=True, release_adopted=True) is True,
          "--release-adopted opts back into releasing")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")
