#!/usr/bin/env bash
# Keep the bridge checkpoints it would otherwise throw away (hazync#347 item 6.6).
#
#   hazync-archive-checkpoint                 # archive if a rung is due
#   SPACING=25000 hazync-archive-checkpoint   # rung every N blocks (default 25000)
#   DRY=1 hazync-archive-checkpoint           # say what it would do, copy nothing
#
# WHY. The bridge checkpoints every HAZYNC_BRIDGE_CKPT blocks (2000 live) into ONE `state.bin`, which it
# overwrites each time — 125 writes in 24 h on the live coordinator, all to the same path. Nothing is kept.
#
# That matters because the live bridge also runs with HAZYNC_BRIDGE_EMIT_FROM=967500: below that height it
# advances state and writes NO bundle. So heights 418,269–967,499 are being walked past right now with
# neither a bundle nor a retained checkpoint behind them. Reaching any of them afterwards means replaying
# from genesis — about six days at the 533 ms/block measured on the live bridge at h≈706,000 (2026-09-18).
# A rung is ~8.6 GB against 3.9 TB free on /srv/bulk; 29 rungs to the tip is ~250 GB. Cheap insurance, and
# the window closes as the bridge walks.
#
# ⛔ WHY THE JOURNAL IS THE SOURCE OF TRUTH FOR THE HEIGHT, and why that is safe:
# `bridge_save_state()` is called BEFORE the "bridge: checkpoint @ N" line is printed (main.rs:3857 then
# :3861). So when N appears in the log, state.bin already holds N or later. A rung named from the journal is
# therefore at worst CONSERVATIVE — never labelled higher than it actually is. That direction matters:
# prune_bundles.py decides what may be deleted from `has_checkpoint_below()`, which parses the filename, and
# a rung labelled too HIGH would authorise deleting bundles it cannot actually rebuild.
#
# ⛔ AND WHY AN 8.6 GB COPY CANNOT TEAR: the bridge writes state.bin.tmp then renames (main.rs:3644,
# "atomic: never a torn state.bin"). A rename swaps the directory entry; this script's open read handle keeps
# the OLD inode and finishes reading it consistently. At 748 MB/s on /srv/bulk a copy is ~12 s against a
# checkpoint interval measured at 1168–1600 s, so it will not usually race at all — but correctness does not
# depend on that.
#
# EXIT CODES, matching the other integrity checks so `hazync-run-check` can alert on it:
#   0  ran; a rung was archived, or none was due
#   1  something is wrong (copy failed, verification failed)
#   2  could not check (no state.bin, no height, no space) — nothing is ever written on a 2
set -uo pipefail

DIR="${HAZYNC_BRIDGE_OUT:-/srv/bulk/hazync/bridge_bundles}"
ARCHIVE="${HAZYNC_CKPT_ARCHIVE:-/srv/bulk/hazync/checkpoints}"
SPACING="${SPACING:-25000}"
UNIT="${BRIDGE_UNIT:-hazync-bridge}"
DRY="${DRY:-0}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"

say() { echo "[ckpt] $*"; }

[ -s "$DIR/state.bin" ] || { say "cannot check: no $DIR/state.bin"; exit 2; }

# Height from the journal. See the note above: printed AFTER the save, so it never overstates what is on disk.
H=$(journalctl -u "$UNIT" -n 200 --no-pager 2>/dev/null \
      | grep -oE 'checkpoint @ [0-9]+' | tail -1 | grep -oE '[0-9]+')
[ -n "${H:-}" ] || { say "cannot check: no 'checkpoint @ N' line in the last 200 journal entries of $UNIT"; exit 2; }

mkdir -p "$ARCHIVE" || { say "cannot check: cannot create $ARCHIVE"; exit 2; }

# Is a rung due? Archive when this height crosses a new multiple of SPACING relative to the newest rung.
#
# ⛔ A GLOB, NOT `ls | sed`. shellcheck SC2010 rejects parsing ls output and it is right to: a filename
# carrying a newline or a glob character breaks the parse silently, and "silently wrong" is the worst
# failure mode for something that decides whether bundles may be deleted. The loop below cannot misparse
# — a name that is not exactly state_<digits>.bin is skipped rather than half-read.
LAST=0
for _f in "$ARCHIVE"/state_*.bin; do
    [ -e "$_f" ] || continue                       # no match: the glob stays literal
    _b=${_f##*/}; _b=${_b#state_}; _b=${_b%.bin}
    case "$_b" in ''|*[!0-9]*) continue ;; esac    # not a plain height — ignore it
    [ "$_b" -gt "$LAST" ] && LAST="$_b"
done
if [ "$((H - LAST))" -lt "$SPACING" ]; then
    say "height $H, newest rung $LAST, spacing $SPACING — not due (next at $((LAST + SPACING)))"
    exit 0
fi

SZ=$(stat -c%s "$DIR/state.bin")
FREE_GB=$(df -BG --output=avail "$ARCHIVE" | tail -1 | tr -dc '0-9')
NEED_GB=$(( (SZ / 1073741824) + 2 ))
if [ "${FREE_GB:-0}" -lt "$((NEED_GB + MIN_FREE_GB))" ]; then
    say "cannot check: ${FREE_GB}G free, need ${NEED_GB}G plus a ${MIN_FREE_GB}G floor"
    exit 2
fi

DEST="$ARCHIVE/state_${H}.bin"
[ -e "$DEST" ] && { say "rung $H already archived"; exit 0; }

if [ "$DRY" = "1" ]; then
    say "DRY: would archive height $H ($((SZ / 1048576)) MB) -> $DEST"
    exit 0
fi

# Copy to .tmp then rename, so a reader never sees a partial rung — the same discipline the bridge uses.
say "archiving height $H ($((SZ / 1048576)) MB) -> $DEST"
if ! cp "$DIR/state.bin" "$DEST.tmp"; then
    say "copy failed"; rm -f "$DEST.tmp"; exit 1
fi
COPIED=$(stat -c%s "$DEST.tmp")
# ⛔ Verify against the SOURCE AT COPY TIME, not against SZ: if the bridge checkpointed mid-copy, our handle
# read the old inode whole, and its size is the honest answer. A mismatch here means a real short read.
if [ "$COPIED" -lt "$SZ" ]; then
    say "verification failed: copied $COPIED bytes, source was at least $SZ"; rm -f "$DEST.tmp"; exit 1
fi
mv "$DEST.tmp" "$DEST" || { say "commit failed"; rm -f "$DEST.tmp"; exit 1; }

# Same reasoning as the LAST loop above: a glob cannot misparse a filename, `ls | grep` can.
N=0
for _f in "$ARCHIVE"/state_*.bin; do [ -e "$_f" ] && N=$((N + 1)); done
say "archived. $N rung(s), $(du -sh "$ARCHIVE" | cut -f1) total, $(df -BG --output=avail "$ARCHIVE" | tail -1 | tr -d ' ') free"
exit 0
