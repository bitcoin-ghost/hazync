#!/usr/bin/env bash
# Two-arm GPU A/B of the four-lever stack against stock control, block 962,000.
#
#   Arm C (control): `main`, bigint2 OFF, stock libsecp
#   Arm S (stack):   `feat/stack-integration` + patch 0005 + HAZYNC_BIGINT2_ECDSA=1
#
# Answers the one question the fleet size depends on: the PROVING ratio on a real
# tip block. Execute-mode measured 4.48x; bigint2 is a separate coprocessor circuit,
# so that figure does NOT carry over and the card count stays unknown until this runs.
#
# Run the SAME script on BOTH boxes. Each box does both arms, so the ratio is free of
# box-to-box variance, and two boxes give an independent replication of it.
#
#   Usage:  ./scripts/gpu-stack-ab.sh            # both arms, resumable
#           ARM=C ./scripts/gpu-stack-ab.sh      # one arm only
set -uo pipefail

REPO="${REPO:-$HOME/hazync}"
BASE="${HAZYNC_BASE:-$HOME/hazync-build}"
OUT="${OUT:-$HOME/stack-ab}"
BLOCK="${BLOCK:-block_962000.json}"
CHUNKS="${CHUNKS:-16}"
SEG_PO2="${SEG_PO2:-22}"
CLEAN_MD5="308fc36774999286dcc77bf7c7df87b9"   # ecdsa_impl.h with patch 0005 NOT applied
ECDSA_H="$BASE/secp256k1/src/ecdsa_impl.h"
LOG="$OUT/run.log"
CUDA_VER="${HAZYNC_CUDA_VER:-12.8}"

# Mirror what provision-vps.sh phase 8 exports. A non-interactive ssh does not read .bashrc,
# so none of this is set for us. HAZYNC_BASE is how the guest build.rs finds the Core source.
export HAZYNC_BASE="$BASE"
export RISC0_HOME="$HOME/.risc0"
export CUDA_PATH="/usr/local/cuda-${CUDA_VER}"
export PATH="$HOME/.risc0/bin:$HOME/.cargo/bin:/usr/local/cuda-${CUDA_VER}/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda-${CUDA_VER}/lib64:${LD_LIBRARY_PATH:-}"
# VENDORED zkr: risc0-circuit-recursion's build.rs otherwise fetches a 57 MB blob that has
# returned 403 from every network tried, aborting the build ~14 minutes in.
[ -f "$REPO/reproduce/vendor/recursion_zkr.zip" ] && export RECURSION_SRC_PATH="$REPO/reproduce/vendor/recursion_zkr.zip"

# ⛔ This script git-checkouts `main` for arm C, and `main` does not contain this file — so the
# checkout DELETES the script out from under the running shell. The first run survives only because
# bash already holds the inode open; any relaunch dies with "No such file or directory" (rc 127),
# and a box sits idle until somebody notices. Re-exec from a copy outside the repo before doing
# anything, so the script's own existence never depends on which branch is checked out.
SELF="$(readlink -f "$0")"
case "$SELF" in
  "$(readlink -f "$REPO")"/*)
    cp -f "$SELF" "$HOME/.gpu-stack-ab.run.sh"
    chmod +x "$HOME/.gpu-stack-ab.run.sh"
    exec "$HOME/.gpu-stack-ab.run.sh" "$@"
    ;;
esac

mkdir -p "$OUT"
say() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
die() { say "FATAL: $*"; echo "REAL_EXIT=1" >> "$LOG"; exit 1; }

# --- Guard 0: the card. L40S is the reference; H100 = 0.95x, B200 = 8.7% slower. ------------
CARD=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
[ -n "$CARD" ] || die "no nvidia-smi / no GPU visible"
say "CARD: $CARD  (reference is L40S; record this beside every number)"
case "$CARD" in *L40S*) ;; *) say "WARNING: not an L40S. cycles/sec is the portable metric, NOT wall-clock." ;; esac

[ -f "$ECDSA_H" ] || die "no ecdsa_impl.h at $ECDSA_H — is HAZYNC_BASE right? (provision-vps.sh sets it)"
cp -n "$ECDSA_H" "$OUT/ecdsa_impl.h.clean" 2>/dev/null || true

# --- build one arm, with BOTH bigint2 traps checked -----------------------------------------
build_arm() {
  local arm="$1" branch="$2" want_bigint2="$3"
  say "=== ARM $arm: $branch  bigint2=$want_bigint2 ==="

  local freeg; freeg=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
  say "arm $arm: ${freeg}G free before build"
  if [ "$freeg" -lt 18 ]; then
    say "arm $arm: under 18G free — running cargo clean to reclaim before building"
    ( cd "$REPO/prover" && cargo clean ) >>"$LOG" 2>&1
    freeg=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
    say "arm $arm: ${freeg}G free after clean"
  fi
  [ "$freeg" -ge 10 ] || die "arm $arm: only ${freeg}G free — refusing to start a build that will die mid-way"

  git -C "$REPO" fetch --all -q
  git -C "$REPO" checkout -q "$branch" || die "checkout $branch failed"
  git -C "$REPO" reset --hard -q "origin/$branch" || die "reset $branch failed"
  say "arm $arm at $(git -C "$REPO" rev-parse --short HEAD)"

  # TRAP (off): a prior bigint2 build leaves libsecp256k1.a referencing hazync_ecmult_verify.
  # Restoring the C header changes the cc input, so the archive rebuilds without the #ifdef.
  cp -f "$OUT/ecdsa_impl.h.clean" "$ECDSA_H"
  rm -rf "$REPO/prover/target/riscv-guest"

  if [ "$want_bigint2" = "1" ]; then
    # TRAP (on): merging the branch does NOT enable bigint2. The PATCH adds the #ifdef;
    # the ENV VAR defines the macro it tests. Either alone silently builds stock libsecp.
    ( cd "$BASE/secp256k1" && patch -p1 --forward < "$REPO/patches/0005-ecdsa-verify-group-arith-via-bigint2.patch" ) \
      || die "patch 0005 did not apply"
    export HAZYNC_BIGINT2_ECDSA=1
  else
    unset HAZYNC_BIGINT2_ECDSA
  fi

  local md5; md5=$(md5sum "$ECDSA_H" | cut -d' ' -f1)
  say "arm $arm ecdsa_impl.h md5=$md5"
  if [ "$want_bigint2" = "1" ]; then
    [ "$md5" != "$CLEAN_MD5" ] || die "arm $arm wanted bigint2 but header is CLEAN — patch did not land"
  else
    [ "$md5" = "$CLEAN_MD5" ] || die "arm $arm wanted stock but header is PATCHED"
  fi

  # Build via provision-vps.sh phase 8, NOT a raw `cargo build --features cuda`. Phase 7 is the
  # only place that sets GPU_FEATURES, so a hand-rolled build can accept --features cuda and still
  # emit a CPU binary: nothing fails, nothing warns, and the "GPU" prover just runs at CPU speed.
  ( cd "$REPO" && GPU=1 HAZYNC_PROVISION=build ./provision-vps.sh ) >>"$LOG" 2>&1 \
    || die "arm $arm build failed — see $LOG"

  # ...and then prove it actually linked CUDA, rather than trusting the flag.
  ldd "$REPO/prover/target/release/host" 2>/dev/null | grep -qi cuda \
    || die "arm $arm: host is NOT linked against CUDA — this would prove at CPU speed"
  say "arm $arm: CUDA link confirmed"

  # Read METHOD_ID from the binary's own command. NEVER scrape with strings: that returns
  # the Bitcoin genesis hash 000000000019d668…, which is 64 hex chars and entirely plausible.
  local mid; mid=$( cd "$REPO/prover" && ./target/release/host method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1 )
  [ -n "$mid" ] || die "arm $arm: could not read METHOD_ID"
  echo "$mid" > "$OUT/method_id.$arm"
  say "arm $arm METHOD_ID=$mid"
}

# --- prove all chunks for one arm, resumable ------------------------------------------------
prove_arm() {
  local arm="$1"
  local dir="$OUT/receipts.$arm"; mkdir -p "$dir"
  for i in $(seq 0 $((CHUNKS-1))); do
    if [ -s "$dir/chunk_$i.bin" ]; then say "arm $arm chunk $i: already present, skipping"; continue; fi
    say "arm $arm chunk $i: proving"
    ( nvidia-smi --query-gpu=memory.used --format=csv,noheader -l 5 > "$dir/vram_$i.log" 2>/dev/null ) &
    local smi=$!
    local t0=$SECONDS
    ( cd "$REPO/prover" && HAZYNC_BLOCK="$BLOCK" HAZYNC_CHUNKS="$CHUNKS" HAZYNC_SEG_PO2="$SEG_PO2" \
        HAZYNC_OUT="$dir/chunk_$i.bin" ./target/release/host prove-chunk "$i" ) >>"$dir/prove_$i.log" 2>&1
    local rc=$?                       # capture BEFORE anything else touches $?
    kill $smi 2>/dev/null
    local wall=$((SECONDS-t0))
    local peak; peak=$(sort -n "$dir/vram_$i.log" 2>/dev/null | tail -1)
    echo "arm=$arm chunk=$i rc=$rc wall_s=$wall peak_vram=$peak" | tee -a "$OUT/results.tsv" | tee -a "$LOG"
    [ $rc -eq 0 ] || die "arm $arm chunk $i FAILED rc=$rc — see $dir/prove_$i.log"
  done
}

ARMS="${ARM:-C S}"
for a in $ARMS; do
  case "$a" in
    C) build_arm C main 0 ;;
    S) build_arm S feat/stack-integration 1 ;;
    *) die "unknown arm $a" ;;
  esac
  prove_arm "$a"
done

# --- the guard that makes the result mean anything ------------------------------------------
if [ -s "$OUT/method_id.C" ] && [ -s "$OUT/method_id.S" ]; then
  if [ "$(cat "$OUT/method_id.C")" = "$(cat "$OUT/method_id.S")" ]; then
    die "METHOD_IDs are IDENTICAL — one arm did not actually rebuild. Every number here is void."
  fi
  say "METHOD_IDs differ — both arms genuinely rebuilt."
fi

say "=== TOTALS ==="
for a in $ARMS; do
  tot=$(grep -h "arm=$a " "$OUT/results.tsv" 2>/dev/null | sed 's/.*wall_s=\([0-9]*\).*/\1/' | paste -sd+ | bc)
  say "arm $a total prove wall: ${tot:-0} s over $CHUNKS chunks"
done
say "Restoring clean header."
cp -f "$OUT/ecdsa_impl.h.clean" "$ECDSA_H"
echo "REAL_EXIT=0" >> "$LOG"
say "DONE. Ship $OUT/results.tsv + $OUT/method_id.* back."
