#!/usr/bin/env python3
"""The tip rig's controller: keep one block proving across a fleet that resizes itself (Phase 5 ⑧).

WHAT THIS IS FOR. The tip target is every block proven in under 10 minutes and published at tip-1. A
fixed fleet cannot meet that: block cost varies with its size, cards vary in speed, and blocks overlap at
peaks. So the fleet is sized DURING the proof, from the prover's own progress, and never sits idle.

    over-provision  ->  drop the slow tail  ->  add cards when the projection misses the target
                    ->  when a block finishes and no new block is waiting, take the lowest
                        unproven block from the proof-party board instead of standing down

⛔ SEPARATE FROM THE SPONSOR BOT, SHARING CODE ONLY. The roadmap is explicit: tip rig and sponsor rig are
separate deployments, separate RunPod keys, separate budgets. This module reuses sponsor_bot's RunPod
client and pod bookkeeping; it does not extend Bot, and the two must never share a budget or a key.

⛔ NOTHING HERE WIDENS THE seg-serve BIND. `_prove_distributed` binds 127.0.0.1 because the segment wire
is UNAUTHENTICATED (#365), and it refuses to widen silently. Remote cards reach it through a tunnel. A
controller that set HAZYNC_BIND=0.0.0.0 to make scaling convenient would put an unauthenticated proving
port on the public internet, so this module never sets it — the operator does, knowing what it means.

WHY CARDS CAN JOIN A RUNNING PROOF AT ALL: since #402 `seg-connect` retries its first connect with
backoff, so a card that arrives early waits instead of dying. `_prove_distributed` says so in as many
words — "more cards may join at any time". That is what makes scaling UP mid-block possible with no
change to the prover, and scaling DOWN is just terminating a pod, because a dropped link is survivable
by design.
"""

import os
import re


# The tip target: a block proven and published inside this window. The roadmap's "under 10 minutes".
TARGET_S = float(os.environ.get("HAZYNC_TIP_TARGET_S", "600"))

# How close to the target the projection may come before cards are added. A projection at 99% of the
# budget is not comfortable: adding cards has its own latency (boot, verify, dial in), so the trigger
# fires with room to act.
HEADROOM = float(os.environ.get("HAZYNC_TIP_HEADROOM", "0.75"))

# A card is in the slow tail when its measured segment rate is below this fraction of the fleet median.
# Dropping it frees budget for a faster replacement; it does NOT lose work, because seg-serve hands each
# segment out again when a worker goes away.
SLOW_TAIL = float(os.environ.get("HAZYNC_TIP_SLOW_TAIL", "0.6"))

# Most cards to add in one look. Boot, GPU smoke test and dial-in take minutes, so a look cannot measure
# the effect of what it just asked for; growing in steps lets the next look see the result instead of
# stacking asks on a projection that is already out of date.
GROWTH_STEP = int(os.environ.get("HAZYNC_TIP_GROWTH_STEP", "12"))

# test_tip_controller.py --control sets these, to show the two guards can fail. Never set them otherwise.
#
# ⛔ THE FLAGS EXIST BECAUSE MONKEYPATCHING CANNOT DISABLE THESE GUARDS FROM OUTSIDE, and a control that
# cannot disable what it names is a control that proves nothing. Stubbing projected_total_s() to return 0
# for a missing projection does not exercise the guard: 0s reads as comfortably inside budget, so
# scale_decision still holds — for the wrong reason. And slow_tail's floor is `max(1, keep_min)`, so
# passing keep_min=0 cannot lower it. Both patches were silently inert and the control caught that.
_CONTROL_IGNORE_NO_PROJECTION = False
_CONTROL_IGNORE_LAST_CARD = False

# ⛔ A SEPARATE PARSER, NOT A WIDER _PROGRESS_RE. hazync's _PROGRESS_RE gates the beat and the stall
# watchdog, and its comment says the seg-serve shape was deliberately NOT folded into it. Widening a
# load-bearing regex to get a number out of it would risk the watchdog for a convenience.
#
# The line is pinned verbatim by test_mode6_progress.py:
#     "     91/2352 segments  124s elapsed, ~3071s left"
# Number FIRST, noun PLURAL, and the prover has already done the projection arithmetic for us.
_SEG_RE = re.compile(r"(\d+)/(\d+)\s+segments\s+(\d+)s\s+elapsed,\s*~(\d+)s\s+left")

# Assembly: "joins 25/2352". No elapsed or ETA on this line — seg-serve reports joins every 25 joins for
# the whole assembly phase, so progress is known but the projection is not.
_JOIN_RE = re.compile(r"joins\s+(\d+)/(\d+)")


def parse_progress(line):
    """A seg-serve progress line -> dict, or None when the line is not one.

    Returns {"phase": "segments"|"joins", "done", "total", "elapsed_s", "eta_s"}. `elapsed_s` and
    `eta_s` are None in the join phase, which reports no timing of its own.

    ⛔ The ETA is the PROVER'S, not ours. seg-serve already projects its own completion and prints it;
    recomputing it from a rate here would be a second way of being wrong about the same number, and it
    would disagree with what the operator sees in the log.
    """
    m = _SEG_RE.search(line)
    if m:
        done, total, elapsed, eta = (int(g) for g in m.groups())
        return {"phase": "segments", "done": done, "total": total,
                "elapsed_s": elapsed, "eta_s": eta}
    m = _JOIN_RE.search(line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        return {"phase": "joins", "done": done, "total": total, "elapsed_s": None, "eta_s": None}
    return None


def projected_total_s(progress):
    """Elapsed + the prover's own estimate of what is left, or None when it cannot be projected."""
    if not progress or progress["elapsed_s"] is None or progress["eta_s"] is None:
        return None
    return progress["elapsed_s"] + progress["eta_s"]


def scale_decision(progress, cards, *, target_s=None, headroom=None, max_cards=75, min_cards=1):
    """How the fleet should change right now: {"action", "cards", "why"}.

    action is "add", "drop" or "hold"; `cards` is how many to add or drop (0 when holding).

    The rule, as the operator stated it: over-provision, remove cards if they are slow, and ADD cards if
    progress does not look like it will finish inside the target.

    ⛔ HOLD WHEN THE PROJECTION IS UNKNOWN. The join phase reports no timing, and a run that has printed
    nothing yet has no rate. Acting on a missing number means resizing the fleet on noise at the two
    moments it is least informative — the start, and the assembly tail where adding cards cannot help
    because the join tree is what caps a distributed prove.
    """
    target_s = TARGET_S if target_s is None else target_s
    headroom = HEADROOM if headroom is None else headroom

    total = projected_total_s(progress)
    if total is None and not _CONTROL_IGNORE_NO_PROJECTION:
        return {"action": "hold", "cards": 0,
                "why": "no projection yet (the join phase reports no timing, and adding cards cannot "
                       "speed up the join tree anyway)"}
    if total is None:
        # Control only: pretend a missing projection is a badly-missing one, so the fleet resizes on a
        # number that was never there — which is exactly the mistake the guard above prevents.
        total = int(target_s * 10)

    budget = target_s * headroom
    if total <= budget:
        return {"action": "hold", "cards": 0,
                "why": f"projected {total}s is inside {budget:.0f}s ({headroom:.0%} of the {target_s:.0f}s target)"}

    # ⛔ 1/cards IS AN OVER-ESTIMATE, SO THE ASK IS CAPPED PER LOOK. Segment proving divides across
    # workers; the JOIN TREE DOES NOT, and it is what caps a distributed prove (main.rs:5381). The host's
    # own analysis goes further: below a certain block size "distributing segments cannot reach 10
    # minutes no matter how many cards join" (main.rs:4989). So a naive cards*total/budget ask says 178
    # cards for one block — beyond the roadmap's expected 50-75 at peaks, and beyond what the join tree
    # could use. Grow by at most GROWTH_STEP per look instead, and let the next look measure the effect.
    ideal = -(-cards * total // max(int(budget), 1))          # ceiling division
    want = min(max_cards, cards + GROWTH_STEP, max(cards + 1, ideal))
    add = max(0, want - cards)
    if add == 0:
        # ⛔ NOT "hold". A fleet at its ceiling that is still going to miss by 5x is not the same state as
        # a run comfortably inside budget, and reporting both as "hold" leaves the caller unable to tell
        # "all is well" from "we will miss and I have nothing left to try" — which is precisely when the
        # operator wants telling. The tip target is a promise; missing it silently is the failure.
        return {"action": "at_limit", "cards": 0,
                "why": f"projected {total}s misses {budget:.0f}s and the fleet is already at "
                       f"{max_cards} cards — this block will MISS the {target_s:.0f}s target"}
    return {"action": "add", "cards": add,
            "why": f"projected {total}s misses {budget:.0f}s; {cards} -> {want} cards"
                   f"{'' if want >= ideal else f' (of ~{ideal} implied; capped at +{GROWTH_STEP} per look)'}"}


def slow_tail(rates, *, fraction=None, keep_min=1):
    """Which cards are in the slow tail: [worker_id]. `rates` is {worker_id: segments_per_second}.

    Measured, never assumed. A card is dropped only against the fleet's own median on THIS block, so a
    block that is slow for everyone does not cull the fleet.

    TWO SEPARATE PROPERTIES, and they are not the same guard — conflating them cost three wrong fixes:

      * ⛔ THE keep_min FLOOR is a real, disableable guard: however long the tail, at least keep_min
        cards keep proving. Turning it off lets a fleet cull itself down to nothing.
      * ✅ THE LAST CARD IS SAFE BY ARITHMETIC, not by that floor. With one card the median IS that
        card's rate, so `r < median * fraction` is `0.5 < 0.3` — false. A lone card can never be below
        its own median, so it is never in the tail to begin with. No flag can make that case cull, which
        is why every attempt to disable it from outside was silently inert.
    """
    fraction = SLOW_TAIL if fraction is None else fraction
    live = {w: r for w, r in rates.items() if r is not None}
    floor = 0 if _CONTROL_IGNORE_LAST_CARD else max(1, keep_min)
    if len(live) <= floor:
        return []
    ordered = sorted(live.values())
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0
    if median <= 0:
        return []
    slow = [w for w, r in live.items() if r < median * fraction]
    # Keep at least keep_min cards proving, dropping the slowest first.
    droppable = len(live) - floor
    return sorted(slow, key=lambda w: live[w])[:max(0, droppable)]


def next_work(tip_block, claim_fn):
    """What to prove next: the tip block if one is waiting, else the board's next block.

    ⛔ THE BOARD BLOCK COMES FROM THE COORDINATOR'S HANDOUT, NEVER FROM frontier+1 COMPUTED HERE. The
    board publishes a blocker, but it may already be claimed by another contributor — at the time this
    was written block 98,285 was held by a live worker on someone else's box. Claiming a range we were
    not given is what #251 records as 400+ doomed 409s, a healthy run that looks like an attack.
    POST /api/claim decides, signs the claim to this key (#310), and hands back one block.

    `claim_fn` returns a range string, or None when the board has nothing free right now (the
    coordinator answers "nothing available" / "already holds" / "rate limit", which hazync maps to
    EX_TEMPFAIL — a busy board, not a fault).
    """
    if tip_block is not None:
        return {"source": "tip", "range": str(tip_block)}
    rng = claim_fn()
    if rng is None:
        # ⚠ THE REASON TRAVELS WITH THE VERDICT (hazync#505). The session used to supply one fixed
        # sentence for every idle, so a run waiting for the CHAIN reported that the BOARD was busy --
        # a claim about a request it had not made. An idle that states the wrong cause is worse than
        # one that states none: during the 2026-09-23 flagship it was read live as a run that had
        # given up, when it was a run working exactly as designed.
        return {"source": "idle", "range": None,
                "why": "the board has nothing free right now"}
    return {"source": "board", "range": str(rng)}
