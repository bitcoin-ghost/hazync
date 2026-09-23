#!/usr/bin/env bash
# Re-verify a run's receipts on a machine that took no part in producing them.
#
# WHY THIS EXISTS. A run says "VERIFIED" because the coordinator verified in process, on a pod the
# run rented, with a binary the run installed. That is a true statement about our own tooling and a
# weak one to publish. The 966,256 milestone did the stronger thing -- "re-verified afterwards on a
# machine that took no part in the run" -- and that is the claim worth making.
#
#   verify-receipts.sh <rundir> [--host <ssh-host>] [--bin <path-to-host-binary>]
#
# `receipt-digest <file>` calls r.verify(METHOD_ID) and EXITS 1 on failure, so its exit status is the
# verdict; it also prints journal_bytes and journal_digest, which are what a third party compares.
#
# ⛔ GREEN MUST BE POSITIVE. A receipt that fails to verify, a binary that is missing, and an ssh
# that never connected all produce no output. This requires the literal string "VERIFIED against
# METHOD_ID" per receipt and counts them; absence of failure is not success.
#
# ⛔ The sha256 of each receipt FILE is recorded too. Without it "VERIFIED" says nothing about which
# bytes were verified, and the artifact anyone else downloads cannot be tied to this result.
set -uo pipefail

RUNDIR="${1:?usage: verify-receipts.sh <rundir> [--host H] [--bin PATH]}"
shift || true
HOST=""; BIN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:?--host needs a value}"; shift 2 ;;
        --bin)  BIN="${2:?--bin needs a value}";   shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

shopt -s nullglob
receipts=("$RUNDIR"/receipt_*.bin)
if [ ${#receipts[@]} -eq 0 ]; then
    echo "⛔ no receipt_*.bin in $RUNDIR — nothing to verify, and that is a FAILURE for a run" >&2
    echo "   that claims to have proved anything" >&2
    exit 1
fi

if [ -z "$BIN" ]; then
    for c in /usr/local/bin/hazync-host-cuda /usr/local/bin/hazync-host \
             /usr/local/bin/hazync-host-bridge; do
        if [ -n "$HOST" ]; then
            ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "test -x $c" 2>/dev/null && { BIN="$c"; break; }
        elif [ -x "$c" ]; then BIN="$c"; break; fi
    done
fi
if [ -z "$BIN" ]; then
    echo "⛔ no host binary found${HOST:+ on $HOST} — pass --bin" >&2
    exit 1
fi

WHERE="${HOST:-$(hostname -s)}"
OUT="$RUNDIR/verification.json"
echo "[verify] ${#receipts[@]} receipt(s), binary $BIN on $WHERE"
echo "[" > "$OUT"
ok=0; bad=0; first=1
for r in "${receipts[@]}"; do
    h=$(basename "$r" .bin); h="${h#receipt_}"
    sum=$(sha256sum "$r" | awk '{print $1}')
    sz=$(stat -c%s "$r" 2>/dev/null || echo 0)
    if [ -n "$HOST" ]; then
        # ⚠ Copy the artifact to the verifier rather than trusting a path there: the point is that
        # THESE bytes verify, on a machine that did not make them.
        scp -q -o BatchMode=yes -o ConnectTimeout=20 "$r" "$HOST:/tmp/$h.bin" 2>/dev/null
        res=$(ssh -o BatchMode=yes -o ConnectTimeout=30 "$HOST" \
                  "$BIN receipt-digest /tmp/$h.bin 2>&1; rm -f /tmp/$h.bin" 2>&1)
    else
        res=$("$BIN" receipt-digest "$r" 2>&1)
    fi
    if printf '%s' "$res" | grep -q "VERIFIED against METHOD_ID"; then
        verdict="VERIFIED"; ok=$((ok+1))
    else
        verdict="FAILED"; bad=$((bad+1))
    fi
    jd=$(printf '%s' "$res" | awk '/journal_digest/{print $2}' | head -1)
    jb=$(printf '%s' "$res" | awk '/journal_bytes/{print $2}'  | head -1)
    [ $first -eq 1 ] || echo "," >> "$OUT"; first=0
    printf ' {"height": %s, "verdict": "%s", "sha256": "%s", "bytes": %s, "journal_digest": "%s", "journal_bytes": %s, "verified_on": "%s", "binary": "%s"}' \
        "$h" "$verdict" "$sum" "${sz:-0}" "${jd:-}" "${jb:-0}" "$WHERE" "$BIN" >> "$OUT"
    printf "  %-10s %-9s sha256 %s…\n" "$h" "$verdict" "${sum:0:16}"
done
echo "" >> "$OUT"; echo "]" >> "$OUT"

echo "[verify] $ok VERIFIED, $bad FAILED, on $WHERE -> $OUT"
[ "$bad" -eq 0 ] && [ "$ok" -eq "${#receipts[@]}" ] || exit 1
