#!/usr/bin/env bash
# THE GPU benchmark. One command per build, on a fresh GPU box.
#
# Everything in docs/BUILDS.md was measured in EXECUTE mode on CPU: cycles, digests, cycle
# stragglers. Card counts were DERIVED from those by a fixed ratio. This is what turns them into
# wall-clock, and it is the only thing hardware is needed for.
#
#   ./scripts/gpu-benchmark.sh core     # field backend + hint + Core's refit
#   ./scripts/gpu-benchmark.sh ghost    # #139 + hint + field backend + Schnorr + scalar-inv + SHA
#
# ⛔ RUN CORE AND GHOST ON SEPARATE BOXES, or sequentially on one. Never concurrently: they share
# $HAZYNC_BASE, and patch state leaking between them is how an arm silently measures the wrong build.
set -uo pipefail
MODE="${1:-}"; [ -n "$MODE" ] || { echo "usage: $0 core|ghost"; exit 2; }
H=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
P="$H/prover"; BIN="$P/target/release/host"
BASE="${HAZYNC_BASE:-$HOME/hazync-build}"; SECP="$BASE/secp256k1"; CORE="$BASE/bitcoin-core"
BLOCK="${HAZYNC_BLOCK:?set HAZYNC_BLOCK to block_962000.json}"
OUT="${OUT:-$HOME/gpu-bench-$MODE-$(date +%Y%m%d-%H%M)}"; mkdir -p "$OUT"
DIGEST=4fb3e3c5e80417c87584a617d23b53d8c49940348c0e8d455f66299b4bd4656d
say () { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }
die () { echo "FATAL: $*" | tee -a "$OUT/run.log" >&2; exit 1; }

say "CARD: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo UNKNOWN)"
say "output: $OUT"

# ---- restore the BASE, which is NOT upstream: provision applies 0001 and 0002 to bitcoin-core ----
git -C "$SECP" checkout -- src/ 2>/dev/null; git -C "$CORE" checkout -- src/ 2>/dev/null
rm -f "$SECP"/src/field_bigint2*.h "$CORE"/src/crypto/sha256.cpp.rej
git -C "$CORE" apply "$H/patches/0001-serialize-ilp32-int-overload.patch" || die "base 0001"
git -C "$CORE" apply "$H/patches/0002-sha256-route-through-risc0-accelerator.patch" || die "base 0002"
grep -q sys_sha_compress "$CORE/src/crypto/sha256.cpp" || die "base not restored: 0002 absent"

case "$MODE" in
  core)  PATCHES="0012-select-field-bigint2-backend 0013-lift-x-via-witness-hint"
         export HAZYNC_FIELD_BIGINT2=1 HAZYNC_LIFTX_HINT=1
         COSTS="HAZYNC_COST_EC_OP=450020 HAZYNC_COST_SCHNORR_OP=450020 HAZYNC_COST_INPUT_BYTE=1 HAZYNC_COST_INPUT_BASE=37946"
         WANT="hazync_fq_mul_limbs hazync_lift_x_hint"; DENY="hazync_ecmult_verify hazync_lift_x" ;;
  ghost) PATCHES="0005-ecdsa-verify-group-arith-via-bigint2 0013-lift-x-via-witness-hint 0012-select-field-bigint2-backend 0006-schnorr-verify-group-arith-via-bigint2 0008-scalar-inverse-via-bigint2 0009-sha-transform-fastpath 0010-transformd64-via-accelerator"
         export HAZYNC_BIGINT2_ECDSA=1 HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_BIGINT2_SCHNORR=1 \
                HAZYNC_SCALAR_INV_ACCEL=1 HAZYNC_SHA_FASTPATH=1 HAZYNC_AGG_READSLICE=1 HAZYNC_SHA_D64_ACCEL=1
         # ⛔ Ghost ships DEFAULT packing constants. Core's refit is a REGRESSION here: 1.469 -> 1.884.
         COSTS=""
         WANT="hazync_ecmult_verify hazync_lift_x_hint hazync_scalar_inverse hazync_fq_mul_limbs"; DENY="hazync_lift_x" ;;
  *) die "unknown mode '$MODE'" ;;
esac
export HAZYNC_ECMULT_WINDOW=21 HAZYNC_BASE="$BASE" HAZYNC_BLOCK="$BLOCK"

for p in $PATCHES; do
  case "$p" in 0009*|0010*) T="$CORE";; *) T="$SECP";; esac
  ( cd "$T" && patch -s -p1 --forward --batch < "$H/patches/$p.patch" ) || die "$p did not apply"
done
say "patches: $PATCHES"

say "building (GPU) — phase 8 sets GPU_FEATURES; a hand-rolled cargo build can accept --features cuda and still emit a CPU binary"
( cd "$H" && GPU=1 HAZYNC_PROVISION=build ./provision-vps.sh ) >"$OUT/build.log" 2>&1 || die "build failed, see $OUT/build.log"
ldd "$BIN" 2>/dev/null | grep -qi cuda || die "host is NOT linked against CUDA — this would prove at CPU speed"
say "CUDA link confirmed"

# ⛔ assert the build, two-sided, EXACT symbol match. Eight silent no-ops were caught this way in one
# session; every one reported a moved METHOD_ID and a clean digest while measuring nothing.
syms=$(grep -oa "hazync_[a-z0-9_]*" "$BIN" | sort -u)
grep -qa hazync_NOT_A_REAL_SYMBOL "$BIN" && die "negative control matched — the symbol check is meaningless"
for w in $WANT; do echo "$syms" | grep -qx "$w" || die "$w ABSENT but required for $MODE"; done
for d in $DENY; do echo "$syms" | grep -qx "$d" && die "$d PRESENT but must not be in $MODE"; done
say "assert OK: $MODE build verified in the binary"

say "=== 1. whole block, execute — confirms the CPU cycle figure and the digest on this box ==="
( export HAZYNC_CHUNKS=1 HAZYNC_PROFILE_EXEC=1; "$BIN" chunk-profile ) >"$OUT/exec1.log" 2>&1
D=$(grep -o 'journal sha256 [0-9a-f]*' "$OUT/exec1.log" | tail -1 | awk '{print $3}')
C=$(grep -oE 'cycles +[0-9]+' "$OUT/exec1.log" | tail -1 | awk '{print $2}')
[ "$D" = "$DIGEST" ] || die "DIGEST MISMATCH ($D) — this build does not reproduce the consensus output"
say "digest PASS; cycles $C"

say "=== 2. sixteen chunks, PROVING — the wall-clock the card count actually needs ==="
say "    ⏰ this is the number docs/BUILDS.md 4 says is derived rather than measured"
# ⛔ `prove-chunk` is SINGULAR and takes an index; there is no `prove-chunks`. An unrecognised
# argument does not error -- it falls through to a demo path and exits 0 in twenty seconds, which
# looks exactly like a successful run. Loop the indices explicitly, as scripts/gpu-stack-ab.sh does.
mkdir -p "$OUT/receipts"
: > "$OUT/results.tsv"
for i in $(seq 0 15); do
  ( nvidia-smi --query-gpu=memory.used --format=csv,noheader -l 5 >"$OUT/vram_$i.log" 2>/dev/null ) & SMI=$!
  T0=$SECONDS
  # ⛔ RETRY #119. The CUDA prover intermittently emits a succinct receipt that fails its own
  # verify() -- "verification indicates proof is invalid". It is transient and recovers on retry
  # (5 in 293 proves, every one recovering). Without a retry one occurrence loses a chunk, and then
  # agg-chunks cannot run at all. Retry ONLY that fault: an OOM or a malformed segment fails
  # identically every time and retrying wastes the card for three times as long.
  RC=1
  for ATT in 1 2 3 4; do
    ( cd "$P" && export HAZYNC_CHUNKS=16 HAZYNC_OUT="$OUT/receipts/chunk_$i.bin" $COSTS
      "$BIN" prove-chunk "$i" ) >"$OUT/prove_$i.log" 2>&1
    RC=$?
    [ $RC -eq 0 ] && { [ $ATT -gt 1 ] && { echo "  [#119] chunk $i: succeeded on attempt $ATT" | tee -a "$OUT/run.log"; echo "$i $ATT" >>"$OUT/119_retries.tsv"; }; break; }
    grep -q "verification indicates proof is invalid" "$OUT/prove_$i.log" || break
    echo "  [#119] chunk $i: invalid receipt on attempt $ATT, retrying" | tee -a "$OUT/run.log"
  done
  kill $SMI 2>/dev/null
  WALL=$((SECONDS-T0)); PEAK=$(sort -n "$OUT/vram_$i.log" 2>/dev/null | tail -1)
  echo "chunk=$i rc=$RC wall_s=$WALL peak_vram=$PEAK" | tee -a "$OUT/results.tsv" | tee -a "$OUT/run.log"
  [ $RC -eq 0 ] || say "  ⚠ chunk $i FAILED rc=$RC — see $OUT/prove_$i.log"
done
# the straggler, from WALL-CLOCK rather than cycles -- the whole point of running on a GPU
awk -F'wall_s=' '/wall_s=/{split($2,a," ");w[n++]=a[1];s+=a[1];if(a[1]>m)m=a[1]}
  END{if(n)printf "  MEASURED straggler: max %d vs mean %.0f = %.3fx over %d chunks\n",m,s/n,m/(s/n),n}'   "$OUT/results.tsv" | tee -a "$OUT/run.log"

# ⛔ agg-chunks needs the chunk receipts to exist. Without proving they do not, and it dies with
# "chunk receipt chunk_0.bin: No such file or directory" -- which is what happened first time.
ls "$OUT/receipts"/chunk_*.bin >/dev/null 2>&1 || die "no chunk receipts — proving did not produce them"
say "=== 3. aggregate — the term worth 25% of Ghost's budget and 12% of Core's ==="
say "    ⏰ issue #207: does it still saturate at N=2? That ceiling has never been re-measured"
( cd "$P" && export HAZYNC_CHUNKS=16 HAZYNC_RECEIPTS="$OUT/receipts"; "$BIN" agg-chunks ) >"$OUT/agg.log" 2>&1; say "agg REAL_EXIT=$?"
grep -iE "aggregate|wall|seconds" "$OUT/agg.log" | tail -10 | tee -a "$OUT/run.log"

say "DONE — $OUT"
say "Report: per-chunk wall_s (max/mean = the REAL straggler), aggregate wall-clock, GPU utilisation."
