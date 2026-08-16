#!/bin/bash
# Publish the archive node's height where the coordinator can read it.
#
# The coordinator runs as `hazync`; bitcoind's datadir is mode 700 and its cookie 600, both owned by
# root. So the coordinator cannot ask the node how tall the chain is, and before this existed it fell
# back to a compiled-in constant (TIP_HEIGHT) that was stale the day after it was set — the public
# board advertised a chain height of 958,301 while the node was at 962,795.
#
# Runs as root from a timer. Writes atomically, and writes NOTHING on failure: the coordinator treats a
# file older than TIP_FILE_MAX_AGE as absent, so a broken node degrades to the old floor behaviour
# instead of pinning the board to a number that has quietly stopped moving.
set -u

DATADIR="${HAZYNC_BITCOIN_DATADIR:-/root/.bitcoin}"
OUT="${TIP_FILE:-/var/lib/hazync/node_tip}"
OWNER="${TIP_FILE_OWNER:-hazync:hazync}"

h=$(bitcoin-cli -datadir="$DATADIR" getblockcount 2>/dev/null)

# Refuse anything that is not a plain positive integer. `getblockcount` on a node that is still starting
# up prints an error to stderr and nothing to stdout, and an empty file would parse as garbage.
case "$h" in
    ''|*[!0-9]*) echo "hazync-node-tip: no usable height from bitcoin-cli (got '${h}')" >&2; exit 1 ;;
esac
[ "$h" -gt 0 ] || { echo "hazync-node-tip: height 0, refusing to publish" >&2; exit 1; }

tmp="${OUT}.tmp.$$"
printf '%s\n' "$h" > "$tmp" || exit 1
chown "$OWNER" "$tmp" 2>/dev/null || true      # readable by the coordinator; not fatal if the user differs
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"                            # atomic: a reader never sees a half-written height
