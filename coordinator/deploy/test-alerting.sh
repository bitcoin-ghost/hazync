#!/usr/bin/env bash
# Can the alert path still say something?
#
# On 2026-09-14 the G1 retention check had been failing on the coordinator every night since at least
# 2026-09-12 and nobody knew: a failed systemd unit is a journal line nobody reads. hazync-alert.sh and
# the off-box watchdog are the fix, and they have the same shape of failure as the check they report
# on — the dangerous outcome is not a false alarm, it is SILENCE while something is wrong. So every
# case below is a positive control for a way the alert path could go quiet.
#
# Scope, stated plainly: this drives the scripts against a local stand-in for ntfy and synthetic
# probe targets. It does NOT prove the live wiring (drop-ins installed, topic subscribed on a phone) —
# that is checked on the boxes with a real test push, see RUNBOOK § Alerts.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
ALERT="$PWD/hazync-alert.sh"
WD="$PWD/watchdog/hazync-watchdog.sh"
fail=0
note() { printf '  %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; fail=1; }

TMP=$(mktemp -d)
SRV_PID=""
trap '[ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; rm -rf "$TMP"' EXIT
export HAZYNC_ALERT_ENV=/nonexistent          # never pick up a real topic from the machine running this
export ALERT_STATE_DIR="$TMP/alert-state"
unset NTFY_URL

echo "== 1. POSITIVE CONTROL: an alert with nowhere to go must FAIL LOUDLY =="
if "$ALERT" "title" "body" >"$TMP/o" 2>&1; then
    bad "no NTFY_URL, yet exit 0 — an alert went nowhere and reported success"; cat "$TMP/o"
elif grep -q "went NOWHERE" "$TMP/o"; then
    note "ok   no destination -> non-zero exit, and it says the alert went nowhere"
else
    bad "failed, but without saying the alert was lost"; cat "$TMP/o"
fi

# Every title now opens with a MARK: a green tick for good news, a red siren for bad (2026-09-16).
# Built from the same UTF-8 bytes hazync-alert.sh emits rather than pasted emoji, so these greps do
# not depend on this file's encoding surviving an editor or on the runner's locale.
TICK="$(printf '\xe2\x9c\x85')"
SIREN="$(printf '\xf0\x9f\x9a\xa8')"

echo "== 2. --unit names the failed unit in the title =="
if NTFY_URL=https://ntfy.invalid/t HAZYNC_ALERT_DRYRUN=1 "$ALERT" --unit hazync-retention-check.service >"$TMP/o" 2>&1 \
   && grep -q "^Title: $SIREN Hazync: hazync-retention-check.service FAILED on " "$TMP/o"; then
    note "ok   --unit -> '<siren> Hazync: <unit> FAILED on <host>'"
else
    bad "--unit did not produce the expected title"; cat "$TMP/o"
fi

echo "== 3. --crash: a deliberate stop is quiet, a crash alerts, a crash LOOP alerts once =="
export NTFY_URL=https://ntfy.invalid/t HAZYNC_ALERT_DRYRUN=1
out=$(SERVICE_RESULT=success "$ALERT" --crash hazync-coordinator.service 2>&1)
if [ -z "$out" ]; then note "ok   SERVICE_RESULT=success (a restart/deploy) sends nothing"
else bad "a clean stop produced output — every deploy would page someone"; echo "$out"; fi
out=$(SERVICE_RESULT=exit-code EXIT_CODE=exited EXIT_STATUS=1 "$ALERT" --crash hazync-coordinator.service 2>&1)
if printf '%s' "$out" | grep -q "^Title: $SIREN Hazync: hazync-coordinator.service CRASHED on "; then
    note "ok   SERVICE_RESULT=exit-code -> CRASHED alert"
else bad "a crash did not alert — OnFailure= alone never fires under Restart=always"; echo "$out"; fi
out=$(SERVICE_RESULT=exit-code "$ALERT" --crash hazync-coordinator.service 2>&1)
if printf '%s' "$out" | grep -q "suppressed" && ! printf '%s' "$out" | grep -q "^Title:"; then
    note "ok   a second crash inside the window is suppressed, not re-sent"
else bad "a crash loop would send one push per restart"; echo "$out"; fi
out=$(SERVICE_RESULT=signal "$ALERT" --crash hazync-bridge.service 2>&1)
if printf '%s' "$out" | grep -q "^Title: $SIREN Hazync: hazync-bridge.service CRASHED"; then
    note "ok   ...and the window is per unit: another unit's crash still alerts"
else bad "one unit's crash suppressed another's"; echo "$out"; fi
unset HAZYNC_ALERT_DRYRUN NTFY_URL

echo "== 4. a real POST reaches the server with the title, priority and tags headers =="
python3 - "$TMP" <<'PY' &
import http.server, socketserver, sys, os
d = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        k = len([f for f in os.listdir(d) if f.startswith("posted")]) + 1
        with open(os.path.join(d, f"posted{k}"), "w") as f:
            f.write("TITLE=" + self.headers.get("Title", "") + "\nPRIORITY=" + self.headers.get("Priority", "")
                    + "\nTAGS=" + self.headers.get("Tags", "") + "\n" + self.rfile.read(n).decode())
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
with socketserver.TCPServer(("127.0.0.1", 0), H) as s:
    open(os.path.join(d, "port"), "w").write(str(s.server_address[1]))
    for _ in range(3):
        s.handle_request()
PY
SRV_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP/port" ] && break; sleep 0.1; done
URL="http://127.0.0.1:$(cat "$TMP/port")/topic"
# ⚠ The stand-in server reads the Title header with http.client, which decodes headers as latin-1 per
# RFC 7230, so a UTF-8 mark arrives here as mojibake ("Ã°ÂÂ¨") even though the bytes on the wire are
# correct and the phone renders it. Compare against the latin-1 reading of the same bytes rather than
# against the mark itself, or this asserts the harness's decoding instead of the alert's behaviour.
SIREN_L1="$(printf '\xf0\x9f\x9a\xa8' | iconv -f latin1 -t utf-8 2>/dev/null || printf '\xf0\x9f\x9a\xa8')"
if NTFY_URL="$URL" "$ALERT" "Hazync: test" "hello from the test" >"$TMP/o" 2>&1 \
   && grep -q "^TITLE=$SIREN_L1 Hazync: test" "$TMP/posted1" 2>/dev/null && grep -q "hello from the test" "$TMP/posted1"; then
    note "ok   POSTed body + Title header to the ntfy URL"
else
    bad "the alert did not arrive at the server"; cat "$TMP/o" "$TMP/posted1" 2>/dev/null
fi
grep -q "^PRIORITY=high$" "$TMP/posted1" 2>/dev/null && grep -q "^TAGS=rotating_light$" "$TMP/posted1" \
    && note "ok   by default an alert rings as high priority (unchanged for every existing caller)" \
    || { bad "default priority/tags changed"; cat "$TMP/posted1"; }
NTFY_URL="$URL" ALERT_PRIORITY=low ALERT_TAGS=white_check_mark,floppy_disk "$ALERT" "Hazync backups: all good" "summary" >"$TMP/o" 2>&1
grep -q "^PRIORITY=low$" "$TMP/posted2" 2>/dev/null && grep -q "^TAGS=white_check_mark,floppy_disk$" "$TMP/posted2" \
    && note "ok   ALERT_PRIORITY / ALERT_TAGS reach ntfy (a daily summary does not ring like a failure)" \
    || { bad "ALERT_PRIORITY/ALERT_TAGS were not sent"; cat "$TMP/o" "$TMP/posted2" 2>/dev/null; }

# The mark is chosen from the TAGS, and until now nothing tested that choice — the point of the change
# is that you can tell good news from bad at a glance on a phone, without reading the title. Same
# latin-1 caveat as above, so compare against the header's decoded form.
TICK_L1="$(printf '\xe2\x9c\x85' | iconv -f latin1 -t utf-8 2>/dev/null || printf '\xe2\x9c\x85')"
if grep -q "^TITLE=$TICK_L1 Hazync backups: all good" "$TMP/posted2" 2>/dev/null; then
    note "ok   a good tag (white_check_mark) opens the title with a green tick, not a siren"
else
    bad "good news did not get a green tick — it is indistinguishable from a failure on the phone"
    cat "$TMP/posted2" 2>/dev/null
fi
NTFY_URL="$URL" ALERT_PRIORITY=$'high\r\nX-Evil: 1' ALERT_TAGS='a b;c' "$ALERT" "Hazync: odd" "odd" >"$TMP/o" 2>&1
grep -q "^PRIORITY=high$" "$TMP/posted3" 2>/dev/null && grep -q "^TAGS=abc$" "$TMP/posted3" \
    && note "ok   an unknown priority falls back to high and tags are reduced to safe characters" \
    || { bad "unsanitised priority/tags reached the headers"; cat "$TMP/o" "$TMP/posted3" 2>/dev/null; }
wait "$SRV_PID" 2>/dev/null; SRV_PID=""

echo "== 5. watchdog: a dead coordinator alerts once, then recovers once =="
cat > "$TMP/stub-alert.sh" <<EOF
#!/bin/bash
echo "\$1" >> "$TMP/calls"
EOF
chmod +x "$TMP/stub-alert.sh"
printf '{"method_id": "37987b85", "frontier": 1}' > "$TMP/meta.json"
printf '<html><body>502 Bad Gateway</body></html>' > "$TMP/error.html"
wd() { WATCHDOG_TARGETS="$1" WATCHDOG_STATE_DIR="$TMP/wd" WATCHDOG_FAILS_BEFORE_ALERT=2 \
       WATCHDOG_REALERT_SECS=3600 WATCHDOG_PROBE_TIMEOUT=5 HAZYNC_ALERT="$TMP/stub-alert.sh" "$WD" >/dev/null 2>&1; }
calls() { cat "$TMP/calls" 2>/dev/null | tr '\n' '|'; }
DOWN="coord=http://127.0.0.1:1/api/meta"
wd "$DOWN"
[ -z "$(calls)" ] && note "ok   one failed probe is not an alert (a blip)" || bad "alerted on a single failed probe: $(calls)"
wd "$DOWN"
[ "$(calls)" = "Hazync: coord DOWN|" ] && note "ok   the second consecutive failure alerts DOWN" || bad "expected one DOWN alert, got: $(calls)"
wd "$DOWN"
[ "$(calls)" = "Hazync: coord DOWN|" ] && note "ok   still down inside the re-alert window -> no repeat" || bad "re-alerted every probe: $(calls)"
wd "coord=file://$TMP/meta.json"
[ "$(calls)" = "Hazync: coord DOWN|Hazync: coord RECOVERED|" ] && note "ok   answering again -> one RECOVERED" || bad "expected RECOVERED, got: $(calls)"
wd "coord=file://$TMP/meta.json"
[ "$(calls)" = "Hazync: coord DOWN|Hazync: coord RECOVERED|" ] && note "ok   healthy stays quiet" || bad "a healthy probe alerted: $(calls)"

echo "== 6. POSITIVE CONTROL: a proxy error page is DOWN, not up =="
rm -rf "$TMP/wd" "$TMP/calls"
wd "web=file://$TMP/error.html"; wd "web=file://$TMP/error.html"
[ "$(calls)" = "Hazync: web DOWN|" ] && note "ok   a page without method_id counts as down" \
    || bad "an nginx error page satisfied the probe — the watchdog cannot see a dead coordinator behind a live proxy: $(calls)"

echo "== 7. every alert drop-in points at an alert command that exists =="
for f in dropins/*-alert.conf; do
    grep -q "OnFailure=hazync-alert@%n.service" "$f" || bad "$f has no OnFailure= to hazync-alert@"
done
[ -f hazync-alert@.service ] && grep -q "hazync-alert.sh --unit %i" hazync-alert@.service \
    && note "ok   drop-ins -> hazync-alert@.service -> hazync-alert.sh --unit" || bad "hazync-alert@.service missing or wrong"
for u in hazync-coordinator hazync-bridge litestream; do
    grep -q "ExecStopPost=+/usr/local/bin/hazync-alert.sh --crash %n" "dropins/$u-alert.conf" \
        || bad "$u (Restart=always) has no --crash hook — a crash loop would be silent"
done

echo
if [ "$fail" -ne 0 ]; then echo "Alerting is NOT trustworthy — see failures above."; exit 1; fi
echo "Alerting says something when it should, and only then."
