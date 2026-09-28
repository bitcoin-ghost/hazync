#!/usr/bin/env python3
"""The session's hour starts at the first TIP block, not when the fleet is ready (hazync#553).

⛔ WHAT THIS EXISTS FOR. The 2026-09-28 tip hour opened its window the moment 17 cards passed the
gates, and then waited for the chain to mine a block above the --fresh-tip floor:

    12:50:35  SESSION · 1.0 h on 17 cards, budget $150.00
    12:50:35  FRESH TIP: floor set at 968982
    12:50:36    proving 131892 (attempt 1)          <- board fill, not a tip block
    ...         eight board blocks, 14+ minutes     <- the hour draining

Every one of those minutes came out of the hour. `--fresh-tip` deliberately makes the session WAIT
for a block that did not exist when it booted -- that is what makes the measurement honest -- so the
two flags together guaranteed that a chunk of the window was spent before the thing being measured
existed. The published figure would have been "blocks proved in one hour" where the hour was however
much of it the chain left over, which is a number about Bitcoin's block interval, not about a fleet.

⚠ THE BILL IS NOT DEFERRED, ONLY THE CLOCK. The fleet bills from the moment it is rented and the
budget is checked against the whole spend. `summary` reports the wait and its cost separately so the
hour's cost can be quoted without hiding boot inside it, and without pretending boot was free.

    python3 test_clock_from_tip.py             # the clock starts at the first tip block
    python3 test_clock_from_tip.py --control   # clock armed at boot — the wait must eat the hour
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_session as S  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0

HOUR = 3600.0
T0 = 1_000_000.0
WAIT_S = 840.0          # 14 min, as measured live
BOARD_WALL = 60.0
TIP_WALL = 300.0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


# ── 1. the pure clock ────────────────────────────────────────────────────────────────────────────
unarmed = S.new_state(started_at=T0, duration_s=HOUR, armed=False, arm_deadline_s=2 * HOUR)
check(S.is_armed(unarmed) is False, "a session built with armed=False is not armed")
check(S.remaining_s(unarmed, T0 + WAIT_S) == HOUR,
      f"after {WAIT_S/60:.0f} min of waiting the whole hour is still ahead "
      f"(got {S.remaining_s(unarmed, T0 + WAIT_S)/60:.1f} min)")
check(S.elapsed_s(unarmed, T0 + WAIT_S) == 0.0, "nothing of the window is spent while unarmed")
check(round(S.waited_s(unarmed, T0 + WAIT_S), 1) == WAIT_S,
      "the wait itself is measured, so it can be priced")

check(S.arm_clock(unarmed, T0 + WAIT_S) is True, "the first arm_clock starts the window")
check(S.arm_clock(unarmed, T0 + WAIT_S + 999) is False,
      "a second arm_clock does NOT restart it — a resumed session must not get a fresh hour")
check(round(S.remaining_s(unarmed, T0 + WAIT_S), 1) == HOUR,
      "the full hour is ahead at the moment of arming")
check(round(S.waited_s(unarmed, T0 + WAIT_S + 999), 1) == WAIT_S,
      "the wait is frozen once the clock starts")

# ⛔ AN OLD SESSION FILE HAS NO armed_at AT ALL, and must not read as unarmed — that would hand a
# resumed session a brand-new hour it has already spent.
legacy = {"started_at": T0, "duration_s": HOUR, "blocks": {}, "attempts": {}, "fleet": [],
          "spend_usd": 0.0}
check(S.clock_origin(legacy) == T0, "a session file from before this feature reads as armed at boot")
check(round(S.remaining_s(legacy, T0 + WAIT_S), 1) == round(HOUR - WAIT_S, 1),
      "so resuming an old file does not reset its window")

# ── 2. the bound on waiting ──────────────────────────────────────────────────────────────────────
stuck = S.new_state(started_at=T0, duration_s=HOUR, armed=False, arm_deadline_s=2 * HOUR)
p = S.plan_next(stuck, T0 + 2 * HOUR - 1, work={"source": "board", "range": "7"})
check(p["action"] == "prove", "inside the wait limit the session keeps working")
p = S.plan_next(stuck, T0 + 2 * HOUR + 1, work={"source": "board", "range": "7"})
check(p["action"] == "stop" and "waited" in p["why"],
      f"past the wait limit it stops rather than hold a fleet for ever (got {p['action']})")
never = S.new_state(started_at=T0, duration_s=HOUR, armed=False, arm_deadline_s=None)
check(S.plan_next(never, T0 + 100 * HOUR, work={"source": "board", "range": "7"})["action"] == "prove",
      "⚠ with NO deadline an unarmed session never stops — which is why tip_smoke always sets one")

# ── 3. the whole session, against a fake clock ───────────────────────────────────────────────────
# A tip block becomes available WAIT_S into the session. Before that the board hands out gap work.
def run(armed):
    clock = {"t": T0}
    seq = {"n": 0}
    sourced = {}

    def now():
        return clock["t"]

    def sleep(s):
        clock["t"] += float(s)

    def work_fn():
        seq["n"] += 1
        rng = str(seq["n"])
        sourced[rng] = "tip" if clock["t"] - T0 >= WAIT_S else "board"
        return {"source": sourced[rng], "range": rng}

    def prove(rng):
        wall = TIP_WALL if sourced[rng] == "tip" else BOARD_WALL
        clock["t"] += wall
        return {"ok": True, "wall_s": wall, "digest": "d" * 64, "cards": 17}

    st = S.new_state(started_at=T0, duration_s=HOUR, fleet_ids=["a"],
                     armed=armed, arm_deadline_s=None if armed else 2 * HOUR)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        path = fh.name
    try:
        summ = S.run_session(state=st, path=path, prove=prove, work_fn=work_fn,
                             now=now, sleep=sleep, block_estimate_s=TIP_WALL)
    finally:
        os.unlink(path)
    tip_ok = sum(1 for r, b in st["blocks"].items() if b.get("ok") and sourced.get(r) == "tip")
    return st, summ, tip_ok, clock["t"] - T0


st, summ, tip_ok, life_s = run(armed=CONTROL)

# The arithmetic, stated so a wrong number is obvious: an hour of TIP_WALL blocks is HOUR/TIP_WALL.
FULL = int(HOUR // TIP_WALL)                       # 12
EATEN = int((HOUR - WAIT_S) // TIP_WALL)           # 9

if CONTROL:
    check(tip_ok == EATEN,
          f"control reproduces it: only {tip_ok} tip blocks fitted, not {FULL} — the "
          f"{WAIT_S/60:.0f} min wait took {FULL - tip_ok} blocks out of the hour")
    # ⚠ NOT exactly HOUR: plan_next refuses to start a block it cannot finish, so the run ends up to
    # one TIP_WALL short of the deadline. The point is that it ended relative to BOOT, not to the
    # first tip block — in the real arm it ends WAIT_S later than this.
    check(HOUR - TIP_WALL <= life_s <= HOUR,
          f"control: the session ended {life_s/60:.1f} min after BOOT, so the window covered the wait")
else:
    check(tip_ok == FULL,
          f"the hour held {tip_ok} tip blocks — a full window regardless of how long the chain took "
          f"(expected {FULL})")
    check(round(life_s) == round(WAIT_S + HOUR),
          f"the session lived {life_s/60:.1f} min: {WAIT_S/60:.0f} waiting + a full 60 of window")
    check(summ["armed"] is True, "the summary says the clock started")
    check(round(summ["waited_s"]) == round(WAIT_S),
          f"the summary reports the wait ({summ['waited_s']}s)")
    # ⛔ The board blocks proved during the wait are real work and are counted -- but NOT as the
    # hour's result. Quoting blocks_ok as "blocks in the hour" is the mistake this splits apart.
    check(summ["blocks_ok"] > summ["blocks_ok_on_clock"],
          f"board fill is counted ({summ['blocks_ok']}) but kept out of the hour "
          f"({summ['blocks_ok_on_clock']})")
    check(summ["blocks_ok_on_clock"] == tip_ok,
          f"the on-clock count is exactly the tip blocks ({summ['blocks_ok_on_clock']} vs {tip_ok})")
    check(summ["blocks_ok"] - summ["blocks_ok_on_clock"] == int(WAIT_S // BOARD_WALL),
          f"and the rest are the {int(WAIT_S // BOARD_WALL)} board blocks proved while waiting")

# ── 4. the first tip block is INSIDE the hour it starts ──────────────────────────────────────────
# ⛔ Arming after prove() would give the session its full window PLUS one free block, and the free
# one is the most expensive: TIP_WALL is the longest thing the session does.
if not CONTROL:
    first_tip_at = min(float(b["at"]) for r, b in st["blocks"].items()
                       if b.get("ok") and float(b["at"]) - T0 > WAIT_S)
    check(first_tip_at > float(st["armed_at"]),
          "the first tip block FINISHED after the clock started, so its wall time is inside the hour")
    check(round(float(st["armed_at"]) - T0) == round(WAIT_S),
          f"the clock is stamped at the moment that block began, not when it ended")

# ── 5. the spend split ───────────────────────────────────────────────────────────────────────────
# ⚠ MEASURED FROM THE SAME spend_fn THE SESSION USES, not recomputed here. The budget is checked
# against the TOTAL; the split exists so the hour's cost can be quoted without pretending the wait
# was free (it was ~$9 of the 2026-09-28 fleet) or folding it into the hour.
sp = S.new_state(started_at=T0, duration_s=HOUR, armed=CONTROL,
                 arm_deadline_s=None if CONTROL else 2 * HOUR)
S.add_spend(sp, 9.04)                       # the wait
S.arm_clock(sp, T0 + WAIT_S)                # a no-op in the control: it is already armed
S.add_spend(sp, 31.50)                      # the hour
s2 = S.summary(sp, T0 + WAIT_S + HOUR)
if CONTROL:
    check(s2["spend_before_clock_usd"] == 0.0 and s2["spend_on_clock_usd"] == 40.54,
          f"control reproduces it: the ${9.04:.2f} wait is charged to the hour "
          f"(before=${s2['spend_before_clock_usd']:.2f} on-clock=${s2['spend_on_clock_usd']:.2f})")
else:
    check(s2["spend_usd"] == 40.54, f"the total is the whole bill (got {s2['spend_usd']})")
    check(s2["spend_before_clock_usd"] == 9.04,
          f"the wait is priced separately (got {s2['spend_before_clock_usd']})")
    check(s2["spend_on_clock_usd"] == 31.5,
          f"and the hour carries only its own cost (got {s2['spend_on_clock_usd']})")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")
