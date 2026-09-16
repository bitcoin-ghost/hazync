#!/usr/bin/env bash
# Hazync alert -> phone push (ntfy). Installed as /usr/local/bin/hazync-alert.sh on the coordinator and
# on the web box. Three callers:
#
#   hazync-alert.sh --unit  <unit>    OnFailure= of a oneshot unit (retention check, backup, node tip)
#   hazync-alert.sh --crash <unit>    ExecStopPost= of a Restart=always service; alerts only when the
#                                     stop was NOT clean (SERVICE_RESULT != success)
#   hazync-alert.sh <title> <body>    anything else (the off-box watchdog)
#
# WHY THIS EXISTS. On 2026-09-14 the G1 retention check had been failing every night since at least
# 2026-09-12 — 251 proven heights with no per-block receipt — and nobody knew. A failed systemd unit
# is a line in a journal nobody reads. A check that fails silently is only slightly better than no
# check.
#
# WHY --crash AS WELL AS OnFailure=. OnFailure= fires when a unit enters the `failed` state. The
# coordinator (Restart=always, RestartSec=3) and bridge (RestartSec=10) never get there: systemd's
# default start limit is 5 starts in 10 s, and a restart delay of 3 s or more cannot reach it. A
# crash-looping coordinator restarts for ever and OnFailure= never says a word. ExecStopPost= runs on
# EVERY stop, with SERVICE_RESULT telling a crash from a deliberate restart.
#
# Config: NTFY_URL (https://ntfy.sh/<private topic>) from the environment or /etc/hazync/alert.env.
# The topic is the only secret — anyone who knows it can read and post alerts — so it is NOT in git.
#   HAZYNC_ALERT_DRYRUN=1       print what would be sent, send nothing (tests)
#   ALERT_STATE_DIR             crash de-duplication stamps (default /var/lib/hazync-alert)
#   ALERT_REPEAT_SECS           at most one crash alert per unit per this many seconds (default 900)
#   ALERT_PRIORITY              ntfy priority: min|low|default|high|urgent (default high). The daily backup
#                               summary sends low, so a routine "all good" does not ring like a failure.
#   ALERT_TAGS                  ntfy tags, comma-separated emoji short codes (default rotating_light)
set -uo pipefail

ENV_FILE="${HAZYNC_ALERT_ENV:-/etc/hazync/alert.env}"
if [ -z "${NTFY_URL:-}" ] && [ -r "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi
STATE="${ALERT_STATE_DIR:-/var/lib/hazync-alert}"
REPEAT="${ALERT_REPEAT_SECS:-900}"
HOST="$(hostname -s 2>/dev/null || hostname)"

journal_tail() {
    journalctl -u "$1" -n 15 --no-pager -o cat 2>/dev/null | tail -c 3000 || true
}

case "${1:-}" in
    --unit)
        unit="${2:?usage: hazync-alert.sh --unit <unit>}"
        title="Hazync: $unit FAILED on $HOST"
        body="$(printf '%s failed on %s at %s UTC.\n\n%s' "$unit" "$HOST" "$(date -u +%F\ %T)" "$(journal_tail "$unit")")"
        ;;
    --crash)
        unit="${2:?usage: hazync-alert.sh --crash <unit>}"
        result="${SERVICE_RESULT:-unknown}"
        if [ "$result" = "success" ]; then
            exit 0                       # a deliberate stop or restart — not news
        fi
        # A crash loop restarts every few seconds; one push per REPEAT window, not one per restart.
        mkdir -p "$STATE" 2>/dev/null
        stamp="$STATE/crash-${unit//\//_}"
        now=$(date +%s)
        last=$(cat "$stamp" 2>/dev/null || echo 0)
        if [ $((now - last)) -lt "$REPEAT" ]; then
            echo "[hazync-alert] $unit stopped again ($result) — suppressed, alerted $((now - last))s ago"
            exit 0
        fi
        echo "$now" > "$stamp" 2>/dev/null
        title="Hazync: $unit CRASHED on $HOST"
        body="$(printf '%s stopped uncleanly on %s at %s UTC: result=%s exit=%s/%s. systemd is restarting it; further crashes in the next %ss are not re-sent.\n\n%s' \
            "$unit" "$HOST" "$(date -u +%F\ %T)" "$result" "${EXIT_CODE:-?}" "${EXIT_STATUS:-?}" "$REPEAT" "$(journal_tail "$unit")")"
        ;;
    ""|-h|--help)
        sed -n '2,9p' "$0"; exit 2
        ;;
    *)
        title="$1"
        body="${2:-$1}"
        ;;
esac

# Only values ntfy understands reach the headers; anything else falls back to the alarm defaults.
case "${ALERT_PRIORITY:-high}" in
    min|low|default|high|urgent|[1-5]) PRIO="${ALERT_PRIORITY:-high}" ;;
    *) PRIO=high ;;
esac
TAGS="$(printf '%s' "${ALERT_TAGS:-rotating_light}" | tr -cd 'a-z0-9_,-')"
[ -n "$TAGS" ] || TAGS=rotating_light

# The title must say good-or-bad at a glance, before any text: a phone shows the title and little else,
# and until now EVERY push carried rotating_light because that is the default tag — so "check-spine
# RECOVERED" arrived looking identical to a failure. Classify from the tags the caller already sets
# (hazync-offsite-watch.py has passed white_check_mark for "all good" and RECOVERED since it was
# written); --unit and --crash are always bad and never reach here with a good tag.
# printf interprets \u only in its FORMAT string, not in a variable it expands, so the escape must be
# the format itself — otherwise the title literally begins "\U0001F6A8".
case ",$TAGS," in
    *,white_check_mark,*|*,heavy_check_mark,*|*,tada,*|*,+1,*) MARK="$(printf '\xe2\x9c\x85')" ;;          # green tick
    *)                                                        MARK="$(printf '\xf0\x9f\x9a\xa8')" ;;      # red siren
esac
title="$MARK $title"

if [ "${HAZYNC_ALERT_DRYRUN:-0}" = "1" ]; then
    printf 'DRYRUN url=%s\nTitle: %s\nPriority: %s\nTags: %s\n\n%s\n' "${NTFY_URL:-<unset>}" "$title" "$PRIO" "$TAGS" "$body"
    [ -n "${NTFY_URL:-}" ] || { echo "[hazync-alert] FATAL: NTFY_URL is not set" >&2; exit 3; }
    exit 0
fi

if [ -z "${NTFY_URL:-}" ]; then
    # Fail LOUD. An alerter with nowhere to send is exactly the silent failure it exists to end.
    echo "[hazync-alert] FATAL: NTFY_URL is not set (env or $ENV_FILE) — this alert went NOWHERE: $title" >&2
    exit 3
fi

if printf '%s' "$body" | curl -fsS -m 20 --retry 3 --retry-delay 5 \
        -H "Title: $title" -H "Priority: $PRIO" -H "Tags: $TAGS" \
        --data-binary @- "$NTFY_URL" >/dev/null; then
    echo "[hazync-alert] sent: $title"
else
    echo "[hazync-alert] FAILED to send: $title" >&2
    exit 4
fi
