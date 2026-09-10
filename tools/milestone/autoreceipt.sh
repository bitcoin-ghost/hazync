#!/bin/bash
# Wait for run 3 to VERIFY, then immediately regenerate the final receipt AS A FILE.
# seg-serve (published v0.21.0) prints a digest and exits without writing anything; agg-chunks does
# write, so the artifact is reconstructed from the chunk receipts. Fixed properly in 4cad145, but
# this run uses the published binary, so the recovery is wired in rather than remembered.
S=${HAZYNC_RUNDIR:?set HAZYNC_RUNDIR to a working directory for this run}
K=~/.ssh/ghost_signet_ed25519
SPID=ida3uz358w59fq; SIP=38.65.239.32; SPORT=23491
LOG=$S/autoreceipt.log; : > $LOG
say(){ echo "[$(date -Is)] $*" >> $LOG; }
for i in $(seq 1 200); do
  grep -q "CONTINUOUS BLOCK PROOF" $S/continuous.log 2>/dev/null && { say "run 3 verified"; break; }
  grep -qE "ABORT|without VERIFIED" $S/continuous.log 2>/dev/null && { say "run 3 failed; nothing to persist"; exit 1; }
  sleep 15
done
grep -q "CONTINUOUS BLOCK PROOF" $S/continuous.log 2>/dev/null || { say "timed out"; exit 1; }

D=/home/defenwycke/hazync-milestone-966256-run3; mkdir -p $D/receipts
while read -r PID IP PORT LOC CHUNK SEGS RATE; do
  ( RDIR=/workspace; grep -qx "$CHUNK" $S/_reassigned 2>/dev/null && RDIR=/workspace/re$CHUNK
    for a in 1 2 3; do
      timeout 120 scp -q -o ConnectTimeout=20 -i $K -P "$PORT" root@"$IP":$RDIR/chunk_$CHUNK.bin $D/receipts/ 2>/dev/null
      [ -s "$D/receipts/chunk_$CHUNK.bin" ] && break
    done ) &
done < $S/assign_opt.txt
wait
N=$(ls $D/receipts/*.bin 2>/dev/null | wc -l)
say "gathered $N receipts"
[ "$N" -lt 27 ] && { say "INCOMPLETE - cannot regenerate"; exit 1; }

timeout 120 ssh -n -o ConnectTimeout=20 -i $K -p "$SPORT" root@"$SIP"  'for d in /usr/local/cuda*/compat; do [ -d "$d" ] && mv "$d" "${d}.disabled"; done; ldconfig 2>/dev/null
  mkdir -p /workspace/agg3 && cd /workspace && ([ -x hazync-host-cuda ] || curl -fsSL -o hazync-host-cuda https://github.com/bitcoin-ghost/hazync/releases/download/v0.21.0/hazync-host-x86_64-linux-gnu-cuda) && chmod +x hazync-host-cuda
  ([ -f block_966256.json ] || { curl -fsSLO https://bitcoinghost.org/hazync/repro/block_966256.json.gz && gunzip -f block_966256.json.gz; })
  cp hazync-host-cuda block_966256.json agg3/ && echo ready' >/dev/null 2>&1
for f in $D/receipts/chunk_*.bin; do
  ( b=$(basename $f)
    for a in 1 2 3; do
      timeout 120 scp -q -o ConnectTimeout=20 -i $K -P "$SPORT" "$f" root@"$SIP":/workspace/agg3/ 2>/dev/null
      OK=$(timeout 20 ssh -n -o ConnectTimeout=10 -i $K -p "$SPORT" root@"$SIP" "test -s /workspace/agg3/$b && echo Y" 2>/dev/null)
      [ "$OK" = "Y" ] && break
    done ) &
done
wait
say "staged $(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p $SPORT root@$SIP 'ls /workspace/agg3/chunk_*.bin | wc -l')/27 on the regen card"
timeout 45 ssh -n -o ConnectTimeout=15 -i $K -p "$SPORT" root@"$SIP"  "cd /workspace/agg3 && HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_ECMULT_WINDOW=21   HAZYNC_BLOCK=/workspace/agg3/block_966256.json HAZYNC_CHUNKS=27 HAZYNC_OUT=/workspace/agg3/block_966256_run3_receipt.bin   nohup setsid ./hazync-host-cuda agg-chunks > regen3.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1
say "regeneration started"
for i in $(seq 1 200); do
  # ⛔ agg-chunks names the output file itself and ignores HAZYNC_OUT -- waiting only on the
  # HAZYNC_OUT name would time out on a run that had already succeeded. Accept either.
  R=$(timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p "$SPORT" root@"$SIP" 'ls -l /workspace/agg3/block_966256_run3_receipt.bin /workspace/agg3/block_966256.receipt 2>/dev/null | awk "{print \$5}"; tail -2 /workspace/agg3/regen3.log 2>/dev/null | cut -c1-140' 2>/dev/null)
  echo "$R" | grep -qE "^[0-9]+" && { say "RECEIPT WRITTEN"; say "$R"; break; }
  echo "$R" | grep -qi panic && { say "regeneration FAILED"; say "$R"; break; }
  sleep 20
done
timeout 180 scp -q -o ConnectTimeout=20 -i $K -P "$SPORT" root@"$SIP":/workspace/agg3/block_966256_run3_receipt.bin $D/ 2>/dev/null
[ -s $D/block_966256_run3_receipt.bin ] || timeout 180 scp -q -o ConnectTimeout=20 -i $K -P "$SPORT" root@"$SIP":/workspace/agg3/block_966256.receipt $D/block_966256_run3_receipt.bin 2>/dev/null
say "local receipt: $(ls -l $D/block_966256_run3_receipt.bin 2>/dev/null | awk '{print $5}') bytes"
say "sha256: $(sha256sum $D/block_966256_run3_receipt.bin 2>/dev/null | cut -d' ' -f1)"
say END
