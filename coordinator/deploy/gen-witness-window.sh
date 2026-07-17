#!/usr/bin/env bash
# Generate the witness window the coordinator serves: block_1.json .. block_N.json.
# These are what contributors' provers replay to rebuild the accumulator. Idempotent (skips existing).
#
#   usage:  gen-witness-window.sh <up-to-height> [witness-dir]
#   e.g.:   ./gen-witness-window.sh 1000 /opt/hazync/witnesses
#
# Witnesses come from prover/fetch_block.py (pulls each block + its prevouts from a public API).
# For a big/whole-chain window you'd co-locate an archive/bridge node instead (the archive decision) —
# a rolling window is the launch default.
set -euo pipefail
N="${1:?usage: gen-witness-window.sh <up-to-height> [witness-dir]}"
DIR="${2:-/opt/hazync/witnesses}"
FETCH="${HAZYNC_FETCH:-/opt/hazync/prover/fetch_block.py}"

mkdir -p "$DIR"
made=0
for h in $(seq 1 "$N"); do
  out="$DIR/block_$h.json"
  [ -s "$out" ] && continue                    # already have it
  echo "== witness block $h"
  python3 "$FETCH" "$h" "$out"                  # fetch_block.py <height> <out.json>
  made=$((made+1))
done
echo "witness window ready: blocks 1..$N in $DIR ($made newly fetched)"
