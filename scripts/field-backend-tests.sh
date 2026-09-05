#!/usr/bin/env bash
# Validate the bigint2 field backend WITHOUT a GPU or a guest build.
#
# Runs, in order:
#   0. a mod-p harness cross-checked against Python arbitrary precision, over values that INCLUDE
#      the lazy representations (p, p+1, 2^256-1) libsecp's own tests cannot construct;
#   1. libsecp256k1's own test suite against field_bigint2, with a stock field_10x26 control
#      built from the same command line;
#   2. mutation controls on BOTH -- each deliberately broken backend must be caught by at least one,
#      or that gate is not exercising the backend and proves nothing.
#
# The four coprocessor primitives are supplied by testsupport/field_bigint2_native.c, a schoolbook
# host reference. It stands in for risc0-crypto's Fq, so what these gates test is the BACKEND GLUE:
# the representation, the lazy invariant, and libsecp's contracts. They do NOT test the coprocessor,
# and they are NOT the digest gate -- see docs/FIELD_BIGINT2_BACKEND.md §5.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
GUEST="$HERE/prover/methods/guest"
BASE=${HAZYNC_BASE:-$HOME/hazync-build}
SECP="$BASE/secp256k1"
WORK=${WORK:-$(mktemp -d)}
mkdir -p "$WORK" || { echo "FATAL: cannot create WORK=$WORK" >&2; exit 1; }
COUNT=${COUNT:-8}
CC=${CC:-gcc}

say () { echo "[$(date +%H:%M:%S)] $*"; }
die () { echo "FATAL: $*" >&2; exit 1; }

[ -d "$SECP/src" ] || die "no secp256k1 at $SECP -- set HAZYNC_BASE, or run scripts/provision-vps.sh"
for f in field_bigint2.h field_bigint2_impl.h testsupport/field_bigint2_native.c; do
    [ -f "$GUEST/$f" ] || die "missing $GUEST/$f"
done

DEFS=(-DECMULT_WINDOW_SIZE=15 -DECMULT_GEN_KB=22 -DUSE_FIELD_INV_BUILTIN=1 -DUSE_SCALAR_INV_BUILTIN=1 -DVERIFY)

# A pristine patched tree. Everything else is built from a copy of this.
say "staging $SECP -> $WORK/secp"
rm -rf "$WORK/secp"; cp -r "$SECP" "$WORK/secp"
cp "$GUEST/field_bigint2.h" "$GUEST/field_bigint2_impl.h" "$WORK/secp/src/"
# ⛔ Idempotent on purpose. $HAZYNC_BASE/secp256k1 is a SHARED tree and a local guest build may have
# left 0012 applied in it. Plain `patch` then detects a reversed hunk, helpfully un-applies it, and
# the switch vanishes -- which the grep below catches, but only after the damage.
if grep -q HAZYNC_FIELD_BIGINT2 "$WORK/secp/src/field.h"; then
    say "patch 0012 already present in the staged tree (source was pre-patched) -- not re-applying"
else
    patch -s -p1 -d "$WORK/secp" --batch < "$HERE/patches/0012-select-field-bigint2-backend.patch" \
        || die "patch 0012 did not apply"
fi
grep -q HAZYNC_FIELD_BIGINT2 "$WORK/secp/src/field.h" || die "patch 0012 applied but the switch is absent"
grep -q HAZYNC_FIELD_BIGINT2 "$WORK/secp/src/field_impl.h" || die "field_impl.h has no HAZYNC_FIELD_BIGINT2 branch"

build () {  # $1=tree  $2=output  $3...=extra flags/sources
    local tree="$1" out="$2"; shift 2
    "$CC" -O2 -std=c89 -w "${DEFS[@]}" "$@" \
        -I"$tree" -I"$tree/include" -I"$tree/src" -o "$out" \
        "$tree/src/tests.c" "$tree/src/precomputed_ecmult.c" "$tree/src/precomputed_ecmult_gen.c"
}
run () { timeout "${TIMEOUT:-7200}" "$1" "$COUNT" 2>&1 | tail -1; }

HARNESS="$WORK/fe_harness"
say "=== gate 0: mod-p harness vs arbitrary precision ==="
"$CC" -O2 -std=c99 -w -I"$GUEST/testsupport/stub" -I"$GUEST" \
    -o "$HARNESS" "$GUEST/testsupport/fe_harness.c" || die "fe_harness did not build"
python3 "$GUEST/testsupport/check_fe.py" "$HARNESS" || die "the mod-p harness FAILED"

say "=== gate 1: libsecp's own test suite (count=$COUNT) ==="
build "$WORK/secp" "$WORK/t_stock"   -DSECP256K1_WIDEMUL_INT64 || die "stock control failed to build"
build "$WORK/secp" "$WORK/t_bigint2" -DHAZYNC_FIELD_BIGINT2 "$GUEST/testsupport/field_bigint2_native.c" \
    || die "bigint2 backend failed to build"

r_stock=$(run "$WORK/t_stock");   say "  stock field_10x26 : $r_stock"
r_new=$(run "$WORK/t_bigint2");   say "  field_bigint2     : $r_new"
[[ "$r_stock" == *"no problems found"* ]] || die "the STOCK control failed -- the harness itself is broken"
[[ "$r_new"   == *"no problems found"* ]] || die "field_bigint2 FAILED libsecp's test suite"

say "=== gate 2: mutation controls (each must be caught) ==="
# name|sed expression applied to field_bigint2_impl.h
MUTANTS=(
 'mul_wrong_operand|s|hazync_fq_mul_limbs(a->n, b->n, r->n);|hazync_fq_mul_limbs(a->n, a->n, r->n);|'
 'add_drops_the_fold|s|    if (c) hz_fold(r, (uint32_t)c);|    (void)c;|'
 'normalize_is_a_noop|s|hz_canon(r->n); }|(void)r; }|'
 'iszero_misses_p|s|if (a\[i\] != HZ_P\[i\]) return 0;|;|'
 'mulint_no_overflow_fold|s|    if (c) hz_fold(r->n, (uint32_t)c);|    (void)c;|'
 'half_wrong_parity|s|(r->n\[0\] & 1)|(~r->n[0] \& 1)|'
 'limit_accepts_ge_p|s|if (hz_ge_p(t)) return 0;|;|'
 'neg_skips_canon|s|^    hz_canon(t);$||'
 'to_signed30_skips_canon|s|    hz_canon(t);                     /\* the lazy|    if(0) hz_canon(t); /* the lazy|'
)
# ⛔ Two of these pass libsecp's full suite at count 32 and are caught ONLY by gate 0: an element
# >= p is not a state any stock backend has, so no libsecp test generator produces one. A mutant is
# "caught" when EITHER gate catches it; a mutant caught by NEITHER means real coverage is missing.
escaped=0
for m in "${MUTANTS[@]}"; do
    name="${m%%|*}"; expr="${m#*|}"
    tree="$WORK/mut/$name/secp"
    rm -rf "$WORK/mut/$name"; mkdir -p "$WORK/mut/$name"
    cp -r "$WORK/secp" "$tree"
    rm -f "$tree/src/field_bigint2_impl.h"          # never edit in place
    sed "$expr" "$GUEST/field_bigint2_impl.h" > "$tree/src/field_bigint2_impl.h"
    # ⛔ the control is void unless the mutant actually differs AND is what the compiler reads
    if cmp -s "$tree/src/field_bigint2_impl.h" "$GUEST/field_bigint2_impl.h"; then
        die "mutant '$name' is identical to the source -- its sed expression no longer matches"
    fi
    if ! build "$tree" "$WORK/mut/$name/t" -DHAZYNC_FIELD_BIGINT2 \
            "$GUEST/testsupport/field_bigint2_native.c" 2>/dev/null; then
        say "  $name: caught (did not build)"; continue
    fi
    out=$(run "$WORK/mut/$name/t")
    if [[ "$out" != *"no problems found"* ]]; then
        say "  $name: caught by gate 1 (libsecp suite)"
        continue
    fi
    # gate 1 let it through -- gate 0 must catch it, or nothing does
    mh="$WORK/mut/$name/fe_harness"
    if "$CC" -O2 -std=c99 -w -I"$GUEST/testsupport/stub" -I"$tree/src" \
            -o "$mh" "$GUEST/testsupport/fe_harness.c" 2>/dev/null \
       && ! python3 "$GUEST/testsupport/check_fe.py" "$mh" >/dev/null 2>&1; then
        say "  $name: caught by gate 0 (mod-p harness) -- gate 1 alone missed it"
    else
        say "  $name: *** NOT CAUGHT BY EITHER GATE ***"; escaped=1
    fi
done
[ "$escaped" -eq 0 ] || die "$escaped mutation(s) escaped; gate 1 cannot be trusted"

say "ALL GATES PASSED (count=$COUNT)"
say "⛔ still outstanding: the journal-digest gate on block 962,000 (arm C2). This proves the glue, not the block."
