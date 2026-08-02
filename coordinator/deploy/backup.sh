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

# Fail loudly on a mis-pointed path BEFORE writing anything. Without this the failure is silent and
# worse than no backup: `sqlite3 <missing-path> ".backup ..."` happily CREATES an empty database and
# exits 0, and a missing proofs/ dir is skipped by the `[ -d ]` test below — so a wrong COORD_DB/
# COORD_PROOFS yields a green, rotating, offsite-copied snapshot containing nothing at all. The paths
# are env-driven and must match whatever the coordinator unit sets; they differ per deployment.
for _v in DB PROOFS; do
    _p="${!_v}"                      # indirect expansion, not eval — no re-parsing of the value
    if [ ! -e "$_p" ]; then
        echo "[backup] FATAL: $_v path does not exist: $_p" >&2
        echo "[backup] set COORD_DB / COORD_PROOFS (or HZ_HOME) to match the coordinator's own unit —" >&2
        echo "[backup] check: systemctl cat hazync-coordinator | grep -E 'COORD_DB|COORD_PROOFS'" >&2
        exit 1
    fi
done
[ -s "$DB" ] || { echo "[backup] FATAL: ledger is empty: $DB" >&2; exit 1; }
# Check it is actually a SQLite database, by magic header. This runs with no dependencies, because the
# sqlite3 schema probe below cannot: on a box without sqlite3 we fall back to `cp`, which will happily
# copy any file at all — so without this a wrong-but-existing path (or a stray file) is archived as
# though it were the ledger.
case "$(head -c 15 "$DB" 2>/dev/null)" in
    "SQLite format 3") : ;;
    *) echo "[backup] FATAL: $DB is not a SQLite database (bad magic header)" >&2; exit 1 ;;
esac
# And that it is OUR schema — an unrelated SQLite file, or a stray empty DB left by an earlier
# mis-pointed run, is not the coordinator ledger and must not pass as a good backup.
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" "select count(*) from ranges;" >/dev/null 2>&1 || {
        echo "[backup] FATAL: $DB is not a coordinator ledger (no 'ranges' table)" >&2; exit 1; }
else
    echo "[backup] WARNING: sqlite3 not installed — falling back to a plain cp of the ledger, which is" >&2
    echo "[backup] NOT WAL-consistent under a live coordinator. Install sqlite3 on this box." >&2
fi

mkdir -p "$DEST"

# 1. Consistent DB snapshot — use sqlite's online backup (safe while the coordinator is running/WAL).
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$DEST/coordinator.db'"
else
    cp -- "$DB" "$DEST/coordinator.db"   # fallback; prefer sqlite3 for a WAL-consistent copy
fi

# 2. Proof receipts — the re-verifiable artifacts.
#
# The coordinator is LIVE while this runs and workers land new receipts continuously, so a naive
# `tar <dir>` hits "file changed as we read it", exits 1, and `set -e` kills the whole backup — i.e. it
# would fail every night on exactly the busy coordinator it exists to protect.
#
# So: snapshot the file LIST first and archive only those entries. Receipts are immutable once written
# (one file per proven block), so anything that appears mid-run simply belongs to the next snapshot.
# tar's exit 1 is the "changed/vanished as we read" warning class and is tolerated; >=2 is a real error.
PROOF_COUNT=0
if [ -d "$PROOFS" ]; then
    _list="$DEST/.proof-list"
    ( cd "$(dirname "$PROOFS")" && find "$(basename "$PROOFS")" -type f -print > "$_list" )
    PROOF_COUNT=$(wc -l < "$_list" | tr -d ' ')
    set +e
    tar -C "$(dirname "$PROOFS")" -czf "$DEST/proofs.tar.gz" -T "$_list"
    _tar_rc=$?
    set -e
    rm -f "$_list"
    if [ "$_tar_rc" -ge 2 ]; then
        echo "[backup] FATAL: tar failed on $PROOFS (exit $_tar_rc)" >&2; exit 1
    elif [ "$_tar_rc" -eq 1 ]; then
        echo "[backup] note: some receipts changed/vanished while archiving (live coordinator) — expected"
    fi
fi

# 3. Manifest + checksums so a restore can be verified.
{ echo "hazync backup $STAMP"; echo "db: $DB"; echo "proofs: $PROOFS";
  echo "receipts: $PROOF_COUNT"; } > "$DEST/MANIFEST.txt"
( cd "$DEST" && sha256sum ./* > SHA256SUMS 2>/dev/null || true )
echo "[backup] wrote $DEST ($(du -sh "$DEST" | cut -f1))"

# 4. Offsite copy (optional but strongly recommended — a same-disk backup dies with the box).
#
# Each run ships a FULL snapshot to its own $STAMP directory, so the remote must be rotated too or it
# grows without bound until the target fills and every subsequent backup fails. $BACKUP_KEEP used to be
# applied only to the local $OUT below, which meant configuring an offsite target quietly converted a
# same-disk problem into a full-disk one. Snapshots track the proof set and are not a fixed size — this
# one is 5.7G and growing ~10G/day while the board re-proves — so an unrotated remote does not degrade
# slowly, it fills in days.
#
# Note --link-dest would NOT help here: the receipts are archived into a single proofs.tar.gz that
# differs every run, so there are no unchanged files to hardlink. Making that work would mean syncing
# the receipts directory instead of a tarball — a layout change, deliberately not done here.
if [ -n "$REMOTE" ]; then
    # BACKUP_REMOTE_DB_ONLY=1 ships the LEDGER offsite but leaves the proofs local.
    #
    # The two halves are not comparable. coordinator.db is ~37 MB and is the record of who proved what —
    # lose it and the contributor history is gone at any price. proofs/ is 7.7 GB growing to ~165 GB and
    # IS regenerable, deterministically, from the guest plus chain data (expensively, but recoverable).
    #
    # So when no target is large enough for both — which is the common case — copying only the ledger
    # closes most of the actual risk for 0.5% of the bytes. "We cannot back up the proofs" is not the
    # same constraint as "we cannot back up anything", and conflating them leaves the irreplaceable
    # half on a single disk.
    SRC="$DEST"
    if [ "${BACKUP_REMOTE_DB_ONLY:-0}" = "1" ]; then
        SRC="$DEST/.db-only"
        mkdir -p "$SRC"
        cp -- "$DEST/coordinator.db" "$SRC/" 2>/dev/null || { echo "[backup] FATAL: no ledger to ship offsite" >&2; exit 1; }
        cp -- "$DEST/MANIFEST.txt" "$SRC/" 2>/dev/null || true
        ( cd "$SRC" && sha256sum ./coordinator.db > SHA256SUMS 2>/dev/null || true )
        echo "[backup] offsite copy is LEDGER-ONLY ($(du -h "$SRC/coordinator.db" | cut -f1)); proofs stay local"
    fi

    # ── receipts offsite, as an append-only MIRROR (hazync#49) ────────────────────────────────────
    #
    # The dated-snapshot scheme above is right for the ledger and wrong for receipts. The ledger is
    # small and MUTABLE, so history matters; a receipt is ~220 KB and IMMUTABLE — block N's proof never
    # changes — so 14 dated copies of it is 14x the cost of the one thing you actually need.
    #
    # Mirroring instead makes the offsite cost 1x the store rather than KEEP x, which is the difference
    # between "affordable" and "not". Measured 2026-08-02: 759 MB of receipts against 14 GB free on the
    # target — as snapshots that is ~10 GB and rising; as a mirror it is 759 MB.
    #
    # NAMESPACED BY GUEST ID, and this is not optional. Receipts are only meaningful relative to the
    # guest that produced them, and filenames REPEAT across re-baselines: proof_10000.bin exists under
    # every id, with different bytes. A flat mirror would either collide or (with --ignore-existing)
    # silently skip the new one and keep serving the retired proof back to you on restore.
    #
    # No --delete, deliberately. A re-baseline clears the local store; the offsite copy of the retired
    # baseline is then the only surviving record of work that really happened, and it stays
    # re-verifiable with the archived host binary.
    #
    # CEILING, stated rather than discovered: the store grows toward ~165 GB at full chain and the
    # current target has 14 GB. This buys a long runway, not a permanent home — when it fills, the
    # answer is a bigger target, not silently dropping back to ledger-only.
    if [ "${BACKUP_REMOTE_PROOFS:-0}" = "1" ] && [ -d "$PROOFS" ]; then
        _mid="$(grep -vE '^[[:space:]]*#' "${HZ_REPO:-$HZ_HOME}/reproduce/METHOD_ID" 2>/dev/null                 | grep -oE '[0-9a-f]{64}' | head -1)"
        _mid="${_mid:0:8}"
        if [ -z "$_mid" ]; then
            echo "[backup] WARNING: could not read METHOD_ID — skipping receipt mirror rather than" >&2
            echo "[backup]          writing receipts to an unnamespaced path" >&2
        else
            case "$REMOTE" in
                rclone:*) rclone copy "$PROOFS" "${REMOTE#rclone:}/proofs-$_mid" ;;
                *:*)      rsync -a --ignore-existing "$PROOFS/" "$REMOTE/proofs-$_mid/" ;;
            esac && echo "[backup] receipts mirrored offsite -> proofs-$_mid ($(du -sh "$PROOFS" | cut -f1), append-only)"                  || echo "[backup] WARNING: receipt mirror failed — ledger snapshot still shipped" >&2
        fi
    fi
    case "$REMOTE" in
        rclone:*)
            _rc="${REMOTE#rclone:}"
            rclone copy "$SRC" "$_rc/$STAMP"
            # Prune oldest-first, keeping $KEEP. `lsf --dirs-only` sorts lexically, and the stamp format
            # (UTC %Y%m%dT%H%M%SZ) is lexically ordered, so this is chronological.
            _old=$(rclone lsf --dirs-only "$_rc" 2>/dev/null | sed 's:/$::' | sort | head -n -"$KEEP")
            for _d in $_old; do
                rclone purge "$_rc/$_d" >/dev/null 2>&1 \
                    && echo "[backup] pruned remote snapshot $_d" \
                    || echo "[backup] WARNING: could not prune remote snapshot $_d" >&2
            done
            ;;
        *:*)
            rsync -a "$SRC/" "$REMOTE/$STAMP/"
            # user@host:/path — prune over ssh on the same host. Split on the FIRST colon only, so
            # paths containing colons still work.
            _host="${REMOTE%%:*}"; _path="${REMOTE#*:}"
            if ! ssh -o BatchMode=yes "$_host" \
                    "ls -1d '$_path'/*/ 2>/dev/null | sort | head -n -$KEEP | xargs -r rm -rf"; then
                echo "[backup] WARNING: remote prune failed on $_host — $_path will grow unbounded" >&2
            fi
            ;;
        *)
            # A bare local path (no host): rsync it, then prune in place.
            rsync -a "$SRC/" "$REMOTE/$STAMP/"
            ls -1dt "$REMOTE"/*/ 2>/dev/null | tail -n +"$((KEEP+1))" | xargs -r rm -rf
            ;;
    esac
    rm -rf "$DEST/.db-only"
    echo "[backup] copied offsite → $REMOTE/$STAMP (keeping newest $KEEP)"
else
    echo "[backup] WARNING: BACKUP_REMOTE unset — this snapshot is on the SAME DISK as the data it backs up."
fi

# 5. Rotate local snapshots.
ls -1dt "$OUT"/*/ 2>/dev/null | tail -n +"$((KEEP+1))" | xargs -r rm -rf
echo "[backup] done; kept newest $KEEP local snapshots in $OUT"
