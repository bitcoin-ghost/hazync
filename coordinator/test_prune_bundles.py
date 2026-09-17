#!/usr/bin/env python3
"""Tests for the bundle pruning rule (hazync#347).

WHY THIS EXISTS. Bundles are the largest thing on the coordinator's disk and they grow with height:
measured 2026-09-17, 5.4 KB near genesis against 2.18 MB at 230,000, and 95 GB for the first 230,000
blocks. Deleting the finished ones is how that stops growing monotonically.

⛔ DELETION IS REAL, which is the whole reason this file is careful. `bundle_path()` falls back to a
legacy `block_<n>.json`, so it LOOKS like a deleted bundle would still be served — but that directory
is EMPTY on the live coordinator (checked 2026-09-17, 0 files). Removing a bundle genuinely removes
the ability to serve or claim that height, and the only way back is replaying the bridge forward from
a checkpoint below it.

⛔ THE SCENARIOS USE PINNED LITERALS, NOT THE LIVE VALUES. `test_claim_grace.py` records why: its
first version derived the ages from the constant its own control changed, so the control moved the
scenarios with it and passed for a reason that had nothing to do with the thing under test. Every
height, margin and spine top below is a literal.

THE RULE (agreed 2026-09-15). Delete `bundle_N.json` only when ALL hold:
  1. N has a verified proof on the board.
  2. N is inside the genesis-anchored spine.
  3. That proof's receipt is confirmed in BOTH R2 and B2.
  4. N is at least `margin` blocks below the spine top, AND the nightly re-verify passed.
Plus a way back: an archived checkpoint must exist below N, or there is nothing to rebuild from.

WHAT THIS DOES NOT COVER: no real R2 or B2 (the confirmation sets are injected, the same way
`hazync-offsite-proofs.main(argv, client=...)` takes an injected client), and no real bridge replay.

Usage:
  python3 test_prune_bundles.py            # assertions; exit 0 on success
  python3 test_prune_bundles.py --control  # B2 is ignored, as if condition 3 were half-written;
                                           # the "missing from B2" test MUST fail
"""

import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy"))
import prune_bundles  # noqa: E402

_real_decision = prune_bundles.prune_decision
if CONTROL:
    # The control: condition 3 only half-implemented — R2 is checked, B2 is not. This is the exact
    # omission #347 names as the control case, and scenario 2 below must catch it.
    def _decision(h, **kw):
        kw["in_b2"] = True
        return _real_decision(h, **kw)
    prune_bundles.prune_decision = _decision

fails = []
def check(ok, what):
    if ok:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)

# Pinned. NOT read from the spine, the ledger or the environment.
TOP, MARGIN, H = 100_000, 10_000, 50_000

def decide(**over):
    kw = dict(verified=True, in_spine=True, in_r2=True, in_b2=True,
              spine_top=TOP, margin=MARGIN, reverify_ok=True,
              checkpoint_below=True, require_checkpoint=True)
    kw.update(over)
    return prune_bundles.prune_decision(H, **kw)

print(f"  (scenarios pinned at height {H}, spine top {TOP}, margin {MARGIN}"
      f"{'; CONTROL: B2 ignored' if CONTROL else ''})")

# 1. Everything holds -> delete. If this fails the job can never free anything.
d, why = decide()
check(d, f"a finished block {MARGIN} below the spine top, proven, spined and in both stores, is deleted")

# 2. ⛔ THE CONTROL CASE. A receipt in R2 but not B2 has ONE offsite copy. #347 requires two, because
#    the whole point of the second provider is surviving the first one losing it.
d, why = decide(in_b2=False)
check(not d, "a receipt missing from B2 is NEVER deleted")

# 3. The mirror of 2 — neither store is privileged.
d, why = decide(in_r2=False)
check(not d, "a receipt missing from R2 is never deleted")

# 4. No proof on the board: the bundle is the only way to prove that height at all.
d, why = decide(verified=False)
check(not d, "a height with no verified proof is never deleted")

# 5. Outside the spine, the proof is not yet anchored to genesis.
d, why = decide(in_spine=False)
check(not d, "a height outside the genesis-anchored spine is never deleted")

# 6. Inside the margin. Pinned literal: 95,000 is 5,000 below a 100,000 top, inside a 10,000 margin.
d, why = prune_bundles.prune_decision(
    95_000, verified=True, in_spine=True, in_r2=True, in_b2=True,
    spine_top=TOP, margin=MARGIN, reverify_ok=True, checkpoint_below=True, require_checkpoint=True)
check(not d, f"a height within {MARGIN} of the spine top is never deleted")

# 7. A GLOBAL GATE. If the nightly re-verify did not hold, the stored proofs are in question — which
#    is exactly when their bundles are most needed.
d, why = decide(reverify_ok=False)
check(not d and "re-verify" in why, "a failed nightly re-verify stops every deletion")

# 8. THE WAY BACK. Without an archived checkpoint below it, recovery means replaying from genesis.
d, why = decide(checkpoint_below=False)
check(not d and "checkpoint" in why, "no archived checkpoint below it means no deletion")

# 9. ...and that guard is escapable deliberately, not by accident.
d, why = decide(checkpoint_below=False, require_checkpoint=False)
check(d, "--no-require-checkpoint allows deletion when every other condition holds")

# 10. Every kept case must SAY why. A dry run that prints "kept 229,999" cannot be acted on.
#
# ⛔ Deliberately NOT using in_b2 here. It did at first, which made this a second detector of the very
# condition the control disables: one disabled guard produced two failures, and a control that trips
# several assertions cannot tell you which guard it removed. These three are untouched by the control.
for over in (dict(in_r2=False), dict(verified=False), dict(in_spine=False)):
    _, why = decide(**over)
    if not why:
        check(False, f"a kept bundle must carry a reason ({over})")
        break
else:
    check(True, "every kept bundle carries a reason")

# ── the readers, against real files ──────────────────────────────────────────────────────────────────

tmp = tempfile.mkdtemp(prefix="prune_")

# reverify: "<fails> <last_alert>", 0 fails means the last run held.
with open(os.path.join(tmp, "check-proofs"), "w") as f:
    f.write("0 0\n")
ok, _ = prune_bundles.reverify_passed(tmp)
check(ok, "a check-proofs marker of '0 0' reads as passed")

with open(os.path.join(tmp, "check-proofs"), "w") as f:
    f.write("3 1789600000\n")
ok, why = prune_bundles.reverify_passed(tmp)
check(not ok and "3" in why, "a marker with consecutive failures reads as NOT passed, and says so")

# ⛔ Absent is not a pass. Nobody having verified these proofs is when deleting is least safe.
ok, why = prune_bundles.reverify_passed(os.path.join(tmp, "nope"))
check(not ok, "a missing check-proofs marker is NOT treated as a pass")

# spine top: absent spine means 0, which makes every height fail condition 2 rather than pass it.
check(prune_bundles.spine_top(os.path.join(tmp, "nope")) == 0,
      "a missing spine.json reads as top 0, so nothing is inside the spine")

os.makedirs(os.path.join(tmp, "spine"), exist_ok=True)
with open(os.path.join(tmp, "spine", "spine.json"), "w") as f:
    f.write('{"lo": 1, "hi": 66000}')
check(prune_bundles.spine_top(os.path.join(tmp, "spine")) == 66000, "spine.json's hi is the spine top")

# checkpoints: absent archive means none, so the way-back gate holds everything.
check(prune_bundles.archived_checkpoints("") == [], "no archive directory means no checkpoints")
os.makedirs(os.path.join(tmp, "ck"), exist_ok=True)
for h in (50_000, 100_000):
    open(os.path.join(tmp, "ck", f"state_{h}.bin"), "w").close()
cks = prune_bundles.archived_checkpoints(os.path.join(tmp, "ck"))
check(cks == [50_000, 100_000], "archived checkpoints are found and sorted")
check(prune_bundles.has_checkpoint_below(60_000, cks), "a checkpoint below the height is found")
check(not prune_bundles.has_checkpoint_below(50_000, cks),
      "a checkpoint AT the height is not below it — replaying forward from it produces that block")

print()
if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
