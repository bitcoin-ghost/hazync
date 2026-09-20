#!/usr/bin/env python3
"""Delete bridge bundles whose block is finished for good, keeping a way back (hazync#347).

    prune_bundles.py                      # DRY RUN: report what it would delete, touch nothing
    prune_bundles.py --apply              # actually delete
    prune_bundles.py --margin 10000       # blocks below the spine top to keep (default 10000)

Bundles are the per-block witness files the bridge writes. The coordinator serves them at
`/api/witness/<n>`, uses them to decide which heights can be claimed, and the sponsor bot reads them.
They are the largest thing on the coordinator's disk and they grow with height: measured 2026-09-17,
5.4 KB near genesis against 2.18 MB at 230,000, 95 GB for the first 230,000 blocks.

⛔ DELETION IS REAL. `bundle_path()` falls back to a legacy `block_<n>.json`, but that directory is
EMPTY on the live coordinator (checked 2026-09-17), so removing a bundle genuinely removes the ability
to serve or claim that height. Every condition below is load-bearing, not belt-and-braces.

THE RULE (agreed with the operator 2026-09-15, #347). Delete `bundle_N.json` only when ALL hold:

  1. Block N has a verified proof on the board.
  2. N is inside the genesis-anchored spine.
  3. That proof's receipt is confirmed in BOTH R2 and B2.
  4. N is at least `--margin` blocks below the spine's top, AND the latest nightly re-verify passed.

Condition 4's second half reads the marker `hazync-run-check` writes: `$CHECK_STATE_DIR/check-proofs`
holds "fails last_alert", so a leading 0 means the most recent `check-proofs` run held. A check exits
0 when it holds, 1 on an integrity failure and 2 when it could not check; 1 and 2 both count as
failing, and this job treats "could not tell" as failing too.

⛔ AND A WAY BACK MUST EXIST. #347's recovery story is "rebuild from the nearest archived checkpoint".
The bridge today keeps only its latest `state.bin` and overwrites it (`HAZYNC_BRIDGE_CKPT=2000`), so
until roadmap item 6.6 archives them, a deleted bundle is rebuildable ONLY by replaying from genesis.
That is not catastrophic — the bridge measured 0.145 s/block at h=230,000 on 2026-09-17, so 230,000
blocks is about 9.3 hours — but it is not a cost to incur silently. So this job REFUSES to delete a
height with no archived checkpoint below it unless `--no-require-checkpoint` is passed deliberately.
With no archive directory at all, it reports and deletes nothing, which is the correct state today.

EXIT CODES, matching the integrity checks so `hazync-run-check` can alert on it:
  0  ran, and everything it examined was consistent (whether or not it deleted anything)
  1  something is wrong and a human should look
  2  could not check (offsite unreachable, ledger unreadable) — NOTHING is ever deleted on a 2

WHAT THIS DOES NOT DO: it does not archive checkpoints (6.6), does not touch proofs or the spine, and
never deletes a bundle at or above the spine top however old it looks.
"""

import argparse
import json
import os
import sqlite3
import sys


# ── the decision ────────────────────────────────────────────────────────────────────────────────────

def prune_decision(h, *, verified, in_spine, in_r2, in_b2, spine_top, margin,
                   reverify_ok, checkpoint_below, require_checkpoint=True):
    """(delete?, why_kept). `why_kept` is empty when deleting.

    Returning the REASON matters: a dry run that prints "kept 229,999" and nothing else cannot be
    acted on, and the first question anyone asks of this job is "why is it not deleting anything".

    Order is deliberate. The two global gates — the nightly re-verify and the way back — are checked
    first, so a failing board or a missing archive produces one clear reason for every height rather
    than a per-height answer that hides it.
    """
    if not reverify_ok:
        return False, "the latest nightly re-verify did not pass"
    if require_checkpoint and not checkpoint_below:
        return False, "no archived checkpoint below it to rebuild from (hazync#347 6.6)"
    if not verified:
        return False, "no verified proof on the board"
    if not in_spine:
        return False, "not inside the genesis-anchored spine"
    if not in_r2:
        return False, "receipt not confirmed in R2"
    if not in_b2:
        return False, "receipt not confirmed in B2"
    if h > spine_top - margin:
        return False, f"within {margin} blocks of the spine top ({spine_top})"
    return True, ""


# ── reading the world ───────────────────────────────────────────────────────────────────────────────

def spine_top(spine_dir):
    """The genesis-anchored spine's top height, or 0 if there is no spine yet.

    Reads the small json the coordinator writes, exactly as `server.spine_head()` does — never the
    receipt, which is hundreds of KB and says nothing this needs.
    """
    try:
        with open(os.path.join(spine_dir, "spine.json")) as f:
            return int(json.load(f).get("hi") or 0)
    except Exception:
        return 0


def verified_single_heights(conn):
    """Heights with a verified single-block proof on the board.

    Single-block ids only. A wide range `lo-hi` proves its span, but this job deletes per-block
    bundles and pairing a wide proof with one block's bundle is exactly the kind of inference that
    should not stand between a file and `rm`.
    """
    out = {}
    for row in conn.execute("SELECT id, lo, hi, receipt_sha FROM ranges WHERE status='verified'"):
        if row["lo"] == row["hi"] and row["receipt_sha"]:
            out[int(row["lo"])] = row["id"]
    return out


def reverify_passed(state_dir, name="check-proofs"):
    """(passed, why). `hazync-run-check` writes "<fails> <last_alert>"; 0 fails means it held.

    ⛔ Unreadable or absent is NOT a pass. The marker missing means nobody has verified these proofs
    on this box, which is precisely when deleting their bundles is least safe.
    """
    p = os.path.join(state_dir, name)
    try:
        with open(p) as f:
            fails = f.read().split()[0]
        return (fails == "0"), ("" if fails == "0" else f"{name} reports {fails} consecutive failures")
    except Exception as e:
        return False, f"cannot read {p}: {e.__class__.__name__}"


def archived_checkpoints(archive_dir):
    """Heights for which an archived bridge checkpoint exists, newest-first-sortable.

    Looks for `state_<height>.bin`, which is what an archiving bridge would write. Returns an empty
    list when the directory does not exist — the state today, and the reason this job deletes nothing
    until 6.6 lands.
    """
    if not archive_dir or not os.path.isdir(archive_dir):
        return []
    hs = []
    for n in os.listdir(archive_dir):
        if n.startswith("state_") and n.endswith(".bin"):
            try:
                hs.append(int(n[len("state_"):-len(".bin")]))
            except ValueError:
                continue
    return sorted(hs)


def has_checkpoint_below(h, checkpoints):
    """Is there an archived checkpoint strictly below `h` to replay forward from?"""
    return any(c < h for c in checkpoints)


def bundle_file(bridge_dir, h):
    return os.path.join(bridge_dir, f"bundle_{h}.json")


# ── main ────────────────────────────────────────────────────────────────────────────────────────────

def main(argv=None, confirmed_names=None):
    """`confirmed_names` is an injection point: a dict {store: set(proof names)} the offsite check
    produced. Injectable so the tests exercise every condition without touching R2 or B2 — the same
    shape `hazync-offsite-proofs.main(argv, client=...)` uses."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete; default is a dry run")
    ap.add_argument("--margin", type=int, default=10000)
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"))
    ap.add_argument("--spine", default=os.environ.get("COORD_SPINE", "/var/lib/hazync/spine"))
    ap.add_argument("--bundles", default=os.environ.get("HAZYNC_BRIDGE_OUT",
                                                       "/var/lib/hazync/bridge_bundles"))
    ap.add_argument("--checkpoints", default=os.environ.get("HAZYNC_CKPT_ARCHIVE", ""))
    ap.add_argument("--state-dir", default=os.environ.get("CHECK_STATE_DIR", "/var/lib/hazync-checks"))
    ap.add_argument("--no-require-checkpoint", action="store_true",
                    help="delete even with no archived checkpoint below (replay would start at genesis)")
    a = ap.parse_args(argv)

    ok, why = reverify_passed(a.state_dir)
    top = spine_top(a.spine)
    ckpts = archived_checkpoints(a.checkpoints)

    # ⛔ AN UNCONFIGURED CHECKPOINT ARCHIVE IS "CANNOT CHECK", NOT "NOTHING TO PRUNE".
    # `--checkpoints` defaults to $HAZYNC_CKPT_ARCHIVE else "", and archived_checkpoints("") returns []
    # through its own `if not archive_dir` guard. With no rungs every height is then kept for "no
    # archived checkpoint below it", and the run prints a confident `would delete 0 bundle(s), 0.00 GB`
    # -- indistinguishable from a healthy box with nothing due. Measured 2026-09-20 on server 1: a
    # systemd-run invocation that omitted the variable reported exactly that while the real archive held
    # 24 rungs. `--no-require-checkpoint` says the rungs are deliberately irrelevant, so it is exempt.
    if not a.no_require_checkpoint and not os.path.isdir(a.checkpoints or ""):
        print(f"[prune] cannot check: no checkpoint archive at {a.checkpoints or '<unset>'} "
              "(set HAZYNC_CKPT_ARCHIVE or pass --checkpoints; pass --no-require-checkpoint only if "
              "replaying from genesis is genuinely intended)", file=sys.stderr)
        return 2

    if not os.path.isdir(a.bundles):
        print(f"[prune] cannot check: no bundle directory at {a.bundles}", file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(a.db, timeout=30)
        conn.row_factory = sqlite3.Row
        verified = verified_single_heights(conn)
        conn.close()
    except Exception as e:
        print(f"[prune] cannot check: ledger unreadable ({e})", file=sys.stderr)
        return 2

    if confirmed_names is None:
        print("[prune] cannot check: no offsite confirmation supplied "
              "(run hazync-offsite-proofs check for r2 and b2 first)", file=sys.stderr)
        return 2
    r2, b2 = confirmed_names.get("r2", set()), confirmed_names.get("b2", set())

    heights = []
    for n in os.listdir(a.bundles):
        if n.startswith("bundle_") and n.endswith(".json"):
            try:
                heights.append(int(n[len("bundle_"):-len(".json")]))
            except ValueError:
                continue
    heights.sort()

    # ⛔ AND AN EMPTY BUNDLE DIRECTORY IS ALSO "CANNOT CHECK". `os.path.isdir` above passes on a
    # directory that exists and holds nothing, which is exactly what a stale path looks like: on server 1
    # the pre-/srv/bulk `/var/lib/hazync/bridge_bundles` still exists with 0 files while the live store
    # holds 418,269, so a run that inherited the old default listed it happily and concluded "would
    # delete 0". A coordinator with no bundles at all is not a state this job should reason about.
    if not heights:
        print(f"[prune] cannot check: no bundle_<height>.json files under {a.bundles} "
              "(the directory exists but is empty — check HAZYNC_BRIDGE_OUT points at the live store)",
              file=sys.stderr)
        return 2

    doomed, kept_reasons, freed = [], {}, 0
    for h in heights:
        rid = verified.get(h)
        name = f"proof_{rid}.bin" if rid else None
        delete, why_kept = prune_decision(
            h,
            verified=rid is not None,
            in_spine=0 < h <= top,
            in_r2=bool(name) and name in r2,
            in_b2=bool(name) and name in b2,
            spine_top=top, margin=a.margin,
            reverify_ok=ok,
            checkpoint_below=has_checkpoint_below(h, ckpts),
            require_checkpoint=not a.no_require_checkpoint,
        )
        if delete:
            doomed.append(h)
            try:
                freed += os.path.getsize(bundle_file(a.bundles, h))
            except OSError:
                pass
        else:
            kept_reasons[why_kept] = kept_reasons.get(why_kept, 0) + 1

    print(f"[prune] spine top {top}, margin {a.margin}, "
          f"{len(heights)} bundles, {len(ckpts)} archived checkpoint(s)")
    if not ok:
        print(f"[prune] GATE: {why} — nothing will be deleted")
    for reason, n in sorted(kept_reasons.items(), key=lambda kv: -kv[1]):
        print(f"[prune]   kept {n:>7}: {reason}")
    print(f"[prune] {'deleting' if a.apply else 'would delete'} {len(doomed)} bundle(s), "
          f"{freed / 1e9:.2f} GB")

    if a.apply:
        gone = 0
        for h in doomed:
            try:
                os.remove(bundle_file(a.bundles, h))
                gone += 1
            except OSError as e:
                print(f"[prune] could not delete bundle_{h}.json: {e}", file=sys.stderr)
                return 1
        print(f"[prune] deleted {gone} bundle(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
