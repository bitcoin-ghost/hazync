#!/usr/bin/env bash
# Parallel range-fold: prove blocks LO..HI as INDEPENDENT range proofs across NGPU GPUs (level 0),
# then fold adjacent ranges pairwise in a tree (each level parallel across GPUs), then verify the
# genesis-anchored result. This is the parallel backfill: per-block proofs are independent, folds are
# log-depth. Wall-clock ~ (one block prove) + log2(N)*(one fold), not N sequential steps.
cd /root/hazync/prover
source ~/.cargo/env
export PATH="$HOME/.risc0/bin:$HOME/.cargo/bin:/usr/local/cuda-12.6/bin:$PATH"
export HAZYNC_BASE=/root/hazync-build CUDA_PATH=/usr/local/cuda-12.6 RISC0_HOME=$HOME/.risc0 RUST_LOG=error
export HAZYNC_WITNESS_DIR=${HAZYNC_WITNESS_DIR:-/root/witnesses_bridge}
HOST=./target/release/host
NGPU=${NGPU:-2}; LO=${LO:-1}; HI=${HI:-8}
rm -f range_*.bin fold_*.bin
T0=$(date +%s)
echo "=== LEVEL 0: prove blocks $LO..$HI as ranges across $NGPU GPUs ($HAZYNC_WITNESS_DIR) ==="
for ((base=LO; base<=HI; base+=NGPU)); do
  for ((g=0; g<NGPU && base+g<=HI; g++)); do
    n=$((base+g)); CUDA_VISIBLE_DEVICES=$g HAZYNC_OUT=range_${n}.bin $HOST prove-range $n &
  done
  wait
done
echo "=== TREE FOLD (parallel per level) ==="
files=(); for ((n=LO; n<=HI; n++)); do files+=("range_${n}.bin"); done
level=1
while [ ${#files[@]} -gt 1 ]; do
  next=(); i=0; g=0
  while [ $i -lt ${#files[@]} ]; do
    if [ $((i+1)) -lt ${#files[@]} ]; then
      out="fold_L${level}_${i}.bin"
      CUDA_VISIBLE_DEVICES=$((g%NGPU)) $HOST fold-range "${files[$i]}" "${files[$((i+1))]}" "$out" &
      next+=("$out"); g=$((g+1)); i=$((i+2))
      [ $((g % NGPU)) -eq 0 ] && wait
    else
      next+=("${files[$i]}"); i=$((i+1))
    fi
  done
  wait
  files=("${next[@]}")
  echo "  level $level -> ${#files[@]} range(s) remain"
  level=$((level+1))
done
echo "=== VERIFY (genesis-anchored) ==="
$HOST verify-range "${files[0]}"
echo "TOTAL wall-clock: $(( $(date +%s) - T0 ))s"
