#!/usr/bin/env python3
"""A pod released on purpose is not a card that failed (hazync#492).

⛔ WHY THIS EXISTS. The run rents spares, runs four gates, keeps the best --cards and RELEASES the
rest. That is the design working. But the collector built its card list from whatever CSV files were
sitting in stream/, and a released pod keeps its capture file for ever -- so every deliberate release
came back as `hz-smoke-8 · down` in RED, and inflated the fleet denominator with it.

Measured live on block 968,243: 23 CSV files, 16 entries in pods.txt, and the published frame read

    16/23 up   $7.84/hr  ·  7 down

Every one of those 7 was a release, not a failure. The reading is worse than noise: `down` is the
word the frame uses for a card that broke, so a healthy 16/16 fleet looked like it had lost a third
of itself, on every frame, for the whole run.

⛔ AND IT MUST NOT UNDO hazync#476. That issue is the opposite error -- a card that stops answering
still bills, so it must stay visible and stay in the rate. pods.txt is the list of cards being PAID
FOR: in it and silent means DOWN, not in it at all means released. Both are pinned below.

  python3 test_released_not_down.py            # must PASS
  python3 test_released_not_down.py --control  # the pods.txt filter removed; MUST FAIL
"""
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collect                                                                  # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def rundir(*, rented, csvs, silent=()):
    """`rented` go in pods.txt; `csvs` get capture files. `silent` last streamed 10 min ago."""
    d = tempfile.mkdtemp(prefix="rel_")
    os.makedirs(os.path.join(d, "stream"))
    now = time.time()
    with open(os.path.join(d, "pods.txt"), "w") as p:
        for i, n in enumerate(rented):
            p.write(f"pod{i} {n} 10.0.0.{i + 1} 2200{i} 0.4900 NVIDIA_A40\n")
    for n in csvs:
        last = now - (600.0 if n in silent else 0.5)
        with open(os.path.join(d, "stream", f"{n}.csv"), "w") as fh:
            for k in range(40):
                ts = last - (39 - k)
                fh.write(f"{ts:.6f},95,4000,60,146.0,2000,7251,proving,200,0,968243,0,0\n")
    return d


# ⛔ THE CONTROL REMOVES THE SHIPPED FILTER, in the real function, by neutering the lookup it uses.
if CONTROL:
    _orig_read_pods = collect.read_pods

    def no_filter(rd):
        # CONTROL: an empty pods map makes `if pods and name not in pods` fall through, which is
        # exactly the pre-fix behaviour -- every CSV in stream/ becomes a card.
        return {}

    collect.read_pods = no_filter


def cards_of(d):
    return collect.read_streams(d, time.time(), {})


# ── 1. ⛔ THE CENTRAL CASE. 16 rented, 23 capture files: the 7 releases must not appear ───────────
RENTED = [f"hz-smoke-{i}" for i in (12, 14, 16, 17, 18, 19, 20, 21, 22, 23, 24, 26, 3, 5, 6, 7)]
RELEASED = [f"hz-smoke-{i}" for i in (1, 2, 4, 8, 9, 10, 11)]
# ⚠ THE RELEASED PODS ARE SILENT, because that is what a released pod is: it was terminated, so
# its capture stopped and only the stale file remains. A fixture that keeps them streaming would
# show them as `up` and never reproduce the `7 down` the live frame actually printed.
cards = cards_of(rundir(rented=RENTED, csvs=RENTED + RELEASED, silent=RELEASED))
names = {c["name"] for c in cards}

check(len(cards) == 16,
      f"the fleet is the 16 cards being paid for, not the 23 capture files (got {len(cards)})")
check(not (names & set(RELEASED)),
      f"no released pod appears at all (leaked: {sorted(names & set(RELEASED)) or 'none'})")
check(sum(1 for c in cards if not c["up"]) == 0,
      f"and nothing reads as down — every rented card is streaming "
      f"({sum(1 for c in cards if not c['up'])} down)")

# ── 2. ⛔ hazync#476 MUST SURVIVE. A RENTED card that went quiet is still down, and still costs ───
cards = cards_of(rundir(rented=RENTED, csvs=RENTED + RELEASED,
                        silent=tuple(RELEASED) + ("hz-smoke-14",)))
names = {c["name"] for c in cards}
down = [c["name"] for c in cards if not c["up"]]

check("hz-smoke-14" in names,
      "a rented card that stopped streaming is STILL on the frame — it is still being paid for")
check(down == ["hz-smoke-14"],
      f"and it is the ONLY card reading down (got {down})")
check(len(cards) == 16,
      f"the denominator is still the rented fleet, 16 (got {len(cards)})")

# ── 3. the fallback: no pods.txt at all must not blank the frame ─────────────────────────────────
d = rundir(rented=[], csvs=RENTED[:3])
cards = cards_of(d)
check(len(cards) == 3,
      f"with no pods.txt, every capture still renders rather than nothing (got {len(cards)})")

# ⚠ NAME EVERY ONE AND COUNT THEM. A subset lets an unrelated breakage ride along inside a
# "CONTROL OK". Removing one filter wrecks FIVE readouts, which is the measure of how far a single
# wrong card list propagates: the fleet size, the names on the lanes, the down tally, which cards
# the down tally names, and the denominator every per-card figure is drawn against.
EXPECTED_CONTROL = {
    "the fleet is the 16 cards being paid for",
    "no released pod appears at all",
    "and nothing reads as down",
    "and it is the ONLY card reading down",
    "the denominator is still the rented fleet",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — without the pods.txt filter, 7 deliberate releases came back as failed "
              "cards:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly {len(EXPECTED_CONTROL)} failures; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("released pods leave the frame; a rented card that goes quiet stays on it")
