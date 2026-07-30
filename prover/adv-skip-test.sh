#!/bin/bash
# hazync#34 — adversarial tests for the proof-gated script skip.
#
# #33 established that the honest path reaches an identical chainstate. That is the least interesting
# property of consensus-critical code. What matters is what happens when the input is wrong, and the
# invariant under test is:
#
#   NO input — absent, corrupt, truncated, unanchored, or describing a different chain — may cause a
#   script check to be elided, and in every case the node must still reach the correct chainstate.
#
# The expected chainstate is not computed here; it is the value independently established by the
# full-validation baseline in #33. Recomputing it in the same harness that consumes it would make the
# comparison circular.
set -uo pipefail

GHOSTD=/src/gbuild/bin/ghostd
CLI=/src/gbuild/bin/ghost-cli
GOOD=/work/fold_8.snark          # valid, genesis-anchored [1..1000]
NEG=/work/neg500.snark              # valid SNARK, range [500..1000] — NOT genesis-anchored
H=1000

# From #33's full-validation baseline.
WANT_TIP=00000000c937983704a73af28acdec37b049d214adbda81d7e2a3dd146f6ed09
WANT_UTXO=29a42ec29ebde38c3ddde7da608f28545b7968aa41ed9fa9fb88ab90206d0e75
WANT_TXOUTS=998

PASS=0; FAIL=0
say() { echo "[$(date -u +%T)] $*"; }

# Build the corrupt fixtures from the good proof.
prep() {
    cp "$GOOD" /work/corrupt.snark
    # Flip one bit deep inside the proof body. Must not be in the first bytes, which bincode reads as
    # length prefixes — that would fail as a PARSE error and never reach proof verification, testing
    # the deserialiser rather than the verifier.
    printf '\x01' | dd of=/work/corrupt.snark bs=1 seek=2000 conv=notrunc status=none
    head -c 1200 "$GOOD" > /work/truncated.snark
    printf '' > /work/empty.snark
    ls -l /work/*.snark
}

# $1 = case name, $2 = expected skip count, rest = extra ghostd args
run_case() {
    local name="$1" want_skip="$2"; shift 2
    local dir="/work/adv-$name"
    rm -rf "$dir"; mkdir -p "$dir"
    say "── case: $name (expect $want_skip skips) ──"

    "$GHOSTD" -datadir="$dir" -port=18444 -rpcport=18443 -rpcuser=t -rpcpassword=t \
        -connect=127.0.0.1:8333 -stopatheight=$H -listen=0 -dbcache=2000 -assumevalid=0 \
        -debug=validation -printtoconsole=0 "$@" >"$dir/run.out" 2>&1
    local rc=$?

    local skips tip utxo txouts height
    skips=$(grep -c "script checks SKIPPED" "$dir/debug.log" 2>/dev/null | head -1); skips=${skips:-0}

    if [ $rc -ne 0 ]; then
        # A node that refuses to start on a bad proof is a denial of service on itself. Rejecting the
        # proof and carrying on is the correct behaviour; exiting is a finding, not a pass.
        echo "  ✗ ghostd exited $rc — a bad proof must be rejected, not fatal"
        tail -5 "$dir/debug.log" 2>/dev/null | sed 's/^/     /'
        FAIL=$((FAIL+1)); return
    fi

    "$GHOSTD" -datadir="$dir" -port=18444 -rpcport=18443 -rpcuser=t -rpcpassword=t \
        -connect=0 -listen=0 -assumevalid=0 -daemon "$@" >/dev/null 2>&1
    local q="$CLI -datadir=$dir -rpcport=18443 -rpcuser=t -rpcpassword=t"
    for _ in $(seq 60); do $q getblockcount >/dev/null 2>&1 && break; sleep 1; done
    while [ "$($q getblockcount 2>/dev/null || echo 0)" -gt "$H" ]; do
        $q invalidateblock "$($q getblockhash $((H+1)))" >/dev/null 2>&1 || break
    done
    height=$($q getblockcount 2>/dev/null)
    tip=$($q getbestblockhash 2>/dev/null)
    local info; info=$($q gettxoutsetinfo 2>/dev/null)
    utxo=$(grep -oE '"hash_serialized_3": *"[0-9a-f]+"' <<<"$info" | grep -oE '[0-9a-f]{64}')
    txouts=$(grep -oE '"txouts": *[0-9]+' <<<"$info" | grep -oE '[0-9]+')
    $q stop >/dev/null 2>&1; sleep 3

    local ok=1
    [ "$skips" = "$want_skip" ] || { echo "  ✗ skips=$skips want=$want_skip"; ok=0; }
    [ "$tip" = "$WANT_TIP" ]    || { echo "  ✗ tip=$tip"; ok=0; }
    [ "$utxo" = "$WANT_UTXO" ]  || { echo "  ✗ utxo commitment=$utxo"; ok=0; }
    [ "$txouts" = "$WANT_TXOUTS" ] || { echo "  ✗ txouts=$txouts"; ok=0; }
    [ "$height" = "$H" ]        || { echo "  ✗ height=$height"; ok=0; }

    if [ $ok -eq 1 ]; then
        echo "  ✓ skips=$skips, chainstate correct"
        PASS=$((PASS+1))
    else
        grep -i "hazync" "$dir/debug.log" 2>/dev/null | grep -vE "Command-line arg" | head -3 | sed 's/^/     /'
        FAIL=$((FAIL+1))
    fi
    rm -rf "$dir"
}

prep
echo

# The control. If this does not skip, the harness is broken and every "0 skips" below is meaningless.
run_case honest        1000 -hazyncproof="$GOOD" -hazyncskipvalidation

# Default posture: a verified proof alone must change nothing.
run_case proof-no-flag    0 -hazyncproof="$GOOD"

# The flag on its own must not be a way to turn off validation.
run_case flag-no-proof    0 -hazyncskipvalidation

# Corrupt / truncated / empty: rejected, nothing adopted, node carries on.
run_case corrupt          0 -hazyncproof=/work/corrupt.snark -hazyncskipvalidation
run_case truncated        0 -hazyncproof=/work/truncated.snark -hazyncskipvalidation
run_case empty            0 -hazyncproof=/work/empty.snark -hazyncskipvalidation

# Missing file: a typo in a path must not silently mean "no proof, carry on skipping".
run_case missing-file     0 -hazyncproof=/work/does-not-exist.snark -hazyncskipvalidation

# A valid SNARK over [500..1000]. Cryptographically sound, but it proves a range that starts from an
# unproven mid-chain state. Accepting it would reduce the claim to "someone proved a thousand blocks
# somewhere" — this is the single most important rejection in the whole design.
run_case not-anchored     0 -hazyncproof="$NEG" -hazyncskipvalidation

echo
echo "════════ #34 RESULT ════════"
echo "passed $PASS, failed $FAIL"
[ $FAIL -eq 0 ] && echo "✓ no bad input caused a script check to be elided, and every case reached the correct chainstate"
exit $FAIL
