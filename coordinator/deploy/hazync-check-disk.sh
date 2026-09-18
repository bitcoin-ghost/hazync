#!/usr/bin/env bash
# Free space on the filesystems the coordinator cannot do without (hazync#397).
#
#   hazync-check-disk                          # check the defaults
#   HAZYNC_DISK_FLOOR_GB=800 hazync-check-disk # a different floor
#   HAZYNC_DISK_PATHS="/srv/bulk" hazync-check-disk
#
# WHY THIS EXISTS. Until now NOTHING on this box watched free space. check-retention.py sounds like it
# might, and does not: it is the G1 gate, asserting every proven height has its own retained receipt.
# prune_bundles.py can reclaim space but has never been deployed, and cannot help much yet anyway --
# it only deletes blocks already proven and inside the spine, and the frontier is ~74,927, so it can
# reach only the small near-genesis bundles while the 1.2 TB is dominated by high blocks at ~17 MB.
#
# That mattered the moment the bridge was allowed to become tip-following (#397). Tip-following writes
# ~6.3 MB/block, about 9 GB/day at 144 blocks/day. Against the 3.9 TB free measured 2026-09-18 that is
# roughly 430 days -- comfortable, but only if somebody is told before it runs out, and nobody was.
#
# THE FLOOR IS A WARNING LINE, NOT A CLIFF. 500 GB is ~55 days of tip-following at the measured rate:
# enough notice to deploy pruning, raise a volume, or stop the bridge deliberately rather than
# discovering it when the coordinator, bitcoind and the proof store all fail at once.
#
# EXIT CODES, matching the other integrity checks so hazync-run-check can alert on it:
#   0  every path is above its floor
#   1  a path is below the floor
#   2  could not check (df gave nothing for a path) -- says nothing about free space
set -uo pipefail

PATHS="${HAZYNC_DISK_PATHS:-/srv/bulk /}"
FLOOR_GB="${HAZYNC_DISK_FLOOR_GB:-500}"

say() { echo "[disk] $*"; }
low=0
cannot=0
checked=0

for p in $PATHS; do
    avail=$(df -BG --output=avail "$p" 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -z "${avail:-}" ]; then
        say "cannot check: df returned nothing for $p"
        cannot=1
        continue
    fi
    checked=$((checked + 1))
    if [ "$avail" -lt "$FLOOR_GB" ]; then
        say "LOW: $p has ${avail}G free, below the ${FLOOR_GB}G floor"
        low=1
    else
        say "ok: $p has ${avail}G free (floor ${FLOOR_GB}G)"
    fi
done

# A real finding outranks an unreadable path: "below the floor" is definite, and exiting 2 would hide it.
if [ "$low" != 0 ]; then
    say "free space is below the floor — reclaim, extend, or stop what is writing"
    [ "$cannot" != 0 ] && say "(and at least one path could not be read — see above)"
    exit 1
fi
if [ "$cannot" != 0 ] || [ "$checked" = 0 ]; then
    say "COULD NOT CHECK — this says NOTHING about free space"
    exit 2
fi
say "all $checked path(s) above the ${FLOOR_GB}G floor"
exit 0
