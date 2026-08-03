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
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    REPO_DIR=/hazync-zkvm \
    WORK=/root/hazync-build \
    HAZYNC_BASE=/root/hazync-build \
    RISC0_HOME=/root/.risc0 \
    PATH=/root/.risc0/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /hazync-zkvm

# ── STAGE 1: dependencies (hazync#87) ─────────────────────────────────────────────────────────────
#
# COPY ONLY WHAT PHASES 1-7 READ. This used to be `COPY . /hazync-zkvm` followed by the whole script,
# so any repo edit — a doc typo — invalidated the layer and re-ran the RISC0 toolchain install and the
# Core clone. A build took 1h45m, of which the guest compile was minutes.
#
# That mattered more than it sounds. A local build produces a DIFFERENT id by design (the ELF embeds
# absolute build paths, which is the variable this container exists to normalise), so this container is
# the ONLY way to learn the canonical id — and at two hours, the cost of checking was high enough to
# discourage checking the one value every published proof is verified against.
#
# These three inputs change rarely, so this layer is now a cache hit on an ordinary code change and
# re-baseline verification costs minutes instead of hours.
COPY provision-vps.sh /hazync-zkvm/provision-vps.sh
COPY patches/ /hazync-zkvm/patches/
COPY coreshim/ /hazync-zkvm/coreshim/

# GITHUB_TOKEN may be passed as an OPTIONAL BuildKit secret so rzup's GitHub calls are authenticated
# (avoids the anonymous rate limit); it never lands in an image layer. Only EXPORT it when the secret is
# present and non-empty: an empty GITHUB_TOKEN makes rzup send an empty credential and get a 401.
# SKIP_GROTH16: this build exists to compute the guest image id, and groth16 has no bearing on it — it
# is needed only by `host snark-wrap`. Measured 2026-08-01: without this, a from-source build spent TWO
# HOURS timing out on that one component after everything else had succeeded.
RUN --mount=type=secret,id=gh_token,required=false \
    if [ -s /run/secrets/gh_token ]; then export GITHUB_TOKEN="$(cat /run/secrets/gh_token)"; fi && \
    SKIP_GROTH16=1 HAZYNC_PROVISION=deps ./provision-vps.sh

# ── STAGE 2: the guest build ──────────────────────────────────────────────────────────────────────
#
# Now the repo. Everything above is cached, so this is the only layer an ordinary change rebuilds.
# Paths are IDENTICAL to the single-stage build that preceded this split — HOME=/root,
# HAZYNC_BASE=/root/hazync-build — because the id depends on them. A split that moved a path would
# produce a different id and silently invalidate the artifact this container exists to produce.
COPY . /hazync-zkvm

RUN SKIP_GROTH16=1 HAZYNC_PROVISION=build ./provision-vps.sh

# Default: emit the guest image id. Run this container on any machine; the id must match.
CMD ["/hazync-zkvm/prover/target/release/host", "method-id"]
