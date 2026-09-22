#!/usr/bin/env python3
"""A session must not idle its whole window on work it has already exhausted (hazync#464).

⛔ WHY. `plan_next` idles for two reasons and only one can resolve itself:

    kind="board"      the board has nothing free — another contributor finishes, a claim expires,
                      the tip moves. Waiting is correct.
    kind="exhausted"  WE are refusing: already proved this session, or MAX_ATTEMPTS reached.
                      Re-asking cannot change the answer.

The second is reachable: attempt counts are LOCAL to the session, and the board does not park
failing blocks at all (#460 — 0 failed ranges across 123,331 proven blocks), so it hands the same
block back for ever. At 26x RTX 4090 = $19.24/hr a 24-hour session pinned this way burns ~$462 for
zero blocks, and `--budget-usd` is optional.

⚠ THE SECOND CHECK IS THE LOAD-BEARING ONE. Counting a *board* idle would stop a session early
whenever the board is merely busy — turning a fix into a worse bug.

  python3 test_session_idle_cap.py            # assertions; exit 0 on success
  python3 test_session_idle_cap.py --control  # the cap removed; MUST fail
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tip_session as S  # noqa: E402


def drive(work_seq, control=False, duration_s=24 * 3600, pre_attempts=None):
    """Run a session against a scripted board. Returns (events, loops)."""
    events, clock = [], {"t": 1_000_000.0}
    state = S.new_state(started_at=clock["t"], duration_s=duration_s)
    # ⚠ Pre-exhaust the block rather than failing it here: proving it would trip
    # MAX_CONSECUTIVE_FAILS (the fleet-is-dead stop) before the idle cap is ever reached, and that
    # is a DIFFERENT guard. The case under test is a block already exhausted when the board offers it.
    for rng, n in (pre_attempts or {}).items():
        state["attempts"][rng] = n
    calls = {"n": 0}

    def work_fn(*a, **k):
        i = min(calls["n"], len(work_seq) - 1)
        calls["n"] += 1
        return work_seq[i]

    def prove(rng, **k):
        return {"ok": False, "why": "fleet is dead"}

    def now():
        return clock["t"]

    def sleep(s):
        clock["t"] += max(s, 1.0)          # always advance, so the deadline is reachable
        if calls["n"] > 400:               # a runaway loop must not hang the test
            raise RuntimeError("loop did not terminate")

    saved = S.MAX_CONSECUTIVE_EXHAUSTED_IDLES
    if control:                            # the control removes the cap entirely
        S.MAX_CONSECUTIVE_EXHAUSTED_IDLES = 10 ** 9
    with tempfile.TemporaryDirectory() as d:
        try:
            S.run_session(state=state, path=os.path.join(d, "s.json"), prove=prove,
                          work_fn=work_fn, now=now, sleep=sleep, idle_s=30.0,
                          on_event=lambda m: events.append(m))
        except RuntimeError as e:
            # ⚠ The runaway guard firing IS the finding, not a test error: with no cap the loop is
            # unbounded. Surface it as an event so the caller can assert on it.
            events.append(f"RUNAWAY: {e}")
        finally:
            S.MAX_CONSECUTIVE_EXHAUSTED_IDLES = saved
    return events, calls["n"]


def check_exhausted_idle_stops(control=False):
    """⛔ THE ONE THAT MATTERS: the board hands back the same exhausted block for ever."""
    # the board hands back a block this session has already exhausted, for ever
    ev, loops = drive([{"range": "881457"}] * 400, control=control,
                      pre_attempts={"881457": S.MAX_ATTEMPTS})
    stopped = any("cannot resolve" in e for e in ev)
    if control:
        if stopped:
            return False, "CONTROL DID NOT FAIL: the session still stopped with the cap removed"
        runaway = any(e.startswith("RUNAWAY") for e in ev)
        return True, ("control: with no cap the session never stops — it ran to the loop guard"
                      if runaway else f"control: no cap, no stop ({loops} loops)")
    if not stopped:
        return False, (f"the session idled {loops} times on an exhausted block and never stopped — "
                       "a rented fleet would bill for the whole window")
    return True, f"stopped after a bounded run of unresolvable idles ({loops} loops)"


def check_board_idle_does_not_count(control=False):
    """⚠ LOAD-BEARING: a merely-busy board must NOT trip the cap."""
    # ⚠ A SHORT window, so the deadline is actually reachable inside the loop guard.
    ev, loops = drive([{"range": None}] * 400, duration_s=600)
    if any("cannot resolve" in e for e in ev):
        return False, ("a board with nothing free tripped the exhausted-idle cap — this would stop "
                       "a session early whenever the board is simply busy")
    if not any("window closed" in e or "session is over" in e for e in ev):
        return False, f"the session neither stopped on the deadline nor ran: {ev[-2:]}"
    return True, "a busy board idles to the deadline, as it should"


def check_kinds_are_tagged(control=False):
    """Every idle must say which kind it is, or the counter cannot tell them apart."""
    st = S.new_state(started_at=0.0, duration_s=3600)
    p = S.plan_next(st, 1.0, work={"range": None})
    if p.get("kind") != "board":
        return False, f"a board idle is not tagged: {p}"
    st["blocks"]["5"] = {"ok": True}
    p = S.plan_next(st, 1.0, work={"range": "5"})
    if p.get("kind") != "exhausted":
        return False, f"an already-proved idle is not tagged exhausted: {p}"
    st2 = S.new_state(started_at=0.0, duration_s=3600)
    st2["attempts"]["7"] = S.MAX_ATTEMPTS
    p = S.plan_next(st2, 1.0, work={"range": "7"})
    if p.get("kind") != "exhausted":
        return False, f"a MAX_ATTEMPTS idle is not tagged exhausted: {p}"
    return True, "board / exhausted idles are distinguishable"


CHECKS = [check_exhausted_idle_stops, check_board_idle_does_not_count, check_kinds_are_tagged]
CONTROL_MUST_FAIL = {check_exhausted_idle_stops}


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        try:
            ok, why = fn(control=control)
        except Exception as e:                      # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"
        if control and fn not in CONTROL_MUST_FAIL:
            continue
        print(f"  {'ok  ' if ok else 'FAIL'} {fn.__name__}: {why}")
        bad += 0 if ok else 1
    if bad:
        print(f"\n{bad} check(s) wrong")
        return 1
    print("\nan unresolvable idle stops the session" if not control else "\ncontrol behaved as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
