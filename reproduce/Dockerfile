# Reproducible-build container for the Hazync guest image id (METHOD_ID).
#
# A proof only verifies against the guest it was made with, so the METHOD_ID must be reproducible by
# anyone — otherwise a from-source `host` can't verify published proofs (see docs/PROVING.md). The id
# is a hash of the exact guest build, and we found it also depends on absolute build paths baked into
# the ELF ($HOME/.cargo, $HAZYNC_BASE). This container removes that last variable by FIXING every
# path: HOME=/root, so $CARGO_HOME=/root/.cargo and the Core/secp sources live at /root/hazync-build
# on every machine. Combined with the pinned toolchain + Core v28.0 + secp256k1 v0.5.1 + committed
# Cargo.lock (all via provision-vps.sh), the resulting METHOD_ID is identical wherever this is built.
#
#   docker build -t hazync-repro -f reproduce/Dockerfile .
#   docker run --rm hazync-repro          # prints the canonical METHOD_ID
#
# Two independent builds (different machines) that print the same id == reproducibility proven.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    REPO_DIR=/hazync-zkvm \
    WORK=/root/hazync-build \
    HAZYNC_BASE=/root/hazync-build \
    RISC0_HOME=/root/.risc0 \
    PATH=/root/.risc0/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /hazync-zkvm
COPY . /hazync-zkvm

# provision-vps.sh (run as root; it detects that and skips sudo) installs the pinned toolchain, clones
# Core v28.0 + secp256k1 v0.5.1, applies the shims, and builds the prover — all at the fixed paths above.
# GITHUB_TOKEN may be passed as an OPTIONAL BuildKit secret so rzup's GitHub calls are authenticated
# (avoids the anonymous rate limit); it never lands in an image layer. Without it, rzup runs
# unauthenticated, which is fine for a one-off build — but only EXPORT the token when the secret is
# actually present and non-empty: an empty GITHUB_TOKEN makes rzup send an empty credential and get a
# 401. If you hit the rate limit locally, run: docker build --secret id=gh_token,env=GITHUB_TOKEN ...
RUN --mount=type=secret,id=gh_token,required=false \
    if [ -s /run/secrets/gh_token ]; then export GITHUB_TOKEN="$(cat /run/secrets/gh_token)"; fi && \
    ./provision-vps.sh

# Default: emit the guest image id. Run this container on any machine; the id must match.
CMD ["/hazync-zkvm/prover/target/release/host", "method-id"]
