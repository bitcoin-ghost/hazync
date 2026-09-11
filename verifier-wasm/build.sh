#!/bin/bash
# Build the WASM verifier. No wasm-bindgen, no wasm-pack, no post-processing — the .wasm is a plain
# cargo output so anyone can rebuild it and compare byte for byte against what is served.
set -euo pipefail
cd "$(dirname "$0")" || exit 1
rustup target add wasm32-unknown-unknown 2>/dev/null || true
# #249: the served wasm must be byte-reproducible from ANY checkout, on any machine.
#
# Two things made it path-dependent, and both are handled here:
#   1. Embedded paths. Panic locations survive `strip`: the v0.21.0 wasm carried 14 x the builder's
#      .rustup path and 2 x the checkout path -- a username in a public artifact. --remap-path-prefix
#      rewrites them to fixed strings.
#   2. The checkout location itself. Cargo hashes each PATH dependency's absolute location into its
#      crate metadata, which perturbs codegen even with every path string remapped: measured, two
#      checkouts of one commit gave two different binaries (193 bytes apart, no path string in the diff).
#      Cargo's `trim-paths` would fix it but is unstable in 1.97. So build from ONE fixed location:
#      stage the crate and its path dependencies (verifier -> rangestate -> coinbase-smt) under $STAGE.
# Same source + same toolchain => same bytes, wherever the repo lives.
REPO=$(cd .. && pwd)
STAGE=${HAZYNC_WASM_STAGE:-/tmp/hazync-wasm-build}
rm -rf "$STAGE" && mkdir -p "$STAGE"
for c in verifier-wasm verifier rangestate coinbase-smt; do
    mkdir -p "$STAGE/$c"
    (cd "$REPO/$c" && tar --exclude=./target -cf - .) | (cd "$STAGE/$c" && tar -xf -)
done
SYSROOT=$(rustc --print sysroot)
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$SYSROOT=/rust-sysroot --remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo --remap-path-prefix=$STAGE=/hazync"
(cd "$STAGE/verifier-wasm" && cargo build --release --target wasm32-unknown-unknown)
mkdir -p target/wasm32-unknown-unknown/release          # where package-release.sh and the docs expect it
cp "$STAGE/verifier-wasm/target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm" \
   target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm
W=target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm
printf 'built %s\n  raw     %8d bytes\n  gzipped %8d bytes  (what a browser downloads)\n  sha256  %s\n' \
    "$W" "$(stat -c%s "$W")" "$(gzip -9 -c "$W" | wc -c)" "$(sha256sum "$W" | cut -d' ' -f1)"
