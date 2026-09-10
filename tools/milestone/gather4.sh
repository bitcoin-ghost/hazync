#!/bin/bash
# Everything off the cards for run 4 BEFORE teardown: per-card facts, prove logs, GPU samples.
S=${HAZYNC_RUNDIR:?set HAZYNC_RUNDIR to a working directory for this run}
K=~/.ssh/ghost_signet_ed25519
D=/home/defenwycke/hazync-milestone-966256-run4
LOG=$S/gather4.log; : > $LOG
while read -r PID IP PORT LOC CHUNK _ _; do
 ( D2=$D/cards/chunk_$CHUNK; mkdir -p $D2
   RDIR=/workspace; grep -qx "$CHUNK" $S/_reassigned 2>/dev/null && RDIR=/workspace/re$CHUNK
   for f in result.json facts.json prove.log gpu_samples.csv run.log aggw.log aa.log; do
     timeout 90 scp -q -o ConnectTimeout=20 -i $K -P "$PORT" root@"$IP":$RDIR/$f $D2/ 2>/dev/null
   done
   # identity + placement, captured live while the pod still exists
   timeout 30 ssh -n -o ConnectTimeout=12 -i $K -p "$PORT" root@"$IP" \
     'echo "gpu=$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader)"
      echo "cpu=$(nproc) cores, $(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2- | sed "s/^ //")"
      echo "ram=$(free -g | awk "/^Mem:/{print \$2}") GB"
      echo "host=$(hostname)"' > $D2/host.txt 2>/dev/null
   echo "$CHUNK $LOC $PID $IP:$PORT $(ls $D2 | tr "\n" "," )" >> $LOG ) &
done < $S/assign_opt.txt
wait
sort -n $LOG -o $LOG
echo "DONE $(wc -l < $LOG) cards" >> $LOG
