#!/bin/bash
S=${HAZYNC_RUNDIR:?set HAZYNC_RUNDIR to a working directory for this run}
K=~/.ssh/ghost_signet_ed25519
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
 ( RDIR=/workspace; grep -qx "$CHUNK" $S/_reassigned 2>/dev/null && RDIR=/workspace/re$CHUNK
   R=$(timeout 25 ssh -n -o ConnectTimeout=10 -i $K -p "$PORT" root@"$IP" \
     "RDIR=$RDIR; CHUNK=$CHUNK; "'if [ -f $RDIR/chunk_$CHUNK.bin ]; then echo "DONE"; else P=$(grep -c "segments at po2" $RDIR/prove.log 2>/dev/null || echo 0); L=$(grep -oE "segment [0-9]+/[0-9]+" $RDIR/prove.log 2>/dev/null | tail -1); U=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits|head -1); V=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits|head -1); N=$(pgrep -cf hazync-host-cuda); echo "proving=$P seg=[$L] gpu=${U}% vram=${V} procs=$N"; fi' 2>/dev/null | tail -1)
   printf '%-3s %-4s %s\n' "$CHUNK" "$LOC" "${R:-NO-SSH}" ) &
done < $S/assign_opt.txt
wait
