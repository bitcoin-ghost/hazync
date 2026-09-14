#!/usr/bin/env bash
# Off-box liveness for the Hazync coordinator. Installed on the WEB box as
# /usr/local/bin/hazync-watchdog.sh, run every 5 minutes by hazync-watchdog.timer.
#
# WHY OFF-BOX. Every other alert (hazync-alert.sh via OnFailure=/ExecStopPost=) runs ON the coordinator,
# so it dies with it: a powered-off, disk-full, wedged or partitioned coordinator runs no hook and
# sends nothing. Silence then reads exactly like "all fine". Only something on another machine can
# notice the absence.
#
# "Up" means a real /api/meta answer (JSON carrying method_id), not merely HTTP 200 — nginx serves
# error pages too, and a probe that a proxy error page satisfies cannot fail.
#
#   WATCHDOG_TARGETS              space-separated name=url pairs
#   WATCHDOG_FAILS_BEFORE_ALERT   consecutive failed probes before the first push (default 2 = ~10 min)
#   WATCHDOG_REALERT_SECS         re-push while still down at most this often (default 3600)
#   WATCHDOG_STATE_DIR            per-target "fails last_alert" (default /var/lib/hazync-watchdog)
#   HAZYNC_ALERT                  the alert command (default /usr/local/bin/hazync-alert.sh)
set -uo pipefail

TARGETS="${WATCHDOG_TARGETS:-coordinator=http://152.53.93.164:8899/api/meta public=https://bitcoinghost.org/hazync/api/meta}"
FAILS_BEFORE_ALERT="${WATCHDOG_FAILS_BEFORE_ALERT:-2}"
REALERT_SECS="${WATCHDOG_REALERT_SECS:-3600}"
STATE="${WATCHDOG_STATE_DIR:-/var/lib/hazync-watchdog}"
ALERT="${HAZYNC_ALERT:-/usr/local/bin/hazync-alert.sh}"
PROBE_TIMEOUT="${WATCHDOG_PROBE_TIMEOUT:-20}"

mkdir -p "$STATE" || { echo "[watchdog] FATAL: cannot create $STATE" >&2; exit 1; }

for t in $TARGETS; do
    name="${t%%=*}"; url="${t#*=}"
    f="$STATE/$name"
    fails=0; last_alert=0
    [ -s "$f" ] && read -r fails last_alert < "$f"
    body="$(curl -fsS -m "$PROBE_TIMEOUT" "$url" 2>&1)"; rc=$?
    now=$(date +%s)
    if [ "$rc" -eq 0 ] && printf '%s' "$body" | grep -q '"method_id"'; then
        if [ "$fails" -ge "$FAILS_BEFORE_ALERT" ]; then
            "$ALERT" "Hazync: $name RECOVERED" "$url is answering again after $fails failed probes." \
                || echo "[watchdog] could not send the recovery alert for $name" >&2
        fi
        echo "0 0" > "$f"
        echo "[watchdog] $name up"
    else
        fails=$((fails + 1))
        if [ "$fails" -ge "$FAILS_BEFORE_ALERT" ] && [ $((now - last_alert)) -ge "$REALERT_SECS" ]; then
            if "$ALERT" "Hazync: $name DOWN" \
                    "$url has failed $fails probes in a row (every 5 min). Last answer: $(printf '%s' "$body" | head -c 300)"; then
                last_alert=$now
            else
                echo "[watchdog] could not send the DOWN alert for $name — will retry next probe" >&2
            fi
        fi
        echo "$fails $last_alert" > "$f"
        echo "[watchdog] $name DOWN (fails=$fails rc=$rc)"
    fi
done
exit 0
