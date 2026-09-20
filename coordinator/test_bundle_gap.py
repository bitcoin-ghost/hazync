#!/usr/bin/env python3
"""Tests for hazync-check-bundle-gap.sh (hazync-admin#2).

WHY THIS EXISTS. The tip bridge pushes bundles to the coordinator over a WRITE-ONLY channel, so the
sending side cannot see what landed and can never report a loss. This check is the only thing that can,
and the loss it guards against is the silent kind: `/api/vranges` derives the work on offer from
`bundle_path(h) is None`, so a bundle that never arrives makes its block unclaimable with no error
anywhere -- it is simply never offered to a worker.

That makes the dangerous direction the SAFE-LOOKING one: reporting "holds" when bundles are missing or
when nothing has arrived for hours. The cases below pin both, and the two failures are kept distinct:

  GAP   a height missing between two we hold -- judged from one reading, unambiguous.
  STALL the highest bundle not advancing while the chain does -- judged only over SEVERAL readings,
        because during catch-up the bridge is legitimately far behind the tip and an absolute
        "how far behind" test would fire for two days and be muted.

No real bundles and no network: a temp directory stands in for the store.

  python3 test_bundle_gap.py            # assertions; exit 0 on success
  python3 test_bundle_gap.py --control  # the gap arithmetic is removed; MUST fail
"""
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deploy", "hazync-check-bundle-gap.sh")
EMIT_FROM = 967500

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


work = tempfile.mkdtemp(prefix="bgap_")
script = os.path.join(work, "check.sh")
src = open(SCRIPT).read()

GUARD = 'MISSING=$(( SPAN - N ))'
if GUARD not in src:
    print(f"CANNOT TEST: the gap arithmetic is not in {SCRIPT}.")
    print("These tests are pinned to it; if it was rewritten, update them deliberately.")
    sys.exit(2)
if CONTROL:
    # Removes ONLY the gap arithmetic, so exactly the gap case should trip.
    src = src.replace(GUARD, 'MISSING=0')
open(script, "w").write(src)
os.chmod(script, 0o755)


def run(heights, *, tip=None, state_dir=None, below=()):
    """Run the check against a store holding exactly `heights` (plus `below`, under EMIT_FROM)."""
    d = tempfile.mkdtemp(prefix="store_", dir=work)
    for h in list(heights) + list(below):
        open(os.path.join(d, f"bundle_{h}.json"), "w").write("{}")
    env = dict(os.environ, HAZYNC_BRIDGE_OUT=d, HAZYNC_BRIDGE_EMIT_FROM=str(EMIT_FROM),
               CHECK_STATE_DIR=state_dir or tempfile.mkdtemp(prefix="state_", dir=work),
               BUNDLE_STALL_RUNS="4")
    if tip is not None:
        tf = os.path.join(d, "node_tip")
        open(tf, "w").write(str(tip))
        env["NODE_TIP_FILE"] = tf
    p = subprocess.run([script], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


# 1. Nothing yet is NOT a failure -- for ~2 days after the split there are legitimately no tip bundles.
rc, o = run([])
check(rc == 0 and "has not started" in o, f"an empty store holds, it does not page (rc={rc})")

# 2. A contiguous run holds.
rc, o = run(range(EMIT_FROM, EMIT_FROM + 20), tip=EMIT_FROM + 25)
check(rc == 0 and "holds" in o, f"20 contiguous bundles hold (rc={rc})")

# 3. ⛔ THE CASE THIS FILE EXISTS FOR: a hole must be found, and NAMED.
got = list(range(EMIT_FROM, EMIT_FROM + 20))
got.remove(EMIT_FROM + 7)
rc, o = run(got, tip=EMIT_FROM + 25)
check(rc == 1 and "GAP" in o, f"a missing height exits 1 (rc={rc})")
check(f"bundle_{EMIT_FROM + 7}.json" in o, "the gap report NAMES the missing bundle")

# 4. Bundles BELOW EMIT_FROM are not this check's business and must not fabricate a gap. The store holds
#    418,269 historical bundles ending at 418,268; counting them would report a hole half a million wide.
rc, o = run(range(EMIT_FROM, EMIT_FROM + 5), below=(100, 418268), tip=EMIT_FROM + 10)
check(rc == 0, f"historical bundles below EMIT_FROM are ignored (rc={rc})")

# 5. STALL needs repetition, not one reading: the same height four times over, with the chain ahead.
sd = tempfile.mkdtemp(prefix="stall_", dir=work)
rcs = [run(range(EMIT_FROM, EMIT_FROM + 3), tip=EMIT_FROM + 900, state_dir=sd)[0] for _ in range(5)]
check(rcs[0] == 0, f"a first reading never alleges a stall (rc={rcs[0]})")
check(1 in rcs[1:], f"a height that never advances eventually exits 1 (rcs={rcs})")

# 6. ... and progress clears it, so a slow-but-moving catch-up is never paged.
sd2 = tempfile.mkdtemp(prefix="adv_", dir=work)
rcs2 = [run(range(EMIT_FROM, EMIT_FROM + 3 + i), tip=EMIT_FROM + 900, state_dir=sd2)[0] for i in range(6)]
check(all(r == 0 for r in rcs2), f"an advancing height never alleges a stall (rcs={rcs2})")

EXPECTED_CONTROL_FAILURES = {
    "a missing height exits 1 (rc=0)",
    "the gap report NAMES the missing bundle",
}

print()
if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — the gap arithmetic was removed and exactly the {len(got)} assertion(s) "
              "that depend on it failed:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the gap arithmetic did not produce the expected failures.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES):
        print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
