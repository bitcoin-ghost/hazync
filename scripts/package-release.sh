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

# ---- coordinator + fleet launcher -----------------------------------------------------------------
# Everything a person DOWNLOADS AND EXECUTES should be covered by SHA256SUMS.txt.asc, and two things
# were not:
#
#   * `run-workers.sh` — CONTRIBUTING fetched it from raw.githubusercontent, unsigned, on the line
#     immediately after boasting that hazync-worker is "a SIGNED release artifact". HTTPS gives
#     transport security and GitHub's word; it does not give the release signature the same paragraph
#     is selling. This is the script that launches the fleet.
#   * `server.py` — hazync#69 is about third parties running their OWN coordinator, and there was no
#     documented way to get it at all except cloning the repo.
#
# Both are plain scripts, so packaging is a copy — but a copy that lands in the manifest.
python3 -c "import ast,sys; ast.parse(open('coordinator/server.py').read())" \
    || { echo "::error::coordinator/server.py does not parse — refusing to ship it"; exit 1; }
cp coordinator/server.py "$OUT/hazync-coordinator.py"

bash -n coordinator/run-workers.sh \
    || { echo "::error::coordinator/run-workers.sh does not parse — refusing to ship it"; exit 1; }
cp coordinator/run-workers.sh "$OUT/hazync-run-workers.sh"
chmod +x "$OUT/hazync-run-workers.sh"

# Named `hazync-*` deliberately: release-sign.yml downloads the release assets by that glob, so an
# asset outside it is published and then silently left out of the signed manifest — the exact failure
# step 7 of release.sh exists to catch.

# ---- browser verifier ---------------------------------------------------------------------------
if command -v cargo >/dev/null && rustup target list --installed 2>/dev/null | grep -q wasm32-unknown-unknown; then
    ./verifier-wasm/build.sh >/dev/null
    cp verifier-wasm/target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm "$OUT/hazync-verify.wasm"
else
    echo "! skipping hazync-verify.wasm — no wasm32 target installed (rustup target add wasm32-unknown-unknown)" >&2
fi

echo "packaged into $OUT:"
# List everything this script stages, not a subset. A summary that under-reports is how an asset gets
# dropped without anyone noticing — the operator reads the summary, not the source.
for f in hazync-worker hazync-coordinator.py hazync-run-workers.sh hazync-verify.wasm; do
    [ -f "$OUT/$f" ] && printf '  %-22s %9d bytes  %s\n' "$f" "$(stat -c%s "$OUT/$f")" "$(sha256sum "$OUT/$f" | cut -c1-16)"
done
echo
echo "Upload these to the release BEFORE publishing — release-sign.yml signs whatever is attached"
echo "when the release is published, and will not pick up anything added afterwards."
