#!/bin/bash
# Run 4 final receipt. All 27 chunk receipts are ALREADY on the coordinator at /workspace, so
# agg-chunks runs in place -- no staging. seg-serve verified but does not write a file on this
# published binary, and losing a verified receipt has happened twice tonight; this closes it.
S=${HAZYNC_RUNDIR:?set HAZYNC_RUNDIR to a working directory for this run}
K=~/.ssh/ghost_signet_ed25519
CIP=80.15.7.37; CPORT=46144
LOG=$S/regen4.log; : > $LOG
say(){ echo "[$(date -Is)] $*" >> $LOG; }
D=/home/defenwycke/hazync-milestone-966256-run4; mkdir -p $D/receipts
# evidence first: pull all 27 chunk receipts off the workers before anything can clear them
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
 ( RDIR=/workspace; grep -qx "$CHUNK" $S/_reassigned 2>/dev/null && RDIR=/workspace/re$CHUNK
   for a in 1 2 3; do
     timeout 120 scp -q -o ConnectTimeout=20 -i $K -P "$PORT" root@"$IP":$RDIR/chunk_$CHUNK.bin $D/receipts/ 2>/dev/null
     [ -s "$D/receipts/chunk_$CHUNK.bin" ] && break
   done ) &
done < $S/assign_opt.txt
wait
say "chunk receipts on laptop: $(ls $D/receipts/chunk_*.bin 2>/dev/null | wc -l)/27"
say "coordinator staged: $(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p $CPORT root@$CIP 'ls /workspace/chunk_*.bin 2>/dev/null | wc -l')/27"
timeout 45 ssh -n -o ConnectTimeout=15 -i $K -p $CPORT root@$CIP \
 "cd /workspace && rm -f regen.log regen.err && HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_ECMULT_WINDOW=21 \
  HAZYNC_BLOCK=/workspace/block_966256.json HAZYNC_CHUNKS=27 \
  nohup setsid ./hazync-host-cuda agg-chunks > regen.log 2> regen.err < /dev/null & disown; exit 0" >/dev/null 2>&1
say "agg-chunks started on the coordinator"
for i in $(seq 1 300); do
  R=$(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p $CPORT root@$CIP \
    'ls -l /workspace/*.receipt 2>/dev/null | awk "{print \$9, \$5}"
     tail -2 /workspace/regen.log 2>/dev/null | cut -c1-160
     echo "ERR:$(tail -2 /workspace/regen.err 2>/dev/null | tr "\n" " " | cut -c1-160)"
     echo "ALIVE:$(ps -eo comm | grep -c "^hazync-host-cud")"' 2>/dev/null)
  echo "$R" | grep -qE '\.receipt [0-9]+' && { say "=== RECEIPT WRITTEN ==="; say "$R"; break; }
  echo "$R" | grep -q '^ALIVE:0$' && { say "=== exited ==="; say "$R"; break; }
  sleep 20
done
RF=$(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p $CPORT root@$CIP 'ls /workspace/*.receipt 2>/dev/null | head -1')
[ -n "$RF" ] && timeout 300 scp -q -o ConnectTimeout=20 -i $K -P $CPORT root@$CIP:"$RF" $D/block_966256_run4.receipt 2>/dev/null
say "local: $(ls -l $D/block_966256_run4.receipt 2>/dev/null | awk '{print $5}') bytes  sha256=$(sha256sum $D/block_966256_run4.receipt 2>/dev/null | cut -d' ' -f1)"
say END
