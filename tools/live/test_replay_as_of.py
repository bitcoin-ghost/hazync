#!/usr/bin/env python3
"""A finished capture can be replayed as it looked at any moment of the run (--as-of).

⛔ WHY THIS EXISTS. A tip run's most legible artifact is the hour compressed into a few seconds, and
the archived PNGs cannot provide one. Measured on the tip hour 4 capture, 1,748 archived frames:

  1. they span MORE THAN ONE RUN -- the earliest show tip 968,367 on a 15-card fleet, because
     frame-archive.sh watches frame.png and captures whichever collector owns it;
  2. the renderer was FIXED DURING the run (done-counter, green->orange reversion, the finish pulse,
     the session timer), so the page changes appearance partway through the timelapse;
  3. none of them carries block numbers on the grid -- that shipped afterwards (#586/#595).

Re-rendering from the capture answers all three: one run, one renderer, today's fixes. That is only
sound if `--as-of` really rewinds -- if it silently ignored the cutoff, every frame would be the END
state and the timelapse would be a still image that nobody would question.

    python3 test_replay_as_of.py             # the cutoff is honoured, early != late
    python3 test_replay_as_of.py --control   # cutoff ignored -- every frame must look identical
"""
import json
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── a tiny capture: two cards, two blocks, one finishing well after the other ───────────────────
RUN = tempfile.mkdtemp(prefix="asof-")
os.makedirs(os.path.join(RUN, "stream"))
T0 = 1790000000

with open(os.path.join(RUN, "pods.txt"), "w") as fh:
    fh.write("hz-1 1.1.1.1 2.09\nhz-2 1.1.1.2 2.09\n")

# columns, per read_streams: t,?,?,?,w,?,?,phase,seg_n,seg_total,height,...
for card in ("hz-1", "hz-2"):
    with open(os.path.join(RUN, "stream", f"{card}.csv"), "w") as fh:
        for i in range(600):
            t = T0 + i
            if i < 200:            # first block, proving then done
                ph, h, n, tot = ("proving", 500001, i, 200)
            elif i < 400:          # idle between blocks
                ph, h, n, tot = ("idle", "", 0, 0)
            else:                  # second block
                ph, h, n, tot = ("proving", 500002, i - 400, 200)
            fh.write(f"{t}.0,0,0,31,35.9,180,405,{ph},{n},{tot},{h},0,0\n")

with open(os.path.join(RUN, "tip_ledger.jsonl"), "w") as fh:
    fh.write(json.dumps({"event": "appeared", "t": T0 + 10, "height": 500001}) + "\n")
    fh.write(json.dumps({"event": "accepted", "t": T0 + 210, "height": 500001,
                         "chain_tip_now": 500001}) + "\n")
    fh.write(json.dumps({"event": "appeared", "t": T0 + 410, "height": 500002}) + "\n")
    fh.write(json.dumps({"event": "accepted", "t": T0 + 610, "height": 500002,
                         "chain_tip_now": 500002}) + "\n")


def snap(as_of):
    """Collect one snapshot at `as_of`. The control drops the flag, which is the bug being guarded."""
    out = os.path.join(RUN, f"s_{as_of}.json")
    cmd = [sys.executable, os.path.join(HERE, "collect.py"), "--rundir", RUN,
           "--tip-ledger", os.path.join(RUN, "tip_ledger.jsonl"), "--once", "--out", out]
    if not CONTROL:
        cmd += ["--as-of", str(as_of)]
    else:
        cmd += ["--replay"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(out):
        check(False, f"collect at {as_of} failed: {(r.stderr or '')[-300:]}")
        return None
    return json.load(open(out))


EARLY, LATE = T0 + 250, T0 + 620
a, b = snap(EARLY), snap(LATE)

if a and b:
    # ── 1. the clock is rewound ─────────────────────────────────────────────────────────────────
    check(a["t"] < b["t"], f"the snapshot clock differs ({a['t']} vs {b['t']})")
    if not CONTROL:
        check(abs(a["t"] - EARLY) < 1.5, f"and IS the requested moment ({a['t']} vs {EARLY})")

    # ── 2. ⛔ THE ONE THAT MATTERS: the later block must not exist yet ──────────────────────────
    ha = {blk["h"] for blk in a["blocks"]}
    hb = {blk["h"] for blk in b["blocks"]}
    check(500002 in hb, f"the second block is present at the end ({sorted(hb)})")
    check(500002 not in ha,
          f"⛔ and ABSENT at the earlier moment ({sorted(ha)}) — otherwise every frame is the end state")

    # ── 3. the chain tip is the tip AS IT WAS ───────────────────────────────────────────────────
    check(b["chain"]["tip"] == 500002, f"chain tip at the end is 500,002 (got {b['chain']['tip']})")
    check(a["chain"]["tip"] == 500001,
          f"⛔ and 500,001 earlier (got {a['chain']['tip']}) — the final tip must not be painted "
          f"over the whole run")

    # ── 4. observed time accrues, so the two frames are not the same frame ──────────────────────
    # ⚠ observed_s, NOT spend: spend is observed_s × the card's rate, and the rate comes from
    # pods.txt, so a fixture without a priced feed reports $0.00 at both ends and the assertion
    # would fail for a reason that has nothing to do with the cutoff. Count the samples instead —
    # that is the quantity --as-of actually truncates.
    oa = sum(c.get("observed_s") or 0 for c in a["cards"])
    ob = sum(c.get("observed_s") or 0 for c in b["cards"])
    check(0 < oa < ob, f"observed card-seconds grow across the run ({oa} -> {ob})")

print()
if CONTROL:
    # Without the cutoff both calls read the whole capture, so the assertions above must FAIL.
    if fails:
        print(f"PASS (control): {len(fails)} assertion(s) failed as they must — the cutoff is what "
              f"makes a timelapse possible, not the replay flag")
        sys.exit(0)
    print("FAIL (control): no cutoff and yet the snapshots differed — this test proves nothing")
    sys.exit(1)

if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")
