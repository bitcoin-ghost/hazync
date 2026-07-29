#!/bin/bash
# hazync#33 — proof-backed IBD: does skipping proven script checks reach the SAME chainstate?
#
# Two IBDs to height 1000 from the local archive node, identical but for the proof:
#   A  baseline      — every script verified from genesis
#   B  proof-backed  — scripts elided for blocks the proof covers
#
# The claim under test is EQUIVALENCE, not speed. At N=1000 there are ~1020 transactions and almost
# no signature work to skip, so a time difference here would be noise; the thing worth proving at this
# size is that the two runs agree on the UTXO set exactly.
#
# -assumevalid=0 on BOTH runs is what makes the comparison mean anything. The shipped assumevalid hash
# sits near the chain tip, so blocks 1..1000 are its ancestors and Core ALREADY skips their scripts.
# Without disabling it the "baseline" also skips, both runs are identical, and the test measures
# nothing. Disabled, the proof is the only thing that can elide a script check.
set -uo pipefail
GHOSTD=/src/gbuild/bin/ghostd
CLI=/src/gbuild/bin/ghost-cli
PROOF=/work/fold_1000.snark
H=1000
say() { echo "[$(date -u +%T)] $*"; }
die() { echo "FAIL: $*" >&2; exit 1; }

[ -x "$GHOSTD" ] || die "no ghostd at $GHOSTD"
[ -s "$PROOF" ] || die "no proof at $PROOF"

# Ports well clear of the live node (8332/8333/8334). The test node must never be mistaken for it.
run() {
    local tag="$1" dir="/work/run-$1"; shift
    rm -rf "$dir"; mkdir -p "$dir"
    say "── run $tag: IBD to $H ──"
    local t0 t1
    t0=$(date +%s.%N)
    "$GHOSTD" -datadir="$dir" -port=18444 -rpcport=18443 -rpcuser=t -rpcpassword=t \
        -connect=127.0.0.1:8333 -stopatheight=$H -listen=0 -dbcache=2000 -assumevalid=0 \
        -debug=validation -printtoconsole=0 "$@" >"$dir/run.out" 2>&1
    local rc=$?
    t1=$(date +%s.%N)
    [ $rc -eq 0 ] || { tail -30 "$dir/debug.log" 2>/dev/null; die "run $tag exited $rc"; }
    ELAPSED=$(echo "$t1 - $t0" | bc)
    say "run $tag finished in ${ELAPSED}s"

    # Re-open with no peers so the chainstate cannot advance while we read it.
    "$GHOSTD" -datadir="$dir" -port=18444 -rpcport=18443 -rpcuser=t -rpcpassword=t \
        -connect=0 -listen=0 -assumevalid=0 -daemon "$@" >/dev/null 2>&1
    for i in $(seq 60); do
        "$CLI" -datadir="$dir" -rpcport=18443 -rpcuser=t -rpcpassword=t getblockcount >/dev/null 2>&1 && break
        sleep 1
    done
    local q="$CLI -datadir=$dir -rpcport=18443 -rpcuser=t -rpcpassword=t"
    # -stopatheight halts AFTER connecting $H, but blocks past it were already downloaded and the
    # restart connects them from disk — and how many differs per run. Roll back to exactly $H so both
    # runs are compared at the same point rather than at whatever they happened to reach.
    while [ "$($q getblockcount)" -gt "$H" ]; do
        $q invalidateblock "$($q getblockhash $((H + 1)))" >/dev/null 2>&1 || break
    done
    HEIGHT=$($q getblockcount)
    TIP=$($q getbestblockhash)
    UTXO=$($q gettxoutsetinfo 2>/dev/null)
    $q stop >/dev/null 2>&1; sleep 3
    SKIPPED=$(grep -c "script checks SKIPPED" "$dir/debug.log" 2>/dev/null | head -1)
    SKIPPED=${SKIPPED:-0}
}

run baseline
A_H=$HEIGHT; A_TIP=$TIP; A_UTXO=$UTXO; A_T=$ELAPSED; A_SKIP=$SKIPPED

run proof -hazyncproof="$PROOF" -hazyncskipvalidation
B_H=$HEIGHT; B_TIP=$TIP; B_UTXO=$UTXO; B_T=$ELAPSED; B_SKIP=$SKIPPED

pick() { grep -oE "\"$1\": *[^,}]+" <<<"$2" | head -1 | sed -E 's/.*: *//; s/"//g'; }
echo
echo "════════ RESULT ════════"
printf '%-22s %-34s %s\n' "" "BASELINE" "PROOF-BACKED"
printf '%-22s %-34s %s\n' "height"        "$A_H" "$B_H"
printf '%-22s %-34s %s\n' "tip"           "$A_TIP" "$B_TIP"
printf '%-22s %-34s %s\n' "txouts"        "$(pick txouts "$A_UTXO")" "$(pick txouts "$B_UTXO")"
printf '%-22s %-34s %s\n' "total_amount"  "$(pick total_amount "$A_UTXO")" "$(pick total_amount "$B_UTXO")"
printf '%-22s %-34s %s\n' "utxo commitment" "$(pick hash_serialized_3 "$A_UTXO")" "$(pick hash_serialized_3 "$B_UTXO")"
printf '%-22s %-34s %s\n' "blocks skipped" "$A_SKIP" "$B_SKIP"
printf '%-22s %-34s %s\n' "wall seconds"  "$A_T" "$B_T"
echo

FAIL=0
[ "$A_TIP" = "$B_TIP" ] || { echo "✗ TIP MISMATCH"; FAIL=1; }
[ "$A_H" = "$H" ] && [ "$B_H" = "$H" ] || { echo "✗ wrong height"; FAIL=1; }
[ -n "$(pick hash_serialized_3 "$A_UTXO")" ] && [ "$(pick hash_serialized_3 "$A_UTXO")" = "$(pick hash_serialized_3 "$B_UTXO")" ] || { echo "✗ UTXO SET DIFFERS"; FAIL=1; }
[ "$A_SKIP" = "0" ] || { echo "✗ baseline skipped $A_SKIP blocks — it must skip NONE"; FAIL=1; }
[ "$B_SKIP" -gt 0 ] 2>/dev/null || { echo "✗ proof run skipped NOTHING — the gate never fired, so this run proves nothing"; FAIL=1; }

if [ $FAIL -eq 0 ]; then
    echo "✓ EQUIVALENT: $B_SKIP blocks had script verification elided and the resulting UTXO set is"
    echo "  byte-identical to full validation from genesis."
else
    echo "RUN FAILED"
fi
exit $FAIL
