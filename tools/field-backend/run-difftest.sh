#!/usr/bin/env bash
# Differential test: hzfe against stock libsecp256k1. hazync#129 Step 2.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECP="${SECP_SRC:-$HOME/hazync-build/secp256k1}"
N="${N:-20000}"
W="$(mktemp -d)"
[ -d "$SECP/src" ] || { echo "FATAL: no secp256k1 at $SECP" >&2; exit 1; }

# USE_FORCE_WIDEMUL_INT64 selects the 10x26 backend -- the one riscv32im compiles. Without it an
# x86-64 host builds 5x52 and the comparison is against a backend the guest never runs.
cc -O2 -g -o "$W/difftest" \
   -I"$HERE" -I"$SECP" -I"$SECP/src" -I"$SECP/include" \
   -DUSE_FORCE_WIDEMUL_INT64=1 -DECMULT_WINDOW_SIZE=19 -DECMULT_GEN_KB=22 \
   "$HERE/difftest.c" "$HERE/hzfe.c" "$HERE/modmul_host.c" \
   "$SECP/src/precomputed_ecmult.c" "$SECP/src/precomputed_ecmult_gen.c" 2>&1 | head -20
"$W/difftest" "$N"
rc=$?
rm -rf "$W"
exit $rc
