#!/usr/bin/env bash
# Measure fold concurrency (K) and per-GPU distribution, to size a whole-board fold (#24 Phase 2).
#
# Folds WITHIN a tree level are mutually independent, so they can run concurrently — but concurrency is
# bounded by VRAM and overcommitting does not degrade gracefully, it OOMs mid-fold. This finds the
# ceiling empirically rather than assuming it.
#
# WHY THIS EXISTS IN THIS FORM: the ad-hoc version of this measurement silently produced NO results —
# `bc` was missing on a fresh box, the arithmetic in the summary line evaluated to nothing, and the
# script still printed its "board fold projection" header with zero data underneath. Silence looked
# like success. So this one checks its dependencies up front and FAILS if it recorded nothing.
#
#   ./prover/bench-fold-concurrency.sh                 # K = 1 2 4 8 on the default GPU
#   KLIST="1 2 3" ./prover/bench-fold-concurrency.sh   # custom ladder
#   PER_GPU=1 ./prover/bench-fold-concurrency.sh       # also test splitting across all GPUs
#
# Env: HAZYNC_HOST (prover binary), RECEIPTS (dir of N.bin range receipts), KLIST, PER_GPU
set -uo pipefail

HOST="${HAZYNC_HOST:-./prover/target/release/host}"
RECEIPTS="${RECEIPTS:-$HOME/.hazync/receipts}"
KLIST="${KLIST:-1 2 4 8}"
WORK="${WORK:-/tmp/foldbench.$$}"
results=0

die() { echo "FAIL $*" >&2; exit 1; }

# Dependencies FIRST. A missing one must stop the run, not quietly hollow out its output.
for c in nvidia-smi bc; do command -v "$c" >/dev/null || die "$c not installed — install it before benchmarking (this is exactly what silently voided an earlier run)"; done
[ -x "$HOST" ] || die "no prover binary at $HOST (set HAZYNC_HOST)"
[ -d "$RECEIPTS" ] || die "no receipts dir at $RECEIPTS (set RECEIPTS)"

max_k=$(tr ' ' '\n' <<<"$KLIST" | sort -n | tail -1)
need=$((max_k * 2))
have=$(ls "$RECEIPTS"/[0-9]*.bin 2>/dev/null | wc -l)
[ "$have" -ge "$need" ] || die "need $need receipts for K=$max_k, found $have in $RECEIPTS"

gpus=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
echo "prover: $HOST"
echo "GPUs:   $gpus"
echo

# One concurrency level. Echoes exactly one result line, always.
run_level() {                       # $1=K  $2=CUDA_VISIBLE_DEVICES ("" = default)  $3=label
    local K=$1 dev=$2 label=$3
    local W="$WORK/k$K"
    rm -rf "$W"; mkdir -p "$W"
    local i; for i in $(seq 1 $((K * 2))); do cp "$RECEIPTS/$i.bin" "$W/r$i.bin" 2>/dev/null || die "missing $RECEIPTS/$i.bin"; done

    local peak=0
    ( while [ ! -f "$W/stop" ]; do
        u=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | paste -sd+ | bc)
        [ -n "$u" ] && [ "$u" -gt "$peak" ] 2>/dev/null && { peak=$u; echo "$peak" > "$W/peak"; }
        sleep 1
      done ) & local sp=$!

    local t0 pids=() rc=0 j a b
    t0=$(date +%s)
    for j in $(seq 0 $((K - 1))); do
        a=$((j * 2 + 1)); b=$((j * 2 + 2))
        # NOTE: exporting CUDA_VISIBLE_DEVICES="" hides EVERY device — the fold aborts with a core
        # dump and a 0 MiB peak, which reads like a mysterious crash. Only set it when pinning.
        if [ -n "$dev" ]; then
            ( cd "$W" && CUDA_VISIBLE_DEVICES="$dev" "$HOST" fold-range "r$a.bin" "r$b.bin" "o$j.bin" >"$W/e$j" 2>&1 ) &
        else
            ( cd "$W" && "$HOST" fold-range "r$a.bin" "r$b.bin" "o$j.bin" >"$W/e$j" 2>&1 ) &
        fi
        pids+=($!)
    done
    for p in "${pids[@]}"; do wait "$p" || rc=1; done
    local t=$(( $(date +%s) - t0 ))
    touch "$W/stop"; wait $sp 2>/dev/null
    local pk; pk=$(cat "$W/peak" 2>/dev/null || echo 0)

    if [ "$rc" -ne 0 ]; then
        echo "  K=$K $label FAILED after ${t}s (peak ${pk} MiB) :: $(grep -ohiE 'out of memory|illegal memory access|panicked at.*' "$W"/e* 2>/dev/null | head -1)"
        rm -rf "$W"; return 1
    fi
    echo "  K=$K $label ok  wall=${t}s  eff=$(echo "scale=2; $t/$K" | bc)s/fold  peak=${pk} MiB"
    results=$((results + 1))
    rm -rf "$W"; return 0
}

echo "== concurrency on the default GPU =="
for K in $KLIST; do run_level "$K" "" "" || { echo "  (ceiling reached — stopping the ladder)"; break; }; done

# Splitting across cards matters: risc0 uses ONE device unless told otherwise, so on a multi-GPU box
# the extra cards sit idle and the VRAM ceiling is per-card, not total. Measured on a 2x L40S box:
# K=4 OOM'd at 36.9 GiB peak with 92 GiB installed, because it was all on device 0.
if [ "${PER_GPU:-0}" = "1" ] && [ "$gpus" -gt 1 ]; then
    echo
    echo "== same K, pinned to each GPU (does the second card help?) =="
    for d in $(nvidia-smi --query-gpu=index --format=csv,noheader); do
        run_level 2 "$d" "[gpu $d]" || true
    done
fi

rm -rf "$WORK"
echo
[ "$results" -gt 0 ] || die "no concurrency level completed — nothing was measured (do NOT read this as a pass)"
echo "recorded $results result(s)."
