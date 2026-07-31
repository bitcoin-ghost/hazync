#!/usr/bin/env bash
# Run N proof-party workers in parallel against a coordinator, and keep them running.
#
#   ./coordinator/run-workers.sh 4              # start 4 workers
#   ./coordinator/run-workers.sh 4 --stop       # stop them
#
# Env:
#   HAZYNC_HOST   path to the prover binary            (required)
#   COORD_URL     coordinator base URL                 (default https://bitcoinghost.org/hazync)
#   HAZYNC_BASE   Core/secp source root                (default $HOME/hazync-build)
#   LOG_DIR       per-worker logs                      (default $HOME/hazync-workers)
#
# One `hazync run` proves one BLOCK and exits, so each worker is a loop. Nothing is claimed, so a
# worker that dies simply stops — there is no lease to expire and nothing to hand back. Several in parallel keeps a
# GPU busy while others are fetching a bundle or waiting on the coordinator.
#
# The PRE-FLIGHT below is the important part. A worker whose guest id differs from the coordinator's
# expected id will prove happily and have every single submission rejected — burning GPU hours to
# produce receipts nothing will accept. That is exactly what happens if you forget to update the
# binary after a re-baseline, so this refuses to start rather than let it run.
set -uo pipefail

N="${1:-4}"
STOP="${2:-}"
COORD_URL="${COORD_URL:-https://bitcoinghost.org/hazync}"
LOG_DIR="${LOG_DIR:-$HOME/hazync-workers}"
CLI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/hazync"

if [ "$STOP" = "--stop" ]; then
    pkill -f 'hazync-worker-loop' 2>/dev/null
    pkill -f 'hazync run' 2>/dev/null
    sleep 2
    echo "stopped (remaining: $(pgrep -fc "hazync-worker-loop" 2>/dev/null || true))"
    exit 0
fi

: "${HAZYNC_HOST:?set HAZYNC_HOST to the prover binary}"
[ -x "$HAZYNC_HOST" ] || { echo "HAZYNC_HOST is not executable: $HAZYNC_HOST" >&2; exit 1; }
[ -x "$CLI" ] || { echo "contributor CLI not found next to this script: $CLI" >&2; exit 1; }
export HAZYNC_BASE="${HAZYNC_BASE:-$HOME/hazync-build}"
mkdir -p "$LOG_DIR"

mine=$("$HAZYNC_HOST" method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1)
want=$(curl -s --max-time 20 "$COORD_URL/api/meta" 2>/dev/null \
       | grep -oE '"method_id"[[:space:]]*:[[:space:]]*"[0-9a-f]{64}"' | grep -oE '[0-9a-f]{64}')

echo "worker guest id : ${mine:-<none>}"
echo "coordinator     : ${want:-<unreachable>}  ($COORD_URL)"
echo "seg-po2         : $("$HAZYNC_HOST" seg-po2 2>/dev/null | tail -1)"

if [ -z "$mine" ]; then
    echo "FATAL: could not read a guest id from $HAZYNC_HOST" >&2; exit 1
fi
if [ -n "$want" ] && [ "$mine" != "$want" ]; then
    echo >&2
    echo "FATAL: guest id mismatch — every proof this worker makes would be REJECTED." >&2
    echo "Rebuild against the canonical guest (prover/build-release.sh) or download the current" >&2
    echo "release binary; see reproduce/METHOD_ID." >&2
    exit 1
fi
[ -z "$want" ] && echo "WARNING: coordinator unreachable — starting anyway, id NOT confirmed" >&2

for i in $(seq 1 "$N"); do
    (
        exec -a "hazync-worker-loop-$i" bash -c '
            export BUNDLE_DIR="'"$LOG_DIR"'/bundles_'"$i"'"
            mkdir -p "$BUNDLE_DIR"
            cd "$(dirname "'"$CLI"'")"
            while true; do ./hazync run >> "'"$LOG_DIR"'/worker_'"$i"'.log" 2>&1 || sleep 3; done
        '
    ) </dev/null >/dev/null 2>&1 &
    disown
done

sleep 3
echo "started $(pgrep -fc "hazync-worker-loop" 2>/dev/null || true) worker(s); logs in $LOG_DIR"
echo "stop with: $0 $N --stop"
