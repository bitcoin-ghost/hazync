#!/usr/bin/env bash
# LEGACY / FALLBACK ONLY. The live party is served by the archive-node bridge (`host bridge`), which emits
# per-block *bundles* the coordinator serves from HAZYNC_BRIDGE_OUT — provers no longer replay the chain.
# This script generates the OLD per-block witness window (block_1.json .. block_N.json) that the replay
# path (`host prove-range`) consumes; keep it only for a bridge-less test deploy. Idempotent (skips existing).
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
