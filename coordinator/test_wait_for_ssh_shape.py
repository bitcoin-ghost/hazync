#!/usr/bin/env python3
"""wait_for_ssh returns a DICT keyed by pod name, and every caller must read it as one.

⛔ WHAT THIS EXISTS FOR. `--grow-to`'s recruiter gate did `c = cs[0]`. That is a list access against a
dict, so it raised `KeyError: 0` on the very first recruit the feature ever gated on real hardware
(2026-09-28, 2x H100 NVL). Every recruit was therefore rented, failed instantly and released:

    09:07:18   recruiter: rented hz-grow-1 — gating it off the clock
    09:07:31   recruiter: releasing hz-grow-1 — KeyError: 0
    09:08:18   recruiter: rented hz-grow-2 — gating it off the clock
    09:08:52   recruiter: releasing hz-grow-2 — KeyError: 0

The feature could never have added a card, and it would have looked like bad luck with capacity
rather than a defect — the fleet simply never grew. It cost $0.47 to find, only because the run was
watched.

⚠ The main path has always read it correctly (`order = [cards[n] for n in sorted(cards)]`). This was
the one caller that did not, written months later by someone (me) who assumed a list without
checking — the same class as reading `--min-cards` into a parameter that means a target.

    python3 test_wait_for_ssh_shape.py             # the contract, and every caller against it
    python3 test_wait_for_ssh_shape.py --control   # `cs[0]` restored — must raise KeyError: 0
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


# ── 1. the contract: it builds and returns a dict keyed by name ──────────────────────────────────
body = SRC[SRC.index("def wait_for_ssh("):]
body = body[:body.index("\ndef ", 1)]
check("ready[p[\"name\"]] = card" in body, "wait_for_ssh keys its result by pod NAME")
check(re.search(r"return\s+ready\s*,\s*portmap", body) is not None,
      "wait_for_ssh returns (ready, portmap)")

# ── 2. every caller reads it as a dict ───────────────────────────────────────────────────────────
# ⛔ Anchored to the CALL and the line that consumes it. A bare grep for "cs[0]" would pass the day
# someone renames the variable, which is exactly how this shipped.
calls = re.findall(r"^\s*(\w+), (\w+) = wait_for_ssh\([^\n]*\)\n(.*?)(?=\n\s*\n)", SRC, re.S | re.M)
check(len(calls) >= 2, f"found {len(calls)} call sites to check (expected at least 2)")
for var, _pm, after in calls:
    listish = re.search(re.escape(var) + r"\[\s*\d+\s*\]", after)
    if CONTROL:
        continue
    check(listish is None,
          f"`{var}` is not indexed by integer after the call"
          + (f" — found `{listish.group(0)}`" if listish else ""))

# ── 3. the failure, reproduced against the real shape ────────────────────────────────────────────
ready = {"hz-grow-1": object()}          # what wait_for_ssh actually hands back

if CONTROL:
    try:
        _ = ready[0]                      # the bug, verbatim
        check(False, "control did NOT raise — it is testing nothing")
    except KeyError as exc:
        check(str(exc) == "0", f"control reproduces it: KeyError: {exc} on a dict keyed by name")
else:
    try:
        c = next(iter(ready.values()))
        check(c is not None, "the fix reads the first value out of the dict without assuming a key")
    except Exception as exc:              # noqa: BLE001
        check(False, f"the fix raised {type(exc).__name__}: {exc}")
    try:
        ready[0]
        check(False, "a dict keyed by name still refuses integer access")
    except KeyError:
        check(True, "a dict keyed by name still refuses integer access")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")
