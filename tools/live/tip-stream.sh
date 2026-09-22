#!/bin/bash
# Live per-card telemetry for the tip run: one PERSISTENT ssh per pod, one line per second.
#
#   HAZYNC_RUNDIR=~/tiprun ./tip-stream.sh start     # streams into $RUNDIR/stream/<card>.csv
#   HAZYNC_RUNDIR=~/tiprun ./tip-stream.sh stop
#   HAZYNC_RUNDIR=~/tiprun ./tip-stream.sh status
#
# Reads $RUNDIR/pods.txt (written by fleet.sh):  <id> <name> <ip> <ssh-port> <cost/hr> <gpu>
# Writes  $RUNDIR/stream/<name>.csv:
#   epoch,util_pct,mem_mib,temp_c,power_w,sm_mhz,mem_mhz,phase,seg_n,seg_total,block
#
# ⛔ gpu_samples.csv cannot be reconstructed after a pod is gone (tools/milestone/README.md). This is
#    the live equivalent: start it BEFORE the run, not during it.
#
# Lessons taken from tools/milestone/README.md rather than re-learned:
#   * the remote body is SINGLE-quoted — in double quotes $( ) expands LOCALLY and every card
#     reports the same constant, which once made healthy cards look frozen
#   * no `pkill -f`: the pattern matches the invoking shell. Stop uses recorded PIDs only
#   * every ssh is backgrounded and reconnects on its own; one dead pod never blocks the others
#   * phase/segments are parsed with the WORKER'S OWN patterns (dist/hazync-worker:636-640):
#       executed, N segments | segment n/N | assembling N segment receipts
set -u
S=${HAZYNC_RUNDIR:?set HAZYNC_RUNDIR}
K=${HAZYNC_SSH_KEY:-~/.ssh/ghost_signet_ed25519}
# ⛔ /workspace/hazync-workers, NOT $HOME/hazync-workers. run-workers.sh documents the latter, but
# fleet.sh launches with LOG_DIR=/workspace/hazync-workers — tailing the wrong directory reports
# phase=idle forever and the traces look plausible while being dead.
LOGDIR_REMOTE=${LOG_DIR:-/workspace/hazync-workers}
PODS=$S/pods.txt
STREAM=$S/stream
PIDS=$S/stream.pids
CMD=${1:-start}

remote_body() {
  # $1 = remote log dir. Single-quoted heredoc: nothing here is expanded locally.
  cat <<'EOS'
LOGDIR="__LOGDIR__"
while true; do
  read -r U M T P SM MM <<<"$(nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw,clocks.sm,clocks.mem --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' | tr ',' ' ')"
  # ── what is this card doing, RIGHT NOW ──────────────────────────────────────────────────────
  # ⛔ POSITION WITHIN THE CURRENT BLOCK, NOT `tail -1` OF THE WHOLE FILE (hazync#481, #482).
  # The old parse took the last line matching ANY pattern, anywhere in the log. On a 32-second block
  # that cannot describe a cycle: before the first progress line it said `idle`, and after the last
  # `joins` line it said `assembling` until the file was replaced — exactly the proving → folding →
  # blank → proving flicker seen on a fleet that was in fact proving a block every 32 s cleanly.
  #
  # It was fragile in a second way that bit: `ls -t | head -1` picked ONE file, so which of agg.log,
  # aggw.log or $LOGDIR/*.log happened to be newest decided whether a height was found at all.
  # Measured 2026-09-22: 0 of 120 samples carried a height, while the identical grep run by hand on
  # the same card against the same file returned 119498.
  #
  # Now: read EVERY candidate log, anchor on the last RANGE banner (where the current block begins),
  # and take the phase from what appears AFTER it. Both progress numbers are reported, so the frame
  # can hold proving at 100% while the fold climbs, instead of swapping one for the other.
  PHASE=idle; N=0; TOT=0; BLK=; FN=0; FT=0
  read -r PHASE N TOT BLK FN FT <<<"$(
    for f in "$LOGDIR"/*.log /workspace/prove.log /workspace/agg.log /workspace/aggw.log; do
      [ -f "$f" ] && tail -c 40000 "$f"
    done 2>/dev/null | awk '
      # ⚠ EVERY SHAPE THE PROVER ACTUALLY WRITES, captured from a live mode-6 block on 2026-09-22.
      #   aggregate: "=== ... RANGE [119471..119471] ...", "execution 10.9 s   16 segments",
      #              "7/15 segments  22s elapsed", "joins 5/5", "receipt written to ...range_N.hzk"
      #   worker:    "<epoch_ms> [w1] task kind=segment idx=0 ... done=1", "... kind=join ... done=4"
      # ⛔ THE NUMBER COMES FIRST in both "7/15 segments" and "joins 5/5". That trap was found once,
      # for joins, and never generalised — which is why proving was invisible for a whole run.
      tolower($0) ~ /range \[[0-9]+\.\.[0-9]+\]/ {
        match($0, /\[[0-9]+/); blk = substr($0, RSTART + 1, RLENGTH - 1)
        seg_n = 0; seg_t = 0; fold_n = 0; fold_t = 0; fin = 0; exec_t = 0; w_seg = 0; w_join = 0
      }
      /execution [0-9.]+ s/ && / segments/ { for (i = 1; i <= NF; i++) if ($i == "segments") exec_t = $(i-1) + 0 }
      /^[ \t]*[0-9]+\/[0-9]+ segments/     { split($1, a, "/"); seg_n = a[1] + 0; seg_t = a[2] + 0 }
      /joins [0-9]+\/[0-9]+/ { for (i = 1; i <= NF; i++) if ($i == "joins") { split($(i+1), b, "/"); fold_n = b[1] + 0; fold_t = b[2] + 0 } }
      /task kind=segment/ { for (i = 1; i <= NF; i++) if ($i ~ /^done=/) { split($i, c, "="); seg_n = c[2] + 0; w_seg = 1 } }
      /task kind=join/    { for (i = 1; i <= NF; i++) if ($i ~ /^done=/) { split($i, c, "="); fold_n = c[2] + 0; w_join = 1 } }
      /receipt written|RECEIPT VERIFIED|PUSH DONE/ { fin = 1 }
      END {
        if (seg_t == 0 && exec_t > 0) seg_t = exec_t
        # ⚠ THE PHASE IS HOW FAR THROUGH THE BLOCK WE ARE, not which line came last.
        ph = "idle"
        if (blk != "")            ph = "executed"
        if (seg_n > 0 || w_seg)   ph = "proving"
        if (fold_n > 0 || w_join) ph = "assembling"
        if (fin)                  ph = "done"
        printf "%s %d %d %s %d %d\n", ph, seg_n, seg_t, (blk == "" ? "-" : blk), fold_n, fold_t
      }')"
  [ "$BLK" = "-" ] && BLK=
  # `RANGE [n..n]` only appears in mode-6/board logs. A milestone chunk run has none, so fall back to
  # the fixture on disk rather than reporting no block at all.
  [ -z "$BLK" ] && BLK=$(ls /workspace/block_*.json 2>/dev/null | head -1 | sed 's/[^0-9]//g')
  # ⚠ THE TWO FOLD COLUMNS ARE APPENDED, NOT INSERTED. collect.py rejects a row with fewer than 11
  # fields, so an older reader still parses a new row and simply does not see the fold numbers.
  echo "$(date +%s.%N),${U:-0},${M:-0},${T:-0},${P:-0},${SM:-0},${MM:-0},$PHASE,$N,$TOT,$BLK,${FN:-0},${FT:-0}"
  sleep 1
done
EOS
}

case "$CMD" in
start)
  [ -s "$PODS" ] || { echo "no $PODS — run fleet.sh first"; exit 1; }
  mkdir -p "$STREAM"; : > "$PIDS"
  body=$(remote_body | sed "s#__LOGDIR__#$LOGDIR_REMOTE#")
  n=0
  while read -r _id name ip port _cost _gpu; do
    [ -n "${name:-}" ] || continue
    (
      while true; do
        # ⛔ NO -n HERE. The body is fed to `bash -s` on STDIN, and -n points stdin at /dev/null —
        # the remote shell then gets an EMPTY script, exits at once, and this loop reconnects every
        # 5 s forever writing nothing. Measured: 1 minute of "streaming", 0 lines, no error anywhere.
        # ServerAlive*: a dead pod drops in ~30 s instead of hanging.
        ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
            -i "$K" -p "$port" root@"$ip" "bash -s" <<< "$body" >> "$STREAM/$name.csv" 2>/dev/null
        sleep 5           # reconnect; the gap is visible in the data as missing seconds, not faked
      done
    ) &
    echo "$! $name" >> "$PIDS"
    n=$((n+1))
  done < "$PODS"
  echo "streaming $n pods -> $STREAM  (stop with: $0 stop)"
  ;;
stop)
  [ -s "$PIDS" ] || { echo "nothing recorded in $PIDS"; exit 0; }
  while read -r pid name; do
    kill "$pid" 2>/dev/null && echo "stopped $name ($pid)"
  done < "$PIDS"
  : > "$PIDS"
  ;;
status)
  now=$(date +%s)
  printf '%-16s %8s %10s %s\n' CARD LINES AGE LAST
  for f in "$STREAM"/*.csv; do
    [ -e "$f" ] || continue
    last=$(tail -1 "$f" 2>/dev/null)
    ts=${last%%,*}
    age=$(( now - ${ts%.*} )) 2>/dev/null || age=-1
    printf '%-16s %8s %9ss %s\n' "$(basename "$f" .csv)" "$(wc -l < "$f")" "$age" "${last:0:60}"
  done
  ;;
*) echo "usage: $0 {start|stop|status}"; exit 2 ;;
esac
