#!/usr/bin/env bash
# Groth16 SNARK verification gate (#23).
#
# Groth16 shipped BROKEN from v0.8.0 and nobody noticed, because nothing anywhere exercised it — not CI,
# not the release smoke tests. This closes the verification half of that gap on every push, cheaply:
# `verify-snark` runs in seconds against checked-in fixtures, so it needs no GPU and no proving.
#
# It deliberately checks BOTH directions. A verifier that accepts everything passes a positive-only test.
#   1. a genesis-anchored wrapped range MUST verify
#   2. a VALID but NON-genesis wrapped range MUST be rejected, and specifically ON THE GENESIS PIN
#
# (2) is the assertion the whole artifact is built around: a smaller proof that checks LESS would be
# worse than the thing it replaces — a fabricated-anchor range in a more shareable package.
#
# The proving half (does snark-wrap still produce a valid proof?) is NOT covered here — a CPU Groth16
# prove is far too slow for per-push CI. See ci_snark_prove.sh, which is opt-in.
set -uo pipefail

HOST="${HAZYNC_HOST:-./target/release/host}"
DIR="$(cd "$(dirname "$0")" && pwd)"
POS="$DIR/testdata/snark/fold_1000.snark"      # [1..1000], genesis-anchored
NEG="$DIR/testdata/snark/neg500.snark"         # [500..500], valid but NOT genesis-anchored

fail() { echo "::error::$*"; exit 1; }

[ -x "$HOST" ] || fail "no host binary at $HOST"
for f in "$POS" "$NEG"; do
    # Guard against the fixture silently vanishing: without this, a missing file makes verify-snark fail
    # and the negative test 'pass' for entirely the wrong reason.
    [ -s "$f" ] || fail "missing SNARK fixture: $f"
done

echo "=== 1. genesis-anchored wrapped range must VERIFY ==="
if ! out=$("$HOST" verify-snark "$POS" 2>&1); then
    echo "$out"
    fail "verify-snark REJECTED a known-good genesis-anchored proof — Groth16 verification is broken"
fi
echo "$out" | tail -3
grep -qi "verified" <<<"$out" || fail "verify-snark exited 0 but did not report a verification"
echo "  ok"

echo "=== 2. non-genesis wrapped range must be REJECTED, on the genesis pin ==="
if out=$("$HOST" verify-snark "$NEG" 2>&1); then
    echo "$out"
    fail "verify-snark ACCEPTED a non-genesis range — the genesis pin is not enforced through the wrap"
fi
if grep -qiE "genesis|in-boundary|block 1" <<<"$out"; then
    echo "  ok — rejected on the genesis pin"
else
    echo "$out"
    fail "rejected, but NOT on the genesis pin — the rejection reason must be the anchor, not an unrelated error"
fi

echo "Groth16 verification gate passed (positive + genesis-pin negative control)"
