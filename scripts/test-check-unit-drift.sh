#!/usr/bin/env bash
# check-unit-drift.sh's drop-in attribution, when one unit's name EXTENDS another's.
#
# The bug: the declared set for a unit was `cat dropins/$u-*.conf`, and that glob also matches every
# drop-in belonging to a LONGER unit name. This repo has two such pairs --
#     hazync-coordinator      / hazync-coordinator-backup
#     hazync-bridge           / hazync-bridge-mem-sampler
# -- so the backup job's BACKUP_DIR, BACKUP_KEEP, BACKUP_REMOTE_PROOFS and HZ_REPO were being read as
# settings hazync-coordinator declares.
#
# ⛔ WHY IT MATTERS IN ONE DIRECTION ONLY. A wider declared set can never produce a false alarm; it
# can only make a live, undeclared setting look accounted for. That is the exact failure this whole
# script exists to catch, so the widening is invisible until the day it hides something. It hid
# nothing on either box on 2026-09-27 -- the fix changed the live output by not one line -- which is
# precisely why it needs a test rather than a measurement.
#
#   ./scripts/test-check-unit-drift.sh              # the real function must attribute correctly
#   ./scripts/test-check-unit-drift.sh --control     # the old glob must REPRODUCE the bug
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

CONTROL=no
[ "${1:-}" = --control ] && CONTROL=yes

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
DROPINS_DIR="$T/dropins"; mkdir -p "$DROPINS_DIR"

# Two units where one name extends the other, and a third that merely shares a leading word.
UNITS="svc svc-backup svcother"
printf '[Service]\nEnvironment=OWN=short\n'   > "$DROPINS_DIR/svc-paths.conf"
printf '[Service]\nEnvironment=STOLEN=long\n' > "$DROPINS_DIR/svc-backup-paths.conf"
printf '[Service]\nEnvironment=OTHER=sep\n'   > "$DROPINS_DIR/svcother-paths.conf"
export DROPINS_DIR UNITS

if [ "$CONTROL" = yes ]; then
    # The pre-fix expression, verbatim.
    declared_dropin_bodies() { cat "$DROPINS_DIR/$1-"*.conf 2>/dev/null; }
else
    fn=$(sed -n '/^declared_dropin_bodies()/,/^}/p' scripts/check-unit-drift.sh)
    # ⛔ A MISSING FUNCTION MUST FAIL, NOT PASS VACUOUSLY. If the name is ever changed, an empty
    # extraction would make every assertion below compare "" against "" and report success.
    printf '%s\n' "$fn" | grep -q 'declared_dropin_bodies()' \
        || { echo "FAIL could not extract declared_dropin_bodies from scripts/check-unit-drift.sh"; exit 1; }
    # shellcheck disable=SC1090
    source <(printf '%s\n' "$fn")
fi

fails=0
ok()  { echo "  ok   $1"; }
bad() { echo "  FAIL $1"; fails=$((fails+1)); }
has() { printf '%s\n' "$2" | grep -qF "$1"; }

short=$(declared_dropin_bodies svc)
long=$(declared_dropin_bodies svc-backup)
sep=$(declared_dropin_bodies svcother)

# 1. the short unit must see its own file
has 'OWN=short' "$short" && ok "svc sees its own drop-in" || bad "svc lost its own drop-in"

# 2. the short unit must NOT see the longer unit's file -- this is the bug
if has 'STOLEN=long' "$short"; then
    if [ "$CONTROL" = yes ]; then ok "control reproduces it: svc absorbs svc-backup's drop-in"
    else bad "svc still absorbs svc-backup's drop-in"; fi
else
    if [ "$CONTROL" = yes ]; then bad "control did NOT reproduce the bug -- it is testing nothing"
    else ok "svc does not absorb svc-backup's drop-in"; fi
fi

# 3. the longer unit must still see its own
has 'STOLEN=long' "$long" && ok "svc-backup sees its own drop-in" || bad "svc-backup lost its own drop-in"

# 4. a name that shares only a leading word is a DIFFERENT unit, not a longer one. `svcother` must
#    neither be stolen from nor steal: the fix keys on the `$u-` boundary, not on a bare prefix.
has 'OTHER=sep' "$sep" && ok "svcother sees its own drop-in" || bad "svcother lost its own drop-in"
has 'OTHER=sep' "$short" && bad "svc absorbed svcother's drop-in" || ok "svc does not absorb svcother"
has 'OWN=short' "$sep"   && bad "svcother absorbed svc's drop-in" || ok "svcother does not absorb svc"

echo
if [ "$fails" = 0 ]; then echo "PASS ($([ "$CONTROL" = yes ] && echo control || echo real))"; exit 0; fi
echo "FAIL: $fails assertion(s)"; exit 1
