#!/usr/bin/env bash
# Hazync coordinator backup — the signed ledger AND the proof receipts.
#
# The DB (coordinator.db) records who proved what; the proofs/ directory holds the actual re-verifiable
# STARK receipts — the artifacts the whole "you don't have to trust us" claim rests on. Losing either
# loses the public record, so BOTH are backed up here, consistently and (optionally) offsite.
#
# Cron example (daily 03:17, offsite target set):
#   17 3 * * * BACKUP_REMOTE=rclone:hazync-backup:hazync /opt/hazync/coordinator/deploy/backup.sh >> /var/log/hazync-backup.log 2>&1
#
# Restore drill: see coordinator/deploy/RUNBOOK.md § Backup & restore.
set -euo pipefail

HZ_HOME="${HZ_HOME:-/opt/hazync}"
DB="${COORD_DB:-$HZ_HOME/coordinator/coordinator.db}"
PROOFS="${COORD_PROOFS:-$HZ_HOME/coordinator/proofs}"
OUT="${BACKUP_DIR:-$HZ_HOME/backups}"
KEEP="${BACKUP_KEEP:-14}"                 # keep this many local snapshots
REMOTE="${BACKUP_REMOTE:-}"              # optional: rsync/rclone target, e.g. rclone:remote:path or user@host:/path
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$OUT/$STAMP"
mkdir -p "$DEST"

# 1. Consistent DB snapshot — use sqlite's online backup (safe while the coordinator is running/WAL).
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$DEST/coordinator.db'"
else
    cp -- "$DB" "$DEST/coordinator.db"   # fallback; prefer sqlite3 for a WAL-consistent copy
fi

# 2. Proof receipts — the re-verifiable artifacts. Hard-link into the snapshot (cheap), then archive.
if [ -d "$PROOFS" ]; then
    tar -C "$(dirname "$PROOFS")" -czf "$DEST/proofs.tar.gz" "$(basename "$PROOFS")"
fi

# 3. Manifest + checksums so a restore can be verified.
{ echo "hazync backup $STAMP"; echo "db: $DB"; echo "proofs: $PROOFS"; } > "$DEST/MANIFEST.txt"
( cd "$DEST" && sha256sum ./* > SHA256SUMS 2>/dev/null || true )
echo "[backup] wrote $DEST ($(du -sh "$DEST" | cut -f1))"

# 4. Offsite copy (optional but strongly recommended — a same-disk backup dies with the box).
if [ -n "$REMOTE" ]; then
    case "$REMOTE" in
        rclone:*) rclone copy "$DEST" "${REMOTE#rclone:}/$STAMP" ;;
        *)        rsync -a "$DEST/" "$REMOTE/$STAMP/" ;;
    esac
    echo "[backup] copied offsite → $REMOTE/$STAMP"
else
    echo "[backup] WARNING: BACKUP_REMOTE unset — this snapshot is on the SAME DISK as the data it backs up."
fi

# 5. Rotate local snapshots.
ls -1dt "$OUT"/*/ 2>/dev/null | tail -n +"$((KEEP+1))" | xargs -r rm -rf
echo "[backup] done; kept newest $KEEP local snapshots in $OUT"
