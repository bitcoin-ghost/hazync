#!/usr/bin/env bash
# Integrity check: does this box run unit configuration the repo does not contain? (hazync#380)
# Installed as /usr/local/sbin/hazync-check-unit-drift and invoked through hazync-run-check.
#
# Exit codes follow the check contract: 0 holds, 1 drift found, 2 could not check. hazync-run-check
# treats 1 and 2 alike -- both push, both dampened -- so a minute of network trouble does not arrive
# looking like drift.
#
# ⛔ WHY A DEDICATED CHECKOUT, AND NOT ONE THAT IS ALREADY THERE.
# scripts/check-unit-drift.sh answers "is anything live that THIS REPO does not declare", and it reads
# the repo it is sitting in. Point it at a tree that has fallen behind main and it reports drift for
# settings that are committed and fine -- they are simply not in that tree. Both checkouts on the box
# are wrong for this:
#
#   /opt/hazync          the deployed coordinator source. Behind main BY DESIGN; it only moves when
#                        something is deployed, which is the whole point of it.
#   /root/hazync-release the release build tree, moved to whatever tag or branch is being built.
#
# Borrowing either makes this check's verdict depend on unrelated activity, and a check that cries
# wolf gets muted. So it keeps its own tree and refreshes it every run, and if it cannot refresh it
# says so (exit 2) rather than comparing against a stale one and reporting confident nonsense.
set -uo pipefail

REPO="${HAZYNC_DRIFT_REPO:-/var/lib/hazync-drift/repo}"
REMOTE="${HAZYNC_DRIFT_REMOTE:-https://github.com/bitcoin-ghost/hazync.git}"
# ⛔ THIS RUNS THE SCRIPT FROM THE FETCHED REF, NOT THE ONE INSTALLED BESIDE IT. That is the point --
# the question is whether the box matches the repo as it stands now -- but it means the timer must not
# be enabled until the drift script's own fixes are ON the ref. Installing this while main still had
# the ssh-only script made every run report "could not read unit state" (measured 2026-09-18).
# HAZYNC_DRIFT_REF exists so this path can be exercised against a branch before it merges.
REF="${HAZYNC_DRIFT_REF:-main}"
HOST="${1:-localhost}"

if [ ! -d "$REPO/.git" ]; then
    mkdir -p "$(dirname "$REPO")" || { echo "cannot create $(dirname "$REPO")"; exit 2; }
    git clone --quiet "$REMOTE" "$REPO" || { echo "clone of $REMOTE into $REPO failed"; exit 2; }
fi

git -C "$REPO" fetch --quiet origin "$REF" || {
    echo "fetch of origin/$REF failed; NOT checking against a stale tree"; exit 2; }
git -C "$REPO" reset --hard --quiet FETCH_HEAD || {
    echo "reset to FETCH_HEAD failed; NOT checking against a stale tree"; exit 2; }

echo "comparing $HOST against $(git -C "$REPO" log --oneline -1)"
exec "$REPO/scripts/check-unit-drift.sh" "$HOST"
