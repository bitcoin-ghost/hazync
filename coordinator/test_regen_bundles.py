#!/usr/bin/env python3
"""Tests for on-demand bundle regeneration (hazync#347).

WHY THIS EXISTS. The live bridge runs with HAZYNC_BRIDGE_EMIT_FROM=967500, so below that height it
advances state and writes no bundle. Measured 2026-09-18: coverage is 1..418,268, the head is past
740,000, and bundles reach 17.0 MB by height 418,000 — so the 549,231-block gap is >9 TB against 3.9 TB
free and cannot simply be stored. Those heights have to be made on demand from an archived checkpoint.

⛔ THE TWO WAYS THIS GOES WRONG ARE BOTH SILENT, which is why the guards are tested and not just written:

  1. NO RUNG BELOW THE TARGET. bridge_load_state() returns None for a missing or unreadable seed and the
     resume path treats that as "rebuild from genesis" (main.rs:3496 says so). At 533-599 ms/block that
     is ~6 days of walking that looks exactly like a working job until the disk fills. `plan()` must
     REFUSE, and `replay()` must additionally verify the bridge said "resuming from checkpoint".

  2. A RUNG AT THE TARGET IS NOT BELOW IT. Replaying forward from a checkpoint at N produces N+1 onward,
     so a rung at N cannot make block N. test_prune_bundles.py asserts the same rule on the deletion
     side; if the two ever disagree, a bundle could be deleted that cannot be rebuilt.

⛔ SCENARIOS USE PINNED LITERALS. test_claim_grace.py records why: deriving them from the value the
control changes moves the scenarios with it, and the control then passes for a reason unrelated to the
thing under test.

WHAT THIS DOES NOT COVER: no real bridge, no real replay (that needs the 240 MB binary, a bitcoin datadir
and hours of walking). The subprocess boundary is where this stops; `replay()` is exercised only for its
refusal to accept a run that did not resume from the seed.

Usage:
  python3 test_regen_bundles.py            # assertions; exit 0 on success
  python3 test_regen_bundles.py --control  # the "no rung below" guard is disabled; MUST fail
"""

import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy"))
import regen_bundles as rb  # noqa: E402

_real_rung_below = rb.rung_below
if CONTROL:
    # The control disables ONLY the strictly-below rule: at-or-below instead of below. That is the one
    # mistake it exists to catch — a rung AT height N cannot produce block N, because replaying forward
    # from a checkpoint at N yields N+1 onward.
    #
    # ⛔ Deliberately NOT "return any rung". That would also break SEED SELECTION, so scenarios 5 and 6
    # would fail too, and a control that trips several assertions cannot tell you which guard it removed.
    # The same over-coupling was caught and fixed in test_prune_bundles.py.
    def _rung_below(height, rungs):
        at_or_below = [r for r in rungs if r <= height]
        return max(at_or_below) if at_or_below else None
    rb.rung_below = _rung_below

fails = []
def check(ok, what):
    if ok:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)

# Pinned: the rung we rescued from the retiring box, and two the archiver will make.
RUNGS = [230000, 740000, 765000]
print(f"  (rungs pinned at {RUNGS}{'; CONTROL: any rung accepted' if CONTROL else ''})")

# 1. The ordinary case: pick the HIGHEST rung below, so the walk is as short as possible.
check(_real_rung_below(800000, RUNGS) == 765000, "the highest rung below the target is chosen, not the lowest")

# 2. ⛔ THE CONTROL CASE. A rung AT the target cannot produce the target.
p = rb.plan([230000], RUNGS)
check(p["missing"] == [230000], "a rung AT the height is not below it, so the height is refused")

# 3. Below every rung: nothing can seed it.
p = rb.plan([1000], RUNGS)
check(p["missing"] == [1000] and p["rung"] is None, "a height below every rung is refused, not replayed from genesis")

# 4. No rungs at all — the state before any archiving. Everything must be refused.
p = rb.plan([600000], [])
check(p["missing"] == [600000], "with no archived rungs at all, nothing is attempted")

# 5. A gap height seeds from the rung below it, and the walk reaches the top of the request.
p = rb.plan([600000, 600001], RUNGS)
check(p["rung"] == 230000 and p["hi"] == 600001 and not p["missing"],
      "a gap height seeds from the rung below and walks to the highest requested height")

# 6. Mixed requests take the LOWEST usable seed, so one walk covers them all.
p = rb.plan([600000, 800000], RUNGS)
check(p["rung"] == 230000 and p["hi"] == 800000, "a mixed request seeds low enough to cover every height")

# 7. Duplicates and disorder must not change the plan.
check(rb.plan([600001, 600000, 600000], RUNGS) == rb.plan([600000, 600001], RUNGS),
      "the plan is insensitive to order and duplicates")

# 8. Empty request is a no-op, not an error.
check(rb.plan([], RUNGS) is None, "an empty request plans nothing")

# ── archived_rungs, against real files ───────────────────────────────────────────────────────────────

tmp = tempfile.mkdtemp(prefix="regen_")
check(rb.archived_rungs("") == [] and rb.archived_rungs(os.path.join(tmp, "nope")) == [],
      "a missing archive directory yields no rungs, so every height is refused")

for h in (765000, 230000, 740000):
    open(os.path.join(tmp, f"state_{h}.bin"), "w").close()
# ⛔ Names that are not exactly state_<digits>.bin must be skipped, not half-parsed. The prune job hit
# shellcheck SC2010 for parsing ls output; this is the same hazard in Python.
for junk in ("state_.bin", "state_abc.bin", "state_230000.bin.tmp", "notastate.bin"):
    open(os.path.join(tmp, junk), "w").close()
check(rb.archived_rungs(tmp) == [230000, 740000, 765000],
      "rungs are found, sorted, and malformed names ignored (including a partial .tmp)")

# ⛔ The .tmp case is not hypothetical: the 230,000 rung was streamed in as state_230000.bin.tmp, and a
# regeneration firing mid-transfer must not treat a half-written file as a usable seed.
check(230000 in rb.archived_rungs(tmp), "a completed rung is usable alongside an in-flight .tmp")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — any-rung accepted and {len(fails)} assertion(s) failed, as they must: "
              + "; ".join(fails))
        sys.exit(0)
    print("CONTROL FAILED — a rung at or above the target was accepted and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
