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
# DISK: ~15 GB free is enough for `cpu`. It is NOT enough for `cuda` — measured 2026-08-02, a CUDA
# build consumed ~18 GB and only completed with 25 GB free. Budget 25 GB for cuda.
#
# Running out does NOT report as a disk error. nvcc writes large intermediates, and the first thing
# you see is cc-rs reporting `exit status: 139` — a SIGSEGV — from a kernel compile, which reads as a
# toolchain bug. The real message is further up the log and easy to miss:
#
#   eval_check_2.cu(6160): catastrophic error: error while writing generated C file: No space left on device
#
# That cost a full CUDA build to diagnose. If nvcc segfaults, check `df -h` before anything else.
#
# Needs, for cuda, a working `--gpus all`. The CUDA kernel compile is the slow
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

# ⚠ DO NOT CHECKOUT ANOTHER BRANCH WHILE THIS RUNS.
#
# The repo is bind-MOUNTED (below), not copied, so the container compiles whatever is in the working
# tree AT THE MOMENT each crate is built — not what was checked out when you started. A CPU build is
# tens of minutes and a CUDA build far longer, which is exactly the window in which switching
# branches feels harmless.
#
# The failure is silent and the artifact looks fine: METHOD_ID prints, the smoke tests pass, and you
# get a binary that is a MIXTURE of the branches that were checked out during the run. Observed
# 2026-08-02 — a build started on a feature branch produced a host with none of that branch's changes
# in it, and nothing in the output said so.
#
# If you need to work while this builds, use a separate worktree or clone.
echo "== building $MODE release from $REPO (HOME=/root inside $IMAGE) =="
echo "   NOTE: $REPO is mounted live — do not switch branches in it until this finishes."
echo "   HEAD: $(git -C "$REPO" describe --tags --always 2>/dev/null || echo unknown) ($(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown))"
# The repo is mounted, so prover/target persists on the host between runs: re-running after a host-only
# source change recompiles just the host crate instead of the whole guest + CUDA kernels.
# SKIP_GROTH16 must be forwarded EXPLICITLY: the build runs in a container, so a variable exported in
# the caller's shell reaches provision-vps.sh only if named here. Setting it and assuming it applied
# cost a release build 30 minutes — two bounded-but-real stalls fetching a component that is only
# needed to snark-wrap, which this build does not do.
#
# RZUP_TIMEOUT is forwarded ONLY when the caller set it. It used to be passed with a `:-600` default,
# which silently overrode provision-vps.sh's own default and pinned every release build to 600s — less
# than the 488 MB rust toolchain needs on a domestic link, so raising the default there would have had
# no effect here at all. A wrapper that hardcodes a default defeats the default it wraps.
# MOUNTED AT /hazync-zkvm, NOT /repo, AND THE PATH IS LOAD-BEARING (hazync#88).
#
# The guest id embeds the ABSOLUTE path of any external path dependency. Since #54 the guest depends on
# coinbase-smt, so the ELF carries e.g. "/repo/coinbase-smt/src/lib.rs" and the id changes with the
# mount point. Building here at /repo while reproduce/Dockerfile builds at /hazync-zkvm produced a host
# reporting 7649f929… against a canonical dfc9eeda… — a shipped host that rejects every proof from the
# guest it is supposed to verify.
#
# Confirmed by building the same tree at three paths and getting three ids, and by finding
# "/repo/coinbase-smt/src/lib.rs" in the guest ELF while the guest's OWN sources appear relative and
# Core's are already normalised by -ffile-prefix-map.
#
# Matching the Dockerfile is the fix that unblocks a release. The deeper fix — remapping external path
# dependencies so the id stops depending on the checkout location at all — is hazync#88.
docker run --rm -v "$REPO:/hazync-zkvm" -e HOME=/root -e DEBIAN_FRONTEND=noninteractive \
    -e "SKIP_GROTH16=${SKIP_GROTH16:-0}" ${RZUP_TIMEOUT:+-e "RZUP_TIMEOUT=$RZUP_TIMEOUT"} \
    "${docker_args[@]}" "$IMAGE" bash -lc '
    set -e
    apt-get update -qq && apt-get install -y -qq binutils >/dev/null 2>&1
    cd /hazync-zkvm && REPO_DIR=/hazync-zkvm ./provision-vps.sh
    H=/hazync-zkvm/prover/target/release/host
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
    # Do not guess rzup'"'"'s layout — it has moved between versions and both guesses (PATH, and
    # ~/.risc0/bin) came back empty on 2026-07-31 even though the log said r0vm was installed. Search.
    R=$(command -v r0vm 2>/dev/null || find /root/.risc0 -name r0vm -type f 2>/dev/null | head -1)
    if [ -n "$R" ]; then
        mkdir -p /hazync-zkvm/dist && cp "$R" /hazync-zkvm/dist/r0vm && echo "=== staged r0vm from $R ($(stat -c%s "$R") bytes) ==="
    else
        echo "=== WARNING: no r0vm found in the container; snark-wrap will not work on this host ===" >&2
        find /root/.risc0 -maxdepth 3 -type d 2>/dev/null | head -10 >&2
    fi
'

# Install atomically. `cp` writes THROUGH an existing file, and on this project the destination is
# routinely a binary that live prover processes are executing right now — the GPU box runs the board
# campaign straight out of dist/. Overwriting a running executable's inode corrupts its text pages
# under it (SIGBUS), and the damage lands on whatever range that worker was midway through proving.
# Observed on 2026-07-31: a release rebuild replaced the binary two active workers were running.
# They survived; that was luck. rename(2) onto the same filesystem swaps the directory entry and
# leaves the old inode alone until its last user exits.
install -m 0755 "$REPO/prover/target/release/host" "$OUT/.$asset.tmp.$$"
mv -f "$OUT/.$asset.tmp.$$" "$OUT/$asset"

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
#
# RE-VERIFIED 2026-08-01, and worth stating outright because the error message implies otherwise:
# wrapping needs the DOCKER IMAGE + r0vm and does NOT need the `risc0-groth16` rzup component at all.
# Measured on a box with no such component installed — the genesis-anchored spine wrapped in 55.9 s
# from 224,506 bytes to 1,777 (126x) and verified as `[1..2] VERIFIED — genesis-anchored`. That
# matters because that component HANGS on download (see provision-vps.sh), so anyone who believes the
# error message will wait hours for something wrapping never wanted.
#
# VERIFIED 2026-07-31 on a fresh GPU box: with r0vm staged (above) and this image present, the CPU
# host wraps a real folded receipt in 60 s and the result verifies. The CUDA host still refuses with
# "Missing required risc0-groth16 rzup component" — that is a SEPARATE, pre-existing problem with the
# CUDA groth16 path, not something this fixes. Wrap with the CPU binary; both produce the same artifact
# and wrapping is not the expensive step.
# r0vm is the other half of being able to wrap: `host snark-wrap` shells out to it, and the container
# installs it somewhere only the container can see. Staged out above; installed here.
# /usr/local/bin needs root, and this script is deliberately runnable as an ordinary user (it only
# needs docker). Rather than warn and leave the box unable to wrap, fall back to a user-writable
# directory and SAY what to do about it — the previous message ("could not install r0vm (need
# root?)") named the failure but left the reader to work out the remedy, and it fires on every
# non-root build, so it read as noise and got ignored.
if [ -f "$REPO/dist/r0vm" ]; then
    if [ ! -x /usr/local/bin/r0vm ] || ! cmp -s "$REPO/dist/r0vm" /usr/local/bin/r0vm; then
        if install -m 0755 "$REPO/dist/r0vm" /usr/local/bin/r0vm 2>/dev/null; then
            echo "== installed r0vm -> /usr/local/bin/r0vm =="
        else
            FALLBACK="${R0VM_DEST:-$HOME/.local/bin}"
            if mkdir -p "$FALLBACK" 2>/dev/null && install -m 0755 "$REPO/dist/r0vm" "$FALLBACK/r0vm" 2>/dev/null; then
                echo "== installed r0vm -> $FALLBACK/r0vm (no root; /usr/local/bin needs sudo) =="
                case ":$PATH:" in
                    *":$FALLBACK:"*) ;;
                    *) echo "   NOTE: $FALLBACK is not on your PATH — \`host snark-wrap\` will not find r0vm."
                       echo "         Add it:  export PATH=\"$FALLBACK:\$PATH\"" ;;
                esac
            else
                echo "   NOTE: r0vm not installed (tried /usr/local/bin and $FALLBACK)." >&2
                echo "         Proving and verifying work regardless; only \`host snark-wrap\` needs it." >&2
                echo "         To install:  sudo install -m 0755 $REPO/dist/r0vm /usr/local/bin/r0vm" >&2
            fi
        fi
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
