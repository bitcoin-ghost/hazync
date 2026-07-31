#!/usr/bin/env bash
# Does the G1 retention gate still detect a missing receipt?
#
# `check-retention.py` is the only thing standing between "the board says this block is proven" and
# "we can actually hand you the proof for it". It has one job and it runs unattended, so the failure
# that matters is not it reporting a hole — it is it reporting CLEAN while a hole exists. A gate that
# cannot fail is not a gate.
#
# Scope, stated plainly: this tests the CHECKER, against synthetic fixtures. It does NOT verify
# production retention — that needs the coordinator's real DB and proof store, which CI does not have
# and should not. Run the checker itself on the coordinator for that.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
CHECKER="$PWD/check-retention.py"
fail=0
note() { printf '  %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; fail=1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A board with three verified heights and a receipt for each.
build_fixture() {
    rm -rf "$TMP/proofs" "$TMP/c.db"
    mkdir -p "$TMP/proofs"
    python3 - "$TMP/c.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("CREATE TABLE ranges(id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, status TEXT)")
for h in (1, 2, 3):
    c.execute("INSERT INTO ranges VALUES(?,?,?,'verified')", (str(h), h, h))
c.commit()
PY
    for h in 1 2 3; do echo "receipt" > "$TMP/proofs/proof_$h.bin"; done
}

run_checker() { COORD_DB="$TMP/c.db" COORD_PROOFS="$TMP/proofs" python3 "$CHECKER" "$@" >"$TMP/out" 2>&1; }

echo "== 1. a complete board passes =="
build_fixture
if run_checker; then note "ok   complete board -> exit 0"
else bad "complete board should pass, got exit $? — the gate cries wolf"; cat "$TMP/out"; fi

# THE POSITIVE CONTROL. Everything else here is scaffolding for this one case: remove a receipt for a
# height the ledger calls proven, and the gate must fail. If this ever passes, the check is inert and
# every "retention OK" it has printed since means nothing.
echo "== 2. POSITIVE CONTROL: a proven height with no receipt must FAIL =="
build_fixture
rm -f "$TMP/proofs/proof_2.bin"
if run_checker; then bad "a missing receipt was NOT detected — the G1 gate is inert"; cat "$TMP/out"
else
    if grep -q "G1 VIOLATION" "$TMP/out"; then note "ok   missing receipt -> exit 1, names it a G1 VIOLATION"
    else bad "failed, but without saying why"; cat "$TMP/out"; fi
fi

echo "== 3. a KNOWN hole can be accepted without masking new ones =="
build_fixture
rm -f "$TMP/proofs/proof_2.bin"
if run_checker --allow 2; then note "ok   --allow 2 accepts the recorded hole"
else bad "--allow did not accept a recorded hole"; cat "$TMP/out"; fi
build_fixture
rm -f "$TMP/proofs/proof_2.bin" "$TMP/proofs/proof_3.bin"
if run_checker --allow 2; then bad "--allow 2 masked an UNRELATED missing receipt at height 3"
else note "ok   --allow 2 still fails on the unrelated hole at 3"; fi

echo "== 4. an empty board is vacuous, not a failure — but a ledger/store mismatch IS =="
build_fixture
python3 - "$TMP/c.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("UPDATE ranges SET status='open'"); c.commit()
PY
rm -f "$TMP"/proofs/proof_*.bin
if run_checker; then note "ok   fresh/re-baselined board -> exit 0 (vacuous)"
else bad "an empty board must not fail — an alarm that is wrong by design gets muted"; cat "$TMP/out"; fi

build_fixture
python3 - "$TMP/c.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("UPDATE ranges SET status='open'"); c.commit()
PY
if run_checker; then bad "receipts on disk with an empty ledger should be reported as a mismatch"
else
    if grep -q "LEDGER/STORE MISMATCH" "$TMP/out"; then note "ok   receipts with an empty ledger -> mismatch reported"
    else bad "failed without naming the mismatch"; cat "$TMP/out"; fi
fi

echo "== 5. a missing proof directory must not read as clean =="
rm -rf "$TMP/proofs"
if run_checker; then bad "a missing proof dir reported success — that is a silent all-clear"
else note "ok   missing proof dir -> refuses to report a clean run"; fi

echo
if [ "$fail" -ne 0 ]; then echo "G1 retention gate is NOT trustworthy — see failures above."; exit 1; fi
echo "G1 retention gate detects what it is supposed to detect."
