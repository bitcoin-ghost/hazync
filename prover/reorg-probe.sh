#!/bin/bash
# hazync#34 case (7), the part reachable on mainnet: does the proof gate behave correctly when blocks
# are DISCONNECTED and RECONNECTED, rather than only during a straight-line IBD?
#
# This is not the fork test — that needs a competing chain and therefore a regtest guest, which does
# not exist (the guest compiles CChainParams::Main()). What it does test is a real risk in the gate:
# the proven anchor is installed once at startup, so if it were somehow one-shot, or depended on IBD
# state, or if the block index lookup stopped resolving once the proven tip sat inside an invalidated
# branch, a reconnect would silently behave differently from the first pass.
#
# Three things are checked:
#   1. the gate fires on the initial sync                            (baseline for comparison)
#   2. invalidateblock below the proven height rolls the tip back    (setup actually happened)
#   3. reconnecting those blocks skips them AGAIN, and only them     (the actual question)
#
# Note (2) matters: if the rollback does not happen, (3) reconnects nothing and a "pass" is vacuous.
set -uo pipefail
GHOSTD=/src/gbuild/bin/ghostd
CLI=/src/gbuild/bin/ghost-cli
PROOF=/work/fold_1000.snark
H=1000
CUT=900
DIR=/work/reorg
say() { echo "[$(date -u +%T)] $*"; }

rm -rf "$DIR"; mkdir -p "$DIR"

# NOT hazed: a hazed node cannot disconnect a stripped block at all (ghost#545), which would mask the
# thing under test. Normal storage, so the disconnect actually works.
say "sync to $H with the proof, skip enabled, assumevalid off"
"$GHOSTD" -datadir="$DIR" -port=18475 -rpcport=18474 -rpcuser=t -rpcpassword=t \
    -connect=127.0.0.1:8333 -stopatheight=$H -listen=0 -assumevalid=0 -dbcache=2000 \
    -debug=validation -printtoconsole=0 \
    -hazyncproof="$PROOF" -hazyncskipvalidation >"$DIR/sync.out" 2>&1

skipped_initial=$(grep -c "script checks SKIPPED" "$DIR/debug.log" 2>/dev/null | head -1)
say "initial sync skipped ${skipped_initial:-0} blocks"
[ "${skipped_initial:-0}" -gt 0 ] || { say "✗ gate never fired on the initial sync — nothing below is meaningful"; exit 1; }

"$GHOSTD" -datadir="$DIR" -port=18475 -rpcport=18474 -rpcuser=t -rpcpassword=t \
    -connect=0 -listen=0 -assumevalid=0 -debug=validation \
    -hazyncproof="$PROOF" -hazyncskipvalidation -daemon >/dev/null 2>&1
q="$CLI -datadir=$DIR -rpcport=18474 -rpcuser=t -rpcpassword=t"
for _ in $(seq 60); do $q getblockcount >/dev/null 2>&1 && break; sleep 1; done
$q getblockcount >/dev/null 2>&1 || { say "✗ node did not come up"; exit 1; }

before_restart=$(grep -c "script checks SKIPPED" "$DIR/debug.log" | head -1)
say "tip after restart: $($q getblockcount); cumulative skips so far: $before_restart"

hash=$($q getblockhash $CUT)
say "invalidateblock $CUT ($hash)"
$q invalidateblock "$hash" >/dev/null 2>&1
sleep 4
rolled=$($q getblockcount 2>/dev/null)
say "tip after invalidate: ${rolled:-GONE}  (expect $((CUT - 1)))"
if [ "${rolled:-0}" != "$((CUT - 1))" ]; then
    say "⚠ rollback did not land — the reconnect below would test nothing"
fi

mark=$(grep -c "script checks SKIPPED" "$DIR/debug.log" | head -1)
say "reconsiderblock — forces $CUT..$H to be validated and connected again"
$q reconsiderblock "$hash" >/dev/null 2>&1
sleep 8
after=$($q getblockcount 2>/dev/null)
now=$(grep -c "script checks SKIPPED" "$DIR/debug.log" | head -1)
reconnect_skips=$((now - mark))
say "tip after reconsider: ${after:-GONE}; skips during reconnect: $reconnect_skips"

# Which heights were skipped during the reconnect, and were any of them outside the proven range?
awk '/script checks SKIPPED/{print}' "$DIR/debug.log" | tail -n "$reconnect_skips" \
  | grep -oP "block \K[0-9]+" | sort -n > "$DIR/reheights.txt" 2>/dev/null
lo=$(head -1 "$DIR/reheights.txt" 2>/dev/null); hi=$(tail -1 "$DIR/reheights.txt" 2>/dev/null)
say "reconnect skip range: ${lo:-none}..${hi:-none}"

$q stop >/dev/null 2>&1; sleep 2

fail=0
# >= not ==: -stopatheight halts after connecting $H, but blocks past it were already downloaded and
# the restart connects them, so the tip legitimately sits a little above $H.
[ "${after:-0}" -ge "$H" ] || { echo "✗ chain did not return to at least $H (got ${after:-GONE})"; fail=1; }
[ "$reconnect_skips" -gt 0 ] || { echo "✗ reconnect skipped NOTHING — the gate is one-shot, it does not survive a disconnect"; fail=1; }
[ "${hi:-99999}" -le "$H" ] || { echo "✗ skipped a block ABOVE the proven height ($hi) — the bound is not holding"; fail=1; }
[ "${lo:-0}" -ge "$CUT" ] || { echo "✗ skipped below the cut ($lo) — more was reconnected than expected"; fail=1; }

if [ $fail -eq 0 ]; then
    echo "✓ the gate survives disconnect/reconnect: $reconnect_skips blocks re-skipped, all within $CUT..$H,"
    echo "  and the chain returned to $after. The anchor is not one-shot, the height bound still holds on"
    echo "  the reconnect path, and the proven-tip lookup still resolves after that branch was invalidated."
fi
exit $fail
