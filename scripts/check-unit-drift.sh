#!/usr/bin/env bash
# Does the DEPLOYED unit contain anything this repo does not know about? (hazync#168 part C)
#
#   ./scripts/check-unit-drift.sh hazync-coord
#
# WHY THIS DIRECTION. The obvious check is "every path a doc names must exist". That check would
# NOT have caught the incident this script exists for. On 2026-08-25 the production coordinator's
# base unit turned out to be a hand-edited hybrid: dead pre-#58 /root paths sitting alongside two
# settings that were live, load-bearing, and present nowhere in this repo --
#
#     COORD_BIND=0.0.0.0        TRUSTED_PROXIES=83.136.255.218
#
# Replacing that base with the repo copy silently dropped both. COORD_BIND fell back to 127.0.0.1,
# which would have stopped the coordinator accepting the nginx proxy from the web box. Every path
# in the repo's unit existed; the danger ran the other way. So the useful question is not "does
# what we wrote down exist" but "is anything running that we never wrote down".
#
# systemd makes the failure silent twice over: `systemctl cat` prints superseded lines as if they
# were live, and `daemon-reload` does not restart, so a bad edit detonates at some arbitrary later
# restart with nothing wrong in any log.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

HOST="${1:-}"
[ -n "$HOST" ] || { echo "usage: $0 <ssh-host>   e.g. $0 hazync-coord" >&2; exit 2; }
DROPINS_DIR=coordinator/deploy/dropins
UNITS="${HAZYNC_UNITS:-hazync-coordinator hazync-bridge}"
ALLOW="${HAZYNC_DRIFT_ALLOW:-coordinator/deploy/unit-drift-allow.txt}"

fail=0
note() { echo "  $*"; }
bad()  { echo "DRIFT $*"; fail=1; }

# The remote side is read-only and answers in one round trip per unit: `systemctl show` rather than
# `systemctl cat`, because cat prints the FILE and show prints what actually runs.
for u in $UNITS; do
    echo
    echo "=== $u on $HOST ==="

    remote=$(ssh -n -o ConnectTimeout=15 "$HOST" "
        systemctl show $u -p Environment --value | tr ' ' '\n' | grep -v '^\$' | sed 's/^/ENV /'
        systemctl show $u -p ExecStart --value | grep -oE 'argv\[\]=[^;]*' | sed 's/^/EXEC /'
        systemctl show $u -p User --value | sed 's/^/USER /'
        ls -1 /etc/systemd/system/$u.service.d/ 2>/dev/null | sed 's/^/DROPIN /'
    " 2>/dev/null)
    if [ -z "$remote" ]; then bad "$u: could not read unit state from $HOST (unreachable, or unit absent)"; continue; fi

    # --- 1. drop-in FILES the repo does not ship ------------------------------------------------
    # A drop-in nobody has committed is config that exists only on one disk. `ratelimit.conf` was
    # exactly this: it is what holds RATE_MAX at 120 rather than the base unit's 1000000, and
    # rebuilding the box from this repo would have quietly restored the million.
    while read -r _ f; do
        [ -n "${f:-}" ] || continue
        if [ ! -f "$DROPINS_DIR/$u-$f" ]; then
            bad "$u: drop-in '$f' is on the box but NOT in $DROPINS_DIR/$u-$f"
        else
            note "ok   drop-in $f is declared"
        fi
    done < <(printf '%s\n' "$remote" | grep '^DROPIN ')

    # --- 2. effective settings the repo cannot account for --------------------------------------
    # Union, not precedence: we are asking "could the repo have produced this value at all", which
    # is the question that matters and needs no simulation of systemd's override order.
    declared=$( { cat "coordinator/deploy/$u.service" 2>/dev/null
                  cat "$DROPINS_DIR/$u-"*.conf 2>/dev/null; } \
                | grep -E '^Environment=' | sed 's/^Environment=//' | sort -u )
    while read -r _ kv; do
        [ -n "${kv:-}" ] || continue
        k="${kv%%=*}"
        if printf '%s\n' "$declared" | grep -qxF "$kv"; then
            continue                                   # exact key=value is in the repo
        elif printf '%s\n' "$declared" | grep -q "^$k="; then
            note "note $k differs from every declared value (per-box override)"
        elif [ -f "$ALLOW" ] && grep -qxF "$k" "$ALLOW"; then
            note "ok   $k is an accepted per-box setting (see $(basename "$ALLOW"))"
        else
            bad "$u: $k is set on the box and appears NOWHERE in the repo   ($kv)"
        fi
    done < <(printf '%s\n' "$remote" | grep '^ENV ')

    # --- 3. the binary it actually executes -----------------------------------------------------
    exec_line=$(printf '%s\n' "$remote" | grep '^EXEC ' | head -1 | sed 's/^EXEC argv\[\]=//')
    note "runs: ${exec_line:-<none>}   (user: $(printf '%s\n' "$remote" | grep '^USER ' | head -1 | cut -d' ' -f2-))"
done

echo
if [ "$fail" = 0 ]; then
    echo "no drift: everything running on $HOST is declared in this repo."
else
    echo "DRIFT FOUND — the box is running configuration this repo does not contain."
    echo "Fix by committing it (a drop-in under $DROPINS_DIR), not by deleting it from the box:"
    echo "a setting that is live and undeclared is load-bearing until proven otherwise."
fi
exit $fail
