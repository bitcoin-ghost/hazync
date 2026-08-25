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
#
# PINNED BY DIGEST, not by tag (hazync#146). `ubuntu:22.04` is a moving pointer: the same Dockerfile
# built a month apart can start from different bytes, and the one property this container exists to
# provide is that it does not. The digest below is the image that produced the canonical guest
# 1d6c3792. Verified: rebuilding with the pin produces the same METHOD_ID as the tag did, so the pin
# freezes what was already true rather than changing it.
#
# To move it deliberately, change the digest and rebuild -- CI's reproducible-image-id job asserts
# the resulting METHOD_ID against reproduce/METHOD_ID, so an unintended change cannot pass silently.
# Declared BEFORE any FROM on purpose: docker only substitutes an ARG into a FROM line if it was
# declared in the global scope. Put it next to stage 2 where it is used and the build fails with
# "base name (${DEPS_IMAGE}) should not be blank".
ARG DEPS_IMAGE=deps-cpu

FROM ubuntu:22.04@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982 AS deps-cpu

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

# LAST in this stage ON PURPOSE. A LABEL is metadata, but Docker layers are ordered: placing it above
# the RUN would invalidate the provision layer and turn every republish into a ~35 minute rebuild.
#
# GitHub links a ghcr package to a repository by this label and nothing else. Without it the package
# publishes fine, resolves fine, and is INVISIBLE on the repo page -- which read as "the push failed"
# when it had not.
LABEL org.opencontainers.image.source=https://github.com/bitcoin-ghost/hazync

# ── STAGE 1b: the same dependencies PLUS CUDA (hazync#167) ────────────────────────────────────────
#
# FROM deps-cpu, not a second copy of stage 1. CUDA is purely ADDITIVE: provision-vps.sh has no GPU
# conditional anywhere in phases 1-6, and its only GPU branch IS phase 7, `install CUDA 12.6`. So
# deps-cuda is deps-cpu plus exactly one phase, and building it as a layer on top says so.
#
# Two images rather than one because the delta is not small -- CUDA is several GB against an 8.35 GB
# cpu image -- and every cpu-build consumer, plus everyone standing up a non-GPU box, would otherwise
# download it for nothing. And two rather than THREE: a "combined" image would be byte-identical to
# this one under a different name.
#
# ⚠ Re-running the deps phase re-runs 1-6. They are idempotent and near-instant on an already
# provisioned image; phase 7 is the part that does work. That is the price of provision-vps.sh
# having no gpu-only phase, and it is paid once per published image, not per build.
FROM deps-cpu AS deps-cuda
RUN --mount=type=secret,id=gh_token,required=false \
    if [ -s /run/secrets/gh_token ]; then export GITHUB_TOKEN="$(cat /run/secrets/gh_token)"; fi && \
    SKIP_GROTH16=1 GPU=1 HAZYNC_PROVISION=deps ./provision-vps.sh
LABEL org.opencontainers.image.source=https://github.com/bitcoin-ghost/hazync

# ── STAGE 2: the guest build ──────────────────────────────────────────────────────────────────────
#
# Now the repo. Everything above is cached, so this is the only layer an ordinary change rebuilds.
# Paths are IDENTICAL to the single-stage build that preceded this split — HOME=/root,
# HAZYNC_BASE=/root/hazync-build — because the id depends on them. A split that moved a path would
# produce a different id and silently invalidate the artifact this container exists to produce.
#
# DEPS_IMAGE lets stage 1 be SKIPPED ENTIRELY (hazync#146). Stage 1 fetches apt packages, the Rust
# toolchain, the RISC0 toolchain, Core v28.0 and secp256k1 v0.5.1 from five different hosts, and when
# any of them stalls the build hangs at ZERO CPU with no output -- the failure this issue was filed
# for. Docker's layer cache already covers a warm rebuild, but a COLD machine (every CI runner, every
# new box) fetches the lot every time, and a cache cannot help a machine that has never built.
#
#   # build the dependency layer once, and pin what you got:
#   docker build --target deps-cpu -t hazync-deps -f reproduce/Dockerfile .
#   docker image inspect hazync-deps --format '{{index .Id}}'
#
#   # thereafter, no network fetch at all:
#   docker build --build-arg DEPS_IMAGE=sha256:<that id> -f reproduce/Dockerfile .
#
# ⚠ THIS MUST NOT BECOME THE ONLY PATH. The default is still `deps`, so a from-scratch build works
# with no arguments and anyone can reproduce the id without trusting an image we handed them. That is
# the whole trust story: `reproducible-image-id` in CI builds from scratch, and a pinned DEPS_IMAGE is
# a convenience that must produce the SAME id or it is wrong.
FROM ${DEPS_IMAGE} AS build

COPY . /hazync-zkvm

# VENDORED zkr ARTEFACT (hazync#164). risc0-circuit-recursion's build.rs downloads a 57 MB zkr from
# risc0-artifacts.s3.us-west-2.amazonaws.com during `cargo build` -- the BUILD stage, not this one --
# and on 2026-08-24 that object began returning 403 from every network we tried, breaking every
# from-scratch build including cutting a release.
#
# Its build.rs takes a local path first and only downloads as a fallback:
#
#     if src_path.exists() && check_sha2(&src_path) { copy; return; }
#     ... download ...
#
# and check_sha2 compares against the SHA256_HASH compiled into the crate. So supplying it locally
# satisfies the SAME integrity check the download would have -- this bypasses a fetch, not a check.
# The S3 key IS that hash, so the artefact is self-verifying: 744b999f...d8849.
#
# The file arrives via `COPY . /hazync-zkvm` above; no separate COPY is needed, and an optional one
# using a glob is unreliable across docker versions. If the file is absent the build still works --
# build.rs falls back to downloading -- so this is a safety net, not a requirement.
ENV RECURSION_SRC_PATH=/hazync-zkvm/reproduce/vendor/recursion_zkr.zip

RUN SKIP_GROTH16=1 HAZYNC_PROVISION=build ./provision-vps.sh

# Default: emit the guest image id. Run this container on any machine; the id must match.
CMD ["/hazync-zkvm/prover/target/release/host", "method-id"]
