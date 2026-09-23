#!/usr/bin/env python3
"""One block, start to receipt, without a human (Phase 5 ⑧).

The phase order is `tools/milestone/run_continuous.sh`, which is the only sequence that has ever produced
a near-tip block under ten minutes — three times, on 2026-09-10. It is reproduced here rather than
redesigned, because every gate in it is a run that died without it.

    0  GATE     every pod cleared AND VERIFIED EMPTY        <- outside the clock: setup, not proving
    1  T0       launch all chunks, in parallel
    2           arm auto-attach so cards cost ~0 to join when the listener opens
    3  POLL     probe in parallel -> plan_tick -> act; receipts stream as they appear
    4  GATHER   sweep anything whose streaming retries failed
    5  GATE     staged == N, asserted
    6  AGGREGATE start seg-serve, wait for VERIFIED

⛔ THE CLOCK STARTS AT PHASE 1, NOT PHASE 0. Clearing the fleet is setup; counting it would flatter every
figure this produces. run_continuous.sh is explicit about it and so is this.

⛔ PHASE 0 IS NOT OPTIONAL AND ITS FAILURE IS NOT A WARNING. A pod carrying a stale `chunk_N.bin` from a
previous run reports DONE in 63 seconds, its receipt is collected, and the aggregate fails — or worse,
succeeds against the wrong work. Every pod is verified empty and the run refuses otherwise.

Injectable `runner`, `now` and `sleep` so the whole sequence can be driven by a test at $0 instead of
$1.39 and ten minutes.
"""

import re
import tip_fleet


class RunRefused(RuntimeError):
    """A gate said no. The message says which, and the run has not started."""


def verify_fleet_empty(runner, cards):
    """Phase 0. Every pod must report LEFT:0 REDIRS:0 — and we must have HEARD from every one.

    ⛔ AN UNREACHABLE POD IS NOT A CLEAN POD. Counting only the ones that answered would let a pod we
    could not talk to carry a stale receipt into the run, which is the failure this phase exists for.
    """
    clean, dirty, silent = [], [], []
    for cid, reply in runner.clear_and_check(cards).items():
        if reply is None:
            silent.append(cid)
        elif "LEFT:0 REDIRS:0" in reply:
            clean.append(cid)
        else:
            dirty.append(cid)
    # ⛔ SORT BY NAME, NOT BY THE OBJECT. A Card defines __eq__ and __hash__ (it is a dict key) but no
    # ordering, so sorted() on Cards raises TypeError. The unit tests used strings, which sort happily,
    # and the first live run is what found it -- sort by str() so any card type works.
    key = str
    return {"clean": sorted(clean, key=key), "dirty": sorted(dirty, key=key),
            "silent": sorted(silent, key=key),
            "ok": not dirty and not silent and len(clean) == len(cards)}


def _joins_done(joins):
    """`joins 12/34` -> 12. The beat's progress signal, and None when there is nothing to read yet."""
    if not joins:
        return None
    m = re.search(r"joins (\d+)/(\d+)", str(joins))
    return int(m.group(1)) if m else None


class RunAborted(Exception):
    """The caller asked for this run to stop; it was not a failure.

    ⛔ A DISTINCT TYPE BECAUSE A FAILURE COUNTER MUST NOT COUNT IT. The session stops a fleet after
    three consecutive failed blocks -- correctly, because three in a row really is the fleet. Board
    work that steps aside for a tip block would otherwise look exactly like that, and a fleet
    proving the board perfectly well between tips would release itself after three tip arrivals.
    """

    def __init__(self, why, *, height=None, elapsed_s=None):
        super().__init__(why)
        self.why, self.height, self.elapsed_s = why, height, elapsed_s


def run_range(*, height, cards, runner, bundle_path, now, sleep, feed=None, on_event=None,
              beat=None, max_ticks=1200, tick_s=6.0, unreachable_limit=20, abort=None):
    """Prove ONE claimed board block from its bundle (mode 6). Returns a summary dict.

    ⛔ THIS IS NOT `run_block` WITH A DIFFERENT FILE. `run_block` gives every card a chunk of the
    FIXTURE to prove and then aggregates the chunk receipts; mode 6 executes the guest ONCE over the
    bundle and pushes segments to whoever has dialled in. There is no per-card chunk phase here at
    all, so the cards do nothing until they attach — which makes the auto-attach the load-bearing
    step rather than a convenience.

    `beat` is called as `beat(progress)` whenever the join count RISES, and only then: a claim must
    not be kept alive by a timer while nothing is happening (#256). It returns the new high-water
    mark, which is threaded back so the caller owns the state.

    `abort` is an optional predicate asked once per tick. Returning a truthy value stops the run and
    raises `RunAborted` -- this is how board work gives way the moment a tip block lands (hazync#506).

    ⛔ THE AGGREGATE IS STOPPED BEFORE THE RAISE, AND THE WORKERS ARE DISARMED. `seg-serve` is a
    detached process holding port 9110, and the cards sit in an attach loop dialling it. Raising
    without tearing that down leaves the next block's aggregate to `bind()` on a port that is still
    held -- which this codebase has already done once, relaunching seg-serve on top of a healthy one
    and killing the new process while the original kept running with its log unlinked.

    ⚠ The abort is asked AFTER the `verified` check, deliberately: a run that has already produced
    its receipt is finished, and discarding it to start the tip six seconds sooner would throw away
    the whole block. Give up work that is still in progress, never work that is already done.
    """
    events = []

    def emit(msg):
        events.append(msg)
        if on_event:
            on_event(msg)

    t0 = now()
    ok, why = runner.start_range_aggregate(height=height, bundle_path=bundle_path)
    if not ok:
        raise RunRefused(f"could not start the range aggregate: {why}")
    emit(f"serving block {height} as a range from its bundle")

    # ⛔ ARM THE WORKERS, OR NOTHING ATTACHES. In mode 6 the cards are idle until they dial in, so a
    # run whose auto-attach never fired looks exactly like a fleet that is merely slow.
    runner.arm_auto_attach(cards)
    emit(f"armed {len(cards)} card(s) to dial the aggregate")

    gone, beaten = 0, 0
    out = {}
    for tick in range(max_ticks):
        out = runner.aggregate_status()
        prog = _joins_done(out.get("joins"))
        if beat is not None and prog is not None and prog > beaten:
            beaten = beat(prog)
        if out.get("verified"):
            return {"ok": True, "block": str(height), "cards": len(cards),
                    "wall_s": round(now() - t0, 1), "digest": out.get("digest"),
                    "joins": out.get("joins"), "events": events}
        # ⛔ ASKED EVERY TICK, AND THE TEARDOWN HAPPENS HERE — not in the caller's `except`. A caller
        # that forgets leaves a detached seg-serve on port 9110 and a fleet still dialling it, and
        # the next block cannot start. Tearing down on the way out makes that impossible to forget.
        if abort is not None:
            try:
                why = abort()
            except Exception as exc:                       # noqa: BLE001
                # ⚠ A BROKEN ABORT MUST NOT KILL A HEALTHY RUN. This predicate reaches over the
                # network to ask whether a tip has landed; an ssh blip is not a reason to throw away
                # a block that is proving. Say so and carry on.
                emit(f"⚠ the abort check failed ({type(exc).__name__}: {exc}) — continuing")
                why = None
            if why:
                emit(f"abandoning block {height} after {now() - t0:.0f}s: {why}")
                stop = getattr(runner, "stop_range_aggregate", None)
                if stop is not None:
                    sok, swhy = stop()
                    emit(f"aggregate stopped: {swhy}" if sok
                         else f"⚠ the aggregate would not stop ({swhy}) — the next block may not bind")
                runner.stop_auto_attach(cards)
                raise RunAborted(str(why), height=height, elapsed_s=round(now() - t0, 1))
        if out.get("unreachable"):
            gone += 1
            if gone >= unreachable_limit:
                raise RunRefused(
                    f"the aggregator has been unreachable for {gone} consecutive polls "
                    f"(~{gone * tick_s:.0f}s). Not waiting out the remaining {max_ticks - tick} ticks.")
        else:
            gone = 0
            if not out.get("alive", True):
                raise RunRefused("the aggregate died before verifying — see agg.err on the card")
        sleep(tick_s)
    raise RunRefused(f"the range aggregate never verified within {max_ticks} ticks "
                     f"(~{max_ticks * tick_s / 60:.0f} min); last joins={out.get('joins')}")


def run_block(*, block, cards, runner, now, sleep, stall_s=None, max_ticks=1200, tick_s=3.0,
              unreachable_limit=20, feed=None, on_event=None):
    """Drive one block to a verified receipt. Returns a summary dict.

    `cards` maps chunk -> card. `runner` supplies the remote actions. `now`/`sleep` are injected so a
    test can run the whole sequence instantly.
    """
    n = len(cards)
    if n == 0:
        raise RunRefused("no cards")

    # ⛔ A RECOVERY MUST BE VISIBLE WHEN IT HAPPENS, NOT IN THE RETURN VALUE. These were collected in
    # `events` and handed back at the end, so while a run was in flight there was no way to see that a
    # card had wedged and its chunk had been moved -- the operator watched an unexplained gap instead.
    # Measured 2026-09-20: a card died on `cudaErrorNoDevice` and its chunk was restarted, and none of
    # that reached the log until the run finished. The list is still returned; it is also reported live.
    # ⚠ A card may be a Card or, in a test, a bare string. Name it either way rather than assuming.
    def name(c):
        return getattr(c, "cid", c)

    def emit(msg):
        events.append(msg)
        if on_event:
            on_event(msg)

    # ── phase 0 ────────────────────────────────────────────────────────────────────────────────────
    empty = verify_fleet_empty(runner, cards)
    if not empty["ok"]:
        raise RunRefused(
            f"fleet not verified empty: {len(empty['clean'])}/{n} clean, "
            f"dirty={empty['dirty']}, unreachable={empty['silent']} — a stale chunk_N.bin reports DONE "
            f"in seconds and poisons the aggregate")

    # ── phase 1: THE CLOCK STARTS ──────────────────────────────────────────────────────────────────
    t0 = now()
    events = []

    # The dashboard's elapsed clock counts from t0, so it is declared HERE and not a line earlier:
    # writing it during the clear would back-date the run by the whole of phase 0 and flatter every
    # per-block figure on the frame.
    #
    # ⚠ A DEAD FEED NEVER FAILS A RUN. The receipt is the product and the dashboard is a view of it;
    # throwing away a fleet that is proving perfectly well because a screen is blank would be a far
    # worse outcome than a gap in a graph. Missing telemetry is RECORDED and the run carries on.
    if feed is not None:
        feed.mark_t0(t0)
        st = feed.staleness(now())
        if not st["ok"]:
            emit(
                f"⚠ telemetry not live at T0 (stale={st['stale']} never-seen={st['never']}) — the "
                f"dashboard will have gaps for those cards. Per-card telemetry CANNOT be "
                f"reconstructed once a pod is gone, so this is not recoverable after the run.")

    runner.launch_all(cards, block=block, chunks=n)
    runner.arm_auto_attach(cards)

    staged, busy, reassigned = set(), set(), set()
    last_size, last_change = {}, {c: t0 for c in cards}

    for _ in range(max_ticks):
        probes = runner.probe_all(cards, reassigned=reassigned)
        plan = tip_fleet.plan_tick(assignments=cards, probes=probes, staged=staged, busy=busy,
                                   last_size=last_size, last_change=last_change,
                                   now=now(), stall_s=stall_s)
        last_size, last_change = plan["last_size"], plan["last_change"]

        for act in plan["actions"]:
            if act["action"] == "stage":
                # Streamed during the chunk phase, not batched at the end -- batching cost 57 s once.
                if runner.stage_receipt(cards[act["chunk"]], act["chunk"]):
                    staged.add(act["chunk"])
            elif act["action"] == "reassign":
                runner.kill_provers(cards[act["chunk"]])
                target = act["to"]
                runner.relaunch(target, act["chunk"], block=block, chunks=n, workdir_suffix=act["chunk"])
                cards[act["chunk"]] = target
                busy.add(target)
                reassigned.add(act["chunk"])
                emit(f"chunk {act['chunk']} reassigned to {name(target)} after "
                     f"{act['static_s']}s static")
            elif act["action"] == "restart_in_place":
                runner.kill_provers(cards[act["chunk"]])
                runner.relaunch(cards[act["chunk"]], act["chunk"], block=block, chunks=n,
                                workdir_suffix=act["chunk"])
                reassigned.add(act["chunk"])
                emit(f"chunk {act['chunk']} restarted in place on {name(cards[act['chunk']])} after "
                     f"{act['static_s']}s static — no other card could take it")

        if len(staged) >= n:
            break
        sleep(tick_s)

    chunk_phase_s = round(now() - t0, 1)

    # ── phase 4: sweep anything the streaming retries failed to collect ────────────────────────────
    for chunk, card in cards.items():
        if chunk not in staged and runner.stage_receipt(card, chunk):
            staged.add(chunk)

    # ── phase 5: the gate that a silent scp drop fails ────────────────────────────────────────────
    count = runner.staged_count()
    if not tip_fleet.staging_complete(count, n):
        raise RunRefused(
            f"coordinator has {count}/{n} chunks — refusing to aggregate. A silent scp drop once staged "
            f"21 of 22 and seg-serve panicked with no useful message")

    # ── phase 6 ───────────────────────────────────────────────────────────────────────────────────
    runner.start_aggregate(block=block, chunks=n)
    # ⛔ AN AGGREGATOR WE CANNOT REACH MUST NOT BE POLLED IN SILENCE. One bad ssh is not a dead
    # aggregate -- saying "dead" there would abort a healthy run -- but an aggregator that stays
    # unreachable is usually gone for good (terminated, evicted, network lost), and the old loop kept
    # asking for the full tick budget with nothing printed. Measured 2026-09-20: a 23-card run sat
    # polling a fleet that no longer existed until an outer `timeout` killed it at 45 minutes, and the
    # run reported nothing at all about why.
    gone = 0
    for tick in range(max_ticks):
        out = runner.aggregate_status()
        if out.get("verified"):
            return {"ok": True, "block": block, "cards": n,
                    "wall_s": round(now() - t0, 1), "chunk_phase_s": chunk_phase_s,
                    "digest": out.get("digest"), "joins": out.get("joins"), "events": events}
        if out.get("unreachable"):
            gone += 1
            if gone >= unreachable_limit:
                raise RunRefused(
                    f"the aggregator has been unreachable for {gone} consecutive polls "
                    f"(~{gone * tick_s:.0f}s). It is probably gone -- terminated, evicted or "
                    f"network-partitioned. Not waiting out the remaining "
                    f"{max_ticks - tick} ticks in silence.")
        else:
            gone = 0
            if not out.get("alive", True):
                raise RunRefused("the aggregate died before verifying — see agg.err on the coordinator")
        sleep(tick_s)

    raise RunRefused(f"the aggregate never verified within {max_ticks} ticks "
                     f"(~{max_ticks * tick_s / 60:.0f} min); last joins={out.get('joins')}")
