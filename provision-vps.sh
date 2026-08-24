#!/usr/bin/env bash
# Hazync prover — VPS provisioning (fresh Ubuntu 22.04/24.04). ~16 GB RAM builds and proves early/small
# blocks; big modern blocks (thousands of inputs) want 64 GB+ and a GPU. ~80 GB disk for the build.
# Turnkey: installs RISC0 + Rust + the Bitcoin Core consensus source + patches, then builds the prover.
# GPU proving (CUDA) is a big speedup — see the GPU section at the bottom (optional).
#
# Usage:  ./provision-vps.sh            # CPU proving
#         GPU=1 ./provision-vps.sh      # also set up CUDA proving (needs an NVIDIA GPU + driver)
set -euo pipefail

# Run privileged steps with sudo on a normal box; skip it when already root (e.g. in the
# reproducible-build container), where sudo may be absent.
SUDO="sudo"; { [ "$(id -u)" = "0" ] || ! command -v sudo >/dev/null; } && SUDO=""

# Where this repo is checked out on the box. This script LIVES IN the repo, so its own directory is
# the answer — asking $HOME was a guess, and it was wrong on every box where the checkout is not at
# $HOME/hazync-zkvm. The failure mode is nasty rather than loud: phase 8 does `cd "$REPO_DIR/prover"`,
# so a wrong value either dies with a confusing "No such file or directory" halfway through, or — far
# worse — silently builds a DIFFERENT checkout than the one you are standing in, and you then read an
# id off a binary that has nothing to do with your working tree.
#
# Falls back to the old guess only when the source path is not a real file (curl | bash), where
# BASH_SOURCE is "main" or a pipe and there is no directory to derive.
if [ -z "${REPO_DIR:-}" ]; then
    _src="${BASH_SOURCE[0]:-$0}"
    if [ -f "$_src" ]; then
        REPO_DIR="$(cd "$(dirname "$_src")" && pwd)"
    else
        REPO_DIR="$HOME/hazync-zkvm"
    fi
fi
WORK="${WORK:-$HOME/hazync-build}"             # scratch for Core clones + the assembled project
# The guest compiles this exact C/C++ source, so it is pinned by IMMUTABLE COMMIT HASH, not just the
# (mutable) tag: if an upstream tag were ever re-pointed, the METHOD_ID would change while the repo still
# claims the canonical id. We clone the tag (fast, shallow) then ASSERT HEAD == the pinned commit, so any
# drift fails the build loudly instead of silently producing a different guest.
# NB: these are the DEREFERENCED commit shas (git rev-parse <tag>^{}) — both are annotated tags, so the
# commit differs from the tag-object sha. `git clone -b <tag>` checks out the commit, so HEAD == these.
CORE_TAG="v28.0";   CORE_COMMIT="110183746150428e6385880c79f8c5733b1361ba"   # bitcoin/bitcoin v28.0
SECP_TAG="v0.5.1";  SECP_COMMIT="642c885b6102725e25623738529895a95addc4f4"   # bitcoin-core/secp256k1 v0.5.1

# PHASE SELECTION (hazync#87). Default is unchanged: run everything, exactly as before.
#
#   HAZYNC_PROVISION=deps    phases 1-7 only (toolchain, Core, shims, env) — NO prover build
#   HAZYNC_PROVISION=build   phase 8 only (build the prover against an already-provisioned box)
#
# Why: reproduce/Dockerfile copied the whole repo and then ran this script, so ANY repo edit
# invalidated the layer and re-ran the toolchain install and Core clone. A container build took 1h45m,
# of which the guest compile was minutes. That made verifying the canonical METHOD_ID — the value every
# published proof is checked against, and obtainable ONLY from this container — a two-hour job, which
# is a good way to ensure nobody verifies it.
#
# Phases 1-7 depend only on provision-vps.sh, patches/ and coreshim/, all of which change rarely.
# Splitting there lets the expensive half cache.
# Initialised BEFORE the phase gate: phase 7 sets it, and HAZYNC_PROVISION=build skips phase 7, so
# under `set -u` the build path aborted with "GPU_FEATURES: unbound variable". Caught by actually
# running the split container build rather than by reading the script.
GPU_FEATURES=""

PHASE="${HAZYNC_PROVISION:-all}"
case "$PHASE" in
  all|deps|build) ;;
  *) echo "HAZYNC_PROVISION must be all, deps or build (got '$PHASE')" >&2; exit 2 ;;
esac

if [ "$PHASE" != "build" ]; then
echo "== 1. system packages =="
$SUDO apt-get update
$SUDO apt-get install -y build-essential cmake git curl ca-certificates pkg-config libssl-dev clang lld python3 protobuf-compiler

echo "== 2. Rust =="
if ! command -v cargo >/dev/null; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
fi
source "$HOME/.cargo/env"

echo "== 3. RISC0 toolchain (rzup -> cargo-risczero, r0vm, riscv32 C/C++ + Rust guest toolchains) =="
if ! command -v rzup >/dev/null; then
  curl -L https://risczero.com/install | bash
  export PATH="$HOME/.risc0/bin:$PATH"
fi
# PINNED toolchain versions. With the risc0 crates (=3.0.5) these determine the guest image id
# (METHOD_ID). Bare `rzup install` grabs whatever is *latest* — which drifts the METHOD_ID over time
# and, unauthenticated, hits GitHub's API rate limit. rzup authenticates via $GITHUB_TOKEN when set.
#
# Each install is wrapped, because rzup has NO download timeout and these are large artifacts (the
# rust toolchain is 488 MB). Three failure modes have each cost a full build:
#   - a timeout set too TIGHT, which kills a download that is progressing perfectly well. The first
#     version of this used 600s, which at a measured 766 KB/s allows only 449 MB — less than the
#     toolchain itself, so the reproduce build could never succeed on a domestic link, and the error
#     blamed the CDN. A timeout must exceed the artifact over the SLOWEST link you care about;
#   - a truncated body ("error decoding response body"), which at least exits;
#   - a silent hang, which does not — one release build sat at 0% CPU for an hour on
#     `risc0-groth16` before anyone looked, having already spent 20 minutes on the toolchain.
# A stall is the expensive one: `set -e` cannot see it, and a build that never returns looks exactly
# like a build that is being slow. Bound it and retry.
rzup_install() {
    local what="$*" attempt
    for attempt in 1 2 3; do
        # shellcheck disable=SC2086
        if timeout "${RZUP_TIMEOUT:-3600}" rzup install --force $what; then
            return 0
        fi
        echo "   rzup install $what failed or timed out after ${RZUP_TIMEOUT:-3600}s (attempt $attempt/3)" >&2
        sleep $(( attempt * 10 ))
    done
    echo "rzup install $what failed three times." >&2
    echo "  These artifacts are LARGE — the rust toolchain alone is 488 MB — so on a slow link the" >&2
    echo "  download can be killed by the timeout while it is still making progress. That is not a" >&2
    echo "  network fault and retrying will not help: raise it, e.g. RZUP_TIMEOUT=7200." >&2
    echo "  At 766 KB/s (a domestic connection) 488 MB needs ~11 minutes; the default allows 60." >&2
    return 1
}
rzup_install rust 1.94.1
rzup_install cpp 2024.1.5
rzup_install cargo-risczero 3.0.5
rzup_install r0vm 3.0.5
# Groth16 (SNARK-wrapping a STARK) needs its own rzup component and is NOT pulled in by the others.
# Without it `host snark-wrap` dies with "Missing required `risc0-groth16` rzup component" — so a
# freshly provisioned box cannot wrap at all, which is not obvious until you try. Pinned like the rest,
# because an unpinned component is how the guest id drifts.
#
# Unlike the others this one is NOT needed to build or to prove — only to wrap — so a box that cannot
# reach the CDN for it should still finish provisioning with a working prover. Set SKIP_GROTH16=1 to
# skip it deliberately; otherwise a failure here warns rather than aborts.
#
# ⚠ risc0-groth16 0.1.0 HANGS ON DOWNLOAD. Observed 2026-08-01 on three machines across two networks:
# rzup prints "Downloading risc0-groth16 version 0.1.0" and then emits nothing at all — no progress,
# no error, no completion — indefinitely. For contrast, the 488 MB rust toolchain downloads and
# reports "✓ Downloaded" in ~13 minutes on the same link in the same run, so this is not bandwidth.
#
# Because it is OPTIONAL, it gets a short timeout of its own rather than the full one: waiting an hour
# (times three attempts) for a component that never arrives cost three hours of a from-source build
# before this was measured. Fail fast, say what is lost, carry on.
if [ "${SKIP_GROTH16:-0}" = 1 ]; then
    echo "== skipping risc0-groth16 (SKIP_GROTH16=1) — this box will prove but not snark-wrap =="
elif ! RZUP_TIMEOUT="${GROTH16_TIMEOUT:-300}" rzup_install risc0-groth16 0.1.0; then
    echo "   NOTE: risc0-groth16 did not install (it is known to hang; see the comment above)." >&2
    echo "   Nothing else is affected: the guest, the prover and every verifier work without it." >&2
    echo "   Only \`host snark-wrap\` needs it. Retry with GROTH16_TIMEOUT=3600 if you want to wait." >&2
fi
export PATH="$HOME/.risc0/bin:$PATH"
# the riscv g++/gcc + libstdc++/libgcc/newlib come with the rzup cpp toolchain extension.

echo "== 4. real consensus source (re-fetchable; not vendored). Layout: \$HAZYNC_BASE/{bitcoin-core,secp256k1,coreshim} =="
mkdir -p "$WORK"
[ -d "$WORK/bitcoin-core" ] || git clone --depth 1 -b "$CORE_TAG" https://github.com/bitcoin/bitcoin.git "$WORK/bitcoin-core"
# Pin secp256k1 to the version Bitcoin Core v28.0 bundles (0.5.1). The guest compiles this source, so
# a floating master would drift the METHOD_ID — and diverge from the libsecp Core actually ships.
[ -d "$WORK/secp256k1" ]    || git clone --depth 1 -b "$SECP_TAG" https://github.com/bitcoin-core/secp256k1.git "$WORK/secp256k1"
# Reproducibility guard: the checked-out source MUST be the pinned commit. A mismatch means an upstream
# tag moved (or a stale clone) — fail rather than silently build a different guest (a different METHOD_ID).
for d_c in "bitcoin-core:$CORE_COMMIT" "secp256k1:$SECP_COMMIT"; do
  d="${d_c%%:*}"; want="${d_c##*:}"; got="$(git -C "$WORK/$d" rev-parse HEAD)"
  if [ "$got" != "$want" ]; then
    echo "FATAL: $d is at $got but the pinned commit is $want — upstream tag moved or stale clone."
    echo "       Re-clone $WORK/$d at the pinned commit; the guest METHOD_ID depends on this exact source." >&2
    exit 1
  fi
done

echo "== 5. apply the target shims (patches 0001 + 0002 — portability only, no consensus-logic change) =="
git -C "$WORK/bitcoin-core" checkout -- src/serialize.h src/crypto/sha256.cpp 2>/dev/null || true
git -C "$WORK/bitcoin-core" apply "$REPO_DIR/patches/0001-serialize-ilp32-int-overload.patch"
git -C "$WORK/bitcoin-core" apply "$REPO_DIR/patches/0002-sha256-route-through-risc0-accelerator.patch"
mkdir -p "$WORK/coreshim/config"
: > "$WORK/coreshim/config/bitcoin-config.h"    # empty config header (SIMD paths #ifdef'd off on riscv)
# Ship the repo's target shims (sync.h / threadsafety.h — single-threaded no-op locking) into the
# coreshim include dir. build.rs puts coreshim FIRST on the include path so these override Core's
# pthread-backed headers, letting the REAL chain.h CBlockIndex + pow.cpp retarget math compile on the
# freestanding riscv32 guest. Non-consensus platform glue only; the METHOD_ID depends on these.
cp "$REPO_DIR"/coreshim/*.h "$WORK/coreshim/"

echo "== 6. env wiring (guest build.rs reads HAZYNC_BASE; toolchain auto-discovered under RISC0_HOME) =="
export HAZYNC_BASE="$WORK"
export RISC0_HOME="$HOME/.risc0"
grep -q 'HAZYNC_BASE' "$HOME/.bashrc" || cat >> "$HOME/.bashrc" <<EOF
export PATH="\$HOME/.risc0/bin:\$HOME/.cargo/bin:\$PATH"
export RISC0_HOME="\$HOME/.risc0"
export HAZYNC_BASE="$WORK"
EOF

# 7. (optional) CUDA for GPU proving — installed BEFORE the build so we can compile the CUDA backend.
# (GPU_FEATURES is initialised above the phase gate — see the note there.)
if [ "${GPU:-0}" = "1" ]; then
  echo "== 7. GPU proving: install CUDA 12.6 (RISC0 3.0.5 kernels DO NOT build against the CUDA 13.x"
  echo "   that some L40S boxes ship — cccl header errors; 12.6 works). =="
  if [ ! -d /usr/local/cuda-12.6 ]; then
    # Pick the CUDA repo matching this Ubuntu release (don't hardcode 24.04).
    . /etc/os-release
    case "${VERSION_ID:-}" in
      24.04) CUDA_REPO=ubuntu2404 ;;
      22.04) CUDA_REPO=ubuntu2204 ;;
      *)     CUDA_REPO=ubuntu2404; echo "  (unrecognised Ubuntu '${VERSION_ID:-?}'; defaulting to ${CUDA_REPO} repo)" ;;
    esac
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/cuda-keyring.deb" \
      "https://developer.download.nvidia.com/compute/cuda/repos/${CUDA_REPO}/x86_64/cuda-keyring_1.1-1_all.deb"
    $SUDO dpkg -i "$tmp/cuda-keyring.deb"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq cuda-toolkit-12-6
    rm -rf "$tmp"
  fi
  $SUDO ln -sfn /usr/local/cuda-12.6 /usr/local/cuda   # make the build pick 12.6, not a shipped 13.x
  export CUDA_PATH=/usr/local/cuda-12.6
  export PATH="/usr/local/cuda-12.6/bin:$PATH"
  export LD_LIBRARY_PATH="/usr/local/cuda-12.6/lib64:${LD_LIBRARY_PATH:-}"
  GPU_FEATURES="--features cuda"
  grep -q 'CUDA_PATH' "$HOME/.bashrc" || cat >> "$HOME/.bashrc" <<'EOF'
export CUDA_PATH=/usr/local/cuda-12.6
export PATH="/usr/local/cuda-12.6/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda-12.6/lib64:${LD_LIBRARY_PATH:-}"
EOF
fi

fi   # end phases 1-7

if [ "$PHASE" = "deps" ]; then
  echo
  echo "provisioned dependencies only (HAZYNC_PROVISION=deps) — prover NOT built."
  echo "run with HAZYNC_PROVISION=build in the same environment to build it."
  exit 0
fi

# `build` skips 1-7, so the env those phases exported is not set in this shell. Re-derive it — these
# are the same values phase 6 writes, and the guest's build.rs reads HAZYNC_BASE to find Core.
export HAZYNC_BASE="$WORK"
export RISC0_HOME="$HOME/.risc0"
export PATH="$HOME/.risc0/bin:$HOME/.cargo/bin:$PATH"

# VENDORED zkr (hazync#164). risc0-circuit-recursion's build.rs downloads a 57 MB blob from
# risc0-artifacts.s3.us-west-2.amazonaws.com during cargo build. On 2026-08-24 that object returned
# 403 from every network tried, aborting the build ~14 minutes in, after every other fetch had
# already succeeded.
#
# Set HERE rather than in reproduce/Dockerfile alone, because there are THREE build paths and only
# one of them is that Dockerfile: prover/build-release.sh bind-mounts the repo into a bare ubuntu
# image, and CI runs this script directly. All three go through this file, so one line covers them.
#
# build.rs prefers a local path and verifies it against the SHA256_HASH compiled into the crate, so
# this satisfies the same check the download would have. Respects an existing value, and if the file
# is missing build.rs simply downloads as before.
if [ -z "${RECURSION_SRC_PATH:-}" ]; then
  _zkr="$REPO_DIR/reproduce/vendor/recursion_zkr.zip"
  [ -f "$_zkr" ] && export RECURSION_SRC_PATH="$_zkr"
fi

# ...including the CUDA env, and GPU_FEATURES itself. Phase 7 is the ONLY place that sets
# GPU_FEATURES=--features cuda, and `build` skips phase 7 — so before this, `GPU=1 HAZYNC_PROVISION=build`
# accepted the flag and silently produced a CPU binary. Nothing failed and nothing warned; you only
# found out by checking `ldd host | grep cuda` on something you believed was a GPU build, or by
# watching a "GPU" prover run at CPU speed.
#
# A flag that is accepted and ignored is worse than one that is rejected, so this mirrors phase 7
# exactly rather than erroring: the same paths, the same feature string.
if [ "${GPU:-0}" = "1" ]; then
  export CUDA_PATH=/usr/local/cuda-12.6
  export PATH="/usr/local/cuda-12.6/bin:$PATH"
  export LD_LIBRARY_PATH="/usr/local/cuda-12.6/lib64:${LD_LIBRARY_PATH:-}"
  GPU_FEATURES="--features cuda"
  command -v nvcc >/dev/null || {
    echo "GPU=1 but nvcc is not on PATH (looked for /usr/local/cuda-12.6/bin/nvcc)." >&2
    echo "Run the deps phase first: GPU=1 HAZYNC_PROVISION=deps $0" >&2
    exit 1
  }
fi

echo "== 8. build the prover (release${GPU_FEATURES:+ + CUDA}) — HAZYNC_BASE is exported above =="
cd "$REPO_DIR/prover"
cargo build --release $GPU_FEATURES

echo
echo "DONE. Verify the build with the self-contained checks (no GPU, no files):"
echo "  cd $REPO_DIR/prover && ./target/release/host regress        # block 170 consensus regression"
echo "  cd $REPO_DIR/prover && ./target/release/host adversarial    # soundness suite (all holes must REJECT)"
echo "Then prove:"
echo "  ./target/release/host prove-block                           # single block 170 -> real STARK receipt"
if [ "${GPU:-0}" = "1" ]; then
  echo "  (CUDA env is set in this shell and persisted to ~/.bashrc for future logins)"
fi
echo "Or join the proof party — see CONTRIBUTING.md."
