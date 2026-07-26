#!/usr/bin/env bash
# End-to-end bundle-path test — the ONE path the unit suite doesn't exercise, and where the v0.9.0 parse
# bug lived: fetch a coordinator-served bundle, prove it via `prove-range-bridge`, and verify the receipt.
#
# A spend-block (default 170, the first real spend) is the meaningful case: its bundle carries non-empty
# txs/tx_prevouts, so a bundle serialisation/structure regression fails HERE even when every in-memory
# prove test passes (they never touch bridge -> bundle_<n>.json -> parse). Run before cutting a release.
#
#   HAZYNC_HOST=/path/to/host COORD_URL=http://<coord>:8899 ./e2e_bundle_test.sh [block]
set -euo pipefail
BLK="${1:-170}"
HOST="${HAZYNC_HOST:?set HAZYNC_HOST to the prover host binary}"
COORD="${COORD_URL:?set COORD_URL to a coordinator that serves the bundle}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
export HAZYNC_BRIDGE_OUT="$WORK"

echo "e2e bundle test — block $BLK (served bundle -> prove -> verify)"

echo "[1/3] fetch bundle for block $BLK"
curl -fsS "$COORD/api/witness/$BLK" -o "$WORK/bundle_$BLK.json"
echo "      bundle: $(stat -c%s "$WORK/bundle_$BLK.json") bytes"
grep -q '"txs"' "$WORK/bundle_$BLK.json" || { echo "FAIL: bundle has no txs field"; exit 1; }

echo "[2/3] prove-range-bridge $BLK   (the path the v0.9.0 parse bug broke)"
( cd "$WORK" && "$HOST" prove-range-bridge "$BLK" >/dev/null )
test -f "$WORK/range_$BLK.bin" || { echo "FAIL: no receipt produced"; exit 1; }

echo "[3/3] verify-any the receipt"
line="$("$HOST" verify-any "$WORK/range_$BLK.bin" | grep '^RANGE-OK' || true)"
echo "      $line"
echo "$line" | grep -q "lo=$BLK hi=$BLK" \
  && echo "E2E PASS — block $BLK bundle proved + verified" \
  || { echo "FAIL: no RANGE-OK for block $BLK"; exit 1; }
