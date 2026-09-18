#!/usr/bin/env python3
"""What the sponsor bot says about held blocks it has no bundle for (hazync#347).

WHY THIS EXISTS. The bot used to log "N held block(s) have no bundle yet and wait for the bridge". Below
h=418,268 that is true and the wait ends. Inside 418,269-967,499 it is FALSE and the wait never ends: the
live bridge runs HAZYNC_BRIDGE_EMIT_FROM=967500, so it walks those heights writing nothing and never
returns for them. A sponsor could hold a block there for ever while the log says to be patient.

So the line now says whether the block can be regenerated from an archived checkpoint, and what that
costs. These tests pin what it says, because the message IS the feature — nothing else reports it.

⛔ THE GUARD UNDER TEST is the refusal when no checkpoint sits below the target. It is not merely policy:
without a rung there is no seed, `plan["rung"]` is None, and the rest of the function cannot even compute
a span. The control shows exactly that — with the refusal disabled the function raises rather than
advising, which is why `advice()` below turns a raise into a failed assertion instead of a crash.

⛔ PINNED LITERALS, not values derived from the thing under test — deriving them moves the scenarios with
the control and the control then passes for an unrelated reason (test_claim_grace.py records the same).

Pure: no coordinator, no bridge, no filesystem. `plan` dicts are written out as regen_bundles.plan()
returns them, so the "strictly below" rule stays in one place.

Usage:
  python3 test_regen_advice.py            # assertions; exit 0 on success
  python3 test_regen_advice.py --control  # the no-checkpoint-below refusal is disabled; MUST fail
"""

import os
import sys

CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sponsor_bot  # noqa: E402

if CONTROL:
    # The file's own convention for this (see _CONTROL_IGNORE_BUNDLES beside has_bundle).
    sponsor_bot._CONTROL_IGNORE_MISSING_RUNG = True

fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)

def advice(plan, waiting):
    """A raise is a FAILED assertion, not a crashed test — see the note above."""
    try:
        return sponsor_bot.regen_advice(plan, waiting)
    except Exception as e:
        return f"RAISED {type(e).__name__}"

# ── the refusal ──────────────────────────────────────────────────────────────────────────────────────

# 1. ⛔ THE CONTROL CASE. Nothing below the target, so there is no seed and a replay would start at
#    genesis — the silent six-day walk that looks like a working job.
m = advice({"rung": None, "lo": 1000, "hi": 1000, "missing": [1000]}, 1)
check("CANNOT be regenerated" in m and "1000" in m and "GENESIS" in m,
      "with no checkpoint below it, the block is refused and genesis is named as the reason")

# 2. The lowest unseedable height is the one reported, not an arbitrary one.
m = advice({"rung": None, "lo": 5000, "hi": 90000, "missing": [90000, 5000]}, 2)
check("below height 5000" in m, "the LOWEST height with no checkpoint below it is the one named")

# ── the expensive case: today's gap ───────────────────────────────────────────────────────────────────

# 3. The real situation as of 2026-09-18: the only rung below the gap is 230,000, so a sponsorship at
#    600,001 seeds from there. 370,001 blocks at 0.294 s/block is ~30.2 h for two bundles.
m = advice({"rung": 230000, "lo": 600000, "hi": 600001, "missing": []}, 2)
check("370,001 blocks" in m and "230,000" in m, "the real gap case reports the seed and the full walk")
check("30.2 h" in m, "the walk is costed at the measured rate, not left as a block count")
check("NOT started" in m, "an expensive replay is reported, never quietly started")
check("hazync#379" in m, "it points at the work that would make this cheap")

# 4. Just over the threshold is still expensive.
m = advice({"rung": 100000, "lo": 150001, "hi": 150001, "missing": []}, 1)
check("NOT started" in m, "a walk one block over the threshold is still reported, not suggested")

# ── the cheap case: what it looks like once #379 has run ─────────────────────────────────────────────

# 5. Exactly at the threshold is NOT expensive — the comparison is >, so equality gives the command.
m = advice({"rung": 100000, "lo": 150000, "hi": 150000, "missing": []}, 1)
check("regen_bundles.py" in m and "NOT started" not in m,
      "exactly at the threshold, the runnable command is given instead")

# 6. A short walk — the state after #379 puts rungs inside the gap — is actionable.
m = advice({"rung": 725000, "lo": 730000, "hi": 730000, "missing": []}, 1)
check("5,000-block replay" in m and "--apply" in m,
      "a near checkpoint yields a short replay and the exact command to run")

# ── nothing to say ───────────────────────────────────────────────────────────────────────────────────

# 7. plan() returns None for an empty request; the bot must then fall back to its own message.
check(sponsor_bot.regen_advice(None, 0) is None, "no plan means no advice, so the caller's message stands")

# 8. ⛔ regen_note NEVER raises, whatever the archive is. It runs before a pod is rented.
#
#    ⛔ Patch the MODULE ATTRIBUTE, not the environment. CKPT_ARCHIVE is read at import, so setting
#    HAZYNC_CKPT_ARCHIVE here would change nothing and this would pass against whatever archive the host
#    happens to have — green for a reason unrelated to its own label. It read that way at first.
_saved = sponsor_bot.CKPT_ARCHIVE
sponsor_bot.CKPT_ARCHIVE = "/nonexistent/archive/path"
try:
    sponsor_bot.regen_note([600000])
    ok = True
except Exception:
    ok = False
sponsor_bot.CKPT_ARCHIVE = _saved
check(ok, "regen_note swallows a missing archive rather than stopping every sponsorship")

print()
EXPECTED_CONTROL_FAILURES = {
    "with no checkpoint below it, the block is refused and genesis is named as the reason",
    "the LOWEST height with no checkpoint below it is the one named",
}

if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — the refusal was disabled and exactly the {len(got)} assertion(s) that "
              "depend on it failed:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — disabling the no-checkpoint-below refusal did not produce the expected failures.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES):
        print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
