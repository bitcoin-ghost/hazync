#!/bin/bash
# Sample the bridge's memory against the size of the state it is holding. Installed as
# /usr/local/sbin/hazync-bridge-mem-sampler and run by hazync-bridge-mem-sampler.service.
#
# WHY IT EXISTS. hazync#350 asks whether coin metadata has to go to disk. That is a question about
# how bridge RSS scales with the UTXO set, and the only way to answer it is to have watched. The
# original of this script was started on 2026-09-15 as a transient `systemd-run` unit with no file on
# disk at all, then made persistent on 2026-09-17 — and lived only on one box until 2026-09-27,
# undeclared in this repo. It is the sole record behind the #435 MemoryHigh sizing.
#
# ⛔⛔ IT MUST SAY WHY A SAMPLE IS EMPTY. The original wrote `rss_mib=? peak_mib=?` and nothing else
# when it could not read the process. hazync-proof's bridge unit was REMOVED when the bridge moved to
# the tip box, so `systemctl show -p MainPID` answered 0, /proc/0/status did not exist, and the
# sampler wrote 1,188 consecutive blind samples between 2026-09-20 and 2026-09-27 — seven days — that
# look exactly like samples. Nothing in the log said the unit was gone. A `why=` field now names the
# reason, so a log that is collecting nothing says so in words.
#
# ⚠ WHAT THIS MEASURES DEPENDS ON WHICH BRIDGE IT WATCHES, and the two are not the same measurement:
#   * the HISTORY WALK (hazync-proof, 2026-09-15..20) grows its UTXO set from ~1M to ~129M coins as
#     it walks, so its samples ARE the memory-vs-UTXO curve #350 needs. 494 real samples, ending at
#     bundle 418,268 where the walk stopped. That curve is as complete as the walk got.
#   * the TIP BRIDGE (hazync-coord) starts at HAZYNC_BRIDGE_EMIT_FROM with the whole UTXO set already
#     loaded. It yields ONE point, repeatedly. That is still worth having — it is the check that the
#     tip bridge's footprint stays under the MemoryHigh set in #435 — but it is NOT a curve, and
#     reading it as one would be wrong.
set -uo pipefail

UNIT="${HAZYNC_SAMPLER_UNIT:-hazync-bridge}"
LOG="${HAZYNC_SAMPLER_LOG:-/var/log/hazync/bridge-mem.log}"
INTERVAL="${HAZYNC_SAMPLER_INTERVAL:-600}"
BUNDLE_DIR="${HAZYNC_SAMPLER_BUNDLE_DIR:-}"
STATE_BIN="${HAZYNC_SAMPLER_STATE_BIN:-}"
# ⚠ `du` on the bundle directory is a FULL TREE WALK of ~1.2 TB across ~1M files. At every sample
# that is real, repeated I/O on the box whose disk we have already had to go and reclaim. Sample it
# once every DU_EVERY rounds (default 6 = hourly at a 10 min interval) and carry the last value.
DU_EVERY="${HAZYNC_SAMPLER_DU_EVERY:-6}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null

# Probed once, not every ten minutes: journald's own grep (systemd 237+) searches the whole journal.
have_g=no
journalctl --help 2>/dev/null | grep -q -- ' -g' && have_g=yes

round=0
bundles_gib='-'
while true; do
    round=$((round + 1))
    why=ok
    rss=; hwm=

    load=$(systemctl show "$UNIT" -p LoadState --value 2>/dev/null)
    act=$(systemctl is-active "$UNIT" 2>/dev/null || true)
    pid=$(systemctl show "$UNIT" -p MainPID --value 2>/dev/null)

    # ⛔ Distinguish the three ways this comes back empty. They mean completely different things:
    # a unit that does not exist on this box is a MISPLACED SAMPLER, a unit that is inactive is a
    # STOPPED BRIDGE, and a live unit whose /proc entry vanished is a restart caught mid-sample.
    if [ "$load" != loaded ]; then
        why="unit-${load:-unknown}"
    elif [ "$act" != active ]; then
        why="unit-${act:-unknown}"
    elif [ -z "$pid" ] || [ "$pid" = 0 ]; then
        why=no-mainpid
    elif [ ! -r "/proc/$pid/status" ]; then
        why=proc-unreadable
    else
        rss=$(awk '/^VmRSS:/{print int($2/1024)}' "/proc/$pid/status" 2>/dev/null)
        hwm=$(awk '/^VmHWM:/{print int($2/1024)}' "/proc/$pid/status" 2>/dev/null)
        [ -n "$rss" ] || why=no-vmrss
    fi

    # ⛔ ANCHOR THE BUNDLE NAME. `sed -E 's/bundle_([0-9]+).json/\1/'` leaves the tail of
    # `bundle_968854.json.tmp` behind as `968854.tmp`, which `sort -n | tail -1` then happily reports
    # as the newest height — a height whose bundle does not exist yet. That exact bug shipped in the
    # tip ledger and had the fleet chasing three tips in a row that were not there.
    hi='-'
    if [ -n "$BUNDLE_DIR" ] && [ -d "$BUNDLE_DIR" ]; then
        hi=$(ls -U "$BUNDLE_DIR" 2>/dev/null \
             | sed -nE 's/^bundle_([0-9]+)\.json$/\1/p' | sort -n | tail -1)
        hi="${hi:--}"
    fi

    # ⛔ TWO SHAPES, AND THE WINDOW WAS TUNED FOR THE WRONG BRIDGE. The original read the last 400
    # journal lines and matched `@ ([0-9]+) \(`. Both parts fail on the tip box:
    #
    #   bridge: checkpoint @ 967457 (165224019 utxos, 165224019 leaves)                    <- the WRITE
    #   bridge: resuming from checkpoint @ height 968136 (165192074 utxos, ...)            <- the RESUME
    #
    # The resume line says `@ height N`, so `@ ([0-9]+)` does not match it — and it is usually the most
    # recent of the two. Worse, the tip bridge logs ~2 lines per block at one block every ~10 min and
    # checkpoints every 2,000 blocks, so 400 lines covers ~a day and a checkpoint lands ~fortnightly:
    # the field would have read `-` essentially for ever. Measured 2026-09-27: 0 matches in the last
    # 400 lines, 893 in the last 20,000.
    #
    # ⚠ `-g` greps inside journald instead of streaming lines to us — it searches the WHOLE journal and
    # measured 3x faster than piping 20,000 lines through grep. Kept behind a capability test because a
    # journald without it must degrade to the window, not to silence.
    # ⚠ ckpt_src= names WHICH line it came from. "last checkpoint written" and "where this run resumed"
    # are different facts and must never be read as one.
    ckpt_line=
    if [ "$have_g" = yes ]; then
        ckpt_line=$(journalctl -u "$UNIT" -o cat --no-pager -g 'checkpoint @' -n 1 2>/dev/null | tail -1)
    fi
    [ -n "$ckpt_line" ] || ckpt_line=$(journalctl -u "$UNIT" -o cat --no-pager -n "${HAZYNC_SAMPLER_JOURNAL_LINES:-20000}" 2>/dev/null \
                                       | grep 'checkpoint @' | tail -1)
    ckpt=$(printf '%s' "$ckpt_line" \
           | sed -nE 's/.*checkpoint @ (height )?([0-9]+) \(([0-9]+) utxos.*/\2 \3/p')
    case "${ckpt:+set}${ckpt_line}" in
        set*resuming*) ckpt_src='resume' ;;
        set*)          ckpt_src='write' ;;
        *)             ckpt_src='none' ;;
    esac

    state_mb='-'
    if [ -n "$STATE_BIN" ] && [ -f "$STATE_BIN" ]; then
        state_mb=$(( $(stat -c %s "$STATE_BIN" 2>/dev/null || echo 0) / 1000000 ))
    fi

    if [ -n "$BUNDLE_DIR" ] && [ -d "$BUNDLE_DIR" ] \
       && { [ "$bundles_gib" = '-' ] || [ $((round % DU_EVERY)) -eq 1 ]; }; then
        bundles_gib=$(du -s --block-size=1G "$BUNDLE_DIR" 2>/dev/null | cut -f1)
        bundles_gib="${bundles_gib:--}"
    fi

    printf '%s unit=%s active=%s why=%s bundle=%s ckpt_src=%s ckpt_height_utxos=%s rss_mib=%s peak_mib=%s avail_mib=%s state_bin_mb=%s bundles_gib=%s\n' \
        "$(date -u +%FT%TZ)" "$UNIT" "${act:-unknown}" "$why" "$hi" "$ckpt_src" "${ckpt:--}" \
        "${rss:--}" "${hwm:--}" \
        "$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)" \
        "$state_mb" "$bundles_gib" >> "$LOG"

    sleep "$INTERVAL"
done
