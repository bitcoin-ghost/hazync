#!/bin/bash
# The WASM build and the CLI must never disagree about a proof.
#
# They share `hazync_verify::verify`, so in principle they cannot — but "in principle" is how the
# RangeState mirrors drifted (#32). This asserts it against real fixtures instead, on every CI run.
#
# The mapping under test:
#
#     CLI exit 0  <->  status "verified"
#     CLI exit 1  <->  status "invalid"
#     CLI exit 2  <->  status "not_anchored"
#
# A disagreement here means one of the two consumers is deciding the anchoring question differently,
# which is the failure the shared library exists to prevent. It would most likely fail OPEN in the
# browser — the copy most people will use, and the one least able to be inspected after the fact.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

WASM=target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm
CLI=../verifier/target/release/hazync-verify
FIX=../prover/testdata/snark
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

command -v node >/dev/null || { echo "SKIP: node not available"; exit 0; }
[ -f "$WASM" ] || { echo "✗ no wasm build — run ./build.sh first"; exit 1; }
[ -x "$CLI" ]  || { echo "✗ no CLI build — cargo build --release in ../verifier first"; exit 1; }

# Fixtures: the two committed proofs, plus mutations that must be refused. A parity test over valid
# inputs only would pass even if both consumers accepted everything.
cp "$FIX/fold_1000.snark" "$TMP/valid_anchored.bin"
cp "$FIX/neg500.snark"    "$TMP/valid_segment.bin"
head -c 200 "$FIX/fold_1000.snark" > "$TMP/truncated.bin"
: > "$TMP/empty.bin"
python3 - "$FIX/fold_1000.snark" "$TMP/bitflip.bin" <<'PY'
import sys
d = bytearray(open(sys.argv[1],'rb').read())
d[-40] ^= 0x01                      # perturb the proof body, not the header
open(sys.argv[2],'wb').write(d)
PY

# `import(expr)` — a static import cannot take a path computed at runtime.
cat > "$TMP/run.mjs" <<'EOF'
import fs from 'fs';
const { loadVerifier } = await import(process.argv[2]);
const v = await loadVerifier(fs.readFileSync(process.argv[3]));
console.log(v.verify(new Uint8Array(fs.readFileSync(process.argv[4]))).status);
EOF

fail=0
for f in "$TMP"/*.bin; do
    name=$(basename "$f")
    # `|| rc=$?` — a bare `cmd; rc=$?` dies under `set -e` before the assignment runs.
    rc=0; "$CLI" "$f" >/dev/null 2>&1 || rc=$?
    st=$(node "$TMP/run.mjs" "$PWD/hazync-verify.js" "$PWD/$WASM" "$f" 2>/dev/null)

    case "$rc" in
        0) want=verified ;;
        1) want=invalid ;;
        2) want=not_anchored ;;
        *) echo "✗ $name: CLI exited $rc, which is not a defined verdict"; fail=1; continue ;;
    esac

    if [ "$st" = "$want" ]; then
        printf '  ✓ %-20s CLI exit %s  ==  wasm %s\n' "$name" "$rc" "$st"
    else
        printf '  ✗ %-20s CLI exit %s (%s)  !=  wasm %s\n' "$name" "$rc" "$want" "${st:-<none>}"
        fail=1
    fi
done

# The whole suite passing because every case is "invalid" would be vacuous — a verifier that rejects
# everything agrees with itself perfectly. Assert both accept the one proof that should pass.
rc=0; "$CLI" "$TMP/valid_anchored.bin" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ] || { echo "✗ the known-good proof did not verify — every result above is meaningless"; fail=1; }

[ $fail -eq 0 ] && echo "✓ CLI and WASM agree on all $(ls "$TMP"/*.bin | wc -l) inputs"
exit $fail
