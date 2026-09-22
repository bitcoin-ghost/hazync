#!/usr/bin/env bash
# A backup that cannot be verified must FAIL LOUDLY, not be announced as written (hazync#461).
#
# ⛔ WHY THIS EXISTS. The checksum step was `sha256sum ./* > SHA256SUMS 2>/dev/null || true` — stderr
# discarded, exit status thrown away. A failure there (disk full, a permission change, a cd that did
# not happen) produced an absent or empty SHA256SUMS while the script printed "[backup] wrote ..."
# and "[backup] done" and the timer went green. RUNBOOK.md documents the restore check as
# `sha256sum -c SHA256SUMS`, so the one moment anyone would discover it is mid-restore, mid-incident.
#
#   ./test_backup_verifiable.sh            # must PASS
#   ./test_backup_verifiable.sh --control  # checksumming is sabotaged; the backup MUST refuse
#
# The control is not a mutation of the script: it breaks `sha256sum` itself, via PATH. That is the
# real hazard — the tool failing on the night it matters — rather than a rewrite that proves only
# that the test can edit a file.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/deploy/backup.sh"
CONTROL=0
[ "${1:-}" = "--control" ] && CONTROL=1

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fails=0
check() {  # check <ok:0|1> <what>
    if [ "$1" -eq 0 ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi
}

# A real coordinator ledger: the script checks the SQLite magic header AND the `ranges` table, so a
# touched file will not do.
python3 - "$WORK/coordinator.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("CREATE TABLE ranges(id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, status TEXT)")
c.execute("INSERT INTO ranges VALUES('1', 1, 1, 'verified')")
c.commit(); c.close()
PY
mkdir -p "$WORK/proofs"
echo "receipt bytes" > "$WORK/proofs/proof_1.bin"

if [ "$CONTROL" -eq 1 ]; then
    # ⛔ THE HAZARD ITSELF: sha256sum present, invoked, and failing. Exactly what `|| true` ate.
    mkdir -p "$WORK/bin"
    printf '#!/bin/sh\nexit 1\n' > "$WORK/bin/sha256sum"
    chmod +x "$WORK/bin/sha256sum"
    export PATH="$WORK/bin:$PATH"
fi

out="$WORK/run.log"
COORD_DB="$WORK/coordinator.db" COORD_PROOFS="$WORK/proofs" BACKUP_DIR="$WORK/backups" \
    bash "$SCRIPT" > "$out" 2>&1
rc=$?
echo "  (backup.sh exited $rc)"

DEST="$(find "$WORK/backups" -mindepth 1 -maxdepth 1 -type d | head -1)"

if [ "$CONTROL" -eq 1 ]; then
    [ "$rc" -ne 0 ]; check $? "a backup whose checksums failed exits NON-ZERO (got $rc)"
    grep -q "FATAL" "$out"; check $? "and says FATAL rather than 'wrote'"
    # ⛔ THE ORIGINAL SYMPTOM, PINNED: it must not claim success anywhere in its output.
    ! grep -qE '^\[backup\] (wrote|done)' "$out"; check $? "and never announces the backup as written"
    echo
    if [ "$fails" -eq 0 ]; then
        echo "CONTROL OK — sha256sum was broken and the backup refused instead of reporting success"
        exit 0
    fi
    echo "CONTROL FAILED — a backup that could not be checksummed still looked fine:"
    sed 's/^/    /' "$out" | tail -5
    exit 1
fi

[ "$rc" -eq 0 ]; check $? "a healthy backup exits 0 (got $rc; see below if not)"
[ -n "$DEST" ] && [ -s "$DEST/SHA256SUMS" ]; check $? "SHA256SUMS exists and is NOT empty"
# ⛔ THE RESTORE DRILL, RUN FOR REAL. RUNBOOK.md § Backup & restore verifies a restore with exactly
# this command; if it does not pass here it will not pass at 4am either.
( cd "$DEST" && sha256sum -c SHA256SUMS >/dev/null 2>&1 )
check $? "the documented restore check (sha256sum -c SHA256SUMS) passes against the backup"
grep -q "checksummed" "$out"; check $? "and the run says HOW MANY files it checksummed, not just 'wrote'"
# Every artefact a restore needs is covered by the manifest, not just whichever one sorted first.
for f in coordinator.db proofs.tar.gz MANIFEST.txt; do
    grep -q " \./$f\$" "$DEST/SHA256SUMS"
    check $? "$f is covered by SHA256SUMS"
done

echo
if [ "$fails" -ne 0 ]; then
    echo "FAILED $fails check(s). backup.sh output:"
    sed 's/^/    /' "$out"
    exit 1
fi
echo "the backup is verifiable, and an unverifiable one cannot pass as written"
