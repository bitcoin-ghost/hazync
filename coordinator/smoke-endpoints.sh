#!/bin/bash
# Hit EVERY coordinator endpoint against a throwaway instance.
#
# Written after a deploy shipped a broken /api/meta. The change was tested live before deploying and
# passed — because the test hit /api/claim, /api/hint and /api/submit, and meta was never requested.
# A partial smoke test is how a removal takes collateral with it and nothing notices: deleting the
# claim handlers also deleted a module-level `_MID_CACHE` that sat between them, and only /api/meta
# used it.
#
# So this enumerates endpoints rather than sampling them, and asserts a RESPONSE, not merely a
# non-crash — HTTP 000 (connection died mid-request) is the exact shape the bug produced.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
PORT="${SMOKE_PORT:-8937}"
T=$(mktemp -d); trap 'kill %1 2>/dev/null; rm -rf "$T"' EXIT

# A stub prover, so /api/meta's real code path runs. Without HAZYNC_HOST the handler legitimately
# reports no id, and the test would then be asserting nothing about the path that actually broke.
printf '#!/bin/sh\necho METHOD_ID 717905842bb012db8c2e62804e68c30b05cb1f08091dd903b85c27bc894af490\n' > "$T/host"
chmod +x "$T/host"

COORD_DB="$T/c.db" COORD_PROOFS="$T/p" COORD_STATE="$T/s" WITNESS="$T/w" BRIDGE_DIR="$T/b" \
COORD_PORT="$PORT" COORD_BIND=127.0.0.1 VERIFY_MODE=real SEED_RANGES=0 TIP_HEIGHT=1000 \
 HAZYNC_HOST="$T/host" \
  python3 server.py > "$T/log" 2>&1 &
for _ in $(seq 30); do curl -s -o /dev/null "http://127.0.0.1:$PORT/api/meta" && break; sleep 0.5; done

fail=0
chk() { # name method path expected-code
    local code
    if [ "$2" = POST ]; then
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
               -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:$PORT$3")
    else
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:$PORT$3")
    fi
    if [ "$code" = "$4" ]; then
        printf '  ok   %-22s %s\n' "$1" "$code"
    else
        printf '  FAIL %-22s got %s, want %s\n' "$1" "$code" "$4"; fail=1
    fi
}

# Live endpoints. 000 means the handler raised and the connection died — the failure this exists for.
chk "GET  /api/meta"      GET  /api/meta        200
chk "GET  /api/state"     GET  /api/state       200
chk "GET  /api/state?slim" GET "/api/state?slim=1" 200
chk "GET  /api/vranges"   GET  /api/vranges     200
# 409, not 200: this throwaway instance serves no witnesses, so there is genuinely nothing to claim
# and refusing is the right answer. What is being asserted is that the endpoint EXISTS and answers —
# a 404 here would mean claims had been removed again.
chk "POST /api/claim"     POST /api/claim       409
chk "POST /api/submit"    POST /api/submit      400   # empty body -> validation error, not a crash
chk "GET  /"              GET  /                200

# Claims are back at WIDTH 1 with a TTL, but the heartbeat/release machinery is NOT — a claim expires
# on its own, so there is nothing to keep alive and nothing to hand back. If either returns, the
# orphaned-claim failure modes have come back with it.
chk "POST /api/heartbeat" POST /api/heartbeat   404
chk "POST /api/release"   POST /api/release     404

# /api/meta must actually carry a guest id — a 200 with an empty body would pass a status-only check
# while telling every worker nothing, and `hazync selftest` compares against this field.
mid=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/api/meta" | grep -o '"method_id": *"[0-9a-f]\{64\}"' | head -1)
if [ -n "$mid" ]; then echo "  ok   meta carries a 64-hex method_id"
else echo "  FAIL /api/meta has no method_id — workers cannot check their guest"; fail=1; fi

[ $fail -eq 0 ] && echo "all endpoints respond correctly"
exit $fail
