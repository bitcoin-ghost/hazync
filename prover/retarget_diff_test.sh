#!/usr/bin/env bash
# Retarget differential test runner. Compiles the REAL Bitcoin Core retarget source (pow.cpp +
# chain.cpp + arith_uint256) natively and checks the carved CalculateNextWorkRequired against the actual
# on-chain nBits at every mainnet retarget (testdata/retarget_vectors.csv), against an independent
# transcription, and against the clamp/powLimit/monotonicity invariants. See testdata/retarget_diff.cpp.
#
# Run from prover/:  HAZYNC_BASE=$HOME/hazync-build ./retarget_diff_test.sh
set -uo pipefail
BASE="${HAZYNC_BASE:-$HOME/hazync-build}"
CORE="$BASE/bitcoin-core/src"
SECP="$BASE/secp256k1/include"
CXX="${CXX:-g++}"
HERE="$(cd "$(dirname "$0")" && pwd)"
fail(){ echo "FAIL: $*" >&2; exit 1; }

[ -d "$CORE" ] || fail "Core source not found at $CORE (run provision-vps.sh, or set HAZYNC_BASE)"
[ -f "$HERE/testdata/retarget_vectors.csv" ] || fail "missing testdata/retarget_vectors.csv"

# The guest build applies the ilp32 serialize patch (0001), which collides on a host where int==int32_t.
# Compile the test against STOCK serialize.h, then always restore the patched file so the guest build is
# unaffected. The retarget math (pow/chain/arith) is byte-identical either way — the patch only adds an
# int overload used on the 32-bit target.
SER="$CORE/serialize.h"
SAVED="$(mktemp)"; cp "$SER" "$SAVED"
restore(){ cp "$SAVED" "$SER"; rm -f "$SAVED" /tmp/retarget_diff_bin; }
trap restore EXIT
if git -C "$BASE/bitcoin-core" rev-parse >/dev/null 2>&1; then
  git -C "$BASE/bitcoin-core" checkout -- src/serialize.h 2>/dev/null || true
fi

"$CXX" -std=c++20 -O2 -w -I"$CORE" -I"$SECP" \
  "$HERE/testdata/retarget_diff.cpp" \
  "$CORE/pow.cpp" "$CORE/chain.cpp" "$CORE/arith_uint256.cpp" "$CORE/uint256.cpp" \
  "$CORE/util/strencodings.cpp" "$CORE/crypto/hex_base.cpp" \
  -o /tmp/retarget_diff_bin || fail "compile failed"

( cd "$HERE" && /tmp/retarget_diff_bin ) || fail "retarget differential test FAILED"
echo "ok: retarget differential test passed"
