#!/usr/bin/env bash
# Push newly written tip bundles from the tip-bridge box to the coordinator (hazync-admin#2).
#
# WHY THIS EXISTS. The tip bridge no longer runs beside the coordinator: it lives on its own box, while
# `server.py` serves `bundle_<n>.json` from a directory it expects to be local. That is not merely a
# serving concern -- `/api/vranges` computes the work on offer as
#
#     waiting = sum(1 for h in range(lo, hi + 1) if bundle_path(h) is None)
#
# so a bundle that never reaches the coordinator makes its block silently unclaimable. Nobody sees an
# error; the block is simply never offered. Hence bundles are pushed rather than fetched on demand, which
# also keeps the sponsor bot's requirement intact: it reads bundles from local disk.
#
# ⛔ THE CHANNEL IS WRITE-ONLY BY DESIGN, AND THAT HAS A CONSEQUENCE. The key this uses is pinned in the
# coordinator's authorized_keys to `rrsync -wo -no-del -no-overwrite <bundle dir>`, so this box can ADD a
# bundle and do nothing else -- it cannot read the store, delete, overwrite, get a pty or forward a port.
# Verified on 2026-09-20: a read attempt returns "reading from write-only server is not allowed", an
# overwrite leaves the original bytes, and a plain ssh returns "SSH_ORIGINAL_COMMAND does not run rsync".
#
# The consequence is that THIS SCRIPT CANNOT VERIFY ITS OWN WORK. It cannot list what the coordinator
# has, so it can never report a missing bundle. That check belongs on the coordinator, which is the side
# that knows what it is missing -- see hazync-check-bundle-gap.sh. Do not "fix" this by granting read
# access; the asymmetry is the security property.
#
# Exit codes follow the check contract: 0 nothing to do or pushed cleanly, 1 push failed, 2 cannot check.
set -uo pipefail

SRC="${HAZYNC_BRIDGE_OUT:-/var/lib/hazync/tip_bundles}"
KEY="${BUNDLE_SYNC_KEY:-/etc/hazync/bundlesync_ed25519}"
DEST="${BUNDLE_SYNC_DEST:-}"
STAMP="${BUNDLE_SYNC_STAMP:-/var/lib/hazync/.bundle-push-stamp}"

[ -n "$DEST" ] || { echo "[bundle-push] cannot check: BUNDLE_SYNC_DEST is unset" >&2; exit 2; }
[ -d "$SRC" ]  || { echo "[bundle-push] cannot check: no bundle directory at $SRC" >&2; exit 2; }
[ -r "$KEY" ]  || { echo "[bundle-push] cannot check: no readable key at $KEY" >&2; exit 2; }

# ⛔ THE SERVICE CANNOT READ /root/.ssh — ProtectHome=yes. The unit is sandboxed with
# ProtectHome=yes and ProtectSystem=strict, so /root is invisible to it. ssh therefore finds NO
# known_hosts, and with StrictHostKeyChecking=yes it correctly refuses:
#
#   No ED25519 host key is known for <coordinator> and you have requested strict checking.
#   Host key verification failed.
#   [bundle-push] push of 781 bundle(s) FAILED; watermark not advanced
#
# ⚠ AND IT HIDES ITSELF. Run by hand as root the push works, because a root shell CAN read
# /root/.ssh — so "it works when I run it" proves nothing here. Worse, once a manual run clears the
# queue the next timer run has nothing to push, never opens a connection, and exits 0: a green run
# that never exercised the failing path. Both of those happened on 2026-09-23 before the cause was
# found.
#
# So the trust anchor is an explicit file inside /etc/hazync, beside the key this service already
# uses, which the sandbox permits. StrictHostKeyChecking stays YES -- the fix is to give strict
# checking something to read, never to turn it off.
KNOWN="${BUNDLE_SYNC_KNOWN_HOSTS:-/etc/hazync/known_hosts}"
if [ ! -r "$KNOWN" ]; then
    echo "[bundle-push] cannot check: no readable known_hosts at $KNOWN — the service is sandboxed" >&2
    echo "[bundle-push] populate it with: ssh-keyscan -H <coordinator> > $KNOWN   (verify the" >&2
    echo "[bundle-push] fingerprints against a trusted source before trusting the result)" >&2
    exit 1
fi
SSH_CMD="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes"
SSH_CMD="$SSH_CMD -o UserKnownHostsFile=$KNOWN -o GlobalKnownHostsFile=/dev/null"

# ⛔ TAKE THE WATERMARK *BEFORE* LISTING, NOT AFTER. A bundle written while this run is in flight would
# otherwise fall between the listing and the stamp update and never be picked up by any run -- a
# permanent single-block hole that only the coordinator-side gap check would ever notice.
NEWSTAMP=$(mktemp) || { echo "[bundle-push] cannot check: mktemp failed" >&2; exit 2; }
LIST=$(mktemp) || { rm -f "$NEWSTAMP"; echo "[bundle-push] cannot check: mktemp failed" >&2; exit 2; }
trap 'rm -f "$NEWSTAMP" "$LIST"' EXIT

# ⛔ NAMES ONLY, AND NEVER A GLOB. `bundle_*.json` as a shell glob is an ARG_MAX accident waiting to
# happen once the store is large, and it has already read as ZERO elsewhere in this project for exactly
# that reason. find + --files-from has no such ceiling.
#
# ⛔ AND state.bin MUST NOT BE PUSHED. It lives in this same directory and is ~14 GB; a pattern of
# "everything here" would ship it every single run and drop it into the coordinator's bundle store.
if [ -f "$STAMP" ]; then
    find "$SRC" -maxdepth 1 -type f -name 'bundle_*.json' -newer "$STAMP" -printf '%f\n' > "$LIST"
else
    find "$SRC" -maxdepth 1 -type f -name 'bundle_*.json' -printf '%f\n' > "$LIST"
fi

# ⛔ `wc -l`, NOT `grep -c .`: grep prints 0 AND exits non-zero on no match, which under pipefail turns
# "nothing to do" into a failure.
N=$(wc -l < "$LIST")
if [ "$N" -eq 0 ]; then
    echo "[bundle-push] nothing new since the last run"
    mv -f "$NEWSTAMP" "$STAMP"
    trap - EXIT; rm -f "$LIST"
    exit 0
fi

if rsync -e "$SSH_CMD" --files-from="$LIST" --ignore-existing --times --quiet "$SRC/" "$DEST:./"; then
    echo "[bundle-push] pushed $N bundle(s): $(head -1 "$LIST") .. $(tail -1 "$LIST")"
    # ⛔ ONLY NOW does the watermark move. A failed push must be retried by the next run, so the stamp
    # stays where it was and the same files are listed again.
    mv -f "$NEWSTAMP" "$STAMP"
    trap - EXIT; rm -f "$LIST"
    exit 0
fi

echo "[bundle-push] push of $N bundle(s) FAILED; watermark not advanced, next run retries them" >&2
exit 1
