#!/usr/bin/env python3
"""One slow card must not hold a ready fleet in the prover-fetch gate (hazync#503).

⛔ WHY THIS EXISTS — measured on the 2026-09-23 flagship run:

    PREPARING · fetching the prover (410 MB) onto 18 cards      ... for EIGHTEEN MINUTES
    17 of 18 cards   410,441,528 / 410,441,528   (seconds)
    hz-smoke-13       69,414,912 / 410,441,528   +4,210,688 bytes in 45 s = 93 KB/s, ~60 min left

Neither existing guard could see it. The stall check asks "did the byte count move" and it was
moving the whole time; the ceiling asks "have 40 minutes passed" and it had not. So 17 ready cards
billed $13.32/hr at 16 W of GPU each while one card trickled, and the only thing that cleared the
gate was terminating the pod by hand — killing the curl did nothing, because `-C -` resumes.

The fix asks a third question, and asks it of the FLEET: is this card going to arrive before the
cards already waiting for it have been paid for again? A card that is not gets dropped, but ⛔ only
while enough others remain to run without it, because the previous fix here (hazync#479) exists to
stop a merely-slow card killing a run that has no spare to swap in.

    python3 test_slow_card_gate.py            # must PASS
    python3 test_slow_card_gate.py --control  # the fleet view is withheld; MUST FAIL

⚠ THE CONTROL CHANGES ONE THING. Same fake card, same byte rate, same poll interval — only
`fleet=` is withheld, which is exactly the old code. What it measures is the thing the incident
cost: how long the gate stays shut.
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_smoke                                                             # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


WANT = 410_441_528
POLL = 15                       # the production wait_s


class Card:
    def __init__(self, cid):
        self.cid = cid


class Clock:
    """A fake clock, so a 40-minute ceiling can be measured without waiting 40 minutes.

    ⚠ `fetch_binary` reads the clock to decide, and the whole question here is WHEN it decides.
    A real clock with `wait_s=0` would make every rate look infinite and the guard would never
    fire — the test would pass while testing nothing.
    """

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class SlowCard:
    """A card that advances `per_poll` bytes per poll, and never stops.

    `on_poll` lets the rest of the fleet finish at a chosen moment, which is what makes this a
    fleet-relative test rather than a per-card one.
    """

    def __init__(self, per_poll, on_poll=None):
        self.per_poll, self.on_poll = per_poll, on_poll
        self.n, self.polls = 0, 0

    def run(self, card, body, env=None, timeout=None):
        if "stat -c%s" in body:
            self.polls += 1
            if self.on_poll:
                self.on_poll(self.polls)
            out = str(self.n)
            self.n = min(WANT, self.n + self.per_poll)
            return out + "\n"
        return "OK\n"


def run_gate(ssh, fleet, clock):
    """Run one card through the gate on the fake clock, and report how long the gate stayed shut."""
    real = tip_smoke.time
    tip_smoke.time = clock
    try:
        t0 = clock.t
        ok, got = tip_smoke.fetch_binary(ssh, Card("hz-smoke-13"), WANT,
                                         wait_s=POLL, fleet=fleet)
        return ok, got, clock.t - t0
    finally:
        tip_smoke.time = real


# ── 1. ⛔ THE INCIDENT. 18 cards, floor 15; 17 land at once, one pulls at 93 KB/s ────────────────
fleet = None if CONTROL else tip_smoke.FetchFleet(18, 15)


def seventeen_land(poll):
    # The fast cards complete during the first sleep, exactly as they did on the night: the gate
    # has everything it needs from poll 2 onwards, and is waiting on one card.
    if poll == 2 and fleet is not None:
        for i in range(17):
            fleet.completed(f"hz-smoke-{i}")


slow = SlowCard(per_poll=93_000 * POLL, on_poll=seventeen_land)      # 93 KB/s, the measured rate
ok, got, held = run_gate(slow, fleet, Clock())

check(not ok, f"the 93 KB/s card is abandoned ({got:,} of {WANT:,} bytes)")
check(held <= 60,
      f"and the gate stays shut for {held:.0f} s, not the 2,400 s ceiling "
      f"({slow.polls} polls)")
if not CONTROL:
    why = fleet.reasons.get("hz-smoke-13", "")
    check("93 KB/s" in why or "KB/s" in why,
          f"and the run says WHY, not just INCOMPLETE — {why!r}")

# ── 2. a card that will arrive inside the grace is NOT dropped ───────────────────────────────────
# ⚠ THE GUARD MUST NOT JUST DROP THE LAST CARD. 4 MB/s finishes ~88 s after the fleet is ready,
# inside the 120 s grace, and waiting for it is cheaper than re-renting.
fleet2 = tip_smoke.FetchFleet(18, 15)


def land2(poll):
    if poll == 2:
        for i in range(17):
            fleet2.completed(f"hz-smoke-{i}")


nearly = SlowCard(per_poll=60_000_000, on_poll=land2)               # 4 MB/s
ok2, got2, held2 = run_gate(nearly, fleet2, Clock())
check(ok2, f"a 4 MB/s card that lands inside the grace is waited for ({got2:,} bytes, {held2:.0f} s)")
check("hz-smoke-13" not in fleet2.reasons, "and is not recorded as dropped")

# ── 3. ⛔ THE hazync#479 GUARANTEE: with no spare, nothing is cut short ──────────────────────────
# 15 cards against a floor of 15. Dropping any one of them ends the run, so the slow card gets the
# old patient treatment and rides the ceiling, exactly as it did before this change.
fleet3 = tip_smoke.FetchFleet(15, 15)
for i in range(14):
    fleet3.completed(f"hz-smoke-{i}")
check(fleet3.abandon("hz-smoke-13", "slow") is False,
      "a fleet with no spare refuses to drop a card (15 rented, 15 needed)")

fleet4 = tip_smoke.FetchFleet(16, 15)
check(fleet4.abandon("a", "slow") is True, "one spare allows exactly one card to be dropped")
check(fleet4.abandon("b", "slow") is False, "and the second is refused — survivors, not casualties")

# ── 4. the clock starts when the fleet could RUN, not when the first card lands ──────────────────
fleet5 = tip_smoke.FetchFleet(18, 15)
for i in range(14):
    fleet5.completed(f"c{i}")
check(fleet5.grace_left() is None,
      "14 of a needed 15 done: no grace clock yet, so no card is judged against it")
fleet5.completed("c14")
left = fleet5.grace_left()
check(left is not None and left > 0, f"the 15th card starts the clock ({left:.0f} s of grace)")

# ── 5. never on a single reading ─────────────────────────────────────────────────────────────────
# ⚠ The loop polls BEFORE it starts the curl, so every card reads 0 bytes at t=0. Condemning a
# card on that reading would drop healthy cards for not having started yet.
fleet6 = tip_smoke.FetchFleet(18, 15)
for i in range(17):
    fleet6.completed(f"c{i}")
dead = SlowCard(per_poll=0)                                          # never writes a byte
ok6, got6, held6 = run_gate(dead, fleet6, Clock())
check(not ok6 and got6 == 0, "a card that never starts IS dropped once the fleet is ready")
check(dead.polls >= 2,
      f"but not on its first reading — it took {dead.polls} polls to make the number a rate")

print()

# ⛔ A POSITIVE CONTROL, THE WAY THIS REPO DOES THEM. The control step in CI must EXIT 0, so it is
# not enough for the run to fail -- it has to fail in the ONE place the fix is supposed to change.
# A control that merely "went red" would also go red on a typo, an import error, or a broken fake,
# and would then be reported as proof that the guard works.
EXPECTED_CONTROL = {"the gate stays shut"}

if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the fleet view withheld, one card holds the gate for the full "
              "40-minute ceiling again:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected only the gate-time assertion to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("a slow card costs a card, not eighteen minutes of a ready fleet")
