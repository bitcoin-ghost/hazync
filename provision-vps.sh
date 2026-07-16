#!/usr/bin/env bash
# Hazync prover — VPS provisioning (fresh Ubuntu 22.04/24.04, ≥64 GB RAM; 256 GB for full blocks).
# Turnkey: installs RISC0 + Rust + the Bitcoin Core consensus source + patches, then builds the prover.
# GPU proving (CUDA) is a big speedup — see the GPU section at the bottom (optional).
#
# Usage:  ./provision-vps.sh            # CPU proving
#         GPU=1 ./provision-vps.sh      # also set up CUDA proving (needs an NVIDIA GPU + driver)
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/hazync-zkvm}"      # where this repo is checked out on the box
WORK="${WORK:-$HOME/hazync-build}"             # scratch for Core clones + the assembled project
CORE_TAG="v28.0"

echo "== 1. system packages =="
sudo apt-get update
sudo apt-get install -y build-essential cmake git curl pkg-config libssl-dev clang lld python3

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
rzup install                                   # installs the pinned toolchain set (v3.0.5-era)
export PATH="$HOME/.risc0/bin:$PATH"
# the riscv g++/gcc + libstdc++/libgcc/newlib come with the rzup cpp toolchain extension.

echo "== 4. real consensus source (re-fetchable; not vendored). Layout: \$HAZYNC_BASE/{bitcoin-core,secp256k1,coreshim} =="
mkdir -p "$WORK"
[ -d "$WORK/bitcoin-core" ] || git clone --depth 1 -b "$CORE_TAG" https://github.com/bitcoin/bitcoin.git "$WORK/bitcoin-core"
[ -d "$WORK/secp256k1" ]    || git clone --depth 1 https://github.com/bitcoin-core/secp256k1.git "$WORK/secp256k1"

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

echo "== 7. build the prover (release) =="
cd "$REPO_DIR/prover"
cargo build --release

if [ "${GPU:-0}" = "1" ]; then
  echo "== 8. GPU proving: install CUDA 12.6 (RISC0 3.0.5 kernels DO NOT build against the CUDA 13.x"
  echo "   that UpCloud L40S boxes ship — cccl header errors; 12.6 works). =="
  sudo apt-get install -y -qq protobuf-compiler   # circom-witnesscalc (Groth16) needs protoc
  if [ ! -d /usr/local/cuda-12.6 ]; then
    ( cd /root && wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
      && sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt-get update -qq \
      && sudo apt-get install -y -qq cuda-toolkit-12-6 )
  fi
  sudo ln -sfn /usr/local/cuda-12.6 /usr/local/cuda   # make the build pick 12.6, not the shipped 13.x
  echo "  Build + prove on GPU:"
  echo "    export CUDA_PATH=/usr/local/cuda-12.6 PATH=/usr/local/cuda-12.6/bin:\$PATH"
  echo "    cargo build --release --features cuda"
  echo "    ./target/release/host prove-block           # single block"
  echo "    NGPU=<n> HAZYNC_CHUNKS=<k> HAZYNC_BLOCK=<block.json> ./cluster.sh   # multi-GPU fan-out"
fi

echo
echo "DONE. Prove with:"
echo "  cd $REPO_DIR/prover && cargo run --release -- prove-block     # single block 170 -> real STARK receipt"
echo "  cd $REPO_DIR/prover && cargo run --release -- prove-chain     # 2-step recursive fold (see PROVING.md)"
echo "  (no arg = the fast execute-mode validation demo)"
