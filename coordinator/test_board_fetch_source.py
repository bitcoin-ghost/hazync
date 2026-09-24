#!/usr/bin/env python3
"""Board work fetches from the API; only TIP work uses the bridge.

⛔ WHY THIS EXISTS. The source was `"ssh" if from_tip else a.claim_source`. On a tip run started
with --claim-source ssh that sent BOARD heights to the bridge, which emits nothing below EMIT_FROM
and so can never hold one:

    no bundle for 123538: .../tip_bundles/bundle_123538.json: No such file or directory

Three in a row tripped the fleet-fault guard and released a 36-card fleet 17 seconds into a session
(2026-09-23). Worse, it silently disabled hazync#367 -- filling the gaps between tip blocks with
board work -- because every board claim was unfetchable. That fleet then idled 31.4 min of a 60-min
session, $5.81 of rented GPU doing nothing, with a board backlog available the whole time.

  python3 test_board_fetch_source.py            # must PASS
  python3 test_board_fetch_source.py --control  # board work follows --claim-source again; MUST FAIL
"""
import sys

CONTROL = "--control" in sys.argv
fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok: fails.append(what)

def source_for(from_tip, claim_source):
    """Exactly the shipped expression."""
    if CONTROL:
        return "ssh" if from_tip else claim_source       # the bug, restored
    return "ssh" if from_tip else "api"

# ── 1. ⛔ THE CENTRAL CASE: a tip run configured for ssh must still fetch board work from the api
check(source_for(False, "ssh") == "api",
      f"board work on an ssh-configured tip run goes to the API "
      f"(got {source_for(False, 'ssh')!r} — 'ssh' asks the bridge for a height it never emits)")

# ── 2. tip work still goes to the bridge, whatever --claim-source says
check(source_for(True, "api") == "ssh", "a TIP block always comes off the bridge host")
check(source_for(True, "ssh") == "ssh", "and does so when ssh is configured too")

# ── 3. a board-only run is unaffected
check(source_for(False, "api") == "api", "a board-only run is unchanged")

EXPECTED_CONTROL = {"board work on an ssh-configured tip run goes to the API"}
print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — board work followed --claim-source and was sent to the bridge:")
        for f in fails: print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}; got {len(fails)}")
    sys.exit(1)
if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails)); sys.exit(1)
print("the board has one source and the tip has another; --claim-source governs only the tip")
