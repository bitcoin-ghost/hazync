#!/usr/bin/env bash
# Profile the field operations one ECDSA verification performs. hazync#129.
#
# Builds a COPY of the pinned secp256k1 v0.5.1 with counters injected. Neither the repo nor
# ~/hazync-build/secp256k1 is modified.
set -euo pipefail

N="${N:-100}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECP_SRC="${SECP_SRC:-$HOME/hazync-build/secp256k1}"
WORK="${WORK:-$(mktemp -d)}"

[ -d "$SECP_SRC/src" ] || {
    echo "FATAL: no secp256k1 source at $SECP_SRC" >&2
    echo "  provision-vps.sh clones it there, or set SECP_SRC=<path>" >&2
    exit 1
}

# Same pin the guest compiles. If this ever disagrees, the profile is about the wrong library.
ver=$(git -C "$SECP_SRC" describe --tags 2>/dev/null || echo "unknown")
echo "  secp256k1 source: $SECP_SRC ($ver)"
[ "$ver" = "v0.5.1" ] || echo "  WARNING: expected v0.5.1 (what Bitcoin Core v28.0 bundles); got $ver"

echo "  scratch: $WORK"
cp -r "$SECP_SRC" "$WORK/secp256k1"
rm -rf "$WORK/secp256k1/.git"

python3 "$HERE/patch_counters.py" "$WORK/secp256k1"

# Emit the op-count probe the harness calls, now that the table size is known.
cat >> "$WORK/secp256k1/hz_field_ops.c" <<'EOF'
int hz_n_ops(void) { return HZ_FIELD_OP_COUNT; }
EOF

# Build flags mirror the guest's, minus the rv32 target: same modules, same ecmult window, so the
# operation MIX matches what the guest executes.
#
# USE_FORCE_WIDEMUL_INT64 is the load-bearing one. Without it libsecp picks its backend from whether
# __int128 exists, and on an x86-64 host it does -- so it compiles field_5x52 and the counters injected
# into field_10x26 never execute. The first version of this script did exactly that and printed a table
# of zeros that looked like a successful run.
cc -O2 -o "$WORK/profile" \
    -I"$WORK/secp256k1" -I"$WORK/secp256k1/src" -I"$WORK/secp256k1/include" \
    -DECMULT_WINDOW_SIZE=19 -DECMULT_GEN_KB=22 \
    -DENABLE_MODULE_SCHNORRSIG=1 -DENABLE_MODULE_EXTRAKEYS=1 \
    -DUSE_FORCE_WIDEMUL_INT64=1 \
    "$WORK/secp256k1/src/secp256k1.c" \
    "$WORK/secp256k1/src/precomputed_ecmult.c" \
    "$WORK/secp256k1/src/precomputed_ecmult_gen.c" \
    "$WORK/secp256k1/hz_field_ops.c" \
    "$HERE/main.c"

"$WORK/profile" "$N"
echo "  (scratch left at $WORK; rm -rf when done)"
