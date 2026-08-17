#!/usr/bin/env bash
# Regression tests for hazync#123 — a failing or hanging offsite copy must not take the local backup
# down with it.
#
# The bug was not that the offsite copy failed. It was that local retention was the LAST step, after the
# network work, so when the remote wedged, rotation never ran: 15 full snapshots (78 GB against an
# 8.6 GB live store) accumulated behind a stalled rsync, and because a systemd timer will not start a
# unit that is already `activating`, every subsequent backup silently stopped.
#
# So the assertions here are about ORDER and BOUNDEDNESS, not about the happy path:
#   * local rotation happens even when the offsite target is unwritable
#   * every network call carries a timeout
#   * the unit bounds its own runtime
#
# Run: bash coordinator/test-backup-hang.sh     (silent success, non-zero exit on failure)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SH="$HERE/deploy/backup.sh"
UNIT="$HERE/deploy/hazync-coordinator-backup.service"
FAILS=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS+1)); }
check(){ if [ "$1" = "0" ]; then ok "$2"; else bad "$2"; fi; }

# The ledger is created with python3's built-in sqlite3 module rather than the sqlite3 CLI. backup.sh
# itself degrades to `cp` when the CLI is absent, so requiring it here would make this whole file skip
# on any box without it — which is exactly the shape of a test that cannot fail.
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available"; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

make_env() {
    rm -rf "$WORK/db" "$WORK/proofs" "$WORK/out"
    mkdir -p "$WORK/proofs" "$WORK/out"
    rm -f "$WORK/coordinator.db"
    python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('create table if not exists ranges(id text)')
c.execute(\"insert into ranges values('x')\")
c.commit(); c.close()
" "$WORK/coordinator.db"
    printf 'receipt\n' > "$WORK/proofs/proof_1.bin"
    # Pre-existing old snapshots that rotation must remove. Distinct mtimes so `ls -t` is deterministic.
    for i in 1 2 3 4 5; do
        mkdir -p "$WORK/out/2026010${i}T000000Z"
        touch -d "2026-01-0${i} 00:00:00" "$WORK/out/2026010${i}T000000Z"
    done
}

run_backup() {   # run_backup <remote>  -> echoes rc, output in $WORK/log
    COORD_DB="$WORK/coordinator.db" COORD_PROOFS="$WORK/proofs" BACKUP_DIR="$WORK/out" \
    BACKUP_KEEP=2 BACKUP_REMOTE="$1" HZ_HOME="$WORK" HZ_REPO="$(cd "$HERE/.." && pwd)" \
    bash "$SH" > "$WORK/log" 2>&1
    echo $?
}

# ── 1. the regression: an UNWRITABLE offsite target must not prevent local rotation ────────────────
make_env
BAD="$WORK/nope/does/not/exist"          # bare local path that cannot be created
rc=$(run_backup "$BAD")
snaps=$(find "$WORK/out" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
if [ "$snaps" -le 2 ]; then
    ok "local rotation ran despite the offsite copy failing (snapshots=$snaps, KEEP=2)"
else
    bad "offsite failure left $snaps local snapshots — rotation was skipped (the #123 bug)"
fi
grep -q "kept newest 2 local snapshots" "$WORK/log" \
    && ok "rotation is reported before the offsite step" \
    || bad "no rotation line in output"
# The newest snapshot (the one just written) must survive rotation.
[ -n "$(find "$WORK/out" -maxdepth 1 -mindepth 1 -type d -newermt '2026-06-01')" ] \
    && ok "the snapshot just written survives rotation" \
    || bad "rotation deleted the snapshot it had just created"

# ── 2. a WORKING bare-local offsite target still behaves ───────────────────────────────────────────
make_env
GOOD="$WORK/offsite"; mkdir -p "$GOOD"
rc=$(run_backup "$GOOD")
check "$rc" "a healthy run exits 0 (rc=$rc)"
[ -n "$(find "$GOOD" -maxdepth 1 -mindepth 1 -type d)" ] \
    && ok "offsite snapshot was written" \
    || bad "offsite target is empty after a healthy run"
snaps=$(find "$WORK/out" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
[ "$snaps" -le 2 ] && ok "local rotation also runs on the happy path (snapshots=$snaps)" \
                   || bad "happy path left $snaps local snapshots"

# ── 3. every network call is bounded ───────────────────────────────────────────────────────────────
# Strip comments FIRST. Grepping the raw file matches the prose above these calls, which describes the
# very thing being checked for — a scanner that flags its own documentation is worse than no scanner,
# because the noise is indistinguishable from a real finding.
CODE="$WORK/code.sh"
sed 's/[[:space:]]*#.*$//' "$SH" | grep -vE '^[[:space:]]*$' > "$CODE"

scan() {  # scan <name> <call-regex> <required-flag-regex>
    local name="$1" call="$2" need="$3" n=0 line
    while IFS= read -r line; do
        printf '%s\n' "$line" | grep -qE "$need" || { n=$((n+1)); echo "    unbounded: $(echo "$line" | sed 's/^ *//')"; }
    done < <(grep -E "$call" "$CODE")
    [ "$n" -eq 0 ] && ok "no $name invocation lacks a timeout" \
                   || bad "$n $name invocation(s) missing a timeout"
}

# Accepting the mere PRESENCE of "$RSYNC_OPTS" is not enough: emptying the array of its timeout leaves
# every call site looking correct while nothing is bounded. Verify the definition carries the flag
# before trusting call sites that defer to it. (Found by mutation: deleting --timeout= from the array
# left this file entirely green.)
grep -qE '^RSYNC_OPTS=\(.*--timeout=' "$CODE" \
    && ok "RSYNC_OPTS itself defines --timeout" \
    || bad "RSYNC_OPTS carries no --timeout — call sites using it are unbounded"

scan rsync  '(^|[^-_[:alnum:]])rsync '            '(--timeout=|RSYNC_OPTS)'
scan rclone '(^|[^-_[:alnum:]])rclone (copy|lsf|purge)' '\-\-timeout'
scan ssh    '(^|[^-_[:alnum:]])ssh .*BatchMode=yes'     'ConnectTimeout'

# ── 4. the unit bounds its own runtime ─────────────────────────────────────────────────────────────
# Type=oneshot defaults TimeoutStartSec to infinity, so this must be set EXPLICITLY.
grep -qE '^TimeoutStartSec=' "$UNIT" \
    && ok "unit sets TimeoutStartSec (oneshot defaults to infinity)" \
    || bad "unit has no TimeoutStartSec — a stall can still wedge it forever"

if [ "$FAILS" -ne 0 ]; then
    echo "backup-hang: $FAILS FAILED"
    exit 1
fi
echo "backup-hang: all checks passed"
