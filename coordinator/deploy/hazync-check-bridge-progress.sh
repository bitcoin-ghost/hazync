#!/usr/bin/env bash
# Integrity check: is the bridge still ADVANCING? (hazync#467)
# Installed as /usr/local/sbin/hazync-check-bridge-progress, invoked through hazync-run-check.
#
#   hazync-check-bridge-progress                      # check with the measured defaults
#   HAZYNC_BRIDGE_STALL_S=5400 hazync-check-bridge-progress
#
# ⛔ WHY THIS EXISTS. During catch-up the bridge is SILENT BY CONSTRUCTION and a stall is invisible.
#
#   * a crash pages, via hazync-bridge.service.d/alert.conf -- that path works and is not the gap
#   * hazync-check-bundle-gap detects a STALL, but it judges by BUNDLES ARRIVING at the coordinator.
#     Below HAZYNC_BRIDGE_EMIT_FROM the bridge advances accumulator state and writes NO bundles, so a
#     stalled bridge and a healthy one produce identical evidence: none.
#
# Measured 2026-09-22: a restart pushed the reload peak over MemoryHigh and the process sat in D state
# (mem_cgroup_handle_over_high) at 0% CPU for ~20 minutes, memory.pressure 97.3%, advancing nothing.
# It did not crash, so alert.conf stayed quiet; it emits no bundles, so bundle-gap had nothing to
# compare. The only evidence was `checkpoint @ N` lines silently not appearing in a journal nobody
# reads. A human happened to be watching. On an unattended night it would have burned until someone
# noticed the frontier had not moved.
#
# EXIT CODES, matching the other integrity checks so hazync-run-check can alert on it:
#   0  the bridge advanced within the window
#   1  it did NOT advance -- a stall
#   2  could not check (not running, no journal, no checkpoint lines yet)
set -uo pipefail

UNIT="${HAZYNC_BRIDGE_UNIT:-hazync-bridge}"

# ⚠ THE WINDOW IS DERIVED FROM THE MEASURED RATE, NOT GUESSED, AND IT IS GENEROUS.
# The bridge checkpoints every HAZYNC_BRIDGE_CKPT blocks (2000 live). At the rate measured on
# 2026-09-22 -- 0.638 / 0.655 / 0.699 s per block over three consecutive intervals -- that is roughly
# 22 minutes per checkpoint. Three missed intervals is the threshold, because:
#
#   * the walk rate is NOT flat (project_hazync_bridge_walk_rates: no flat tail, it degrades with
#     height), so a window sized to today's rate would cry wolf next week
#   * a reload after a restart legitimately takes minutes with no checkpoint written
#   * a check that cries wolf gets muted, and a muted check is worse than none
STALL_S="${HAZYNC_BRIDGE_STALL_S:-4200}"        # 70 min ~= 3 checkpoint intervals at the measured rate

say() { echo "$*"; }

if ! systemctl is-active --quiet "$UNIT"; then
    # ⛔ NOT RUNNING IS NOT "STALLED", AND IT IS NOT "FINE" EITHER. A deliberate stop is a human
    # decision the alerter should not second-guess; a crash is already covered by alert.conf. Either
    # way this check cannot answer its own question, and saying so is the honest outcome.
    say "cannot check: $UNIT is not active ($(systemctl is-active "$UNIT" 2>/dev/null))"
    exit 2
fi

# ⛔ TWO REGIMES, AND THIS CHECK ONLY KNEW ONE (hazync#487). While walking, the bridge writes
# `checkpoint @ N` every HAZYNC_BRIDGE_CKPT (2000) blocks. Once it CATCHES UP it advances one block
# at a time as each finalises and writes `caught up to N` — the next checkpoint is 2000 blocks away,
# which at the tip is about a fortnight of chain. So from the moment the bridge started doing its
# actual job, this check saw no progress line and paged hourly. Measured 2026-09-23: seven
# consecutive FAILEDs against a bridge that was advancing normally, which is precisely the
# cry-wolf-gets-muted failure the original comment warned about.
#
# `caught up to N` counts as progress. Both are read, and the newest wins.
last="$(journalctl -u "$UNIT" -o short-unix --no-pager -n 4000 2>/dev/null \
        | grep -oE '^[0-9]+\.[0-9]+ .*(checkpoint @|caught up to) [0-9]+' | tail -1)"

if [ -z "$last" ]; then
    # ⚠ A bridge that has only just started, or one whose journal has rotated past its last progress
    # line, is unknown -- not failing. Resuming from a 19 GB state file takes minutes before the
    # first line appears, and calling that a stall would alert on every restart.
    say "cannot check: no 'checkpoint @' or 'caught up to' line in the last 4000 entries for $UNIT"
    exit 2
fi

# ⛔ AT THE TIP, "HOW LONG AGO" IS THE WRONG QUESTION. A caught-up bridge is idle by construction
# between blocks, and Bitcoin's inter-block gaps are exponential — 70-minute quiet spells are normal
# and would page every time. The honest question there is POSITIONAL: is the bridge where it is
# supposed to be, i.e. at (node tip - finality)? If it is, it is not stalled however long it has sat
# there. Only if it is BEHIND that does elapsed time mean anything.
FINALITY="$(systemctl show "$UNIT" -p Environment --value 2>/dev/null \
            | tr ' ' '\n' | sed -n 's/^HAZYNC_BRIDGE_FINALITY=//p' | head -1)"
FINALITY="${FINALITY:-100}"
NODE_TIP=""
if command -v bitcoin-cli >/dev/null 2>&1; then
    DD="$(systemctl show "$UNIT" -p Environment --value 2>/dev/null \
          | tr ' ' '\n' | sed -n 's/^HAZYNC_BITCOIN_DATADIR=//p' | head -1)"
    # ⚠ Failure here is not an answer. If the node cannot be asked, fall through to the time-based
    # test rather than assuming the bridge is fine.
    NODE_TIP="$(bitcoin-cli ${DD:+-datadir="$DD"} getblockcount 2>/dev/null | tr -dc '0-9')"
fi

ts="${last%%.*}"
height="$(printf '%s' "$last" | grep -oE '(checkpoint @|caught up to) [0-9]+' | grep -oE '[0-9]+')"
now="$(date +%s)"
age=$(( now - ts ))

# ⚠ A NEGATIVE AGE MEANS THE CLOCK MOVED, NOT THAT THE BRIDGE IS FINE. Treat it as unknown rather
# than as a pass: a wall clock that stepped backwards would otherwise make every stall look fresh.
if [ "$age" -lt 0 ]; then
    say "cannot check: last checkpoint is ${age}s in the FUTURE (clock stepped?) — height $height"
    exit 2
fi

# The positional test, where it can be made: at the tip and nothing to do is HEALTHY.
if [ -n "$NODE_TIP" ] && [ -n "$height" ]; then
    want=$(( NODE_TIP - FINALITY ))
    if [ "$height" -ge "$want" ]; then
        say "ok: $UNIT is AT THE TIP — height $height, node tip $NODE_TIP, finality $FINALITY \
(last progress $((age / 60))m ago; idle between blocks is how a caught-up bridge looks)"
        exit 0
    fi
fi

# ⚠ HAZYNC_BRIDGE_NO_STALL_GUARD exists ONLY for test-bridge-progress.sh --control, which
# must be able to remove the guard and prove the test notices.
if [ "${HAZYNC_BRIDGE_NO_STALL_GUARD:-0}" != "1" ] && [ "$age" -gt "$STALL_S" ]; then
    say "STALLED: $UNIT last checkpointed at height $height, $((age / 60)) min ago (limit $((STALL_S / 60)) min).
The process may be alive but making no progress — check memory.pressure and process state:
  systemctl status $UNIT
  ps -o pid,stat,wchan:24 -p \$(pgrep -f hazync-host-bridge | head -1)
  cat /sys/fs/cgroup/system.slice/$UNIT.service/memory.pressure"
    exit 1
fi

say "ok: $UNIT checkpointed height $height $((age / 60))m ago (limit $((STALL_S / 60))m)"
exit 0
