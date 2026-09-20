#!/usr/bin/env bash
# Integrity check: are the tip bundles the coordinator is serving actually complete? (hazync-admin#2)
# Installed as /usr/local/sbin/hazync-check-bundle-gap and invoked through hazync-run-check.
#
# ⛔ WHY THIS RUNS HERE AND NOT ON THE BOX THAT SENDS THEM. The tip bridge pushes over a WRITE-ONLY
# channel -- `rrsync -wo` -- so it cannot list what arrived and can never report a bundle that went
# missing. The coordinator is the side that knows what it is missing, and it is the side that suffers:
# `/api/vranges` derives the work on offer from `bundle_path(h) is None`, so a missing bundle makes its
# block silently unclaimable. No error appears anywhere; the block is simply never offered to a worker.
# That is the failure this check exists to make loud.
#
# TWO FAILURES, DELIBERATELY DIFFERENT:
#
#   GAP   a height missing BETWEEN two bundles we hold. Unambiguous -- we received n and n+2, so n+1 was
#         written on the tip box and did not survive the trip.
#   STALL the highest bundle has not advanced across several runs while the chain has. Cannot be judged
#         from a single reading: during catch-up the bridge is legitimately hundreds of thousands of
#         blocks behind the tip, so an absolute "how far behind" test would cry wolf for two days and be
#         muted. Progress over time is the only honest signal.
#
# Exit codes follow the check contract: 0 holds, 1 gap or stall, 2 could not check.
set -uo pipefail

DIR="${HAZYNC_BRIDGE_OUT:-/srv/bulk/hazync/bridge_bundles}"
EMIT_FROM="${HAZYNC_BRIDGE_EMIT_FROM:-967500}"
STATE_DIR="${CHECK_STATE_DIR:-/var/lib/hazync-checks}"
STATE="$STATE_DIR/bundle-gap.state"
STALL_RUNS="${BUNDLE_STALL_RUNS:-4}"
NODE_TIP_FILE="${NODE_TIP_FILE:-/var/lib/hazync/node_tip}"

[ -d "$DIR" ] || { echo "[bundle-gap] cannot check: no bundle directory at $DIR" >&2; exit 2; }
mkdir -p "$STATE_DIR" 2>/dev/null || { echo "[bundle-gap] cannot check: cannot create $STATE_DIR" >&2; exit 2; }

# Heights at or above EMIT_FROM -- the ones the tip box is responsible for. Everything below predates the
# split and is not this check's business.
HEIGHTS=$(find "$DIR" -maxdepth 1 -type f -name 'bundle_*.json' -printf '%f\n' 2>/dev/null |
          sed -n 's/^bundle_\([0-9][0-9]*\)\.json$/\1/p' |
          awk -v e="$EMIT_FROM" '$1 >= e' | sort -n)

# ⛔ `grep -c` would print 0 and exit 1 here, turning "no tip bundles yet" into a failure under pipefail.
N=$(printf '%s\n' "$HEIGHTS" | sed '/^$/d' | wc -l)

if [ "$N" -eq 0 ]; then
    echo "[bundle-gap] no tip bundles yet (none at or above $EMIT_FROM) — the stream has not started"
    rm -f "$STATE"
    exit 0
fi

LO=$(printf '%s\n' "$HEIGHTS" | sed '/^$/d' | head -1)
HI=$(printf '%s\n' "$HEIGHTS" | sed '/^$/d' | tail -1)
SPAN=$(( HI - LO + 1 ))
MISSING=$(( SPAN - N ))

if [ "$MISSING" -gt 0 ]; then
    echo "[bundle-gap] GAP: hold $N bundle(s) spanning $LO..$HI, so $MISSING height(s) are missing"
    # Name a few, so the alert is actionable rather than just alarming.
    printf '%s\n' "$HEIGHTS" | sed '/^$/d' | awk -v lo="$LO" -v hi="$HI" '
        { seen[$1] = 1 }
        END { n = 0
              for (h = lo; h <= hi && n < 10; h++) if (!(h in seen)) { print "  missing: bundle_" h ".json"; n++ }
              if (n == 10) print "  ... (first 10 shown)" }'
    echo "[bundle-gap] a missing bundle makes its block unclaimable — /api/vranges will never offer it"
    exit 1
fi

# --- stall: has HI advanced since last time? -------------------------------------------------------
PREV_HI=0; RUNS=0
if [ -r "$STATE" ]; then
    read -r PREV_HI RUNS < "$STATE" 2>/dev/null || { PREV_HI=0; RUNS=0; }
    case "$PREV_HI" in ''|*[!0-9]*) PREV_HI=0 ;; esac
    case "$RUNS"    in ''|*[!0-9]*) RUNS=0 ;; esac
fi

TIP=""
[ -r "$NODE_TIP_FILE" ] && TIP=$(tr -dc '0-9' < "$NODE_TIP_FILE")

if [ "$HI" -gt "$PREV_HI" ]; then
    RUNS=0
else
    RUNS=$(( RUNS + 1 ))
fi
printf '%s %s\n' "$HI" "$RUNS" > "$STATE"

if [ "$RUNS" -ge "$STALL_RUNS" ] && [ -n "$TIP" ] && [ "$HI" -lt "$TIP" ]; then
    echo "[bundle-gap] STALL: highest tip bundle stuck at $HI for $RUNS runs while the node is at $TIP"
    echo "[bundle-gap] contiguous $LO..$HI ($N bundles) — nothing is MISSING, nothing is ARRIVING"
    exit 1
fi

echo "[bundle-gap] holds: $N contiguous tip bundle(s) $LO..$HI${TIP:+, node at $TIP} (no gap, advancing)"
exit 0
