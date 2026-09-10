#!/bin/bash
# CONTINUOUS RUN: block 966,256 across 22 RTX 4090s. One clock, T0 -> VERIFIED, no manual steps.
#
# Every failure from tonight has a check here, because each one silently produced a WRONG RESULT
# rather than an error:
#   * stale receipts       -> a chunk "completed" in 63s. Pods are cleared AND VERIFIED empty first.
#   * silent scp drop      -> coordinator got 21/22 and seg-serve panicked. Every copy is verified,
#                             with retries, and the staged count is asserted == 22 before launching.
#   * sequential ssh hang  -> 12 min wedge. Every ssh is parallel and under `timeout`.
#   * #147 wedge           -> watchdog on prove.log growth, kill + reassign to a finished card.
#   * remote self-kill     -> pkill patterns match the invoking shell; kills go via a script file.
S=${HAZYNC_RUNDIR:?set HAZYNC_RUNDIR to a working directory for this run}
K=~/.ssh/ghost_signet_ed25519
LOG=$S/continuous.log; : > $LOG
say(){ echo "[$(date -Is)] $*" >> $LOG; }
COORD_ID=k0lx8ioh5ztex6; COORD_IP=80.15.7.37; COORD_SSH=46144; AGG_IP=80.15.7.37; AGG_PORT=46145
N=$(wc -l < $S/assign_opt.txt)
SETUP_GRACE=600; STALL=100

# ---------- phase 0: clear + verify (OUTSIDE the clock: setup, not proving) ----------
rm -f $S/_reassigned $S/_busy
say "clearing fleet"
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
  ( scp -q -o ConnectTimeout=15 -i $K -P "$PORT" $S/remote_clear.sh root@"$IP":/tmp/ 2>/dev/null
    R=$(timeout 60 ssh -n -o ConnectTimeout=15 -i $K -p "$PORT" root@"$IP" 'bash /tmp/remote_clear.sh 2>/dev/null | grep LEFT' 2>/dev/null | tail -1)
    echo "$CHUNK:${R:-ERR}" >> $S/_c2 ) &
done < $S/assign_opt.txt
wait
CLEAN=$(grep -c 'LEFT:0 REDIRS:0' $S/_c2); rm -f $S/_c2
say "pods verified empty: $CLEAN/$N"
[ "$CLEAN" -lt $N ] && { say "⛔ ABORT: not all pods clean"; exit 1; }

# ---------- THE CLOCK STARTS ----------
T0=$(date +%s.%N); say "### T0=$T0 ###"
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
  ( timeout 45 ssh -n -o ConnectTimeout=15 -i $K -p "$PORT" root@"$IP" \
      "cd /workspace && HAZYNC_BLOCK_NAME=block_966256.json HAZYNC_CHUNKS=$N nohup setsid ./pod-prove.sh $CHUNK > run.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1 ) &
done < $S/assign_opt.txt
wait
say "$N proves launched at +$(python3 -c "print(round($(date +%s.%N)-$T0,1))")s"
mkdir -p $S/rc
cat > $S/autoattach.sh <<'AA'
#!/bin/bash
# Wait for the aggregate listener, then connect. Pre-positioned so attach costs ~0 once it opens.
AGG=$1; WID=$2
cd /workspace || exit 1
for i in $(seq 1 600); do
  if timeout 3 bash -c "</dev/tcp/${AGG%:*}/${AGG#*:}" 2>/dev/null; then
    HAZYNC_WORKER_ID=$WID exec ./hazync-host-cuda seg-connect "$AGG" > aggw.log 2>&1
  fi
  sleep 1
done
AA
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
  [ "$PID" = "$COORD_ID" ] && continue
  ( scp -q -o ConnectTimeout=15 -i $K -P "$PORT" $S/autoattach.sh root@"$IP":/workspace/ 2>/dev/null
    timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p "$PORT" root@"$IP" \
      "chmod +x /workspace/autoattach.sh; cd /workspace && nohup setsid ./autoattach.sh $AGG_IP:$AGG_PORT w$CHUNK > aa.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1 ) &
done < $S/assign_opt.txt
say "auto-attach armed on all workers"

declare -A LASTSZ LASTCHG DONEC
while read -r P I O L C SG R; do LASTSZ[$C]=0; LASTCHG[$C]=$(date +%s); done < $S/assign_opt.txt
for tick in $(seq 1 120); do
  NDONE=0
  # ⛔ THE POLL WAS SEQUENTIAL: 27 ssh round-trips, one after another, ~0.7 s each. A tick therefore
  # took ~20 s, which is why the last chunk of run 4 sat FINISHED and unnoticed for 24.9 s -- the
  # single largest recoverable waste in the run, bigger than anything left in the chunk phase.
  # Fan the probes out first, collect from files, then walk the results. A tick is now ~1 s.
  rm -rf $S/_probe && mkdir -p $S/_probe
  while read -r PID IP PORT LOC CHUNK SEGS RATE; do
    [ "${DONEC[$CHUNK]:-0}" = "1" ] && continue
    RD=/workspace; grep -qx "$CHUNK" $S/_reassigned 2>/dev/null && RD=/workspace/re$CHUNK
    ( timeout 25 ssh -n -o ConnectTimeout=10 -i $K -p "$PORT" root@"$IP" \
        "RDIR=$RD; CHUNK=$CHUNK; "'if [ -f $RDIR/chunk_$CHUNK.bin ]; then echo DONE; else Z=$(stat -c%s $RDIR/prove.log 2>/dev/null || echo 0); U=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1); V=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1); N=$(pgrep -cf hazync-host-cuda); echo "$Z:${U:-0}:${V:-0}:$N"; fi' 2>/dev/null | tail -1 > $S/_probe/$CHUNK ) &
  done < $S/assign_opt.txt
  wait
  while read -r PID IP PORT LOC CHUNK SEGS RATE; do
    [ "${DONEC[$CHUNK]:-0}" = "1" ] && { NDONE=$((NDONE+1)); continue; }
    # ⛔ Resolve the directory LOCALLY. This referenced $S/_reassigned INSIDE the remote command --
    # a path on the operator's laptop, absent on the pod -- so it always fell back to /workspace,
    # watched the card's FIRST (finished, static) chunk log, and declared a stall. Chunk 4 bounced
    # across three cards in a reassignment loop before this was caught.
    RDIR=/workspace
    grep -qx "$CHUNK" $S/_reassigned 2>/dev/null && RDIR=/workspace/re$CHUNK
    # ⛔⛔ THE SAME BUG, ONE LINE LOWER. This probe was written in DOUBLE quotes, so `$(grep ...)`,
    # `$(stat ...)`, `$P` and `$S` were expanded on the LAPTOP before ssh ever ran. grep/stat found
    # no /workspace/prove.log locally, so the remote command was literally `echo :<scratchpad path>`
    # -- a CONSTANT. LASTCHG therefore never advanced and every healthy card was declared stalled at
    # exactly SETUP_GRACE. It killed chunk 4 five minutes into run 3 and was working down the fleet
    # when the driver was stopped. Single-quote the body; pass only the two variables it needs.
    ST=$(cat $S/_probe/$CHUNK 2>/dev/null)
    # ⛔ An unreachable card must NOT read as a stalled one. Empty ST means the ssh failed; leave the
    # timers alone and try again next tick rather than killing a card we simply could not talk to.
    [ -z "$ST" ] && continue
    NOW=$(date +%s)
    if [ "$ST" = "DONE" ]; then
      DONEC[$CHUNK]=1; NDONE=$((NDONE+1))
      # OVERLAP: stage this receipt now, while other cards are still proving. Batching all of them
      # after the last chunk cost 57s of dead time in the 10.5-min run.
      ( for a in 1 2 3; do
          SRC=/workspace; [ -f $S/_reassigned ] && grep -qx "$CHUNK" $S/_reassigned && SRC=/workspace/re$CHUNK
          timeout 120 scp -q -o ConnectTimeout=15 -i $K -P "$PORT" root@"$IP":$SRC/chunk_$CHUNK.bin $S/rc/ 2>/dev/null
          [ -s "$S/rc/chunk_$CHUNK.bin" ] || continue
          timeout 120 scp -q -o ConnectTimeout=15 -i $K -P "$COORD_SSH" "$S/rc/chunk_$CHUNK.bin" root@"$COORD_IP":/workspace/ 2>/dev/null
          OK=$(timeout 20 ssh -n -o ConnectTimeout=10 -i $K -p "$COORD_SSH" root@"$COORD_IP" "test -s /workspace/chunk_$CHUNK.bin && echo Y" 2>/dev/null)
          [ "$OK" = "Y" ] && break
        done ) &
      continue
    fi
    IFS=: read -r SZ UTIL VRAM NPROC <<< "$ST"
    [ "${SZ:-0}" != "${LASTSZ[$CHUNK]}" ] && { LASTSZ[$CHUNK]=${SZ:-0}; LASTCHG[$CHUNK]=$NOW; }
    # ⛔⛔ NO PROXY. Three runs were wrecked by inferring "stalled" from how fast a log file grew:
    # first the byte-count threshold (killed cards mid-execute), then the tight limit keyed on
    # "proving has begun" -- which fires during the first segment, where CUDA context creation and
    # kernel JIT legitimately produce ~2 minutes of silence on a cold card. Run 3 attempt 2 shot
    # chunk 1 at +2:07 while 22 other cards were at segment 70-of-80 and climbing.
    #
    # A card that is WORKING says so directly: the prove process is alive, it holds VRAM, and the
    # GPU reports utilisation. Kill only when the log is static AND the card is demonstrably idle.
    # This cannot fire on a busy card, which is the only failure mode that costs a whole run.
    DEAD=0
    [ "${NPROC:-1}" -lt 1 ] && DEAD=1
    [ "${UTIL:-100}" -lt 5 ] && [ "${VRAM:-99999}" -lt 2000 ] && DEAD=1
    if [ "$DEAD" = "1" ] && [ $((NOW-${LASTCHG[$CHUNK]})) -gt "$STALL" ]; then
      say "⛔ chunk $CHUNK DEAD on $LOC (gpu=${UTIL}% vram=${VRAM} procs=${NPROC}, log static $((NOW-${LASTCHG[$CHUNK]}))s) -- restarting"
      timeout 25 ssh -n -o ConnectTimeout=10 -i $K -p "$PORT" root@"$IP" 'for p in $(pgrep -f pod-prove); do kill -9 $p; done; for p in $(pgrep -f hazync-host-cuda); do kill -9 $p; done; exit 0' >/dev/null 2>&1
      MOVED=0
      while read -r P2 I2 O2 L2 C2 S2 R2; do
        # ⛔ "Receipt exists" is NOT "card is free". pod-prove.sh writes chunk_N.bin near the end
        # but the process keeps ~22 GB of VRAM until it exits. Reassigning onto such a card starts a
        # second prove on top of the first and both die with the hazync#97 memory failure -- which is
        # exactly what happened to chunks 0 and 4 in run 2 (rc=101, "drop a rung ... SEG_PO2=20").
        # Require the card to be genuinely idle: no prove process AND VRAM released.
        FREE=no
        grep -qx "$P2" $S/_busy 2>/dev/null && continue
        if [ "${DONEC[$C2]:-0}" = "1" ] && [ "$P2" != "$PID" ]; then
          FREE=$(timeout 20 ssh -n -o ConnectTimeout=10 -i $K -p "$O2" root@"$I2" \
            'P=$(pgrep -cf hazync-host-cuda); V=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1); if [ "$P" = "0" ] && [ "${V:-99999}" -lt 2000 ]; then echo yes; else echo no; fi' 2>/dev/null | tail -1)
        fi
        if [ "$FREE" = "yes" ]; then
          timeout 45 ssh -n -o ConnectTimeout=15 -i $K -p "$O2" root@"$I2" \
            "mkdir -p /workspace/re$CHUNK && cd /workspace && HAZYNC_WORKDIR=/workspace/re$CHUNK HAZYNC_BLOCK_NAME=block_966256.json HAZYNC_CHUNKS=$N nohup setsid ./pod-prove.sh $CHUNK > /workspace/re$CHUNK/run.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1
          echo "$P2" >> $S/_busy
          sed -i "s|^$PID $IP $PORT $LOC $CHUNK |$P2 $I2 $O2 $L2 $CHUNK |" $S/assign_opt.txt
          echo "$CHUNK" >> $S/_reassigned
          say "   -> chunk $CHUNK now on $P2 ($L2)"; LASTCHG[$CHUNK]=$NOW; LASTSZ[$CHUNK]=0; MOVED=1; break
        fi
      done < $S/assign_opt.txt
      # ⛔ SELF-HEAL. With no free card the chunk used to stay pinned to the wedged card and NOTHING
      # was logged -- the run could then never finish. A hazync#147 wedge is transient, so restart in
      # place; reassignment is the second resort, not the only one.
      if [ "$MOVED" = "0" ]; then
        timeout 45 ssh -n -o ConnectTimeout=15 -i $K -p "$PORT" root@"$IP" \
          "rm -rf /workspace/re$CHUNK && mkdir -p /workspace/re$CHUNK && cd /workspace && HAZYNC_WORKDIR=/workspace/re$CHUNK HAZYNC_BLOCK_NAME=block_966256.json HAZYNC_CHUNKS=$N nohup setsid ./pod-prove.sh $CHUNK > /workspace/re$CHUNK/run.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1
        grep -qx "$CHUNK" $S/_reassigned 2>/dev/null || echo "$CHUNK" >> $S/_reassigned
        say "   ~ no free card; restarted chunk $CHUNK in place on $LOC"
        LASTCHG[$CHUNK]=$NOW; LASTSZ[$CHUNK]=0
      fi
    fi
  done < $S/assign_opt.txt
  [ "$NDONE" -ge $N ] && { say "chunk phase complete at +$(python3 -c "print(round($(date +%s.%N)-$T0,1))")s"; break; }
  # the tick used to cost ~20 s of sequential ssh, so an 8 s sleep was noise on top of it. Now that
  # the probes fan out and a tick is ~1 s, the sleep IS the remaining detection lag.
  sleep 3
done

# ---------- gather, with every copy verified ----------
# receipts were streamed during the chunk phase; sweep up anything that failed its retries
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
  ( OK=$(timeout 20 ssh -n -o ConnectTimeout=10 -i $K -p "$COORD_SSH" root@"$COORD_IP" "test -s /workspace/chunk_$CHUNK.bin && echo Y" 2>/dev/null)
    if [ "$OK" != "Y" ]; then
      timeout 120 scp -q -o ConnectTimeout=15 -i $K -P "$PORT" root@"$IP":/workspace/chunk_$CHUNK.bin $S/rc/ 2>/dev/null
      timeout 120 scp -q -o ConnectTimeout=15 -i $K -P "$COORD_SSH" "$S/rc/chunk_$CHUNK.bin" root@"$COORD_IP":/workspace/ 2>/dev/null
    fi ) &
done < $S/assign_opt.txt
wait
STAGED=$(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p $COORD_SSH root@$COORD_IP 'ls /workspace/chunk_*.bin 2>/dev/null | wc -l')
say "staged $STAGED/$N at +$(python3 -c "print(round($(date +%s.%N)-$T0,1))")s"
[ "$STAGED" -lt $N ] && { say "⛔ ABORT: coordinator has $STAGED/22"; exit 1; }

# ---------- aggregate ----------
timeout 45 ssh -n -o ConnectTimeout=15 -i $K -p $COORD_SSH root@$COORD_IP \
 "cd /workspace && rm -f agg.log agg.err && HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_ECMULT_WINDOW=21 \
  HAZYNC_BLOCK=/workspace/block_966256.json HAZYNC_CHUNKS=$N HAZYNC_AGG=1 HAZYNC_PORT=9110 \
  nohup setsid ./hazync-host-cuda seg-serve > agg.log 2> agg.err < /dev/null & disown; exit 0" >/dev/null 2>&1
for i in $(seq 1 40); do timeout 5 bash -c "</dev/tcp/$AGG_IP/$AGG_PORT" 2>/dev/null && break; sleep 3; done
say "listener open at +$(python3 -c "print(round($(date +%s.%N)-$T0,1))")s"
say "workers auto-attached at +$(python3 -c "print(round($(date +%s.%N)-$T0,1))")s"
for i in $(seq 1 200); do
  # ⛔ `pgrep -cf seg-serve` SELF-MATCHES: the remote shell's own command line contains the string,
  # so it never returns 0 and a dead aggregate reads as a live one. And `ps -eo comm` truncates to
  # 15 chars, so grepping "^hazync-host-cuda$" matches NOTHING and a LIVE aggregate reads as dead --
  # that mistake had me relaunch seg-serve on top of a healthy one tonight, which then died on
  # bind() while the original kept working with its log unlinked. Match the truncated name.
  # ⛔ `joins` MUST be in this grep. seg-serve prints `joins N/584` all the way through assembly --
  # the only record of the join tree's progress that exists anywhere. Without it the fold has no
  # timing at all, and the pod is gone before anyone wants it. Each sample is appended with its
  # offset so the run leaves behind a real fold curve.
  OUT=$(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p $COORD_SSH root@$COORD_IP 'grep -E "VERIFIED|digest|TOTAL|execution|worker wall|assembly|panicked" /workspace/agg.log 2>/dev/null | tail -8; tail -2 /workspace/agg.err 2>/dev/null; ps -eo comm | grep -c "^hazync-host-cud"; echo "JOINS:$(grep -oE "joins [0-9]+/[0-9]+" /workspace/agg.log 2>/dev/null | tail -1)"' 2>/dev/null)
  J=$(echo "$OUT" | sed -n 's/^JOINS:joins //p')
  [ -n "$J" ] && echo "$(python3 -c "print(round($(date +%s.%N)-$T0,1))") $J" >> $S/joins.tsv
  if echo "$OUT" | grep -q VERIFIED; then
    T_END=$(date +%s.%N); W=$(python3 -c "print(round($T_END-$T0,1))")
    say "=== VERIFIED ==="; say "$OUT"
    say "#############################################################"
    say "### CONTINUOUS BLOCK PROOF: ${W}s = $(python3 -c "print(round($W/60,2))") min"
    say "### block 966,256 | 4,741 txs | 9,079 inputs | ${N}x RTX 4090"
    say "### cost \$$(python3 -c "print(round($N*0.34*$W/3600,3))")"
    say "#############################################################"
    break
  fi
  [ "$(echo "$OUT" | tail -1)" = "0" ] && { say "⛔ exited without VERIFIED"; say "$OUT"; break; }
  sleep 8
done
say "END"
