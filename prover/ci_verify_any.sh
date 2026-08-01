#!/usr/bin/env bash
# `verify-any` gate — the command the COORDINATOR depends on, which had no CI coverage at all.
#
# Every submission to the board goes through `host verify-any`: server.py shells out to it, parses the
# single RANGE-OK line, and decides from that whether a receipt is real and which range it proves. It
# is the sole gate between a submitted receipt and the board — and nothing in CI ran it. The
# standalone verifier is well covered (both fixtures, exact exit codes); `verify-any` is a DIFFERENT
# code path with a DIFFERENT genesis condition, and it was the uncovered one.
#
# Found 2026-08-02 while looking for coverage of a change to its output.
#
# WHY THIS LIVES IN THE reproducible-image-id JOB, not soundness-suite: a RISC0 guest image id absorbs
# the build's $HOME/.cargo paths, so the host that soundness-suite builds on the runner has a
# NON-canonical METHOD_ID and CANNOT verify the checked-in fixtures — it rejects them as a build
# mismatch, which looks like a broken proof and is not. The fixed-path container is the one place a
# canonical host exists. Same reasoning as ci_snark_verify.sh, which sits there for the same reason.
#
# THE CENTRAL ASSERTION IS THE SECOND ONE. Unlike `verify-snark`, `verify-any` must ACCEPT a valid
# non-genesis range — that is its purpose, and it is why the coordinator can record mid-chain work.
# The genesis pin lives in verify-range/verify-snark/verify-chain and in the standalone verifier.
# Getting this backwards in either direction is a real failure: refusing mid-chain ranges would stall
# the board, and pinning nothing anywhere would let a fabricated anchor into the frontier.
set -uo pipefail

HOST="${HAZYNC_HOST:-./target/release/host}"
DIR="$(cd "$(dirname "$0")" && pwd)"
POS="$DIR/testdata/snark/fold_8.snark"    # [1..8], genesis-anchored (measured, not assumed)
NEG="$DIR/testdata/snark/neg500.snark"    # [500..500], valid but NOT genesis-anchored

fail() { echo "::error::$*"; exit 1; }

[ -x "$HOST" ] || fail "no host binary at $HOST"
for f in "$POS" "$NEG"; do
    # Without this a vanished fixture makes the positive test fail and the negative test 'pass' for
    # entirely the wrong reason.
    [ -s "$f" ] || fail "missing fixture: $f"
done

# The coordinator's parse, reproduced EXACTLY as server.py does it:
#   line = next(l for l in out.splitlines() if l.startswith("RANGE-OK"))
#   kv   = dict(t.split("=",1) for t in line[len("RANGE-OK"):].split() if "=" in t)
# Reproduced rather than approximated, because the point is to catch an output change that breaks
# THAT parser, not one that breaks a similar-looking one written here.
kv_get() {  # $1 = full output, $2 = key
    local line
    line=$(grep -m1 '^RANGE-OK' <<<"$1") || return 1
    tr ' ' '\n' <<<"${line#RANGE-OK}" | grep -m1 "^$2=" | cut -d= -f2-
}

echo "=== 1. genesis-anchored range must VERIFY ==="
if ! pos_out=$("$HOST" verify-any "$POS" 2>&1); then
    echo "$pos_out"
    fail "verify-any REJECTED a known-good genesis-anchored proof"
fi
grep -q '^RANGE-OK' <<<"$pos_out" || { echo "$pos_out"; fail "exited 0 but printed no RANGE-OK line — the coordinator would treat this as a rejection"; }
echo "  ok"

echo "=== 2. a valid NON-genesis range must ALSO verify (this is not verify-snark) ==="
# The assertion the whole command is built around. verify-any deliberately does NOT pin genesis;
# refusing here would stall every mid-chain submission on the board.
if ! neg_out=$("$HOST" verify-any "$NEG" 2>&1); then
    echo "$neg_out"
    fail "verify-any REJECTED a valid non-genesis range — mid-chain submissions would all fail"
fi
grep -q '^RANGE-OK' <<<"$neg_out" || { echo "$neg_out"; fail "valid non-genesis range printed no RANGE-OK line"; }
echo "  ok"

echo "=== 3. the RANGE-OK line carries every key server.py reads ==="
# server.py does kv["lo"], kv["hi"], kv["in_tip"], kv["out_tip"] unguarded — a missing one is a
# KeyError inside verify_receipt, i.e. every submission failing at once. The bhash keys use .get(),
# but an absent in_bhash/out_bhash silently breaks seam continuity and the frontier stops advancing,
# which is worse than a crash because it looks like nobody is submitting.
for out_name in pos neg; do
    out_var="${out_name}_out"
    for key in lo hi in_tip out_tip in_bhash out_bhash range_work out_leaves; do
        v=$(kv_get "${!out_var}" "$key")
        [ -n "$v" ] || fail "RANGE-OK ($out_name) is missing '$key=' — server.py reads this key"
    done
done
echo "  ok — lo hi in_tip out_tip in_bhash out_bhash range_work out_leaves all present"

echo "=== 4. the reported range matches the fixture ==="
# Guards against a receipt verifying but being MISREPORTED. server.py refuses a receipt whose
# [lo..hi] differs from the claimed range, so a wrong lo/hi here would reject honest work.
plo=$(kv_get "$pos_out" lo); phi=$(kv_get "$pos_out" hi)
nlo=$(kv_get "$neg_out" lo); nhi=$(kv_get "$neg_out" hi)
[ "$plo" = 1 ]   || fail "genesis-anchored fixture reports lo=$plo, expected 1"
[ "$nlo" = "$nhi" ] || fail "single-block fixture reports [$nlo..$nhi], expected a width of 1"
[ "$nlo" != 1 ] || fail "the NON-genesis fixture reports lo=1 — it is supposed to be mid-chain, so this test proves nothing"
echo "  ok — [$plo..$phi] and [$nlo..$nhi]"

echo "=== 5. NEGATIVE CONTROL: a tampered receipt must be REJECTED ==="
# Without this, every assertion above is satisfied by a verifier that accepts anything. Flip bytes in
# the middle of the receipt (not the header, so it still deserialises far enough to reach the STARK
# check) and require refusal.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp "$POS" "$TMP/tampered.bin"
sz=$(stat -c%s "$TMP/tampered.bin")
printf '\xde\xad\xbe\xef' | dd of="$TMP/tampered.bin" bs=1 seek=$((sz / 2)) conv=notrunc status=none
if out=$("$HOST" verify-any "$TMP/tampered.bin" 2>&1); then
    echo "$out"
    fail "verify-any ACCEPTED a tampered receipt — STARK verification is not being performed"
fi
echo "  ok — tampered receipt refused"

echo
echo "verify-any gate passed (accepts anchored AND mid-chain, full key contract, tamper control)"
