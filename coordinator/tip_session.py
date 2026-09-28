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

# ⛔ AN IDLE THAT CANNOT END MUST NOT RUN THE CLOCK OUT ON A RENTED FLEET.
# `plan_next` idles for two different reasons and only one of them can resolve itself:
#
#   kind="board"      the board has nothing free. Another contributor finishes, a claim expires,
#                     the tip moves -- waiting is correct and this is NOT counted.
#   kind="exhausted"  WE are refusing: the block is already proved this session, or it has hit
#                     MAX_ATTEMPTS. Asking again cannot change the answer.
#
# The second is reachable and expensive. Attempt counts are LOCAL to the session, and the board
# does not park failing blocks at all (hazync#460: 0 failed ranges across 123,331 proven blocks),
# so it hands the same block back for ever. At 26x RTX 4090 = $19.24/hr a 24-hour session pinned
# this way burns ~$462 for zero blocks. --budget-usd bounds it, and is OPTIONAL.
MAX_CONSECUTIVE_EXHAUSTED_IDLES = int(
    os.environ.get("HAZYNC_MAX_EXHAUSTED_IDLES", "5"))


class SessionRefused(RuntimeError):
    """A session-level gate said no."""


def new_state(*, started_at, duration_s, fleet_ids=(), armed=True, arm_deadline_s=None):
    """A fresh session.

    `armed=False` DEFERS THE CLOCK (hazync#553). The window then begins at the first TIP block rather
    than the moment the fleet was ready, so waiting for the chain to mine a block does not eat the
    hour being measured. `started_at` is still recorded — it is when the session began, which is what
    `arm_deadline_s` is measured against and what prices the wait.
    """
    return {"started_at": float(started_at),
            "duration_s": float(duration_s),
            # None = the clock has not started yet. An absent key means a session file written before
            # the deferred clock existed, and `clock_origin` reads that as armed at `started_at`.
            "armed_at": float(started_at) if armed else None,
            "arm_deadline_s": float(arm_deadline_s) if arm_deadline_s else None,
            "spend_at_arm_usd": 0.0 if armed else None,
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


def clock_origin(state):
    """When the session's WINDOW began, or None if it has not started yet.

    ⚠ NOT `started_at`. A session started with `armed=False` has been alive — and billing — since
    `started_at`, but its hour has not begun. The two numbers answer different questions and the
    whole of hazync#553 is keeping them apart.

    ⛔ AN ABSENT KEY IS AN OLD SESSION FILE, NOT AN UNARMED ONE. `None` means deliberately deferred;
    a file written before this existed has no `armed_at` at all and must read as armed at
    `started_at`, or resuming it would hand it a fresh full window it has already spent.
    """
    if "armed_at" not in state:
        return float(state["started_at"])
    at = state["armed_at"]
    return None if at is None else float(at)


def is_armed(state):
    return clock_origin(state) is not None


def arm_clock(state, at):
    """Start the window now. Returns True only on the call that actually starts it.

    ⛔ ONLY THE FIRST CALL MOVES IT. The caller arms on the first tip block, and "first" is decided
    here rather than by the caller remembering — a resumed session must not restart its own hour,
    and re-arming at each tip block would make the window unbounded.
    """
    if is_armed(state):
        return False
    state["armed_at"] = float(at)
    state["spend_at_arm_usd"] = round(float(state.get("spend_usd", 0.0)), 4)
    return True


def waited_s(state, now):
    """How long the session was alive before its clock started — the boot-and-wait cost."""
    origin = clock_origin(state)
    end = float(now) if origin is None else origin
    return max(0.0, end - float(state["started_at"]))


def elapsed_s(state, now):
    origin = clock_origin(state)
    if origin is None:
        return 0.0                       # the window has not opened; nothing of it is spent
    return max(0.0, float(now) - origin)


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
    # ⛔ AN UNARMED SESSION HAS NO DEADLINE, SO IT NEEDS THIS ONE. With the clock deferred to the first
    # tip block, `left` is the full window for as long as no tip arrives -- so a bridge that stops
    # serving tip bundles, or a floor set above a chain that has stalled, would hold a rented fleet
    # for ever and every guard above would agree it had plenty of time. `--budget-usd` is the other
    # backstop and it is OPTIONAL, which is why this one is not.
    if not is_armed(state) and state.get("arm_deadline_s"):
        waited = waited_s(state, now)
        if waited >= float(state["arm_deadline_s"]):
            return {"action": "stop",
                    "why": f"waited {waited/60:.1f} min for a tip block and none arrived — the clock "
                           f"never started, so the fleet is being paid to wait. Check the bridge is "
                           f"still serving tip bundles above the --fresh-tip floor"}
    # ⛔ THE BUDGET IS CHECKED BEFORE THE NEXT BLOCK, NOT AFTER IT. Checking afterwards means the
    # block that crosses the line has already been paid for in full.
    spent = float(state.get("spend_usd", 0.0))
    if budget_usd and spent >= budget_usd:
        return {"action": "stop", "why": f"budget spent: ${spent:.2f} of ${budget_usd:.2f}"}

    rng = (work or {}).get("range")
    if rng is None:
        # ⚠ THE BOARD'S idle, not ours. This one can end on its own — another contributor
        # finishes, a claim expires, the tip moves — so waiting is the right thing to do.
        # ⚠ Whoever decided to idle knows WHY; this used to overwrite that with one fixed
        # sentence about the board (hazync#505). The fallback is kept for a caller that supplies
        # nothing, but a caller that does is believed.
        return {"action": "idle", "kind": "board",
                "why": (work or {}).get("why") or "the board has nothing free right now"}
    rng = str(rng)

    # ⛔ NEVER RE-PROVE A BLOCK THIS SESSION ALREADY PROVED. On a resume the coordinator may hand back
    # the same block -- a claim that has not expired, or the tip not having moved -- and proving it
    # again costs a full block of GPU for a receipt that already exists.
    if state["blocks"].get(rng, {}).get("ok"):
        return {"action": "idle", "kind": "exhausted",
                "why": f"block {rng} is already proved in this session"}

    if state["attempts"].get(rng, 0) >= MAX_ATTEMPTS:
        return {"action": "idle", "kind": "exhausted",
                "why": f"block {rng} has failed {MAX_ATTEMPTS} times and is not being retried — a "
                       f"session must not spend itself on one block"}

    # ⛔ DO NOT START A BLOCK THE SESSION CANNOT FINISH. Starting one with four minutes left rents the
    # fleet through the whole block and throws the result away at the deadline; the cards are billed
    # either way. Stopping early is the cheaper mistake, and it is the reversible one.
    if block_estimate_s and left < block_estimate_s:
        return {"action": "stop",
                "why": f"{left/60:.1f} min left but a block takes ~{block_estimate_s/60:.1f} min — "
                       f"starting one now would pay for it in full and discard it"}

    # ⚠ `source` IS CARRIED THROUGH, NOT RE-DERIVED. run_session arms the clock on the first TIP block
    # and must not guess which blocks those were: board fill and tip work are the same shape by the
    # time they reach here, and a range number does not say where it came from.
    # ⚠ `appeared_at` rides along so run_session can start the window at the moment the bundle
    # became provable rather than the moment this loop reached it.
    return {"action": "prove", "range": rng, "source": (work or {}).get("source"),
            "appeared_at": (work or {}).get("appeared_at")}


def record_attempt(state, rng):
    rng = str(rng)
    state["attempts"][rng] = state["attempts"].get(rng, 0) + 1
    return state["attempts"][rng]


def undo_attempt(state, rng):
    """Give back the attempt a block never got to use (hazync#506).

    ⛔ AN ATTEMPT IS A BUDGET FOR BEING BAD, AND STEPPING ASIDE IS NOT BEING BAD. `MAX_ATTEMPTS` (3)
    exists so one unprovable block cannot consume a session. A board block abandoned because a tip
    landed has demonstrated nothing about itself, and counting it would retire a perfectly good
    block after three tip arrivals -- permanently, since `plan_next` then refuses it for the rest of
    the session while the board goes on handing it back as ours.
    """
    rng = str(rng)
    n = state["attempts"].get(rng, 0)
    if n > 0:
        state["attempts"][rng] = n - 1
    if not state["attempts"].get(rng):
        state["attempts"].pop(rng, None)
    return state["attempts"].get(rng, 0)


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
    # ⛔ THE BLOCKS PROVED BEFORE THE CLOCK ARE NOT THE HOUR'S RESULT. With the clock deferred, a
    # session fills the wait with board work -- 8 blocks in the first 14 minutes of the 2026-09-28
    # run -- and quoting the total as "blocks in the hour" would overstate tip performance with
    # blocks proved before the measurement began. Both numbers are reported; neither is inferred.
    #
    # ⚠ STRICTLY AFTER, NOT AT. `at` is when a block FINISHED. The clock is armed between blocks, so
    # the board block that was running immediately before arming finishes at or just before the
    # origin -- `>=` counted that one as the hour's work, off by one in the headline figure. Every
    # block actually started on the clock finishes strictly after it, since proving takes time.
    origin = clock_origin(state)
    on_clock = ok if origin is None else {r: b for r, b in ok.items()
                                         if float(b.get("at") or 0.0) > origin}
    spend = float(state.get("spend_usd", 0.0))
    at_arm = state.get("spend_at_arm_usd")
    on_clock_spend = None if at_arm is None else round(spend - float(at_arm), 4)
    return {"blocks_ok": len(ok),
            "blocks_failed": len(failed),
            "failed_ranges": sorted(failed),
            "armed": is_armed(state),
            "blocks_ok_on_clock": len(on_clock),
            "waited_s": round(waited_s(state, now), 1),
            "elapsed_s": round(el, 1),
            "remaining_s": round(remaining_s(state, now), 1),
            "median_wall_s": walls[len(walls) // 2] if walls else None,
            "fastest_wall_s": walls[0] if walls else None,
            "spend_usd": round(spend, 4),
            "spend_before_clock_usd": round(float(at_arm), 4) if at_arm is not None else None,
            "spend_on_clock_usd": on_clock_spend,
            "usd_per_block": round(spend / len(ok), 4) if ok else None}


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
                budget_usd=None, spend_fn=None, grow_fn=None):
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
    exhausted_idles = 0
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
        # ⛔ BETWEEN BLOCKS, NEVER DURING ONE. A card admitted here joins the fleet for the NEXT
        # block: the chunk count IS the fleet size, so changing it while a proof is in flight would
        # re-split work the cards are already doing. This is the only point in the loop where no
        # block is running.
        # ⛔ AND IT MUST NEVER DELAY A TIP BLOCK. grow_fn only DRAINS what a background recruiter has
        # already finished gating -- it must not rent, wait or probe. A tip block that arrives while
        # a recruit is still being gated simply proves without it; the recruit joins whenever it is
        # ready, which may be several blocks later. Paying a whole fleet to wait for one more card is
        # the trade this feature exists to avoid making.
        if grow_fn is not None:
            try:
                joined = grow_fn()
                if joined:
                    emit(joined)
            except Exception as exc:      # noqa: BLE001 -- recruiting must never end a session
                emit(f"growth check failed, continuing: {type(exc).__name__}: {exc}")
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
            # ⛔ COUNT ONLY THE IDLES THAT CANNOT RESOLVE THEMSELVES. A board with nothing free is
            # legitimate waiting and resets the counter; an idle caused by OUR OWN refusal cannot
            # change on re-asking, and paying a fleet to re-ask is the failure (hazync#464).
            if plan.get("kind") == "exhausted":
                exhausted_idles += 1
                if exhausted_idles >= MAX_CONSECUTIVE_EXHAUSTED_IDLES:
                    emit(f"stopping: {exhausted_idles} idles in a row that cannot resolve — the "
                         f"board keeps offering work this session has already exhausted, and the "
                         f"fleet is billing to re-ask a question whose answer cannot change")
                    break
            else:
                exhausted_idles = 0
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
        # ⭐ THE CLOCK STARTS HERE (hazync#553), on the first TIP block, BEFORE it is proved. The first
        # tip run measured an hour that began when the fleet was ready: it spent 14 minutes waiting
        # for the chain, so a third of the window was gone before the thing being measured existed.
        #
        # ⛔ BEFORE prove(), NOT AFTER. Arming on completion would exclude the first tip block's own
        # wall time from the hour it starts -- the session would get its full window PLUS one free
        # block, and the headline figure would be the one number this run exists to be honest about.
        #
        # ⚠ BOARD FILL DOES NOT ARM IT. That is the whole point: gap-filling is what the fleet does
        # while it waits, and it must not consume the window.
        #
        # ⭐⭐ AND IT STARTS WHEN THE BUNDLE APPEARED, NOT WHEN WE GOT ROUND TO IT. Arming at `t` gave
        # the fleet every second between a block becoming provable and this loop picking it up --
        # free time, excluded from the hour, in the one measurement the hour exists to make. Measured
        # 2026-09-28: 968,985's bundle appeared at 12:24:59Z and proving began at 12:25:31Z, so 32 s
        # of our own latency would have fallen outside the window. A board block in flight can make
        # that gap much larger.
        #
        # `appeared_at` is when the BUNDLE first existed -- the earliest instant this fleet could
        # have begun. Clamped to `t` so a bad clock can never start the window in the future, and
        # falling back to `t` when the caller does not supply one.
        _app = plan.get("appeared_at")
        _arm_at = min(float(_app), t) if isinstance(_app, (int, float)) else t
        if plan.get("source") == "tip" and arm_clock(state, _arm_at):
            save(path, state)
            emit(f"⏱ CLOCK STARTS at the moment {rng}'s bundle appeared — the "
                 f"{state['duration_s']/3600:.1f}-hour window runs from now. "
                 f"Waited {waited_s(state, t)/60:.1f} min for it, "
                 f"${float(state.get('spend_at_arm_usd') or 0.0):.2f} spent getting here")
        n = record_attempt(state, rng)
        save(path, state)
        emit(f"proving {rng} (attempt {n})")

        try:
            result = prove(rng)
        except Exception as exc:                      # noqa: BLE001 -- a bad block is not a bad run
            result = {"ok": False, "events": [f"{type(exc).__name__}: {exc}"]}
            emit(f"block {rng} failed: {type(exc).__name__}: {exc}")

        # ⛔ AN ABORT IS NEITHER A SUCCESS NOR A FAILURE (hazync#506). Board work between tip blocks
        # steps aside the moment a tip lands. Recording that as a failed block would be wrong twice
        # over: it would retire the block after three tips (MAX_ATTEMPTS) and, far worse, three tip
        # arrivals in a row would trip the fleet-fault guard and RELEASE A HEALTHY FLEET -- turning
        # the feature that fills the gaps into the thing that ends the session.
        if result.get("aborted"):
            undo_attempt(state, rng)
            save(path, state)
            emit(f"stepped aside from {rng} after {result.get('wall_s', '?')}s: "
                 f"{result.get('why') or 'higher-priority work arrived'}")
            continue

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

    # ⛔ SAVE ON THE WAY OUT, ONCE, FOR EVERY EXIT. The spend accrued at the top of the final
    # iteration covers the block that just finished, and nothing after it wrote state -- so the
    # figure on disk was permanently one block behind. Measured 2026-09-24: session.json said $4.54
    # where RunPod billed $11.03, because the last block spent 1,910 s inside one prove() call and
    # its cost was accrued and then dropped on the way out.
    #
    # ⚠ THE FILE IS NOT A REPORT, IT IS THE BUDGET. A resumed session reads spend_usd from it and
    # checks --budget-usd against that number, so the gap refills a budget that was already spent.
    #
    # ⛔ AFTER THE LOOP, NOT AT THE break. There are THREE breaks in it -- the planner's stop, the
    # exhausted-idle guard and the fleet-fault guard -- and my first attempt at this fixed only the
    # first one. The test exercised a different exit and still saw $0.00 on disk, which is the only
    # reason I found out.
    try:
        save(path, state)
    except Exception:
        pass                      # accounting must never be the thing that raises at the end
    return summary(state, now())
