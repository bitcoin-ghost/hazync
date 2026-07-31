#!/usr/bin/env bash
# Build a canonical release `host` binary (CPU or CUDA) in a fixed-path container.
#
#   ./prover/build-release.sh cpu          # -> hazync-host-x86_64-linux-gnu
#   ./prover/build-release.sh cuda         # -> hazync-host-x86_64-linux-gnu-cuda
#
# Env: REPO (default: this checkout) · OUT (default: ./dist) · IMAGE (default: ubuntu:22.04)
#
# Why a container, and why these exact settings — each one is load-bearing:
#
#   HOME=/root       The guest image id absorbs absolute paths from $HOME/.cargo. Build under your own
#                    home directory and you get a DIFFERENT (not wrong, just non-canonical) METHOD_ID
#                    that will not verify published proofs. The repo path does NOT matter — only $HOME.
#                    reproduce/Dockerfile exists to pin exactly this.
#   ubuntu:22.04     glibc 2.34, so the binary runs on 22.04+/Debian 12+. A 24.04 build needs glibc 2.39.
#   CUDA 12          risc0-sys 1.5.0's kernels DO NOT COMPILE under CUDA 13 (`nvcc -arch=native` fails).
#                    The container brings its own CUDA 12 toolchain, so a CUDA-13 host is fine.
#   NVCC multi-arch  Without explicit -gencode flags nvcc targets only the build box's GPU, and the
#                    binary then fails on every other card. We ship sm_80/86/89/90 + compute_90 PTX.
#
# Needs ~15 GB free disk and, for cuda, a working `--gpus all`. The CUDA kernel compile is the slow
# part (tens of minutes); the CPU build is much quicker.
set -euo pipefail

MODE="${1:-}"
case "$MODE" in
    cpu|cuda) ;;
    *) echo "usage: $0 {cpu|cuda}" >&2; exit 2 ;;
esac

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT="${OUT:-$REPO/dist}"
IMAGE="${IMAGE:-ubuntu:22.04}"
mkdir -p "$OUT"

[ -x "$REPO/provision-vps.sh" ] || { echo "no provision-vps.sh under REPO=$REPO" >&2; exit 1; }

NVCC_FLAGS='-gencode arch=compute_80,code=sm_80 -gencode arch=compute_86,code=sm_86 -gencode arch=compute_89,code=sm_89 -gencode arch=compute_90,code=sm_90 -gencode arch=compute_90,code=compute_90'

if [ "$MODE" = cuda ]; then
    docker_args=(--gpus all -e "NVCC_APPEND_FLAGS=$NVCC_FLAGS" -e GPU=1)
    asset=hazync-host-x86_64-linux-gnu-cuda
    want_po2=21                       # cuda default; see seg_po2() in host/src/main.rs
else
    docker_args=()
    asset=hazync-host-x86_64-linux-gnu
    want_po2=20
fi

echo "== building $MODE release from $REPO (HOME=/root inside $IMAGE) =="
# The repo is mounted, so prover/target persists on the host between runs: re-running after a host-only
# source change recompiles just the host crate instead of the whole guest + CUDA kernels.
docker run --rm -v "$REPO:/repo" -e HOME=/root -e DEBIAN_FRONTEND=noninteractive \
    "${docker_args[@]}" "$IMAGE" bash -lc '
    set -e
    apt-get update -qq && apt-get install -y -qq binutils >/dev/null 2>&1
    cd /repo && REPO_DIR=/repo ./provision-vps.sh
    H=/repo/prover/target/release/host
    echo "=== METHOD_ID ==="; $H method-id
    echo "=== seg-po2 ==="; $H seg-po2
    echo "=== GLIBC ==="; objdump -T $H | grep -oE "GLIBC_[0-9]+[.][0-9]+" | sort -V | tail -1
    if [ "${GPU:-0}" = 1 ]; then
      CB=$(ls /usr/local/cuda*/bin/cuobjdump 2>/dev/null | head -1)
      [ -n "$CB" ] && echo "=== SASS ===" && $CB --list-elf $H 2>/dev/null | grep -oE "sm_[0-9]+" | sort -u | tr "\n" " " && echo
    fi
    echo "=== smoke ==="; $H bundle-roundtrip-test; $H regress
    # Stage r0vm into the MOUNTED repo so it survives this --rm container. `host snark-wrap` shells
    # out to it, and provision installs it in here where nothing on the host can reach it.
    R=$(command -v r0vm || ls /root/.risc0/bin/r0vm 2>/dev/null || true)
    if [ -n "$R" ]; then mkdir -p /repo/dist && cp "$R" /repo/dist/r0vm && echo "=== staged r0vm ($(stat -c%s "$R") bytes) ==="; fi
'

cp "$REPO/prover/target/release/host" "$OUT/$asset"

# Leave the HOST able to SNARK-wrap, not just to prove.
#
# Everything above happens inside a --rm container, so a box that has only ever run this script ends
# up with a working prover and no way to wrap a receipt. `host snark-wrap` shells out to the risc0
# Groth16 prover IMAGE; without it the wrap dies with "Missing required risc0-groth16 rzup component",
# which sends you looking for an rzup component when what is actually missing is a docker image — and
# installing rzup needs rustc, which a container-only box also does not have. That dead end cost real
# time on 2026-07-31.
#
# Pulled here rather than documented, because the failure only appears at the moment you try to wrap,
# which is long after the build looked successful. ~5.2 GB; skipped if already present, and a failure
# is a warning rather than fatal — the binary is still good, you just cannot wrap on this box yet.
# r0vm is the other half of being able to wrap: `host snark-wrap` shells out to it, and the container
# installs it somewhere only the container can see. Staged out above; installed here.
if [ -f "$REPO/dist/r0vm" ]; then
    if [ ! -x /usr/local/bin/r0vm ] || ! cmp -s "$REPO/dist/r0vm" /usr/local/bin/r0vm; then
        install -m 0755 "$REPO/dist/r0vm" /usr/local/bin/r0vm 2>/dev/null \
            && echo "== installed r0vm -> /usr/local/bin/r0vm ==" \
            || echo "   WARNING: could not install r0vm (need root?); snark-wrap will not run here." >&2
    fi
    rm -f "$REPO/dist/r0vm"
fi

GROTH16_IMAGE="${GROTH16_IMAGE:-risczero/risc0-groth16-prover:v2025-04-03.1}"
if [ "${SKIP_GROTH16_PULL:-0}" != 1 ]; then
    if docker image inspect "$GROTH16_IMAGE" >/dev/null 2>&1; then
        echo "== groth16 prover image already present ($GROTH16_IMAGE) =="
    else
        echo "== pulling the groth16 prover image so this box can snark-wrap (~5.2 GB) =="
        docker pull "$GROTH16_IMAGE" >/dev/null 2>&1 \
            && echo "   ok — `host snark-wrap` will work on this box" \
            || echo "   WARNING: pull failed. The prover binary is fine, but `host snark-wrap` will not run here." >&2
    fi
fi

echo
echo "wrote $OUT/$asset"
echo "  METHOD_ID : $("$OUT/$asset" method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1)"
echo "  canonical : $(grep -vE '^\s*(#|$)' "$REPO/reproduce/METHOD_ID" | tr -d '[:space:]')"
echo "  seg-po2   : $("$OUT/$asset" seg-po2 2>/dev/null | tail -1)  (expected $want_po2)"
echo
echo "The two ids above MUST match, or this binary cannot verify published proofs."
