#!/bin/bash
# THREE CARDS, ONE BLOCK: chunk proofs across the fleet, then one aggregate -> one verified receipt.
# This is the flow the film depicts and board mode cannot show (there, each card proves its own block).
#
#   S=/home/defenwycke/tiprun-mile ./mile3.sh prep     # stop board workers, fetch+verify block, clear
#   S=... ./mile3.sh prove                             # pod-prove.sh chunk i on each card, in parallel
#   S=... ./mile3.sh watch                             # progress until all three chunks land
#   S=... ./mile3.sh stage                             # receipts -> card 0, count ASSERTED
#   S=... ./mile3.sh agg                               # agg-chunks on card 0
#   S=... ./mile3.sh watchagg                          # joins curve + VERIFIED
#
# Reads $S/pods.txt from fleet.sh:  <id> <name> <ip> <ssh-port> <cost/hr> <gpu>
# Chunk index = line order (0,1,2). Card 0 is the aggregator.
#
# ⛔ WHY agg-chunks AND NOT seg-serve/seg-connect
#    fleet.sh deploys with ports "22/tcp", so 9110 is not exposed; and same-provider RunPod pods have
#    been measured getting instant REFUSED on each other's public ports — an aggregate once ran N=1
#    while reporting N=4. agg-chunks folds in-process on one card: slower, but it cannot silently
#    degrade. Exposing 9110 + a reachability gate is the change for a distributed fold.
#
# Lessons taken from tools/milestone/README.md rather than re-learned:
#   * stale receipts made a chunk "complete" in 63 s        -> clear AND verify empty before T0
#   * a silent scp drop gave the aggregate 21/22, then panic -> staged count is ASSERTED
#   * `ps -eo comm` truncates at 15 chars                    -> match ^hazync-host-cud
#   * `pgrep -cf seg-serve` self-matches                     -> never used here
#   * remote bodies in double quotes expand $( ) LOCALLY     -> every remote body is single-quoted
#   * pkill -f matches the invoking shell                    -> kills go by recorded pid/script only
set -u
S=${S:?set S to the run directory (holding pods.txt)}
K=${HAZYNC_SSH_KEY:-~/.ssh/ghost_signet_ed25519}
BLOCK=${BLOCK:-block_962000.json}
# ✅ RESTORED on hazync.org 2026-09-21: block_962000, block_966256 and block_966280 are
# served again from https://hazync.org/repro/, verified byte-identical to the copies that had
# been left behind on bitcoinghost.org. The canonical host is hazync.org, so point there.
# Where locally-built block files live. ⚠ THE FIXTURE SET IS SMALL — 962000/966256/966280 only,
# NOT hazync.org/repro/ -- that path 404s and has since at least 2026-09-18; the migration to
# hazync.org never carried it across. Verified 2026-09-20: block_966256.json.gz is 1,624,998 bytes
# there and gunzips cleanly. A TIP block is NOT on it either (it serves only
# 962000/966256/966280), so prep copies from here when the file exists and only falls back to the
# download for the published ones.
STAGE=${STAGE:-/home/defenwycke/tipblocks}
CHUNKS=${CHUNKS:-3}
REPO=/home/defenwycke/dev/projects/hazync
PODS=$S/pods.txt
LOG=$S/mile3.log
say(){ echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
SSH(){ local ip=$1 port=$2; shift 2; timeout "${T:-60}" ssh -n -o StrictHostKeyChecking=no \
        -o ConnectTimeout=15 -i "$K" -p "$port" root@"$ip" "$@"; }
[ -s "$PODS" ] || { echo "no $PODS"; exit 1; }
N=$(wc -l < "$PODS")
[ "$N" -eq "$CHUNKS" ] || say "⚠ $N pods but CHUNKS=$CHUNKS — every chunk must be proved by someone"
CARD0=$(head -1 "$PODS")
A_IP=$(echo "$CARD0" | awk '{print $3}'); A_PORT=$(echo "$CARD0" | awk '{print $4}')

case "${1:?phase}" in

prep)
  say "=== prep: stop board workers, place block, verify, clear ==="
  i=0
  while read -r _id name ip port _c _g; do
    ( # board workers must go: one prove per card, two do not fit on one GPU (#97)
      SSH "$ip" "$port" 'cd /workspace && ./hazync-run-workers.sh 2 --stop >/dev/null 2>&1; sleep 1; true'
      # milestone runner + block, then VERIFY the block before anything expensive
      scp -q -o StrictHostKeyChecking=no -i "$K" -P "$port" "$REPO/tools/milestone/pod-prove.sh" \
          root@"$ip":/workspace/ 2>/dev/null
      # a locally-built (tip) block ships from here; published ones still fall back to the download
      [ -f "$STAGE/$BLOCK" ] && timeout 900 scp -q -o StrictHostKeyChecking=no -i "$K" -P "$port" \
          "$STAGE/$BLOCK" root@"$ip":/workspace/ 2>/dev/null
      R=$(T=300 SSH "$ip" "$port" "cd /workspace && chmod +x pod-prove.sh && \
           { [ -f $BLOCK ] || { curl -fsSL -S -O https://hazync.org/repro/${BLOCK}.gz && gunzip -f ${BLOCK}.gz; }; } && \
           rm -f chunk_*.bin prove.log result.json gpu_samples.csv agg.log && \
           echo BLOCKSHA=\$(sha256sum $BLOCK | cut -c1-16) \
                LEFT=\$(ls chunk_*.bin 2>/dev/null | wc -l) \
                PROC=\$(ps -eo comm | grep -c '^hazync-host-cud')")
      echo "$name $R" >> "$S/_prep"
    ) &
    i=$((i+1))
  done < "$PODS"
  wait
  sort -o "$S/_prep" "$S/_prep" 2>/dev/null
  cat "$S/_prep" | tee -a "$LOG"
  # every card must agree on the block, be empty, and have no prover running
  shas=$(awk '{for(j=1;j<=NF;j++) if($j ~ /^BLOCKSHA=/) print $j}' "$S/_prep" | sort -u | wc -l)
  bad=$(grep -c -E 'LEFT=[1-9]|PROC=[1-9]' "$S/_prep")
  [ "$shas" = 1 ] || { say "⛔ ABORT: cards disagree on the block file"; exit 1; }
  [ "$bad" = 0 ]  || { say "⛔ ABORT: a card is not clean (stale receipts or a live prover)"; exit 1; }
  say "prep OK: $N cards, one block sha, all clean"
  ;;

prove)
  say "=== T0: prove chunk i of $BLOCK across $N cards (CHUNKS=$CHUNKS) ==="
  date +%s.%N > "$S/t0"
  i=0
  while read -r _id name ip port _c _g; do
    ( T=45 SSH "$ip" "$port" "cd /workspace && HAZYNC_BLOCK_NAME=$BLOCK HAZYNC_CHUNKS=$CHUNKS \
        nohup setsid ./pod-prove.sh $i > run_$i.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1
      say "launched $name chunk $i" ) &
    i=$((i+1))
  done < "$PODS"
  wait
  say "all chunks launched"
  ;;

watch)
  i=0; : > "$S/_w"
  while read -r _id name ip port _c _g; do
    ( R=$(T=30 SSH "$ip" "$port" 'cd /workspace && echo "$(grep -oE "segment [0-9]+/[0-9]+" prove.log 2>/dev/null | tail -1)|$(ls -s chunk_*.bin 2>/dev/null | tail -1)|$(ps -eo comm | grep -c "^hazync-host-cud")"')
      printf '%-14s %s\n' "$name" "$R" >> "$S/_w" ) &
    i=$((i+1))
  done < "$PODS"
  wait; sort "$S/_w"; rm -f "$S/_w"
  ;;

stage)
  say "=== stage receipts onto card 0 ($A_IP) ==="
  mkdir -p "$S/rc"; i=0
  while read -r _id name ip port _c _g; do
    ( if [ "$ip:$port" != "$A_IP:$A_PORT" ]; then
        timeout 180 scp -q -o StrictHostKeyChecking=no -i "$K" -P "$port" \
            root@"$ip":/workspace/chunk_$i.bin "$S/rc/" 2>/dev/null
        timeout 180 scp -q -o StrictHostKeyChecking=no -i "$K" -P "$A_PORT" \
            "$S/rc/chunk_$i.bin" root@"$A_IP":/workspace/ 2>/dev/null
      fi ) &
    i=$((i+1))
  done < "$PODS"
  wait
  ST=$(T=30 SSH "$A_IP" "$A_PORT" 'ls /workspace/chunk_*.bin 2>/dev/null | wc -l')
  say "staged $ST/$CHUNKS on card 0"
  [ "$ST" -eq "$CHUNKS" ] || { say "⛔ ABORT: aggregate would run on a partial set"; exit 1; }
  ;;

agg)
  say "=== aggregate (agg-chunks, in-process on card 0) ==="
  T=45 SSH "$A_IP" "$A_PORT" "cd /workspace && rm -f agg.log && \
     HAZYNC_LIFTX_HINT=1 HAZYNC_BLOCK=/workspace/$BLOCK HAZYNC_CHUNKS=$CHUNKS HAZYNC_RECEIPTS=/workspace \
     HAZYNC_AGG_OUT=/workspace/block_receipt.bin \
     nohup setsid ./hazync-host-cuda agg-chunks > agg.log 2>&1 < /dev/null & disown; exit 0" >/dev/null 2>&1
  say "aggregate started on card 0"
  ;;

watchagg)
  R=$(T=40 SSH "$A_IP" "$A_PORT" 'cd /workspace && grep -E "VERIFIED|joins [0-9]+/|assembling|panicked|error" agg.log 2>/dev/null | tail -6; echo "ALIVE:$(ps -eo comm | grep -c "^hazync-host-cud")"')
  echo "$R"
  J=$(echo "$R" | grep -oE 'joins [0-9]+/[0-9]+' | tail -1)
  [ -n "$J" ] && { T0=$(cat "$S/t0" 2>/dev/null || echo 0); \
      echo "$(python3 -c "print(round($(date +%s.%N)-$T0,1))") ${J#joins }" >> "$S/joins.tsv"; }
  echo "$R" | grep -q VERIFIED && say "=== VERIFIED ==="
  ;;

*) echo "phases: prep prove watch stage agg watchagg"; exit 2 ;;
esac
