#!/usr/bin/env bash
# Absorb folded ranges into the genesis-anchored spine — PERIODICALLY, not continuously.
#
# ⛔ WHY PERIODIC IS CHEAPER, AND IT IS NOT A SMALL DIFFERENCE. `extend-spine` costs the SAME
# whatever the chunk width, and the spine always takes the widest chunk available. So the price per
# block is set entirely by how far the fold tree has collapsed. Measured on this pod:
#
#     chasing the fold frontier    288 blocks / 840 s   chunks of 2 and 4
#     after two days of folding  8,640 blocks / one pass  chunks up to 4,096
#
# A continuous spine worker lives at the frontier and pays the first price for ever. This one sleeps,
# lets the folding build the tree, then swallows it whole.
#
# ⚠ It also leaves the GPU to the fold loops ~99% of the time, which is what makes the tree collapse
# in the first place. Anchoring harder would make anchoring more expensive.
#
#   ANCHOR_INTERVAL   seconds between passes (default 21600 = 6 h)
#   ANCHOR_MAX_SECS   give a single pass at most this long (default 3600)
set -uo pipefail

INTERVAL="${ANCHOR_INTERVAL:-21600}"
MAX="${ANCHOR_MAX_SECS:-3600}"
cd /workspace || exit 1
export HAZYNC_HOST="${HAZYNC_HOST:-/workspace/hazync-host-cuda}"

say() { echo "[anchor] $(date -u +%FT%TZ) $*"; }

# ⚠ An ERR trap, not just EXIT: a loop that dies mid-pass must say so rather than read as a clean
# finish. If this stops entirely, the coordinator's check-spine notices once the spine passes its
# 72 h ceiling — so a silent death is caught, just not quickly.
trap 'echo "[anchor] ⛔ ERR line $LINENO exit $?" >&2' ERR
trap 'say "stopping on signal"; exit 0' INT TERM

say "started; a pass every $((INTERVAL / 3600)) h, each capped at $((MAX / 60)) min"
while :; do
    say "pass starting"
    # ⚠ The exit status of `timeout` is what matters, not the pipeline's — capture it before the pipe.
    out=$(timeout "$MAX" python3 ./hazync-worker spine 2>&1); rc=$?
    printf '%s\n' "$out" | tail -4 | sed 's/^/  /'
    if [ "$rc" -eq 124 ]; then
        say "⛔ the pass hit the ${MAX}s cap — the backlog is deeper than one pass, next one continues"
    elif [ "$rc" -ne 0 ]; then
        say "⛔ the pass exited $rc"
    else
        # ⚠ "absorbed 0" is the NORMAL state between passes, not a fault: it means the spine is level
        # with what folding has produced. Saying so keeps the log readable rather than alarming.
        printf '%s' "$out" | grep -q "absorbed" && say "pass done" || say "pass done — nothing to absorb"
    fi
    say "sleeping ${INTERVAL}s"
    sleep "$INTERVAL"
done
