#!/usr/bin/env python3
"""A run refuses to claim under an identity the operator did not expect (hazync#528).

⛔ WHY. `tip_board.identity()` returns whatever `$HAZYNC_HOME` holds, and every box has SOME identity
— so a run always claims as someone, and a wrong one looks exactly like a right one. Measured
2026-09-26: a tip session launched on the rig claimed board blocks as

    claiming as 'hazync-coordinator' (9be361b031…)

when hazync#367 decided board work is claimed as **G H O S T** (`c4c7d99b6b…`), whose key lives
deliberately on a box the operator owns and NOT on any server. The log said so in one line, and the
operator caught it — the tooling did not.

⚠ THE CREDIT IS THE POINT OF CLAIMING. Work proved under the wrong identity lands on a public
leaderboard, under a name, permanently. It is not a cosmetic slip.

⚠ And the check has to come BEFORE the money. A refusal after renting 21 pods is an expensive way to
learn the same thing.

  python3 test_claim_identity.py            # must PASS
  python3 test_claim_identity.py --control  # guard removed; the wrong identity MUST get through
"""
import os
import re
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def guard(claim_as, box_handle):
    """The rule as the driver applies it: True = the run is refused."""
    if CONTROL:
        return False
    return bool(claim_as) and claim_as != box_handle


# ── 1. the measured case ───────────────────────────────────────────────────────────────────────
check(guard("G H O S T", "hazync-coordinator"),
      "⛔ asking for 'G H O S T' on a box holding 'hazync-coordinator' is REFUSED")
check(not guard("G H O S T", "G H O S T"),
      "and the right identity runs")

# ⚠ Unset keeps the old behaviour — this must not break every existing invocation.
check(not guard(None, "hazync-coordinator"),
      "without --claim-as nothing changes (the flag is opt-in)")
check(not guard("", "hazync-coordinator"),
      "an empty value is treated as unset, not as a handle nobody has")

# ⚠ Exact match: handles carry spaces and case, and 'ghost' is not 'G H O S T'.
check(guard("G H O S T", "ghost"), "matching is exact — 'ghost' is a different identity")
check(guard("G H O S T", "G H O S T "), "and trailing whitespace is a different identity too")


# ── 2. ⛔ the driver must apply it, and BEFORE renting ──────────────────────────────────────────
src = open(os.path.join(HERE, "tip_smoke.py")).read()

check("--claim-as" in src, "the flag exists")
check("a.claim_as and a.claim_as != ident[2]" in src, "⛔ and the driver actually tests it")

i_guard = src.find("a.claim_as and a.claim_as != ident[2]")
i_rent = src.find("rented = a.cards + a.spares")
check(i_guard != -1 and i_rent != -1 and i_guard < i_rent,
      "⛔ and the check runs BEFORE any pod is rented, so a wrong identity costs nothing")

# ⚠ The refusal must name BOTH identities. "identity mismatch" sends the reader to the wrong file.
m = re.search(r"--claim-as \{a\.claim_as!r\} but this box's identity is \{ident\[2\]!r\}", src)
check(m is not None, "and the refusal names what was asked for AND what the box holds")

EXPECTED_CONTROL = {
    "asking for 'G H O S T' on a box holding 'hazync-coordinator' is REFUSED",
    "matching is exact",
    "and trailing whitespace is a different identity too",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — without the guard a run claims under whatever identity the box holds:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}, got {sorted(hit)}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("a run cannot silently claim board work under the wrong name")
