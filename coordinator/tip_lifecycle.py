#!/usr/bin/env python3
"""The tip rig's fleet lifecycle: qualify, rank, keep, release (Phase 5 ⑧).

`tip_controller` says how many cards. `tip_fleet` says what to do with one mid-block. This says WHICH
cards may be used at all, and which to let go.

⛔ THE TIP RIG AND THE SPONSOR RIG ARE SEPARATE, AND THIS MODULE ENFORCES IT. The roadmap is explicit —
separate deployments, separate RunPod keys, separate budgets, sharing code only. The sponsor bot's key
has a spend limit chosen for sponsorships; the tip rig's 24-hour run has no budget cap by decision. One
key doing both means a runaway tip fleet silently spends the sponsorship budget, and neither side's
accounting is true. So `key_path()` REFUSES to fall back to the sponsor bot's key, rather than being
merely documented not to.

⛔ A CARD THAT CANNOT PRODUCE A VERIFIABLE RECEIPT IS WORSE THAN NO CARD. It consumes a chunk, returns
something, and the aggregate fails at the end — after every other card's work is spent. Qualification is
therefore a gate, not a preference, and the METHOD_ID check is its centre.

⚠ `CANONICAL_METHOD_ID` is the CORE prover guest. The bridge guest is a DIFFERENT image
(`fb4d7352…`, measured on the live bridge host) by design — two binaries, deliberately. A card
reporting the bridge id is not broken; it is the wrong binary for proving, and it must not be kept.

Pure by construction: every function here takes plain data and returns plain data, so the gates can be
tested without renting anything. Renting is `sponsor_bot.RunPod`, which already does deploy/list/
terminate and is reused rather than reimplemented.
"""

import os

CANONICAL_METHOD_ID = "37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd"
# ⛔ MEASURED, NOT REMEMBERED (2026-09-24):
#
#     ssh <bridge> /usr/local/bin/hazync-host-bridge method-id  ->  fb4d7352f9f0…
#
# This said "05a5a279" until then, and that was TRUE WHEN IT WAS WRITTEN — the 2026-09-16 drop-in
# records the bridge being pointed at a build from main @ bffdbbf with that id. The bridge has been
# rebuilt since, its id moved with it (an image id absorbs the build's absolute paths), and nothing
# in the repo was tied to the binary, so three files went on quoting the old number for a week.
#
# ⚠ The VERDICT was never wrong — any non-canonical id is refused a line below either way. What was
# wrong is the REASON, and giving a reason is this constant's only job: a card running the current
# bridge binary was told "not canonical", which is precisely the hunt for a bug it exists to prevent.
#
# ⚠ Do not test this by looking for the string in reproduce/METHOD_ID. "05a5a279" is IN that file —
# as the id a laptop build produces — so that test passes on the stale value. test_tip_lifecycle.py
# requires the line to name `hazync-host-bridge`, which is the only form that tells them apart.
BRIDGE_METHOD_ID = "fb4d7352"          # prefix; a different guest ON PURPOSE, never a proving card

# A card must beat this fraction of the fleet's median rate to be worth keeping. Same default as
# tip_controller's slow tail, and for the same reason: below it, a card costs more in straggler than it
# contributes in throughput.
MIN_RATE_FRACTION = float(os.environ.get("HAZYNC_TIP_MIN_RATE_FRACTION", "0.6"))


def key_path():
    """Where the TIP rig's RunPod key lives. Never the sponsor bot's.

    ⛔ NO FALLBACK. `sponsor_bot.RunPod.key_path()` falls back to `SPONSOR_BOT_HOME/runpod.key`, which
    is correct for the sponsor bot and catastrophic here: the tip rig's 24-hour run has no budget cap,
    so borrowing that key spends the sponsorship budget with no accounting on either side. Refusing is
    the whole point — a missing key stops a run, a shared key ruins two.
    """
    path = os.environ.get("TIP_RUNPOD_KEY_FILE")
    if not path:
        raise SystemExit(
            "tip_lifecycle: set TIP_RUNPOD_KEY_FILE to the TIP rig's own RunPod key. "
            "The sponsor bot's key must not be reused: separate keys, separate budgets."
        )
    if "SPONSOR_BOT_HOME" in os.environ and os.path.abspath(path).startswith(
            os.path.abspath(os.environ["SPONSOR_BOT_HOME"]) + os.sep):
        raise SystemExit(
            f"tip_lifecycle: refusing {path} — it is inside SPONSOR_BOT_HOME. "
            "The tip rig and the sponsor rig must not share a key or a budget."
        )
    return path


def qualify(report):
    """Is this card fit to be given a chunk? Returns (ok, reason).

    `report` is what the card said about itself: its METHOD_ID, the sha256 of the binary it will run,
    whether a smoke prove verified, and whether it could be reached.

    Order matters: report the FIRST disqualifying fact, so the log says why rather than listing
    everything that happened to be wrong.
    """
    if not report.get("reachable", False):
        return False, "unreachable"

    mid = (report.get("method_id") or "").strip().lower()
    if not mid:
        return False, "no METHOD_ID reported"
    if mid.startswith(BRIDGE_METHOD_ID):
        # Not a fault — a deliberately different guest. Say so, or someone will go looking for a bug.
        return False, f"bridge guest {mid[:8]}…, not the prover guest (two binaries by design)"
    if mid != CANONICAL_METHOD_ID:
        return False, f"METHOD_ID {mid[:8]}… is not canonical {CANONICAL_METHOD_ID[:8]}…"

    if report.get("binary_sha") and report.get("expect_binary_sha") \
            and report["binary_sha"] != report["expect_binary_sha"]:
        return False, "binary sha does not match the signed release"

    if not report.get("smoke_ok", False):
        # ⛔ A CARD THAT ANSWERS IS NOT A CARD THAT PROVES. The smoke prove is the only evidence the GPU
        # will actually produce a verifiable receipt; everything above is metadata it could report while
        # being unable to prove anything at all.
        return False, "smoke prove did not verify"

    return True, "ok"


def rank(cards):
    """Fastest first, cheapest as the tie-break. Unqualified cards are not ranked at all.

    `cards` are dicts with at least `id`, `rate` (segments per second) and `price` ($/hr). A card with
    no measured rate sorts last rather than being dropped — it may simply be new, and the caller decides
    whether to keep it long enough to measure.
    """
    ok = [c for c in cards if c.get("qualified", False)]

    def key(c):
        rate = c.get("rate")
        return (0 if rate else 1, -(rate or 0.0), c.get("price") or 0.0, str(c.get("id")))

    return sorted(ok, key=key)


def median(values):
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    mid = len(vs) // 2
    return vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2.0


def keep_and_release(cards, want, *, min_fraction=None):
    """Split a ranked fleet into the cards to keep and the cards to let go.

    Two reasons to release: the fleet is bigger than `want`, or a card is in the slow tail. Both are
    computed against the MEDIAN of measured rates, never the mean — one card at a tenth of the speed
    drags a mean far enough to condemn healthy cards with it.

    ⛔ AN UNMEASURED CARD IS NEVER IN THE SLOW TAIL. It has no rate because nothing has timed it yet;
    releasing it for that is how a fleet churns without ever converging.
    """
    min_fraction = MIN_RATE_FRACTION if min_fraction is None else min_fraction
    ranked = rank(cards)
    unqualified = [c for c in cards if not c.get("qualified", False)]

    med = median([c.get("rate") for c in ranked])
    keep, release = [], []
    for i, c in enumerate(ranked):
        rate = c.get("rate")
        too_slow = med is not None and rate is not None and rate < med * min_fraction
        if i < want and not too_slow:
            keep.append(c)
        else:
            release.append(c)

    return {"keep": keep,
            "release": release + unqualified,
            "median_rate": med}


def spend_so_far(cards, elapsed_s):
    """What the fleet has cost, in dollars, for `elapsed_s` of wall clock."""
    return round(sum((c.get("price") or 0.0) for c in cards) * (elapsed_s / 3600.0), 3)


# The screening prove that `tools/milestone/bootstrap2.sh` runs on every card before it is trusted:
# one chunk of a real near-tip block at HAZYNC_CHUNKS=64, so ~30 segments of representative work.
# Cheap enough to run on every card, real enough that a card which passes it can prove.
SCREEN_MIN_SEGMENTS = int(os.environ.get("HAZYNC_TIP_SCREEN_MIN_SEGMENTS", "1"))

# CUDA error 804 on a consumer card means the driver's compat libraries are shadowing the real ones.
# bootstrap2.sh removes them first for exactly this reason. A card failing this way is NOT a bad card —
# it is an unprepared one, and saying so is the difference between fixing it and discarding it.
CUDA_COMPAT_HINT = "cuda error 804"


def report_from_screen(screen, *, method_id, binary_sha=None, expect_binary_sha=None, log=""):
    """Turn a card's `screen.json` plus its reported METHOD_ID into the dict `qualify()` consumes.

    ⛔ A ZERO EXIT IS NOT A PASS. The screening can exit 0 having produced no segments at all — which is
    a card that ran something and proved nothing. `segments` is the evidence, not `rc`, and a card whose
    screening yielded nothing measurable must not be handed a chunk of a real block.

    ⚠ `rate` is segments per second, the figure ranking uses. It is None when the screening did not
    measure one; `keep_and_release` deliberately never puts an unmeasured card in the slow tail.
    """
    screen = screen or {}
    rc = screen.get("rc")
    segs = screen.get("segments") or 0
    sps = screen.get("s_per_segment")

    smoke_ok = (rc == 0) and segs >= SCREEN_MIN_SEGMENTS and bool(sps)

    note = ""
    if not smoke_ok:
        if CUDA_COMPAT_HINT in (log or "").lower():
            # Distinguishable on purpose: this one is fixed by removing /usr/local/cuda*/compat, which
            # bootstrap2.sh does. Discarding the card instead would throw away a working GPU.
            note = "CUDA 804 — the driver compat libraries are shadowing the real ones; remove them and retry"
        elif rc != 0:
            note = f"screening prove exited {rc}"
        elif segs < SCREEN_MIN_SEGMENTS:
            note = f"screening produced {segs} segments — it ran but proved nothing"
        else:
            note = "screening measured no per-segment rate"

    return {
        "reachable": True,               # we have its screen.json, so we reached it
        "method_id": method_id,
        "binary_sha": binary_sha,
        "expect_binary_sha": expect_binary_sha,
        "smoke_ok": smoke_ok,
        "rate": (1.0 / sps) if sps else None,
        "segments": segs,
        "gpu": screen.get("gpu"),
        "peak_vram_mib": screen.get("peak_vram_mib"),
        "note": note,
    }


# ── card-to-card reachability, and the bind that makes it possible ────────────────────────────────
#
# Two separate failures live here and they pull in opposite directions.
#
# ⛔ A LOOPBACK BIND WITH REMOTE CARDS IS A SILENT NO-OP. seg-serve defaults to 127.0.0.1 because the
# segment wire is UNAUTHENTICATED — `seg-connect` opens a bare TcpStream and HAZYNC_WORKER_ID is a log
# label, not an identity. With that default, a worker on another machine cannot attach; since #402 it
# RETRIES with backoff rather than dying, so it sits in its 600-second loop looking armed and
# contributing nothing. The run then proceeds with fewer cards than anyone believes it has.
#
# ⛔ AND 0.0.0.0 PUTS THAT UNAUTHENTICATED PORT ON EVERY INTERFACE. Anyone who can reach it can take
# work and stall the run. The prover's own words: "Prefer a tunnel on a public host."
#
# So the gate refuses BOTH: a bind too narrow for the fleet to attach, and one wider than the operator
# has said they want.
PUBLIC_BIND = "0.0.0.0"
LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1")


# ⛔ KEYS THE DRIVER COMPUTES. These describe THIS run -- which block, which port, which card is the
# aggregate -- and are derived per launch. If an operator has one of them lying around in their shell
# (HAZYNC_BLOCK from a manual prove, say), forwarding it would silently retarget the run at a
# different block while every log still named the one that was asked for. The driver always wins.
RESERVED_ENV = frozenset({
    "HAZYNC_AGG", "HAZYNC_AGG_OUT", "HAZYNC_BIND", "HAZYNC_BLOCK", "HAZYNC_BLOCK_NAME",
    "HAZYNC_BRIDGE_OUT", "HAZYNC_CHUNK", "HAZYNC_CHUNKS", "HAZYNC_HOME", "HAZYNC_HOST_BYTES",
    "HAZYNC_HOST_URL", "HAZYNC_OUT", "HAZYNC_PORT", "HAZYNC_PUBLISH_DEST", "HAZYNC_PUBLISH_KEY",
    "HAZYNC_RANGE", "HAZYNC_SEG_REMOTE", "HAZYNC_TIP_FANOUT", "HAZYNC_WORKDIR", "HAZYNC_WORKER_ID",
})


def lever_env(environ=None):
    """The HAZYNC_* levers the operator set, to be forwarded to the cards.

    ⛔ WITHOUT THIS, NO LEVER CAN BE TESTED AT ALL. `prove_env` was a hardcoded dict of five keys, so
    setting HAZYNC_RESOLVE_LOCAL=1 (or WORKER_LIFTS, or JOIN_LOCAL_MAX) in the driver's shell reached
    nothing: BOTH arms of an A/B would run identically and the measurement would report "no
    difference" -- a false negative that looks exactly like a lever that does not work.

    ⚠ Three of the five keys that dict carried -- HAZYNC_LIFTX_HINT, HAZYNC_FIELD_BIGINT2,
    HAZYNC_ECMULT_WINDOW -- are read ONLY in methods/build.rs and methods/guest/build.rs. They are
    BUILD-time guest flags baked into the released binary, so passing them at runtime has never done
    anything. They are kept here only because they are harmless and their absence would look like a
    regression to anyone diffing a run's environment.
    """
    import os as _os
    src = _os.environ if environ is None else environ
    return {k: v for k, v in sorted(src.items())
            if k.startswith("HAZYNC_") and k not in RESERVED_ENV}


def effective_bind(env):
    """What seg-serve will actually listen on, by the prover's own rule.

    `HAZYNC_BIND` wins; else `HAZYNC_SEG_REMOTE=1` means 0.0.0.0; else loopback. Mirrors
    `seg_bind_addr()` in prover/host/src/main.rs — if that changes, this must change with it.
    """
    if env.get("HAZYNC_BIND"):
        return env["HAZYNC_BIND"]
    if env.get("HAZYNC_SEG_REMOTE") == "1":
        return PUBLIC_BIND
    return "127.0.0.1"


def bind_verdict(env, *, cards_are_remote, accept_public=False):
    """Can the fleet attach, and is the port wider than intended? Returns (ok, reason)."""
    bind = effective_bind(env)

    if cards_are_remote and bind in LOOPBACK_BINDS:
        return False, (
            f"seg-serve would bind {bind}, so workers on other machines CANNOT attach — they will "
            f"retry for 600 s looking armed and contribute nothing. Set HAZYNC_SEG_REMOTE=1, or "
            f"HAZYNC_BIND to a tunnel or private address.")

    if bind == PUBLIC_BIND and not accept_public:
        return False, (
            "seg-serve would bind 0.0.0.0, putting an UNAUTHENTICATED proving port on every interface — "
            "anyone who can reach it can take work and stall the run. Prefer a tunnel "
            "(HAZYNC_BIND=<private addr>), or pass accept_public if that is genuinely intended.")

    if not cards_are_remote and bind not in LOOPBACK_BINDS:
        return True, f"bind {bind} is wider than this local-only fleet needs, but it will work"

    return True, f"bind {bind}"


def reachability_by_block(results, hosts):
    """Group a reachability result by network /24, and say whether the split is structural.

    ⛔ WHY GROUPED. A bare count ("7 of 17 cannot reach the aggregate") tells an operator how bad it
    is and nothing about what to do. Measured 2026-09-24, the same numbers grouped are a different
    fact entirely -- reachability was predicted PERFECTLY by network block:

        194.68.245.x   10 reached, 0 failed
        69.30.85.x      0 reached, 4 failed   <- the aggregate's OWN block
        63.141.33.x     0 reached, 3 failed

    That is not seven unlucky pods, it is two unreachable networks, and it is knowable before the
    clock starts rather than by grepping the log afterwards -- which is how it was actually found.

    ⚠ IT REPORTS, IT DOES NOT DIAGNOSE. The obvious reading is NAT hairpinning (workers dial the
    aggregate's public mapped port, and a host behind the same edge may fail to loop back out and
    in) -- but that is a hypothesis from one run. ⛔ Note it predicts that renting within one data
    centre, the intuitive fix, would make things WORSE. Nothing here acts on the guess.

    `hosts` maps card id -> host/ip. Returns {block: {"ok": n, "bad": n, "cards": [...]}}.
    """
    out = {}
    for cid, reached in results.items():
        host = str(hosts.get(cid, "") or "")
        block = ".".join(host.split(".")[:3]) if host.count(".") >= 3 else (host or "?")
        row = out.setdefault(block, {"ok": 0, "bad": 0, "cards": []})
        row["ok" if reached else "bad"] += 1
        row["cards"].append(str(cid))
    for row in out.values():
        row["cards"].sort()
    return out


def reachability_is_structural(by_block):
    """True when every block is all-good or all-bad, and at least one of each exists.

    ⚠ That shape is what says "this is the network, not the pods". A mixture inside a block is the
    ordinary unlucky-pod case and needs no special explanation.
    """
    if len(by_block) < 2:
        return False
    pure = all(row["ok"] == 0 or row["bad"] == 0 for row in by_block.values())
    has_bad = any(row["bad"] for row in by_block.values())
    has_ok = any(row["ok"] for row in by_block.values())
    return pure and has_bad and has_ok


def reachability_verdict(results):
    """Every card must have reached the aggregate's port. Returns (ok, reason, unreachable).

    ⛔ NO QUORUM. A card that cannot attach is not a slow card — it is a card that will sit in its retry
    loop and do nothing, while the fleet size everyone is reasoning about silently includes it. Running
    anyway means the straggler, the projection and the cost are all computed against a fleet that does
    not exist. Refuse, drop the card deliberately, and re-plan with the size you actually have.
    """
    unreachable = sorted(str(c) for c, ok in results.items() if not ok)
    if not results:
        return False, "no cards were probed for reachability", []
    if unreachable:
        return False, (f"{len(unreachable)} of {len(results)} card(s) cannot reach the aggregate: "
                       f"{', '.join(unreachable[:8])}"
                       f"{'…' if len(unreachable) > 8 else ''}"), unreachable
    return True, f"all {len(results)} cards reached the aggregate", []
