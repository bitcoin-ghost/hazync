#!/usr/bin/env bash
# Multi-GPU tip-prover fan-out: prove the block's chunks across NGPU GPUs (one process per GPU via
# CUDA_VISIBLE_DEVICES), each chunk receipt -> chunk_<i>.bin, then aggregate from the files.
#   NGPU=8 HAZYNC_CHUNKS=50 HAZYNC_BLOCK=/path/block.json bash cluster.sh
cd "$(dirname "$0")"
export RUST_LOG=${RUST_LOG:-error}
export HAZYNC_BLOCK=${HAZYNC_BLOCK:?set HAZYNC_BLOCK to the block JSON}
NGPU=${NGPU:-1}; NCHUNKS=${HAZYNC_CHUNKS:-8}; export HAZYNC_CHUNKS=$NCHUNKS
echo "CLUSTER: $NCHUNKS chunks across $NGPU GPU(s)"
rm -f chunk_*.bin
for ((base=0; base<NCHUNKS; base+=NGPU)); do          # waves of NGPU concurrent chunk proofs
  for ((g=0; g<NGPU && base+g<NCHUNKS; g++)); do
    i=$((base+g)); CUDA_VISIBLE_DEVICES=$g ./target/release/host prove-chunk $i &
  done
  wait                                                 # wait for the wave (one chunk per GPU)
done
./target/release/host agg-chunks                        # env::verify all chunk receipts -> block proof
