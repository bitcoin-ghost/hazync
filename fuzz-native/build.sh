#!/usr/bin/env bash
# Compile the CONSENSUS-MATH half of the guest's Core build for the HOST, so it can be
# differentially fuzzed without a zkVM. docs/FUZZING.md calls this "the highest-value next step" and
# names the missing prerequisite: standing up $HAZYNC_BASE the way provision-vps.sh does.
#
# Mirrors prover/methods/guest/build.rs exactly, MINUS the rv32im/ilp32 flags — Core consensus code
# is portable, which is the whole reason this is possible.
#
#   -std=c++20 -fexceptions -fno-rtti -O3 -w
#   include order: coreshim FIRST (its no-op sync.h/threadsafety.h override Core's pthread versions),
#   then Core's src, then secp's include.
set -euo pipefail
BASE="${HAZYNC_BASE:-$HOME/hazync-build}"
CORE="$BASE/bitcoin-core/src"; SHIM="$BASE/coreshim"; SECP="$BASE/secp256k1"
OUT="${OUT:-$(cd "$(dirname "$0")" && pwd)/build}"; mkdir -p "$OUT"
[ -d "$CORE" ] || { echo "no Core at $CORE — run provision-vps.sh (HAZYNC_PROVISION=deps)"; exit 2; }

# The pure-math consensus TUs: subsidy, retarget, merkle, PoW, chain work. No script interpreter,
# no secp — those need the full link and come in a later pass.
TUS=(
  arith_uint256.cpp uint256.cpp
  consensus/merkle.cpp
  pow.cpp chain.cpp
  primitives/block.cpp primitives/transaction.cpp
  kernel/chainparams.cpp util/chaintype.cpp
  crypto/sha256.cpp crypto/sha512.cpp crypto/ripemd160.cpp crypto/sha1.cpp crypto/hmac_sha512.cpp
  hash.cpp util/strencodings.cpp crypto/hex_base.cpp
  consensus/tx_check.cpp script/script.cpp script/script_error.cpp
  script/interpreter.cpp pubkey.cpp             # VerifyScript + the signature checker
  crypto/sha256_sse4.cpp                        # the SIMD path the guest disables and the host needs
)

# ⛔ BOTH base patches make the provisioned tree non-portable, not just 0001.
#   0001  adds a Serialize(int) overload that is a redefinition on LP64   -> serialize.h
#   0002  routes CSHA256 through RISC0's accelerator (sys_sha_compress)   -> crypto/sha256.cpp
# docs/FUZZING.md says "Core consensus code is portable". Core is; the tree after provision-vps.sh
# is not. Compile pristine copies of exactly those two files for the host.
OVERLAY_TUS=(crypto/sha256.cpp)

# secp256k1, the same TUs and defines the guest uses (build.rs), minus the rv32im flags.
SECP_TUS=(secp256k1.c precomputed_ecmult.c precomputed_ecmult_gen.c)
# ⚠ NO ECMULT_WINDOW_SIZE here, deliberately. The guest builds window 21 and build.rs REGENERATES
# src/precomputed_ecmult.c to match; the checked-in table is for secp's default and hard-errors on a
# mismatch ("configuration mismatch, invalid ECMULT_WINDOW_SIZE"). The window is a speed/size trade
# over a precomputed table — it does not change what verification DECIDES — so the host harness uses
# the shipped table. If this harness is ever extended to compare TIMING rather than results, that
# stops being true.
SECP_DEFS=(-DENABLE_MODULE_SCHNORRSIG=1 -DENABLE_MODULE_EXTRAKEYS=1 -DENABLE_MODULE_RECOVERY=1 -DENABLE_MODULE_ELLSWIFT=1)
# ⛔ patches/0001 makes the tree ILP32-SPECIFIC and it does NOT build natively.
# On rv32im `int` and `int32_t` are distinct types, so Core needs an extra Serialize(int) overload
# and provision-vps.sh applies one. On x86-64 they are the SAME type, so that overload is a
# redefinition:
#
#   serialize.h:267: error: redefinition of 'template<class Stream> void Serialize(Stream&, int)'
#
# docs/FUZZING.md says "Core consensus code is portable", which is true of Core itself and NOT of
# the tree after provisioning. Overlay the pristine serialize.h ahead of $CORE on the include path.
OVERLAY="$OUT/overlay"; mkdir -p "$OVERLAY"
if [ ! -f "$OVERLAY/serialize.h" ]; then
  git -C "$BASE/bitcoin-core" show HEAD:src/serialize.h > "$OVERLAY/serialize.h" 2>/dev/null \
    || { echo "could not extract a pristine serialize.h from the Core checkout"; exit 2; }
  echo "== overlaid pristine serialize.h (reverts patches/0001 for the host build only) =="
fi

FLAGS=(-std=c++20 -fexceptions -fno-rtti -O3 -w -I"$OVERLAY" -I"$SHIM" -I"$CORE" -I"$SECP/include")
# The comma in -fsanitize=address,undefined belongs to the FLAG, not to the array. Unquoted it
# is read as an element separator (SC2054). NB a comment whose first word is the linter's name is
# parsed as a DIRECTIVE (SC1072/SC1073), which is why this sentence is phrased around it.
[ "${ASAN:-0}" = 1 ] && FLAGS+=("-fsanitize=address,undefined" -fno-omit-frame-pointer -g)

echo "== compiling $(( ${#TUS[@]} )) Core TUs for the host =="
objs=()
for tu in "${OVERLAY_TUS[@]}"; do
  mkdir -p "$OVERLAY/$(dirname "$tu")"
  [ -f "$OVERLAY/$tu" ] || git -C "$BASE/bitcoin-core" show "HEAD:src/$tu" > "$OVERLAY/$tu" \
    || { echo "could not extract pristine $tu"; exit 2; }
done
for tu in "${TUS[@]}"; do
  src="$CORE/$tu"; [ -f "$OVERLAY/$tu" ] && src="$OVERLAY/$tu"
  o="$OUT/$(echo "$tu" | tr / _).o"
  g++ "${FLAGS[@]}" -c "$src" -o "$o" 2>>"$OUT/compile.log" || { echo "  FAILED $tu (see $OUT/compile.log)"; exit 1; }
  objs+=("$o"); printf '.'
done
echo
echo "== compiling secp256k1 for the host =="
for tu in "${SECP_TUS[@]}"; do
  o="$OUT/secp_$(echo "$tu" | tr / _).o"
  gcc -O2 -w "${SECP_DEFS[@]}" -I"$SECP" -I"$SECP/src" -I"$SECP/include" \
      -c "$SECP/src/$tu" -o "$o" 2>>"$OUT/compile.log" || { echo "  FAILED secp $tu"; exit 1; }
  objs+=("$o"); printf '.'
done
echo
ar rcs "$OUT/libcoreconsensus.a" "${objs[@]}"
echo "== wrote $OUT/libcoreconsensus.a ($(stat -c%s "$OUT/libcoreconsensus.a") bytes) =="
