#!/usr/bin/env bash
# Groth16 PROVING gate (#23) — the half ci_snark_verify.sh cannot cover.
#
# Verification is cheap and runs on every push. Proving is not: a CPU Groth16 wrap measured 76.6 s for a
# 1000-block fold and 825.7 s for a single block-170 chain step on a fast box, and far worse on a shared
# 2-core runner. So this is OPT-IN — run it before a release, or via workflow_dispatch.
#
# It is the test that would actually have caught the v0.8.0 regression: Groth16 shipped broken and
# nothing ever asked it to produce a proof.
#
# Usage:  ./ci_snark_prove.sh <receipt.bin>        (a genesis-anchored range receipt)
#         HAZYNC_HOST=/path/to/host ./ci_snark_prove.sh ~/.hazync/receipts/1.bin
#
# NOTE: needs a host with a WORKING Groth16 backend. Today that means a CPU build — Groth16 crashes in
# sppark on every CUDA build we ship (#20). Running this against a CUDA host is expected to fail, and
# that failure is #20, not a regression here.
set -uo pipefail

HOST="${HAZYNC_HOST:-./target/release/host}"
IN="${1:-}"
OUT="$(mktemp -t snarkprove.XXXXXX.snark)"
trap 'rm -f "$OUT"' EXIT

fail() { echo "::error::$*"; exit 1; }

[ -x "$HOST" ] || fail "no host binary at $HOST"
[ -n "$IN" ] || fail "usage: $0 <receipt.bin>"
[ -s "$IN" ] || fail "no receipt at $IN"

echo "=== 0. the input receipt must verify BEFORE we wrap it ==="
pre=$("$HOST" verify-range "$IN" 2>&1) || { echo "$pre"; fail "input receipt does not verify — nothing to wrap"; }
echo "$pre" | tail -2

echo "=== 1. snark-wrap (Groth16) ==="
t0=$(date +%s)
if ! wrap=$("$HOST" snark-wrap "$IN" "$OUT" 2>&1); then
    echo "$wrap" | tail -20
    fail "snark-wrap FAILED — Groth16 proving is broken (if this is a CUDA build, see #20)"
fi
el=$(( $(date +%s) - t0 ))
[ -s "$OUT" ] || fail "snark-wrap exited 0 but produced no artifact"
echo "  wrapped $(stat -c%s "$IN") B -> $(stat -c%s "$OUT") B in ${el}s"

echo "=== 2. the wrapped proof must verify ==="
post=$("$HOST" verify-snark "$OUT" 2>&1) || { echo "$post"; fail "the freshly wrapped proof does not verify"; }
echo "$post" | tail -2

echo "=== 3. the wrap must not change what is claimed ==="
# A wrap that silently altered the journal would still 'verify' — it would just be proving something
# else. Compare the committed fields either side of the wrap.
for field in out_tip_hash range_work total_cum_work; do
    a=$(grep -oE "$field [0-9a-f]+" <<<"$pre"  | head -1 | awk '{print $2}')
    b=$(grep -oE "$field [0-9a-f]+" <<<"$post" | head -1 | awk '{print $2}')
    [ -n "$a" ] || { echo "  (no $field in verify-range output; skipping)"; continue; }
    [ "$a" = "$b" ] || fail "$field changed across the wrap: $a -> $b"
    echo "  $field preserved"
done

echo "Groth16 proving gate passed (wrap produced a verifying proof, journal preserved)"
