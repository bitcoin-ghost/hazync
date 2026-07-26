#!/usr/bin/env bash
# Chainparams anchor test runner. Compiles Bitcoin Core's real kernel/chainparams.cpp (+ consensus.h +
# interpreter.h) natively and asserts every consensus constant the guest relies on — read from Core's
# own CChainParams::Main() — equals the canonical mainnet value. See testdata/chainparams_check.cpp.
# The guest sources these same constants from the same compiled source and runtime-pins its Rust
# literals to them (assert_core_constants), so this test is the outer anchor to the canonical values.
#
# Run from prover/:  HAZYNC_BASE=$HOME/hazync-build ./chainparams_check.sh
set -uo pipefail
BASE="${HAZYNC_BASE:-$HOME/hazync-build}"
CORE="$BASE/bitcoin-core/src"; SECP="$BASE/secp256k1/include"; SHIM="$BASE/coreshim"
CXX="${CXX:-g++}"; HERE="$(cd "$(dirname "$0")" && pwd)"
fail(){ echo "FAIL: $*" >&2; exit 1; }
[ -d "$CORE" ] || fail "Core source not found at $CORE (run provision-vps.sh, or set HAZYNC_BASE)"

# The guest patches serialize.h (ilp32) + sha256.cpp (risc0 accel); both collide with a native host
# build (int==int32_t; sys_sha_compress is guest-only). Compile against the STOCK versions, then always
# restore the patched files — the consensus constants are identical either way.
SER="$CORE/serialize.h"; SHA="$CORE/crypto/sha256.cpp"
S1="$(mktemp)"; S2="$(mktemp)"; cp "$SER" "$S1"; cp "$SHA" "$S2"
restore(){ cp "$S1" "$SER"; cp "$S2" "$SHA"; rm -f "$S1" "$S2" /tmp/chainparams_check_bin; }
trap restore EXIT
if git -C "$BASE/bitcoin-core" rev-parse >/dev/null 2>&1; then
  git -C "$BASE/bitcoin-core" checkout -- src/serialize.h src/crypto/sha256.cpp 2>/dev/null || true
fi

"$CXX" -std=c++20 -O2 -w -msse4.1 -I"$SHIM" -I"$CORE" -I"$SECP" \
  "$HERE/testdata/chainparams_check.cpp" \
  "$CORE/kernel/chainparams.cpp" "$CORE/primitives/block.cpp" "$CORE/util/chaintype.cpp" \
  "$CORE/consensus/merkle.cpp" "$CORE/primitives/transaction.cpp" "$CORE/script/script.cpp" \
  "$CORE/uint256.cpp" "$CORE/arith_uint256.cpp" "$CORE/hash.cpp" \
  "$CORE/crypto/sha256.cpp" "$CORE/crypto/sha256_sse4.cpp" "$CORE/crypto/sha512.cpp" \
  "$CORE/crypto/ripemd160.cpp" "$CORE/crypto/hmac_sha512.cpp" "$CORE/crypto/sha1.cpp" \
  "$CORE/util/strencodings.cpp" "$CORE/crypto/hex_base.cpp" \
  -o /tmp/chainparams_check_bin || fail "compile failed"

/tmp/chainparams_check_bin || fail "chainparams check FAILED"
echo "ok: chainparams anchor test passed"
