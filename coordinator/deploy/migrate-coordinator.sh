#!/usr/bin/env bash
# Migrate the live coordinator (DB + proof receipts) from the OLD box to the NEW co-located bridge box.
# Run from a workstation that can SSH to BOTH boxes (relays old -> local -> new; no box-to-box trust).
#
# It takes a WAL-consistent snapshot of the old DB (sqlite .backup) so it is safe to run while the old
# coordinator is still serving. It does NOT stop or delete the old box — cut the proxy over, verify the
# new board renders identically, and only THEN decommission the old one.
#
#   OLD=root@94.237.59.55  NEW=root@152.53.93.164  SSH_KEY=~/.ssh/ghost_signet_ed25519 \
#     ./migrate-coordinator.sh
set -euo pipefail

OLD="${OLD:?set OLD=user@old-coordinator}"
NEW="${NEW:?set NEW=user@new-bridge-box}"
KEY="${SSH_KEY:-$HOME/.ssh/ghost_signet_ed25519}"
OLD_DB="${OLD_DB:-/opt/hazync/coordinator.db}"
OLD_PROOFS="${OLD_PROOFS:-/opt/hazync/coordinator/proofs}"
NEW_DB="${NEW_DB:-/root/coordinator.db}"
NEW_PROOFS="${NEW_PROOFS:-/root/hazync-proofs}"
SSH="ssh -i $KEY -o StrictHostKeyChecking=no"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "[1/4] WAL-consistent snapshot of the old DB ($OLD:$OLD_DB)"
$SSH "$OLD" "sqlite3 '$OLD_DB' \".backup '/tmp/coord_migrate.db'\""
scp -i "$KEY" -o StrictHostKeyChecking=no "$OLD:/tmp/coord_migrate.db" "$TMP/coordinator.db"
$SSH "$OLD" "rm -f /tmp/coord_migrate.db"
echo "     pulled $(du -h "$TMP/coordinator.db" | cut -f1) — $(sqlite3 "$TMP/coordinator.db" 'SELECT COUNT(*) FROM vranges' 2>/dev/null || echo '?') verified ranges"

echo "[2/4] proof receipts ($OLD:$OLD_PROOFS)"
mkdir -p "$TMP/proofs"
rsync -az -e "$SSH" "$OLD:$OLD_PROOFS/" "$TMP/proofs/" 2>/dev/null || echo "     (no proofs dir — skipping)"

echo "[3/4] push to new box ($NEW)"
$SSH "$NEW" "mkdir -p '$NEW_PROOFS' \"\$(dirname '$NEW_DB')\""
scp -i "$KEY" -o StrictHostKeyChecking=no "$TMP/coordinator.db" "$NEW:$NEW_DB"
rsync -az -e "$SSH" "$TMP/proofs/" "$NEW:$NEW_PROOFS/" 2>/dev/null || true

echo "[4/4] verify on new box"
$SSH "$NEW" "sqlite3 '$NEW_DB' 'SELECT (SELECT COUNT(*) FROM contributors)||\" contributors, \"||(SELECT COUNT(*) FROM vranges)||\" ranges, \"||(SELECT COUNT(*) FROM submissions)||\" submissions\"'"
echo "done. DB+proofs on $NEW. Next: start the new coordinator, smoke-test /api/state, THEN repoint the"
echo "web-box nginx proxy to $NEW and verify the board. Only after that: decommission $OLD."
