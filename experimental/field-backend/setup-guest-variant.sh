#!/usr/bin/env bash
# Assemble a THROWAWAY guest build using the hzfe field backend, for the Step 3 measurement. hazync#129.
#
# Nothing in the repo or in ~/hazync-build is modified. Everything happens in a scratch tree, because
# this build necessarily produces a different METHOD_ID -- the guest is the thing being changed. That id
# is a fact about a disposable binary, not a change to the project's baseline. Adopting the backend
# later is a separate, scheduled decision.
#
# Cycles are counted in EXECUTE mode, which needs no GPU: sys_bigint is emulated by the executor. This
# is the same method that produced the 2,299,144-cycle baseline and the +10% result for the naive
# intercept, both recorded in docs/ACCELERATION.md.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_BASE="${HAZYNC_BASE_SRC:-$HOME/hazync-build}"
OUT="${1:?usage: setup-guest-variant.sh <scratch-dir>}"

mkdir -p "$OUT"
echo "  scratch: $OUT"

# 1. consensus sources
mkdir -p "$OUT/base"
for d in secp256k1 bitcoin-core coreshim; do
    [ -e "$SRC_BASE/$d" ] || { echo "FATAL: missing $SRC_BASE/$d" >&2; exit 1; }
    cp -r "$SRC_BASE/$d" "$OUT/base/"
done
rm -rf "$OUT/base/secp256k1/.git" "$OUT/base/bitcoin-core/.git"
S="$OUT/base/secp256k1"

# 2. the backend, into libsecp's own source tree
cp "$REPO/experimental/field-backend/hzfe.h" "$REPO/experimental/field-backend/hzfe.c" \
   "$REPO/experimental/field-backend/hzfe_inv.c" \
   "$REPO/experimental/field-backend/field_hzfe.h" "$REPO/experimental/field-backend/field_hzfe_impl.h" "$S/src/"

# 3. backend selection. Prepend a branch to the existing #if/#elif chain rather than wrapping it --
#    inserting #else before an #elif is invalid and silently drops the header.
python3 - "$S" <<'PYEOF'
import sys, pathlib
S = pathlib.Path(sys.argv[1])
for name, first, hdr in (
        ("field.h",      '#if defined(SECP256K1_WIDEMUL_INT128)\n#include "field_5x52.h"',      "field_hzfe.h"),
        ("field_impl.h", '#if defined(SECP256K1_WIDEMUL_INT128)\n#include "field_5x52_impl.h"', "field_hzfe_impl.h")):
    p = S / "src" / name
    t = p.read_text()
    if first not in t:
        sys.exit(f"FATAL: selection block not found in {name}")
    t = t.replace(first, f'#if defined(USE_HZFE_FIELD)\n#include "{hdr}"\n#elif defined(SECP256K1_WIDEMUL_INT128)\n'
                         + first.split("\n", 1)[1], 1)
    p.write_text(t)
    print(f"    {name}: hzfe branch prepended")
PYEOF

# 4. the prover tree, minus build artefacts, PLUS the sibling crates it depends on by relative path.
#    host/Cargo.toml reaches outside the workspace for ../../accumulator and ../../coinbase-smt, so
#    copying prover/ alone yields a workspace whose members cannot resolve. Enumerated rather than
#    hardcoded, so a new sibling dependency does not silently break this.
mkdir -p "$OUT/prover"
tar -C "$REPO/prover" --exclude=target -cf - . | tar -C "$OUT/prover" -xf -

for sib in $(grep -rhoE 'path *= *"\.\./\.\./[a-z0-9_-]+"' "$REPO"/prover/*/Cargo.toml "$REPO"/prover/*/*/Cargo.toml 2>/dev/null \
             | grep -oE '\.\./\.\./[a-z0-9_-]+' | sed 's|\.\./\.\./||' | sort -u); do
    [ -d "$REPO/$sib" ] || { echo "FATAL: sibling crate $sib not found" >&2; exit 1; }
    mkdir -p "$OUT/$sib"
    tar -C "$REPO/$sib" --exclude=target -cf - . | tar -C "$OUT/$sib" -xf -
    echo "    sibling crate copied: $sib"
done

# 5. build.rs: select the backend and compile the new translation units
python3 - "$OUT/prover" <<'PYEOF'
import sys, pathlib
b = pathlib.Path(sys.argv[1]) / "methods" / "guest" / "build.rs"
t = b.read_text()
anchor = '.define("ECMULT_WINDOW_SIZE", win.as_str())'
if anchor not in t:
    sys.exit("FATAL: build.rs secp block not found")
t = t.replace(anchor, '.define("USE_HZFE_FIELD", "1")\n            ' + anchor, 1)
anchor2 = '.file(format!("{secp}/src/secp256k1.c"))'
t = t.replace(anchor2, anchor2 + '\n            .file(format!("{secp}/src/hzfe.c"))\n            .file(format!("{secp}/src/hzfe_inv.c"))', 1)
b.write_text(t)
print("    build.rs: USE_HZFE_FIELD set, hzfe.c and hzfe_inv.c added")
PYEOF

# 6. the modmul shim. risc0-zkvm does NOT re-export sys_bigint and does not enable the platform crate's
#    `export-syscalls`, so there is no C-callable symbol: the shim has to be Rust, with the platform
#    crate as a direct dependency.
cat > "$OUT/prover/methods/guest/src/hzfe_shim.rs" <<'RSEOF'
//! `hzfe_modmul` for the guest: one bigint precompile call. hazync#129.
//!
//! On the host this function is a schoolbook 512-bit product folded with 2^256 == 2^32 + 977 (mod p),
//! written to be an auditable oracle. Here it is a single `sys_bigint`, which is the entire point of
//! the backend: field elements stay in `[u32; 8]` permanently, so there is no per-operation conversion.
//! Converting per operation was measured at +10% on 2026-07-15 and is why a call swap does not work.
use risc0_zkvm_platform::syscall::{bigint, sys_bigint};

/// p = 2^256 - 2^32 - 977, little-endian words.
const P: [u32; bigint::WIDTH_WORDS] = [
    0xFFFF_FC2F, 0xFFFF_FFFE, 0xFFFF_FFFF, 0xFFFF_FFFF,
    0xFFFF_FFFF, 0xFFFF_FFFF, 0xFFFF_FFFF, 0xFFFF_FFFF,
];

/// Called from libsecp's field backend (field_hzfe_impl.h -> hzfe.c).
///
/// # Safety
/// All three pointers come from `secp256k1_fe`/`hzfe` values, which are `uint32_t[8]` and therefore
/// correctly sized and aligned for `[u32; 8]`.
#[no_mangle]
pub unsafe extern "C" fn hzfe_modmul(r: *mut u32, a: *const u32, b: *const u32) {
    sys_bigint(
        r as *mut [u32; bigint::WIDTH_WORDS],
        bigint::OP_MULTIPLY,
        a as *const [u32; bigint::WIDTH_WORDS],
        b as *const [u32; bigint::WIDTH_WORDS],
        &P,
    );
}
RSEOF

python3 - "$OUT/prover" <<'PYEOF'
import sys, pathlib
g = pathlib.Path(sys.argv[1]) / "methods" / "guest"
m = g / "src" / "main.rs"
t = m.read_text()
if "mod hzfe_shim;" not in t:
    t = t.replace("mod script_flags;", "mod script_flags;\nmod hzfe_shim;", 1)
    m.write_text(t)
c = g / "Cargo.toml"
t = c.read_text()
if "risc0-zkvm-platform" not in t:
    t = t.replace('sha2 = { version = "0.10", default-features = false }',
                  'sha2 = { version = "0.10", default-features = false }\n'
                  '# Direct dependency because risc0-zkvm re-exports neither sys_bigint nor the bigint\n'
                  '# constants, and does not enable the platform crate\'s export-syscalls feature.\n'
                  'risc0-zkvm-platform = { version = "=2.2.3", default-features = false, features = ["rust-runtime"] }', 1)
    c.write_text(t)
print("    guest: hzfe_shim wired in, risc0-zkvm-platform added")
PYEOF

echo "  ready. build with:"
echo "    cd $OUT/prover && HAZYNC_BASE=$OUT/base cargo build -p host"
