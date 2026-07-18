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

REPO_DIR="${REPO_DIR:-$HOME/hazync-zkvm}"      # where this repo is checked out on the box
WORK="${WORK:-$HOME/hazync-build}"             # scratch for Core clones + the assembled project
CORE_TAG="v28.0"

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
rzup install --force rust 1.94.1
rzup install --force cpp 2024.1.5
rzup install --force cargo-risczero 3.0.5
rzup install --force r0vm 3.0.5
export PATH="$HOME/.risc0/bin:$PATH"
# the riscv g++/gcc + libstdc++/libgcc/newlib come with the rzup cpp toolchain extension.

echo "== 4. real consensus source (re-fetchable; not vendored). Layout: \$HAZYNC_BASE/{bitcoin-core,secp256k1,coreshim} =="
mkdir -p "$WORK"
[ -d "$WORK/bitcoin-core" ] || git clone --depth 1 -b "$CORE_TAG" https://github.com/bitcoin/bitcoin.git "$WORK/bitcoin-core"
# Pin secp256k1 to the version Bitcoin Core v28.0 bundles (0.5.1). The guest compiles this source, so
# a floating master would drift the METHOD_ID — and diverge from the libsecp Core actually ships.
[ -d "$WORK/secp256k1" ]    || git clone --depth 1 -b v0.5.1 https://github.com/bitcoin-core/secp256k1.git "$WORK/secp256k1"

echo "== 5. apply the target shims (pure-Core build: patches 0001 + 0002 only; NOT 0003/k256) =="
git -C "$WORK/bitcoin-core" checkout -- src/serialize.h src/crypto/sha256.cpp 2>/dev/null || true
git -C "$WORK/bitcoin-core" apply "$REPO_DIR/patches/0001-serialize-ilp32-int-overload.patch"
git -C "$WORK/bitcoin-core" apply "$REPO_DIR/patches/0002-sha256-route-through-risc0-accelerator.patch"
mkdir -p "$WORK/coreshim/config"
: > "$WORK/coreshim/config/bitcoin-config.h"    # empty config header (SIMD paths #ifdef'd off on riscv)

echo "== 6. env wiring (guest build.rs reads HAZYNC_BASE; toolchain auto-discovered under RISC0_HOME) =="
export HAZYNC_BASE="$WORK"
export RISC0_HOME="$HOME/.risc0"
grep -q 'HAZYNC_BASE' "$HOME/.bashrc" || cat >> "$HOME/.bashrc" <<EOF
export PATH="\$HOME/.risc0/bin:\$HOME/.cargo/bin:\$PATH"
export RISC0_HOME="\$HOME/.risc0"
export HAZYNC_BASE="$WORK"
EOF

# 7. (optional) CUDA for GPU proving — installed BEFORE the build so we can compile the CUDA backend.
GPU_FEATURES=""
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
