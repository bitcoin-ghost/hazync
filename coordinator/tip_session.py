#!/usr/bin/env python3
"""A 24-hour proving session: many blocks, one fleet, one dashboard (Phase 5 ⑩).

`tip_run.run_block` proves ONE block. This is the layer above it — what to prove next, when to grow
or shrink the fleet, when to stop, and how to pick up again after the driver dies. The decisions are
pure functions over a state dict so a whole simulated day can be exercised in milliseconds; the
orchestration that calls them is deliberately thin.

⛔ A SESSION THAT CANNOT RESUME IS WORSE THAN ONE THAT NEVER STARTED. A 24-hour run that dies at hour
19 and silently begins again from zero spends another day's GPU rental to produce the first hour's
work twice, and looks completely healthy while doing it. The bridge backfill already cost us exactly
this: `Restart=on-failure` with no resume sent a 76%-complete walk back to genesis, and the unit
reported `activating (start)` throughout. Every completed block is written to disk the moment it
verifies, and a restart re-reads them.

⛔ THE PROTECTED PODS ARE IN CODE, NOT IN CONFIG. `hz370-a` is the anchoring spine worker and
`hz-board-5` is proving as GHOST; both belong to other work that a tip session must never touch. A
denylist in a config file is a denylist that a fresh checkout does not have.

⛔ THERE IS NO BUDGET CAP, BY DECISION — so spend is not a limit here, it is an OBLIGATION to report.
A session that loses track of what it is spending is the one failure the operator cannot see coming.
"""

import json
import os
import tempfile

# ⛔ NEVER TERMINATE THESE, whatever a plan says. Named, in code, on purpose.
#   hz370-a     the anchoring spine worker    ("No it's the anchoring spine worker leave it alone")
#   hz-board-5  actively proving as GHOST
PROTECTED = frozenset({"hz370-a", "hz-board-5"})

# A block that keeps failing must not consume the session. After this many attempts it is recorded as
# failed and the session moves on -- the alternative is a 24-hour run that proves one block zero times.
MAX_ATTEMPTS = 3

# ⛔ A BAD BLOCK AND A BAD FLEET NEED DIFFERENT EVIDENCE. MAX_ATTEMPTS asks "is THIS block bad?" and
# is right to keep going afterwards -- one bad block must not end a 24-hour run. Nothing asked "is
# the FLEET bad?", and the answer is a different observation: N blocks in a row failing, whichever
# blocks they are. Without it, a dead aggregate makes every block fail, each is retired after 3
# tries, the loop claims another, and the session pays for a fleet that cannot prove anything --
# taking a board claim each time and holding it for its TTL (hazync#443).
# ⚠ Consecutive FAILURES, never slowness: the slowest legitimate block measured 226.7 s against a
# 29.7 s median, and a session that gives up on a slow block is worse than one that waits.
MAX_CONSECUTIVE_FAILS = int(os.environ.get("HAZYNC_MAX_CONSECUTIVE_FAILS", "3"))


class SessionRefused(RuntimeError):
    """A session-level gate said no."""


def new_state(*, started_at, duration_s, fleet_ids=()):
    return {"started_at": float(started_at),
            "duration_s": float(duration_s),
            "blocks": {},            # range -> {"ok", "wall_s", "digest", "cards", "at"}
            "attempts": {},          # range -> int
            "fleet": sorted(str(f) for f in fleet_ids),
            "spend_usd": 0.0}


def save(path, state):
    """Atomically. ⛔ A half-written session file reads as a session that never happened."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".session.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(path):
    """The state on disk, or None. A corrupt file is None — never a silently empty session.

    ⚠ Returning `new_state()` on a parse error would be the dangerous kindness: the session would
    resume having forgotten every block it proved, and re-prove the lot at full cost. The caller is
    told nothing is there and can decide.
    """
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or "started_at" not in state or "blocks" not in state:
        return None
    return state


def is_protected(pod_id):
    return str(pod_id) in PROTECTED


def terminable(pod_ids, session_fleet):
    """Which pods this session may terminate: its OWN, minus the protected ones.

    ⛔ TWO INDEPENDENT GATES, AND BOTH ARE NEEDED. The denylist catches the two pods we know about by
    name; the fleet check catches every pod we do not — another session's cards, a colleague's box, a
    pod rented by hand five minutes ago. A session may only ever destroy what it created.

    Returns `{"terminate": [...], "protected": [...], "not_ours": [...]}` — the refusals are returned
    rather than dropped, because a pod we declined to terminate is a pod that is still being billed.
    """
    fleet = {str(f) for f in session_fleet}
    out = {"terminate": [], "protected": [], "not_ours": []}
    for pid in pod_ids:
        p = str(pid)
        if is_protected(p):
            out["protected"].append(p)
        elif p not in fleet:
            out["not_ours"].append(p)
        else:
            out["terminate"].append(p)
    for k in out:
        out[k].sort()
    return out


def elapsed_s(state, now):
    return max(0.0, float(now) - float(state["started_at"]))


def remaining_s(state, now):
    return float(state["duration_s"]) - elapsed_s(state, now)


def done_blocks(state):
    return {r: b for r, b in state["blocks"].items() if b.get("ok")}


def plan_next(state, now, *, work, block_estimate_s=None, budget_usd=None):
    """What the session does next. `work` is `tip_controller.next_work(...)`'s verdict.

    Returns one of:
        {"action": "prove",  "range": ...}
        {"action": "idle",   "why": ...}     the board had nothing free; wait and ask again
        {"action": "stop",   "why": ...}
    """
    left = remaining_s(state, now)
    if left <= 0:
        return {"action": "stop", "why": f"the {state['duration_s']/3600:.0f}-hour session is over"}
    # ⛔ THE BUDGET IS CHECKED BEFORE THE NEXT BLOCK, NOT AFTER IT. Checking afterwards means the
    # block that crosses the line has already been paid for in full.
    spent = float(state.get("spend_usd", 0.0))
    if budget_usd and spent >= budget_usd:
        return {"action": "stop", "why": f"budget spent: ${spent:.2f} of ${budget_usd:.2f}"}

    rng = (work or {}).get("range")
    if rng is None:
        return {"action": "idle", "why": "the board has nothing free right now"}
    rng = str(rng)

    # ⛔ NEVER RE-PROVE A BLOCK THIS SESSION ALREADY PROVED. On a resume the coordinator may hand back
    # the same block -- a claim that has not expired, or the tip not having moved -- and proving it
    # again costs a full block of GPU for a receipt that already exists.
    if state["blocks"].get(rng, {}).get("ok"):
        return {"action": "idle", "why": f"block {rng} is already proved in this session"}

    if state["attempts"].get(rng, 0) >= MAX_ATTEMPTS:
        return {"action": "idle",
                "why": f"block {rng} has failed {MAX_ATTEMPTS} times and is not being retried — a "
                       f"session must not spend itself on one block"}

    # ⛔ DO NOT START A BLOCK THE SESSION CANNOT FINISH. Starting one with four minutes left rents the
    # fleet through the whole block and throws the result away at the deadline; the cards are billed
    # either way. Stopping early is the cheaper mistake, and it is the reversible one.
    if block_estimate_s and left < block_estimate_s:
        return {"action": "stop",
                "why": f"{left/60:.1f} min left but a block takes ~{block_estimate_s/60:.1f} min — "
                       f"starting one now would pay for it in full and discard it"}

    return {"action": "prove", "range": rng}


def record_attempt(state, rng):
    rng = str(rng)
    state["attempts"][rng] = state["attempts"].get(rng, 0) + 1
    return state["attempts"][rng]


def record_block(state, rng, result, *, at):
    """Record one block's outcome. Called the moment it verifies, not at the end of the session."""
    rng = str(rng)
    state["blocks"][rng] = {"ok": bool(result.get("ok")),
                            "wall_s": result.get("wall_s"),
                            "digest": result.get("digest"),
                            "cards": result.get("cards"),
                            "events": result.get("events") or [],
                            "at": float(at)}
    return state["blocks"][rng]


def add_spend(state, usd):
    """⛔ ACCUMULATE, NEVER REPLACE. The fleet changes size mid-session, so a recomputed
    `cards x rate x elapsed` is wrong the moment a card is added or released -- and it is wrong
    quietly, in whichever direction the last change went."""
    state["spend_usd"] = round(float(state.get("spend_usd", 0.0)) + float(usd), 4)
    return state["spend_usd"]


def summary(state, now):
    ok = done_blocks(state)
    failed = {r: b for r, b in state["blocks"].items() if not b.get("ok")}
    walls = sorted(b["wall_s"] for b in ok.values() if b.get("wall_s"))
    el = elapsed_s(state, now)
    return {"blocks_ok": len(ok),
            "blocks_failed": len(failed),
            "failed_ranges": sorted(failed),
            "elapsed_s": round(el, 1),
            "remaining_s": round(remaining_s(state, now), 1),
            "median_wall_s": walls[len(walls) // 2] if walls else None,
            "fastest_wall_s": walls[0] if walls else None,
            "spend_usd": round(float(state.get("spend_usd", 0.0)), 4),
            "usd_per_block": round(float(state.get("spend_usd", 0.0)) / len(ok), 4) if ok else None}


def resume_verdict(state, live_pod_ids, now):
    """Whether a loaded session can be picked up, and what changed while the driver was away.

    ⛔ A RESUMED SESSION MUST NOT ASSUME ITS FLEET SURVIVED. Pods are evicted, terminated and lost;
    carrying on against a recorded fleet that no longer exists means every block fails for a reason
    that looks like a code fault. And the cards that DID survive have been billing the whole time the
    driver was dead, which is the part that is easy to miss.
    """
    recorded = set(state.get("fleet") or ())
    live = {str(p) for p in live_pod_ids}
    gone, extra = sorted(recorded - live), sorted(live - recorded)
    left = remaining_s(state, now)
    if left <= 0:
        return {"ok": False, "why": "the session's window has already closed",
                "gone": gone, "extra": extra, "remaining_s": round(left, 1)}
    if recorded and not (recorded & live):
        return {"ok": False,
                "why": f"none of the session's {len(recorded)} recorded pods are still alive — this "
                       f"is a new fleet, not a resume",
                "gone": gone, "extra": extra, "remaining_s": round(left, 1)}
    return {"ok": True, "gone": gone, "extra": extra, "remaining_s": round(left, 1),
            "blocks_ok": len(done_blocks(state))}


def run_session(*, state, path, prove, work_fn, now, sleep,
                feed=None, idle_s=30.0, block_estimate_s=None, on_event=None,
                budget_usd=None, spend_fn=None):
    """Drive a whole session. Thin on purpose — every decision above is a pure function.

    `prove(rng)` proves one block and returns `tip_run.run_block`'s dict, or raises.
    `work_fn()` returns `tip_controller.next_work(...)`'s verdict.

    ⛔ THE STATE IS SAVED AFTER EVERY CHANGE, NOT AT THE END. A session file written on the way out is
    a session file that never gets written, because the case it exists for is the driver not reaching
    the end. Saving costs a few milliseconds per block against a day of GPU rental.

    ⛔ A BLOCK THAT RAISES IS RECORDED AND THE SESSION CONTINUES. One bad block must not end a
    24-hour run -- but it is counted, so `MAX_ATTEMPTS` can retire it and the summary can name it.
    """
    def emit(msg):
        if on_event:
            on_event(msg)

    consecutive_fails = 0
    while True:
        t = now()
        # ⛔ CHARGE BEFORE DECIDING, AND ON EVERY LOOP — NOT PER BLOCK. Pods bill continuously:
        # claiming, fetching a bundle, submitting and waiting on a busy board all cost money, and
        # none of it is inside any block's wall_s. Measured 2026-09-21: 42 blocks charged $0.961
        # against ~$1.13 actually billed, a 17% undercount, so a $6.00 cap would have stopped at
        # roughly $7.05 spent. `spend_fn()` now takes NO argument and returns what has accrued
        # since it was last called, which is also what keeps it right when the fleet changes size.
        if spend_fn is not None:
            try:
                add_spend(state, spend_fn())
            except Exception:
                pass                      # accounting must never stop a session
        # ⚠ A CALLABLE ESTIMATE IS RE-ASKED EVERY LOOP. A fixed number cannot learn: this session
        # measured a median of 30.0 s and a MAX of 226.7 s, so a median-based guard would have let
        # the slowest block overrun its window by minutes.
        est = block_estimate_s() if callable(block_estimate_s) else block_estimate_s
        plan = plan_next(state, t, work=work_fn(), block_estimate_s=est, budget_usd=budget_usd)

        if plan["action"] == "stop":
            emit(f"stopping: {plan['why']}")
            break

        if plan["action"] == "idle":
            emit(f"idle: {plan['why']}")
            # ⛔ THE DEADLINE STILL APPLIES WHILE IDLE. An idle session whose board never frees a
            # block would otherwise sleep past its own window, holding a rented fleet for hours with
            # nothing to show. Re-checking the clock at the top of the loop is what makes that safe,
            # so the sleep must be bounded by what is left.
            left = remaining_s(state, t)
            if left <= 0:
                emit("stopping: the session window closed while waiting for work")
                break
            sleep(min(idle_s, left))
            continue

        rng = plan["range"]
        n = record_attempt(state, rng)
        save(path, state)
        emit(f"proving {rng} (attempt {n})")

        try:
            result = prove(rng)
        except Exception as exc:                      # noqa: BLE001 -- a bad block is not a bad run
            result = {"ok": False, "events": [f"{type(exc).__name__}: {exc}"]}
            emit(f"block {rng} failed: {type(exc).__name__}: {exc}")

        record_block(state, rng, result, at=now())
        save(path, state)
        # The fleet verdict, distinct from the per-block one above.
        if result.get("ok"):
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                emit(f"stopping: {consecutive_fails} blocks failed in a row — this is the FLEET, not "
                     f"the blocks. Releasing rather than paying for cards that cannot prove.")
                break

        if result.get("ok"):
            emit(f"block {rng} verified in {result.get('wall_s')}s "
                 f"on {result.get('cards')} cards, digest {(result.get('digest') or '')[:8]}")
            if feed is not None:
                st = feed.staleness(now())
                if not st["ok"]:
                    emit(f"⚠ telemetry gaps: stale={st['stale']} never-seen={st['never']}")

    return summary(state, now())
