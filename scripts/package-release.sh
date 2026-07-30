#!/bin/bash
# Assemble the release artifacts that do NOT need the fixed-path container.
#
# `prover/build-release.sh {cpu|cuda}` builds the host binaries inside ubuntu:22.04 with HOME=/root,
# because the guest image id absorbs $HOME/.cargo paths and a host-built binary would carry a
# non-canonical id. Neither artifact here contains a guest, so neither has that constraint:
#
#   hazync-worker       the Proof Party contributor CLI (hint -> prove -> submit; #37 removed claims)
#   hazync-verify.wasm  the browser verifier
#
# Both belong on the release and in the signed SHA256SUMS. The worker especially: it holds the
# contributor's ed25519 signing key and decides what is submitted under their name, so shipping it
# unsigned — or, as before, not shipping it at all and telling people to clone — is the worst option
# on the list.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
OUT="${OUT:-$PWD/dist}"
mkdir -p "$OUT"

# ---- worker -------------------------------------------------------------------------------------
# A single-file Python script with no imports outside the stdlib except `cryptography` (for ed25519),
# so "packaging" is a copy. Renamed to hazync-worker on the release: `hazync` alone is too generic a
# name to drop into someone's PATH, and it must match the `hazync-*` download glob in release-sign.yml.
python3 -c "import ast,sys; ast.parse(open('coordinator/hazync').read())" \
    || { echo "::error::coordinator/hazync does not parse — refusing to ship it"; exit 1; }
cp coordinator/hazync "$OUT/hazync-worker"
chmod +x "$OUT/hazync-worker"

# It must run. A syntax check passes on a script whose entry point is broken, and the first thing a
# contributor does is run it — `--help` exercises argument handling and the discovery path without
# touching the network or claiming anything.
"$OUT/hazync-worker" --help >/dev/null 2>&1 \
    || "$OUT/hazync-worker" 2>&1 | head -1 | grep -qi hazync \
    || { echo "::error::hazync-worker does not run"; exit 1; }

# ---- browser verifier ---------------------------------------------------------------------------
if command -v cargo >/dev/null && rustup target list --installed 2>/dev/null | grep -q wasm32-unknown-unknown; then
    ./verifier-wasm/build.sh >/dev/null
    cp verifier-wasm/target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm "$OUT/hazync-verify.wasm"
else
    echo "! skipping hazync-verify.wasm — no wasm32 target installed (rustup target add wasm32-unknown-unknown)" >&2
fi

echo "packaged into $OUT:"
for f in hazync-worker hazync-verify.wasm; do
    [ -f "$OUT/$f" ] && printf '  %-22s %9d bytes  %s\n' "$f" "$(stat -c%s "$OUT/$f")" "$(sha256sum "$OUT/$f" | cut -c1-16)"
done
echo
echo "Upload these to the release BEFORE publishing — release-sign.yml signs whatever is attached"
echo "when the release is published, and will not pick up anything added afterwards."
