#!/usr/bin/env bash
# Run one Hazync integrity check and push to the phone only when it matters: once when it starts failing, again at
# most hourly while it keeps failing, and once when it recovers. Installed as /usr/local/sbin/hazync-run-check.
#
#   hazync-run-check <name> <command> [args...]
#
# WHY NOT JUST OnFailure=. The checks run every 10 minutes. OnFailure= would push six times an hour for one problem,
# and an alarm that nags gets muted. The watchdog learned the same thing (hazync-watchdog.sh).
#
#   CHECK_FAILS_BEFORE_ALERT  consecutive failed runs before the first push (default 1)
#   CHECK_REALERT_SECS        re-push while still failing at most this often (default 3600)
#   CHECK_STATE_DIR           per-check "fails last_alert" (default /var/lib/hazync-checks)
#   HAZYNC_ALERT              the alert command (default /usr/local/bin/hazync-alert.sh)
#
# A check exits 0 when it holds, 1 on an integrity failure, 2 when it could not check; 1 and 2 both count as
# failing. This script exits 0 whenever it handled the outcome, including a failure it pushed or deliberately did not
# re-send, and 1 only when a push that was due could not be sent, so the unit's OnFailure= alert is the fallback.
set -uo pipefail

name="${1:?usage: hazync-run-check <name> <command> [args...]}"
shift
[ $# -gt 0 ] || { echo "usage: hazync-run-check <name> <command> [args...]" >&2; exit 2; }
FAILS_BEFORE="${CHECK_FAILS_BEFORE_ALERT:-1}"
REALERT="${CHECK_REALERT_SECS:-3600}"
STATE="${CHECK_STATE_DIR:-/var/lib/hazync-checks}"
ALERT="${HAZYNC_ALERT:-/usr/local/bin/hazync-alert.sh}"
HOST="$(hostname -s 2>/dev/null || hostname)"

mkdir -p "$STATE" || { echo "[check] FATAL: cannot create $STATE" >&2; exit 1; }
f="$STATE/$name"
fails=0
last=0
[ -s "$f" ] && read -r fails last < "$f"

out="$("$@" 2>&1)"
rc=$?
printf '%s\n' "$out"
now=$(date +%s)

if [ "$rc" -eq 0 ]; then
    if [ "$last" -gt 0 ]; then
        if ! "$ALERT" "Hazync: $name RECOVERED on $HOST" \
                "$(printf '%s holds again after %s failed run(s).\n\n%s' "$name" "$fails" "$(printf '%s\n' "$out" | tail -n 4)")"; then
            echo "[check] could not send the recovery push for $name" >&2
        fi
    fi
    echo "0 0" > "$f"
    echo "[check] $name holds"
    exit 0
fi

fails=$((fails + 1))
kind="FAILED"
[ "$rc" -eq 2 ] && kind="could not run"
if [ "$fails" -ge "$FAILS_BEFORE" ] && [ $((now - last)) -ge "$REALERT" ]; then
    detail="$(printf '%s\n' "$out" | grep -E 'FAIL|COULD NOT CHECK|INTEGRITY FAILURE' | head -n 12)"
    body="$(printf '%s %s on %s (exit %s, %s run(s) in a row). Re-sent at most every %ss while it lasts.\n\n%s' \
        "$name" "$kind" "$HOST" "$rc" "$fails" "$REALERT" "${detail:-$(printf '%s\n' "$out" | tail -n 8)}")"
    if "$ALERT" "Hazync: $name $kind on $HOST" "$body"; then
        last=$now
    else
        echo "$fails $last" > "$f"
        echo "[check] $name $kind, and the push could not be sent" >&2
        exit 1
    fi
fi
echo "$fails $last" > "$f"
echo "[check] $name $kind (exit $rc, $fails run(s) in a row)"
exit 0
