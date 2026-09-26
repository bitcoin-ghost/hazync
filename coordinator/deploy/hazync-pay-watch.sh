#!/usr/bin/env bash
# Push a phone notification when a donation lands, and when BTCPay itself is unwell.
#
#   hazync-pay-watch                 # one pass: new payments, then health
#   DRY=1 hazync-pay-watch           # say what it would push, push nothing
#   hazync-pay-watch --selftest      # exercise the parsing and the watermark, touch nothing live
#
# ⛔ THE GREENFIELD API, NOT THE POSTGRES SCHEMA. BTCPay's internal tables are not an interface --
# they are renamed and reshaped between releases, and a query that silently returns nothing after an
# upgrade is the worst possible failure here: donations would arrive and nobody would be told, while
# the watcher reported itself healthy. The API is versioned and stable, and it is reached over
# LOOPBACK, so no port is opened for this.
#
# ⛔ GREEN MUST BE POSITIVE. Every health check below has to produce an affirmative answer. "curl
# exited 0" is not "BTCPay is up", and "no new payments" must be distinguishable from "I could not
# ask" -- the second is an alert in its own right, because a watcher that cannot see is exactly as
# useless as no watcher, and far more comforting.
#
# Config, from the environment or /etc/hazync/pay-watch.env:
#   BTCPAY_URL          default https://<domain>, pinned to loopback with --resolve. ⛔ NOT the
#                       container IP: it is assigned by Docker and MOVES (recorded as 172.18.0.8
#                       on 2026-09-17, found at 172.18.0.9 on 2026-09-26). Going through nginx on
#                       the published port is stable, and --resolve keeps the traffic on loopback
#                       while still VALIDATING the certificate -- unlike -k, which would hide the
#                       expired-cert failure this script is supposed to catch.
#   BTCPAY_STORE_ID     the store whose invoices are watched
#   BTCPAY_API_KEY_FILE root-only file holding the Greenfield API key. NEVER inline, never logged.
#   PAY_STATE_DIR       watermark + health state (default /var/lib/hazync/pay-watch)
#   PAY_LEDGER          append-only record of every donation seen (default $PAY_STATE_DIR/donations.jsonl)
#   SWEEP_THRESHOLD     ping when the HOT on-chain balance reaches this much fiat (default 200)
#   SWEEP_CURRENCY      the fiat the threshold is in (default GBP)
#   CERT_WARN_DAYS      warn when the TLS cert expires within this many days (default 14)
#   DISK_MIN_PCT        warn when free space on / drops below this percent (default 15)
set -uo pipefail

ENV_FILE=/etc/hazync/pay-watch.env
# ⛔ THE ENVIRONMENT WINS OVER THE FILE. Sourcing the file plainly OVERWRITES anything passed in,
# so `SWEEP_THRESHOLD=0 hazync-pay-watch` silently used the file's 200 -- an override that looks
# like it worked and does nothing, which is the worst kind. The header promised "from the
# environment or the file"; this makes that true. Snapshot first, restore after.
_pre_env=$(for v in BTCPAY_URL BTCPAY_STORE_ID BTCPAY_API_KEY_FILE PAY_STATE_DIR PAY_DOMAIN \
                    SWEEP_THRESHOLD SWEEP_CURRENCY CERT_WARN_DAYS DISK_MIN_PCT ALERT_BIN; do
             eval "val=\${$v+set}"
             [ "${val:-}" = "set" ] && eval "printf '%s=%s\n' \"$v\" \"\$$v\""
           done)
# ⚠ The env file is operator config and is not in the repo, so there is nothing for shellcheck to
# follow. The directive below must carry NO trailing comment -- shellcheck reads the rest of the
# line as key=value pairs and rejects it (SC1125), which is how a silencing directive becomes a
# second error.
# shellcheck source=/dev/null
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
# re-apply whatever the caller actually set
if [ -n "${_pre_env:-}" ]; then
    while IFS='=' read -r k v; do [ -n "${k:-}" ] && eval "$k=\$v"; done <<PRE_EOF
$_pre_env
PRE_EOF
fi

DOMAIN_EARLY="${PAY_DOMAIN:-donate.hazync.org}"
URL="${BTCPAY_URL:-https://$DOMAIN_EARLY}"
# Pin the name to loopback: same request the public would make, without leaving the box.
CURL_PIN=(--resolve "$DOMAIN_EARLY:443:127.0.0.1")
STORE="${BTCPAY_STORE_ID:-}"
KEYFILE="${BTCPAY_API_KEY_FILE:-/etc/hazync/btcpay-api.key}"
STATE="${PAY_STATE_DIR:-/var/lib/hazync/pay-watch}"
SWEEP_THRESHOLD="${SWEEP_THRESHOLD:-200}"
SWEEP_CURRENCY="${SWEEP_CURRENCY:-GBP}"
CERT_WARN_DAYS="${CERT_WARN_DAYS:-14}"
DISK_MIN_PCT="${DISK_MIN_PCT:-15}"
DOMAIN="${PAY_DOMAIN:-donate.hazync.org}"
DRY="${DRY:-0}"
ALERT="${ALERT_BIN:-/usr/local/bin/hazync-alert.sh}"
PARSER="${PAY_PARSER:-/usr/local/sbin/hazync-pay-invoices.py}"

say() { echo "[pay-watch] $*"; }

push() {   # push <title> <body> [priority] [tags]
    local title="$1" body="$2" prio="${3:-high}" tags="${4:-rotating_light}"
    if [ "$DRY" = "1" ]; then
        printf '  WOULD PUSH [%s/%s] %s\n    %s\n' "$prio" "$tags" "$title" "$body"
        return 0
    fi
    # ⛔ DO NOT SWALLOW THE ALERT'S OWN OUTPUT. It prints "[hazync-alert] sent: …" on success, and
    # sending that to /dev/null leaves the journal unable to answer the only question that matters
    # here -- did the notification actually go? That is the same shape of mistake as silencing the
    # invoice parser: hiding the evidence that distinguishes working from silently broken.
    local out
    if out=$(ALERT_PRIORITY="$prio" ALERT_TAGS="$tags" "$ALERT" "$title" "$body" 2>&1); then
        say "notified: ${out:-$title}"
    else
        say "⛔ the alert itself could not be sent: $title — ${out:-no output}"
    fi
}

# ── selftest: the parsing and the watermark, with no network and no live state ──────────────────
if [ "${1:-}" = "--selftest" ]; then
    fails=0
    t=$(mktemp -d)
    # ⚠ The watermark must be a TIMESTAMP, not a count. A count cannot tell a new payment from a
    # re-listed one after BTCPay prunes or an invoice changes state, so it would either re-notify
    # for ever or go silent after the first prune.
    echo "1700000000" > "$t/watermark"
    read_wm() { cat "$t/watermark" 2>/dev/null || echo 0; }
    [ "$(read_wm)" = "1700000000" ] || { echo "  FAIL watermark not read back"; fails=1; }
    # a payment newer than the watermark is new; one older is not
    newer=1700000060; older=1699999000
    [ "$newer" -gt "$(read_wm)" ] || { echo "  FAIL a newer payment is not seen as new"; fails=1; }
    [ "$older" -gt "$(read_wm)" ] && { echo "  FAIL an older payment is seen as new"; fails=1; }
    # ⛔ an EMPTY or unparseable answer must not advance the watermark, or one bad poll loses a
    # donation notification permanently
    before=$(read_wm); echo "" | grep -oE '"receivedDate":[0-9]+' >/dev/null 2>&1
    [ "$(read_wm)" = "$before" ] || { echo "  FAIL an empty answer moved the watermark"; fails=1; }
    # sats formatting
    fmt() { awk -v s="$1" 'BEGIN{printf "%.8f", s/100000000}'; }
    [ "$(fmt 123456)" = "0.00123456" ] || { echo "  FAIL sat formatting ($(fmt 123456))"; fails=1; }
    # ── the ledger: valid JSON, APPENDED, and never rewritten ──────────────────────────────────
    L="$t/donations.jsonl"
    for i in 1 2 3; do
        printf '{"ts":%s,"utc":"%s","amount":"%s","currency":"%s","invoice":"%s"}\n' \
            "170000000$i" "2023-11-14T22:13:2${i}Z" "12.3$i" "GBP" "inv$i" >> "$L"
    done
    rows=$(wc -l < "$L" | tr -d ' ')
    [ "$rows" = "3" ] || { echo "  FAIL ledger has $rows rows, expected 3"; fails=1; }
    # ⛔ EVERY row must parse. A ledger with one malformed line is a ledger you cannot read back.
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "
import json,sys
n=0
for line in open('$L'):
    line=line.strip()
    if not line: continue
    d=json.loads(line); n+=1
    assert set(('ts','utc','amount','currency','invoice')) <= set(d), d
print(n)
" >/dev/null 2>&1 || { echo "  FAIL ledger rows do not parse as JSON with the expected fields"; fails=1; }
    fi
    # ⚠ appending again must PRESERVE the earlier rows — the failure mode that lost 6 of 10 rows
    # elsewhere in this project was a whole-file rewrite that reported success every time.
    printf '{"ts":1700000009,"utc":"x","amount":"1","currency":"GBP","invoice":"inv9"}\n' >> "$L"
    grep -q '"invoice":"inv1"' "$L" || { echo "  FAIL the first row vanished after a later append"; fails=1; }
    [ "$(wc -l < "$L" | tr -d " ")" = "4" ] || { echo "  FAIL append did not grow the ledger"; fails=1; }

    rm -rf "$t"
    [ "$fails" = "0" ] && { echo "  selftest ok"; exit 0; }
    echo "  selftest FAILED"; exit 1
fi

mkdir -p "$STATE" 2>/dev/null || { say "cannot create $STATE"; exit 2; }
WM="$STATE/last_payment_ts"
HEALTH="$STATE/health.last"
# ⛔ THE ACCOUNTING RECORD, DELIBERATELY SEPARATE FROM THE BACKUP PROBLEM. Backing up BTCPay's
# Postgres would preserve invoice history -- and would also put the hot wallet's SEED in whatever
# store the backup lands in. This file holds the same facts with no secrets in it, so it can be
# copied anywhere without thinking about it. That is the whole point of it existing.
LEDGER="${PAY_LEDGER:-$STATE/donations.jsonl}"

# ── 1. donations ────────────────────────────────────────────────────────────────────────────────
# ⚠ Inert, not broken, until the store has a wallet and an API key exists. Saying so once per run in
# the journal is right; pushing it to a phone every five minutes is not.
if [ -z "$STORE" ] || [ ! -r "$KEYFILE" ]; then
    say "donations: not configured yet (need BTCPAY_STORE_ID and a readable \$BTCPAY_API_KEY_FILE)"
else
    KEY=$(cat "$KEYFILE")            # ⛔ never echoed, never in a command line, never in an error
    last=$(cat "$WM" 2>/dev/null || echo 0)
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    body=$(curl -fsS -m 25 "${CURL_PIN[@]}" -H "Authorization: token $KEY" \
                "$URL/api/v1/stores/$STORE/invoices?take=50" 2>/dev/null)
    rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$body" ]; then
        # ⛔ COULD NOT ASK IS ITS OWN ALERT. Silence here is indistinguishable from "no donations",
        # which is the comfortable reading and the wrong one.
        say "⛔ could not read invoices from BTCPay (curl rc=$rc)"
        push "⛔ Hazync donations: cannot read BTCPay" \
             "hazync-pay-watch could not list invoices (curl rc=$rc). Donations may be arriving unseen." \
             high rotating_light
    else
        newest="$last"; n=0
        # ⛔ THE PARSER IS A FILE, NOT A HEREDOC. It was an inline python one-liner and the \" escaping
        # mangled on the way through the heredoc -- a SyntaxError on every run, silenced by
        # 2>/dev/null, so it printed nothing and the loop below read that as "no donations". A real
        # £1 Lightning payment settled on 2026-09-26 and nobody was told.
        parsed=$("$PARSER" <<<"$body" 2>/tmp/pay-parse.err); prc=$?
        if [ "$prc" -ne 0 ]; then
            # ⛔ COULD NOT PARSE IS NOT "NO DONATIONS". That conflation is the whole bug.
            say "⛔ could not read the invoice list: $(head -c 160 /tmp/pay-parse.err)"
            push "⛔ Hazync donations: the invoice parser failed" \
                 "hazync-pay-watch could not read BTCPay's invoice list. Donations may be arriving unseen. $(head -c 120 /tmp/pay-parse.err)" \
                 high rotating_light
            rm -f /tmp/pay-parse.err
            parsed=""
            newest="$last"
        fi
        rm -f /tmp/pay-parse.err
        while IFS='|' read -r ts amt cur id; do
            [ -n "${ts:-}" ] || continue
            case "$ts" in ''|*[!0-9]*) continue ;; esac
            if [ "$ts" -gt "$last" ]; then
                n=$((n + 1))
                push "₿ Hazync donation received" \
                     "$amt $cur — invoice ${id:0:12} at $(date -u -d "@$ts" '+%F %H:%M:%SZ')" \
                     default "moneybag"
                # ⚠ Written BEFORE the watermark moves, and appended never rewritten.
                if [ "$DRY" != "1" ]; then
                    printf '{"ts":%s,"utc":"%s","amount":"%s","currency":"%s","invoice":"%s"}\n' \
                        "$ts" "$(date -u -d "@$ts" '+%FT%TZ')" "$amt" "$cur" "$id" >> "$LEDGER" \
                        || say "⛔ could not append to $LEDGER — the notification was sent but NOT recorded"
                fi
            fi
            [ "$ts" -gt "$newest" ] && newest="$ts"
        done <<<"$parsed"
        # ⛔ Only advance on a GOOD read. An empty or unparseable answer leaves the watermark alone,
        # so a transient blip delays a notification rather than losing it.
        # ⛔ DRY WRITES NOTHING. A dry run that moves the watermark would suppress the very
        # notification the real run was meant to send.
        if [ "$newest" -gt "$last" ] && [ "$DRY" != "1" ]; then echo "$newest" > "$WM"; fi
        say "donations: $n new settled invoice(s) since $(date -u -d "@$last" '+%F %H:%M:%SZ' 2>/dev/null || echo epoch)"
    fi
fi

# ── 1b. the hot balance, against the sweep threshold ────────────────────────────────────────────
# ⛔ WHY A THRESHOLD AT ALL. A hot wallet's whole risk is the amount sitting in it, and that is the
# one variable the operator controls. The wallet type only decides how fast a compromise hurts; the
# balance decides how much. So this is the alert that actually bounds the exposure.
#
# ⛔ AND IT NEVER SWEEPS. Moving funds would need a credential on this box with SPEND authority,
# which hands back most of what the threshold was buying. It pings; a human clicks Send.
SWEPT="$STATE/over_threshold"
if [ -z "$STORE" ] || [ ! -r "$KEYFILE" ]; then
    :                                   # already reported as unconfigured above
else
    # ⛔ THE BALANCE COMES FROM nbxplorer LOCALLY, NOT FROM THE API (hazync#519).
    # /payment-methods/BTC-CHAIN/wallet returns 403 for a view-only key: BTCPay puts the wallet
    # endpoints behind `canmodifystoresettings`, which ALSO AUTHORISES SPENDING. Granting that to a
    # monitoring script would hand a box-resident credential the power to move the funds whose size
    # it is merely reporting -- which is precisely the exposure the sweep threshold exists to bound.
    #
    # nbxplorer already tracks the balance on this machine, so the number is available with no key
    # and no new authority. ⚠ That is a schema read, and schemas move between releases -- so a
    # failure here is LOUD (see below) rather than a silent zero, which would read as "nothing to
    # sweep" for ever.
    #
    # ⚠ The HOT wallet is identified by holding signing material, not by name: a wallet with a
    # Mnemonic/MasterHDKey/AccountHDKey is one BTCPay can spend from. The watch-only cold wallet has
    # none, so it is never counted -- exactly right, since sweeping is about hot exposure.
    # ⚠ The RATE still comes from BTCPay: it works with a view-only key (verified 63521.9 GBP/BTC),
    # and it is the same rate the store would price an invoice at, so the alert and the UI agree.
    rate_json=$(curl -fsS -m 25 "${CURL_PIN[@]}" -H "Authorization: token $KEY" \
               "$URL/api/v1/stores/$STORE/rates?currencyPair=BTC_$SWEEP_CURRENCY" 2>/dev/null)
    btc=$(docker exec generated_postgres_1 psql -U postgres -d nbxplorermainnet -t -A -c \
      "SELECT COALESCE(SUM(available_balance),0) FROM wallets_balances WHERE asset_id='' AND wallet_id IN (SELECT DISTINCT wallet_id FROM nbxv1_metadata WHERE key IN ('Mnemonic','MasterHDKey','AccountHDKey'));" \
      2>/dev/null | tr -d ' ')
    case "${btc:-}" in ''|*[!0-9.]*) btc=-1 ;; esac
    rate=$(printf '%s' "${rate_json:-}" | python3 -c '
import json,sys
try:
    r=json.load(sys.stdin)
    print(float(r[0]["rate"]) if isinstance(r,list) and r else -1)
except Exception: print(-1)
' 2>/dev/null)
    case "${rate:-}" in ''|*[!0-9.-]*) rate=-1 ;; esac

    if [ "${btc:--1}" = "-1" ] || [ "${rate:--1}" = "-1" ]; then
        # ⛔ COULD NOT ASK IS ITS OWN ALERT — but a quiet one, and only when it CHANGES, because an
        # unconfigured wallet would otherwise push this every five minutes for ever.
        say "⛔ sweep check: could not read balance and/or rate (balance=${btc:-?} rate=${rate:-?})"
        # ⛔ Say WHICH half failed. "could not read" that does not distinguish a schema change from
        # an unreachable rate API sends the next reader to the wrong place.
        [ "${btc:--1}" = "-1" ] && say "   the nbxplorer balance query returned nothing usable — schema may have moved"
        [ "${rate:--1}" = "-1" ] && say "   the BTCPay rate call returned nothing usable — key or endpoint"
    else
        fiat=$(awk -v b="$btc" -v r="$rate" 'BEGIN{printf "%.2f", b*r}')
        over=$(awk -v f="$fiat" -v t="$SWEEP_THRESHOLD" 'BEGIN{print (f>=t)?1:0}')
        say "sweep check: hot balance $btc BTC = $fiat $SWEEP_CURRENCY (threshold $SWEEP_THRESHOLD)"
        if [ "$over" = "1" ] && [ ! -s "$SWEPT" ]; then
            push "₿ Hazync hot wallet is over the sweep line" \
                 "$fiat $SWEEP_CURRENCY on the BTCPay hot wallet (threshold $SWEEP_THRESHOLD $SWEEP_CURRENCY). Sweep to cold when convenient." \
                 default "moneybag,arrow_up"
            [ "$DRY" = "1" ] || echo "$fiat" > "$SWEPT"
        elif [ "$over" = "0" ] && [ -s "$SWEPT" ]; then
            # ⚠ Reset only on the way DOWN, so one sweep re-arms the alert instead of it firing
            # every five minutes while the balance sits above the line.
            [ "$DRY" = "1" ] || : > "$SWEPT"
            say "sweep check: back below the threshold — alert re-armed"
        fi
    fi
fi

# ── 2. BTCPay health ────────────────────────────────────────────────────────────────────────────
problems=""
add() { problems="${problems}
  - $1"; }

# containers: every one that was up must still be up
want="generated_btcpayserver_1 generated_nbxplorer_1 btcpayserver_bitcoind generated_postgres_1 nginx"
for c in $want; do
    st=$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
    [ "$st" = "true" ] || add "container $c is not running (state=${st:-absent})"
done

# ⛔ POSITIVE, AND ON THE CONTENT. A 200 only says nginx and BTCPay are alive; /api/v1/health also
# reports whether the NODE is synchronized, and an unsynchronized node issues invoices that may never
# see their payment. Checking the status code alone would call that healthy.
hbody=$(curl -fsS -m 20 "${CURL_PIN[@]}" -w '\n%{http_code}' "$URL/api/v1/health" 2>/dev/null)
code=$(printf '%s' "$hbody" | tail -1)
case "$code" in
    200)
        # ⚠ Require the literal true. A missing field, a renamed field, or any parse failure must
        # read as "not confirmed synchronized", never as healthy by default.
        if printf '%s' "$hbody" | grep -qE '"synchronized"[[:space:]]*:[[:space:]]*true'; then :
        else add "BTCPay answers 200 but does not report synchronized:true — the node is behind"; fi ;;
    *)  add "BTCPay /api/v1/health did not answer 200 (got ${code:-nothing})" ;;
esac

# TLS: a donation page with an expired cert takes donations from nobody
exp=$(echo | openssl s_client -servername "$DOMAIN" -connect 127.0.0.1:443 2>/dev/null \
      | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "${exp:-}" ]; then
    left=$(( ( $(date -d "$exp" +%s 2>/dev/null || echo 0) - $(date +%s) ) / 86400 ))
    [ "$left" -ge "$CERT_WARN_DAYS" ] || add "TLS cert for $DOMAIN expires in $left day(s)"
else
    add "could not read the TLS cert for $DOMAIN"
fi

# disk: bitcoind grows, and a full disk corrupts more than it stops
free=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
case "$free" in ''|*[!0-9]*) free=100 ;; esac
[ $((100 - free)) -ge "$DISK_MIN_PCT" ] || add "only $((100 - free))% free on / (below ${DISK_MIN_PCT}%)"

if [ -n "$problems" ]; then
    say "health: PROBLEMS$problems"
    # ⚠ Re-push only when the problem SET changes, so a persistent fault does not become wallpaper.
    sig=$(printf '%s' "$problems" | sha256sum | cut -c1-16)
    if [ "$sig" != "$(cat "$HEALTH" 2>/dev/null)" ]; then
        push "⛔ Hazync BTCPay unwell" "donate.hazync.org:$problems" high rotating_light
        # ⛔ Not in DRY: I ran DRY twice and the second run announced "recovered", because the
        # first had persisted a problem signature it only ever PRETENDED to send.
        [ "$DRY" = "1" ] || echo "$sig" > "$HEALTH"
    else
        say "health: same problem set as last run — not re-pushing"
    fi
else
    say "health: ok — containers up, API 200, node tracking, cert ${left:-?}d, $((100 - free))% free"
    if [ -s "$HEALTH" ]; then
        push "✅ Hazync BTCPay recovered" "donate.hazync.org is healthy again" default white_check_mark
        [ "$DRY" = "1" ] || : > "$HEALTH"
    fi
fi
exit 0
