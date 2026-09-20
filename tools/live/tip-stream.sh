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
  PHASE=idle; N=0; TOT=0; BLK=
  # board mode writes $LOGDIR/worker_N.log; a milestone run writes /workspace/prove.log and, during
  # the fold, /workspace/agg.log. Take the newest of all of them, or a milestone run reports `idle`
  # for its whole duration while looking perfectly healthy.
  L=$(ls -t "$LOGDIR"/*.log /workspace/prove.log /workspace/agg.log /workspace/aggw.log 2>/dev/null | head -1)
  if [ -n "$L" ]; then
    TAIL=$(tail -c 40000 "$L" 2>/dev/null)
    # `chunk N: X inputs, Y segments at po2 Z` is prove-chunk's equivalent of "executed" — without it
    # a card reads `idle` for its whole CPU-only execute phase while sitting at full power.
    # ⛔ THE FOLD WAS INVISIBLE. The aggregate prints 'joins 34/34' and '18/34 segments' -- note the
    # number comes FIRST there -- so none of the patterns below matched it, and a worker's own fold
    # log was not even in the file list. Measured 2026-09-20: the word 'assembling' appeared ZERO
    # times across a whole run, so kfold stayed 0, the join tree never lit, and the fold phase simply
    # did not exist as far as the dashboard was concerned.
    #   aggregator:  'joins N/M'                      <- the join tree advancing
    #   worker:      '[w1] segment 7 in 2.45s'        <- this card taking fold work over the network
    M1=$(printf '%s' "$TAIL" | grep -oE 'joins [0-9]+/[0-9]+|\[w[0-9]+\] segment [0-9]+|segment [0-9]+/[0-9]+|executed, [0-9]+ segments|assembling [0-9]+ segment receipts|chunk [0-9]+: [0-9]+ inputs, [0-9]+ segments at po2 [0-9]+' | tail -1)
    case "$M1" in
      # 'assembling' is the phase word the renderer already keys the fold arc on; both fold signals
      # report it so one card folding and many cards folding look the same to everything downstream.
      joins*)      PHASE=assembling; N=${M1#joins }; TOT=${N#*/}; N=${N%%/*} ;;
      \[w*)        PHASE=assembling; N=$(printf '%s' "$M1" | grep -oE '[0-9]+$'); TOT=0 ;;
      segment*)    PHASE=proving;    N=${M1#segment }; TOT=${N#*/}; N=${N%%/*} ;;
      executed*)   PHASE=executed;   TOT=$(printf '%s' "$M1" | grep -oE '[0-9]+' | head -1) ;;
      assembling*) PHASE=assembling; TOT=$(printf '%s' "$M1" | grep -oE '[0-9]+' | head -1) ;;
      chunk*)      PHASE=executed;   TOT=$(printf '%s' "$M1" | grep -oE '[0-9]+' | sed -n 3p) ;;
    esac
    BLK=$(printf '%s' "$TAIL" | grep -oE 'range \[[0-9]+\.\.[0-9]+\]' | tail -1 | grep -oE '[0-9]+' | head -1)
  fi
  # `range [n..n]` only appears in BOARD worker logs. A milestone chunk run has no such line, so the
  # height would stay pinned at whatever the board last did — the dashboard would show the wrong
  # block for the entire run while everything else looked healthy. Fall back to the block on disk.
  [ -z "$BLK" ] && BLK=$(ls /workspace/block_*.json 2>/dev/null | head -1 | sed 's/[^0-9]//g')
  echo "$(date +%s.%N),${U:-0},${M:-0},${T:-0},${P:-0},${SM:-0},${MM:-0},$PHASE,$N,$TOT,$BLK"
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
