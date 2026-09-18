#!/usr/bin/env bash
# Install the starting checkpoint for the backfill walk (hazync#347).
#
#   hazync-backfill-seed          # copy the 230,000 rung into the backfill working directory
#   DRY=1 hazync-backfill-seed    # say what it would do, copy nothing
#   SEED=250000 hazync-backfill-seed
#
# WHY A SCRIPT AND NOT A cp. The backfill walks 41 hours from this one file, and both ways of getting it
# wrong are silent:
#
#   1. NO SEED AT ALL. bridge_load_state() returns None for a missing or unreadable state.bin and the
#      resume path treats that as "rebuild from genesis" (main.rs:3496). The walk starts at block 1,
#      looks completely healthy, and the first sign of trouble is the disk filling days later.
#
#   2. CLOBBERING A WALK IN PROGRESS. state.bin in the backfill directory is not just the seed, it IS the
#      walk's position — the bridge overwrites it every 25,000 blocks. Re-running this after the walk has
#      started would silently rewind it to 230,000 and throw away however many hours it had done.
#
# So: refuse if a state.bin is already there, refuse if the unit is running, and verify the copy.
#
# EXIT CODES, matching the integrity checks so hazync-run-check can alert on it:
#   0  the seed is in place (copied now, or already correct)
#   1  something is wrong (copy failed, verification failed)
#   2  could not check (no rung to seed from, no space, a walk is in progress) — nothing is written on a 2
set -uo pipefail

ARCHIVE="${HAZYNC_CKPT_ARCHIVE:-/srv/bulk/hazync/checkpoints}"
WORK="${HAZYNC_BRIDGE_OUT:-/srv/bulk/hazync/backfill}"
UNIT="${BACKFILL_UNIT:-hazync-bridge-backfill}"
SEED="${SEED:-230000}"
DRY="${DRY:-0}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"

say() { echo "[seed] $*"; }

SRC="$ARCHIVE/state_${SEED}.bin"
[ -s "$SRC" ] || { say "cannot check: no rung at $SRC"; exit 2; }

# ⛔ REFUSE WHILE THE WALK IS RUNNING. Overwriting state.bin under a live bridge would both rewind the
# walk and race the bridge's own rename of state.bin.tmp over it.
if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    say "cannot check: $UNIT is running — stop it before reseeding, or its position would be rewound"
    exit 2
fi

DEST="$WORK/state.bin"
if [ -e "$DEST" ]; then
    # Already seeded. If it is byte-identical to the rung the walk has not started; either way we must not
    # overwrite it, because past the first checkpoint this file is the walk's POSITION, not its seed.
    if cmp -s "$SRC" "$DEST"; then
        say "already seeded from rung $SEED and unchanged — nothing to do"
        exit 0
    fi
    say "cannot check: $DEST already exists and differs from rung $SEED."
    say "  That is a walk in progress, not a stale copy: the bridge overwrites state.bin every"
    say "  HAZYNC_BRIDGE_CKPT blocks. Delete it deliberately if you really mean to restart the walk."
    exit 2
fi

mkdir -p "$WORK" || { say "cannot check: cannot create $WORK"; exit 2; }

SZ=$(stat -c%s "$SRC")
FREE_GB=$(df -BG --output=avail "$WORK" | tail -1 | tr -dc '0-9')
NEED_GB=$(( (SZ / 1073741824) + 2 ))
if [ "${FREE_GB:-0}" -lt "$((NEED_GB + MIN_FREE_GB))" ]; then
    say "cannot check: ${FREE_GB}G free, need ${NEED_GB}G plus a ${MIN_FREE_GB}G floor"
    exit 2
fi

if [ "$DRY" = "1" ]; then
    say "DRY: would seed $WORK from rung $SEED ($((SZ / 1048576)) MB)"
    exit 0
fi

# .tmp then rename, so a bridge starting concurrently never sees a partial state.bin — the same
# discipline the bridge itself uses (main.rs:3644, "atomic: never a torn state.bin").
say "seeding $WORK from rung $SEED ($((SZ / 1048576)) MB)"
if ! cp "$SRC" "$DEST.tmp"; then
    say "copy failed"; rm -f "$DEST.tmp"; exit 1
fi
if ! cmp -s "$SRC" "$DEST.tmp"; then
    say "verification failed: the copy differs from $SRC"; rm -f "$DEST.tmp"; exit 1
fi
mv "$DEST.tmp" "$DEST" || { say "commit failed"; rm -f "$DEST.tmp"; exit 1; }

say "seeded. the walk will resume from height $SEED — confirm with:"
say "  journalctl -u $UNIT -f | grep 'resuming from checkpoint'"
say "⛔ if that line does NOT appear, the bridge did not load the seed and is walking from GENESIS."
exit 0
