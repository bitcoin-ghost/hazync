#!/usr/bin/env bash
# Keep every distinct frame of a run, so an hour leaves a timelapse instead of one PNG.
#
# WHY THIS EXISTS. `publish.sh` rewrites a single `frame.png` and pushes it; the previous frame is
# gone. That is right for a live page and useless afterwards: the most legible artifact a tip run
# can produce is the hour compressed into a few seconds, and after the run there is nothing to
# compress. This copies each new frame aside as it appears.
#
#   frame-archive.sh <frame.png> <outdir> [interval_s]
#
# ⛔ DEDUPED BY CONTENT, NOT BY TIME. The renderer rewrites the file on a loop whether or not
# anything changed, so a naive copy-every-N-seconds stores thousands of identical frames and an hour
# of a waiting fleet outweighs the minutes that matter. Only a frame whose bytes differ is kept.
#
# ⚠ It copies rather than moves, and never writes to the source. The publisher owns that file.
set -uo pipefail

SRC="${1:?usage: frame-archive.sh <frame.png> <outdir> [interval_s]}"
OUT="${2:?usage: frame-archive.sh <frame.png> <outdir> [interval_s]}"
INT="${3:-5}"

mkdir -p "$OUT" || exit 1
last=""
kept=0
skipped=0

# ⚠ An ERR trap, not just EXIT: a mid-loop death must say so rather than read as a clean finish.
trap 'echo "[frame-archive] ⛔ ERR line $LINENO exit $?" >&2' ERR
trap 'echo "[frame-archive] stopping: kept $kept frame(s), skipped $skipped identical"; exit 0' INT TERM

echo "[frame-archive] $SRC -> $OUT every ${INT}s, keeping only frames whose bytes changed"
while :; do
    if [ -r "$SRC" ]; then
        # ⛔ Hash the file we are about to copy, not a second read of it. A frame rewritten between
        # the hash and the copy would be stored under the wrong fingerprint and dedupe silently.
        tmp="$OUT/.inflight.png"
        if cp -f "$SRC" "$tmp" 2>/dev/null; then
            sum="$(sha256sum "$tmp" 2>/dev/null | cut -c1-16)"
            if [ -n "$sum" ] && [ "$sum" != "$last" ]; then
                mv -f "$tmp" "$OUT/frame_$(date -u +%Y%m%dT%H%M%SZ)_$sum.png"
                last="$sum"
                kept=$((kept + 1))
            else
                rm -f "$tmp"
                skipped=$((skipped + 1))
            fi
        fi
    fi
    sleep "$INT"
done
