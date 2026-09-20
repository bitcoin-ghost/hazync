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
(`05a5a279…`) by design — two binaries, deliberately. A card reporting the bridge id is not broken; it
is the wrong binary for proving, and it must not be kept.

Pure by construction: every function here takes plain data and returns plain data, so the gates can be
tested without renting anything. Renting is `sponsor_bot.RunPod`, which already does deploy/list/
terminate and is reused rather than reimplemented.
"""

import os

CANONICAL_METHOD_ID = "37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd"
BRIDGE_METHOD_ID = "05a5a279"          # prefix; a different guest ON PURPOSE, never a proving card

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
