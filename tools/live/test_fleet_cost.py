#!/usr/bin/env python3
"""A card that stops answering does not stop billing (hazync#476).

⛔ WHY THIS EXISTS. #455 added `· 3 down` beside the fleet rate, with the reason written into
`fleet_sub`: *"THE RATE DOES NOT DROP WHEN A CARD DIES. A rented pod bills whether it answers or
not, so a reduced rate would understate what the run is actually costing."* That function was
correct. One layer upstream, `collect.py` summed `cost_hr` over the cards that were **up**, so the
money had already been removed before `fleet_sub` ever saw it.

Measured on a 45-minute replay of a real 3x RTX 4090 run: with the feed stopped the frame read

    0/3 up   $0.00/hr  ·  3 down

while $2.22/hr was billing — the two halves of one readout disagreeing, which is worse than an
omission because the label points straight at the thing the number left out.

  python3 test_fleet_cost.py            # must PASS
  python3 test_fleet_cost.py --control  # the rate is taken over `up` cards again; MUST FAIL
"""
import json
import os
import subprocess
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def run_collector(rundir):
    """One `--once` tick of the SHIPPED collector, so this cannot drift from what runs."""
    src = os.path.join(HERE, "collect.py")
    if CONTROL:
        # ⛔ THE CONTROL IS THE ORIGINAL LINE, restored in a copy of the real file.
        text = open(src, encoding="utf8").read().replace(
            'rate = sum(c["cost_hr"] for c in cards)',
            'rate = sum(c["cost_hr"] for c in up)  # CONTROL: only the cards that answered')
        src = os.path.join(rundir, "collect_control.py")
        open(src, "w", encoding="utf8").write(text)
    out = os.path.join(rundir, "snap.json")
    subprocess.run([sys.executable, src, "--rundir", rundir, "--out", out, "--once"],
                   capture_output=True, text=True, timeout=120,
                   env={**os.environ, "COORD_URL": "http://127.0.0.1:1"})   # no chain call
    with open(out) as fh:
        return json.load(fh)


def fleet(cards_up, cards_down, *, price=0.74, age_down=600.0, t0_ago=None):
    """A rundir with `cards_up` streaming now and `cards_down` last heard from `age_down` ago."""
    d = tempfile.mkdtemp(prefix="cost_")
    os.makedirs(os.path.join(d, "stream"))
    now = time.time()
    if t0_ago is not None:
        open(os.path.join(d, "t0"), "w").write(f"{now - t0_ago}\n")
    names = []
    with open(os.path.join(d, "pods.txt"), "w") as p:
        for i in range(cards_up + cards_down):
            n = f"hz-card-{i}"
            names.append(n)
            p.write(f"pod{i} {n} 10.0.0.{i + 1} 2200{i} {price} RTX_4090\n")
    for i, n in enumerate(names):
        last = now - (0.5 if i < cards_up else age_down)
        with open(os.path.join(d, "stream", f"{n}.csv"), "w") as fh:
            for k in range(40):
                ts = last - (39 - k)
                fh.write(f"{ts:.6f},95,4000,60,{price * 100:.2f},2700,10251,proving,1,2,741000\n")
    return d


# ── 1. ⛔ THE CENTRAL CASE. Every card down, every card still rented ──────────────────────────────
snap = run_collector(fleet(0, 3))
f = snap["fleet"]
check(f["up"] == 0 and f["cards"] == 3, f"the fleet is 0/3 up (got {f['up']}/{f['cards']})")
check(abs(f["cost_hr"] - 2.22) < 0.01,
      f"and still costs $2.22/hr — three rented 4090s bill whether they answer or not "
      f"(got ${f['cost_hr']:.2f}/hr)")

# ── 2. one card down out of three: the rate must not drop by a third ─────────────────────────────
f = run_collector(fleet(2, 1))["fleet"]
check(abs(f["cost_hr"] - 2.22) < 0.01,
      f"2/3 up still reports the whole fleet's rate (got ${f['cost_hr']:.2f}/hr, not $1.48)")

# ── 3. a healthy fleet is unchanged — the fix must not inflate the normal case ────────────────────
f = run_collector(fleet(3, 0))["fleet"]
check(abs(f["cost_hr"] - 2.22) < 0.01,
      f"3/3 up reports $2.22/hr exactly as before (got ${f['cost_hr']:.2f}/hr)")

# ── 4. spend: the same understatement, one field over ────────────────────────────────────────────
# ⚠ Cards that stopped streaming 10 minutes ago. Sample-counting credits ~40 s each and stops;
# the fleet was rented for the whole 30 minutes.
snap = run_collector(fleet(0, 3, age_down=600.0, t0_ago=1800.0))
spend = snap["fleet"]["spend_usd"]
expect = 2.22 * 1800.0 / 3600.0                      # $1.11
check(abs(spend - expect) < 0.05,
      f"spend bills the rented fleet for the whole run: ${spend:.3f} vs ${expect:.3f} expected "
      f"(sample-counting would have said ~$0.07)")

# ── 5. and without a t0 it falls back rather than reporting nothing ──────────────────────────────
snap = run_collector(fleet(3, 0))                    # no t0 file
check(snap["fleet"]["spend_usd"] > 0,
      f"with no t0 the observed figure is still reported (${snap['fleet']['spend_usd']:.4f}), not 0")

# ⚠ NAME EVERY ONE, AND COUNT THEM. Listing two while three fail lets an unrelated breakage ride
# along inside a "CONTROL OK". `spend` is on the list because it is computed from `rate`, so the
# mutation reaches it too -- which is itself the point of #476: one wrong denominator, two wrong
# numbers on the frame.
EXPECTED_CONTROL = {"and still costs $2.22/hr", "2/3 up still reports",
                    "spend bills the rented fleet"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the rate taken over `up` cards again, a dead fleet reported $0.00/hr:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected the rate assertions to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("a card that stops answering keeps costing money, and the frame says so")
