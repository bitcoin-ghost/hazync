#!/usr/bin/env python3
"""Regenerate bundles for heights nothing can serve, from the nearest archived checkpoint (hazync#347).

    regen_bundles.py --heights 600000,600001      # DRY RUN: say what it would replay, touch nothing
    regen_bundles.py --heights 600000 --apply     # actually replay and install the bundles
    regen_bundles.py --from-waiting --apply       # take the heights from the sponsor bot's waiting set

WHY THIS EXISTS. The live bridge runs with HAZYNC_BRIDGE_EMIT_FROM=967500: below that height it advances
state and writes NO bundle. Measured 2026-09-18, coverage is 1..418,268 and the head is past 740,000, so
heights 418,269-967,499 are walked past with nothing written. They cannot simply be stored: bundles reach
17.0 MB by height 418,000 (measured), so the 549,231-block span is >9 TB against 3.9 TB free. The answer
is to make them on demand and keep only what was asked for.

The sponsor bot already knows which heights those are. sponsor_bot.pending_work(coord, need_bundle=False)
minus pending_work(coord) is exactly "held blocks with no bundle", and it currently logs
"N held block(s) have no bundle yet and wait for the bridge: no pod is rented for them" and stops. Below
418,268 the bridge will indeed arrive eventually; in the gap it never will, so that wait is forever.

HOW. Copy the nearest archived rung below the target into a SCRATCH directory, run the bridge there with
HAZYNC_BRIDGE_ONCE=1 and HAZYNC_BRIDGE_TO=<max target>, then move only the requested bundles into place.
No bridge change is needed — hazync#347's "a way back" describes exactly this procedure.

⛔ NEVER POINT THE REPLAY AT THE LIVE HAZYNC_BRIDGE_OUT. The running bridge owns that directory and
commits its checkpoint by writing state.bin.tmp and renaming over state.bin. A second bridge writing
there would race that rename, and the loser is a corrupt checkpoint for the process the whole board
depends on. The scratch directory is not a tidiness preference; it is the safety property.

⛔ AND REFUSE WHEN NO RUNG SITS BELOW THE TARGET. With no checkpoint to seed from, the bridge starts at
genesis — silently, because bridge_load_state() returns None and the resume path treats that as "rebuild
from genesis" (main.rs:3496 says so in as many words). At the 533-599 ms/block measured on the live
bridge that is ~6 days of walking that looks exactly like a working job until the disk fills.

⛔ REGENERATE ONLY WHAT WAS ASKED FOR. A rung is 25,000 blocks and bundles near the gap are 17 MB and
climbing, so replaying a whole rung to disk is 400+ GB. The replay must still WALK every block from the
rung to the target — that is how the accumulator advances — but only the requested heights are kept.

EXIT CODES, matching the integrity checks so hazync-run-check can alert on it:
  0  ran; bundles were produced, or none were needed
  1  something is wrong (replay failed, a requested height was not produced)
  2  could not check (no rung below the target, no bridge binary, no space) — nothing is replayed on a 2
"""

import argparse
import os
import shutil
import subprocess
import sys


def archived_rungs(archive_dir):
    """Heights with an archived checkpoint, ascending. Empty when the directory does not exist."""
    if not archive_dir or not os.path.isdir(archive_dir):
        return []
    out = []
    for name in os.listdir(archive_dir):
        if name.startswith("state_") and name.endswith(".bin"):
            h = name[len("state_"):-len(".bin")]
            if h.isdigit():
                out.append(int(h))
    return sorted(out)


def rung_below(height, rungs):
    """The highest archived rung strictly below `height`, or None.

    Strictly below, not at: replaying forward from a checkpoint at height N produces block N+1 onward,
    so a rung AT the target cannot produce the target. test_prune_bundles.py asserts the same rule for
    the deletion side, and the two must agree or a bundle could be deleted that cannot be rebuilt.
    """
    below = [r for r in rungs if r < height]
    return max(below) if below else None


def plan(heights, rungs):
    """(rung, lo, hi, missing) — the seed to use and the span to walk, or None when it cannot be done.

    `missing` is the heights with no rung below them; when non-empty the caller must refuse, because a
    replay for those would start at genesis.
    """
    heights = sorted(set(int(h) for h in heights))
    if not heights:
        return None
    missing = [h for h in heights if rung_below(h, rungs) is None]
    if missing:
        return {"rung": None, "lo": heights[0], "hi": heights[-1], "missing": missing}
    seed = min(rung_below(h, rungs) for h in heights)
    return {"rung": seed, "lo": heights[0], "hi": heights[-1], "missing": []}


def replay(bridge, scratch, rung_file, to_height, datadir, timeout_s):
    """Walk the bridge forward in `scratch`, seeded from `rung_file`, up to `to_height`.

    Returns (ok, message). The bridge is run with HAZYNC_BRIDGE_ONCE so it exits at the cap rather than
    polling for new blocks for ever.
    """
    os.makedirs(scratch, exist_ok=True)
    shutil.copy2(rung_file, os.path.join(scratch, "state.bin"))
    env = dict(os.environ)
    env.update({
        "HAZYNC_BRIDGE_OUT": scratch,        # ⛔ scratch, never the live store — see the note above
        "HAZYNC_BRIDGE_TO": str(to_height),
        "HAZYNC_BRIDGE_ONCE": "1",
        "HAZYNC_BITCOIN_DATADIR": datadir,
    })
    env.pop("HAZYNC_BRIDGE_EMIT_FROM", None)  # the whole point is to EMIT in this range
    try:
        p = subprocess.run([bridge, "bridge"], env=env, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, f"replay exceeded {timeout_s}s"
    if p.returncode != 0:
        return False, f"bridge exited {p.returncode}: {(p.stderr or p.stdout or '').strip()[-300:]}"
    # ⛔ A bridge that could not load the seed says so and starts at genesis. Catch that here rather than
    # discovering it six days later.
    if "resuming from checkpoint" not in (p.stdout or ""):
        return False, "bridge did not resume from the seed — it would replay from genesis"
    return True, (p.stdout or "").strip().splitlines()[-1][:200] if p.stdout else "ok"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--heights", help="comma-separated heights to regenerate")
    ap.add_argument("--from-waiting", action="store_true",
                    help="take heights from sponsor_bot.pending_work(need_bundle=False) minus pending_work()")
    ap.add_argument("--apply", action="store_true", help="actually replay; default is a dry run")
    ap.add_argument("--archive", default=os.environ.get("HAZYNC_CKPT_ARCHIVE",
                                                        "/srv/bulk/hazync/checkpoints"))
    ap.add_argument("--out", default=os.environ.get("HAZYNC_BRIDGE_OUT",
                                                    "/srv/bulk/hazync/bridge_bundles"))
    ap.add_argument("--scratch", default="/srv/bulk/hazync/regen-scratch")
    ap.add_argument("--bridge", default="/usr/local/bin/hazync-host-bridge")
    ap.add_argument("--datadir", default=os.environ.get("HAZYNC_BITCOIN_DATADIR",
                                                        "/var/lib/hazync/bitcoin-client"))
    ap.add_argument("--timeout", type=int, default=6 * 3600)
    a = ap.parse_args(argv)

    if a.from_waiting:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        try:
            import sponsor_bot
            coord = sponsor_bot.Coordinator()
            wanted = [h for _, h in sponsor_bot.pending_work(coord, need_bundle=False)]
            have = {h for _, h in sponsor_bot.pending_work(coord)}
            heights = [h for h in wanted if h not in have]
        except Exception as e:
            print(f"[regen] cannot check: sponsor bot unavailable ({e})", file=sys.stderr)
            return 2
    else:
        heights = [int(x) for x in (a.heights or "").split(",") if x.strip()]

    if not heights:
        print("[regen] nothing waiting on a bundle")
        return 0

    if not os.access(a.bridge, os.X_OK):
        print(f"[regen] cannot check: no bridge binary at {a.bridge}", file=sys.stderr)
        return 2

    rungs = archived_rungs(a.archive)
    p = plan(heights, rungs)
    print(f"[regen] {len(heights)} height(s) to make, {len(rungs)} archived rung(s)")
    if p["missing"]:
        print(f"[regen] cannot check: no archived checkpoint below {len(p['missing'])} height(s) "
              f"(lowest {min(p['missing'])}) — replaying those would start at GENESIS", file=sys.stderr)
        return 2

    span = p["hi"] - p["rung"]
    print(f"[regen] seed rung {p['rung']} -> walk to {p['hi']} ({span:,} blocks), keeping {len(heights)}")
    if not a.apply:
        print("[regen] DRY RUN — nothing replayed")
        return 0

    ok, msg = replay(a.bridge, a.scratch, os.path.join(a.archive, f"state_{p['rung']}.bin"),
                     p["hi"], a.datadir, a.timeout)
    if not ok:
        print(f"[regen] replay failed: {msg}", file=sys.stderr)
        return 1

    installed, absent = 0, []
    for h in heights:
        src = os.path.join(a.scratch, f"bundle_{h}.json")
        if os.path.exists(src):
            shutil.move(src, os.path.join(a.out, f"bundle_{h}.json"))
            installed += 1
        else:
            absent.append(h)
    shutil.rmtree(a.scratch, ignore_errors=True)   # discard the rest of the walk; only the asked-for kept

    print(f"[regen] installed {installed} bundle(s)")
    if absent:
        print(f"[regen] {len(absent)} requested height(s) were not produced: {absent[:10]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
