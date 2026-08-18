#!/usr/bin/env bash
# Build libsecp256k1 with the hzfe backend in place of 10x26, and verify real signatures with it.
# hazync#129. This is the first test of the EC layer against a backend with no magnitude system.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECP="${SECP_SRC:-$HOME/hazync-build/secp256k1}"
W="$(mktemp -d)"
echo "  scratch: $W"
cp -r "$SECP" "$W/secp256k1"; rm -rf "$W/secp256k1/.git"
S="$W/secp256k1"

cp "$HERE/field_hzfe.h" "$HERE/field_hzfe_impl.h" "$HERE/hzfe.h" "$S/src/"
cp "$HERE/hzfe.c" "$HERE/modmul_host.c" "$HERE/hzfe_inv.c" "$S/src/"

# Select the backend. field.h picks the representation, field_impl.h picks the implementation.
python3 - "$S" <<'PYEOF'
import sys, pathlib
S = pathlib.Path(sys.argv[1])

# Both files select the backend with a clean #if/#elif/#else chain. Prepend a branch rather than
# wrapping the chain: an earlier attempt inserted #else before an #elif, which is invalid and left the
# impl header simply not included -- presenting as "used but never defined" for every entry point.
for name, first, hdr in (
        ("field.h",      '#if defined(SECP256K1_WIDEMUL_INT128)\n#include "field_5x52.h"',      "field_hzfe.h"),
        ("field_impl.h", '#if defined(SECP256K1_WIDEMUL_INT128)\n#include "field_5x52_impl.h"', "field_hzfe_impl.h")):
    p = S / "src" / name
    t = p.read_text()
    if first not in t:
        sys.exit(f"FATAL: selection block not found in {name}; libsecp layout changed")
    t = t.replace(first,
        f'#if defined(USE_HZFE_FIELD)\n#include "{hdr}"\n#elif defined(SECP256K1_WIDEMUL_INT128)\n'
        + first.split("\n", 1)[1], 1)
    p.write_text(t)
    print(f"    {name}: hzfe branch prepended")
PYEOF

cc -O2 -o "$W/itest" \
   -I"$S" -I"$S/src" -I"$S/include" \
   -DUSE_HZFE_FIELD=1 -DUSE_FORCE_WIDEMUL_INT64=1 \
   -DECMULT_WINDOW_SIZE=19 -DECMULT_GEN_KB=22 \
   -DENABLE_MODULE_SCHNORRSIG=1 -DENABLE_MODULE_EXTRAKEYS=1 \
   "$S/src/secp256k1.c" "$S/src/precomputed_ecmult.c" "$S/src/precomputed_ecmult_gen.c" \
   "$S/src/hzfe.c" "$S/src/modmul_host.c" "$S/src/hzfe_inv.c" \
   "$HERE/itest.c" 2>"$W/cc.log" || { echo "  FATAL: compile failed"; sed -n '1,30p' "$W/cc.log"; exit 1; }

"$W/itest"
rc=$?
rm -rf "$W"
exit $rc
