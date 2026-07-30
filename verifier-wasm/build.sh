#!/bin/bash
# Build the WASM verifier. No wasm-bindgen, no wasm-pack, no post-processing — the .wasm is a plain
# cargo output so anyone can rebuild it and compare byte for byte against what is served.
set -euo pipefail
cd "$(dirname "$0")" || exit 1
rustup target add wasm32-unknown-unknown 2>/dev/null || true
cargo build --release --target wasm32-unknown-unknown
W=target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm
printf 'built %s\n  raw     %8d bytes\n  gzipped %8d bytes  (what a browser downloads)\n  sha256  %s\n' \
    "$W" "$(stat -c%s "$W")" "$(gzip -9 -c "$W" | wc -c)" "$(sha256sum "$W" | cut -d' ' -f1)"
