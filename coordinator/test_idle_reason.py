#!/usr/bin/env python3
"""An idle says what it is actually waiting for (hazync#505).

⛔ WHY THIS EXISTS. A --fresh-tip session waiting for the chain printed, every 30 seconds:

    [20:53:03]   block 968315 verified in 1012.0s on 15 cards, digest c38429b0
    [20:53:03]   idle: the board has nothing free right now
    ...  x12  ...
    [20:58:56]   proving 968316 (attempt 1)

Read live during the 2026-09-23 flagship, that was taken to mean a run that proved one block and
gave up. It was a run working perfectly: waiting for the chain to mine 968,316, which it then
proved.

⛔ AND THE SENTENCE WAS NOT MERELY VAGUE, IT WAS FALSE. It belongs to the board-claim path -- "no
board work is free, another key holds it" -- and that path had not been taken. The one thing the
message asserted was the one thing that did not happen. An idle that states the wrong cause is
worse than one that states none, because it is actionable in the wrong direction.

  python3 test_idle_reason.py            # must PASS
  python3 test_idle_reason.py --control  # the single fixed sentence is restored; MUST show
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_controller                                                        # noqa: E402
import tip_session                                                           # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def plan(work):
    state = tip_session.new_state(started_at=0.0, duration_s=3600)
    if CONTROL:
        # ⛔ THE OLD RULE: one fixed sentence for every idle, whatever the caller said.
        work = dict(work or {})
        work.pop("why", None)
    return tip_session.plan_next(state, 10.0, work=work)


# ── 1. the board's own idle still says the board, because there it is true ──────────────────────
p = plan(tip_controller.next_work(None, lambda: None))
check(p["action"] == "idle" and "board has nothing free" in p["why"],
      f"a genuine board-busy idle still says so ({p['why']!r})")

# ── 2. a caller that knows better is believed ───────────────────────────────────────────────────
waiting = {"source": "idle", "range": None,
           "why": "waiting for the chain — bridge tip 968315, floor 968315"}
p = plan(waiting)
check(p["action"] == "idle" and "waiting for the chain" in p["why"],
      f"a chain-wait idle says it is waiting for the CHAIN ({p['why']!r})")
check("board has nothing free" not in p["why"],
      "⛔ and does not also claim the board was asked — it was not")
check("968315" in p["why"],
      "and names the height, so the wait is self-explanatory to someone watching")

# ── 3. a caller that supplies nothing still gets a sentence ─────────────────────────────────────
p = plan({"source": "idle", "range": None})
check(p["action"] == "idle" and p["why"],
      f"an idle with no stated reason still reads sensibly ({p['why']!r})")

# ── 4. ⛔ AND THE DRIVER MUST ACTUALLY SUPPLY ONE. A reason field nothing fills is decoration ────
_src = open(os.path.join(HERE, "tip_smoke.py")).read()
check('"why": f"waiting for the chain' in _src,
      "the driver supplies a chain-wait reason for the --no-board-fill idle")
check('out["why"] = (f"waiting for the chain' in _src,
      "and for the case where the board is empty AND the chain has not moved")
_ctl = open(os.path.join(HERE, "tip_controller.py")).read()
check('"why": "the board has nothing free right now"' in _ctl,
      "and next_work carries the board's own reason rather than relying on the session's default")

EXPECTED_CONTROL = {"a chain-wait idle says it is waiting for the CHAIN",
                    "⛔ and does not also claim the board was asked",
                    "and names the height"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with one fixed sentence restored, a run waiting for the chain reports "
              "that the board is busy:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("an idle names what it is waiting for, and does not invent a question it never asked")
