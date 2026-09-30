#!/usr/bin/env bash
# deploy-coordinator.sh refuses a branch name that means two different commits on the box.
#
# ⛔ WHAT HAPPENED, 2026-09-30. `deploy-coordinator.sh main` deployed 03541e3 (v0.21.6-4) while
# origin/main was f413da2 (v0.22.1-17). `git fetch` moves refs/remotes/origin/*, never refs/heads/*,
# and nobody pulls on a deployment box -- so the local `main` sat where it was when the checkout was
# made, and `git checkout main` took it.
#
# ⛔⛔ AND EVERY CHECK AFTER IT PASSED. The ref resolved, the checkout succeeded, server.py's hash
# "changed", the service restarted, systemd reported active and /api/state answered 200. The deploy
# printed "== deployed main ==". Two fixes were in neither the deployed file nor the running service.
# The only tell was `git describe` printing a version that made no sense -- the script's own closing
# claim that it "answers what is running, truthfully" is what exposed it.
#
# ⚠ A deploy that reports success over the wrong commit is worse than one that fails: nobody looks.
#
#   ./coordinator/deploy/test-deploy-ref.sh             # ambiguous ref is refused, unambiguous ones work
#   ./coordinator/deploy/test-deploy-ref.sh --control   # guard removed — the stale local branch must win
set -uo pipefail
trap 'echo "test-deploy-ref: ERR at line $LINENO (exit $?)" >&2' ERR

CONTROL=0
[ "${1:-}" = "--control" ] && CONTROL=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/deploy-coordinator.sh"
fails=0
check() { if [ "$1" = 1 ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

# ── a fake "upstream" and a deployment box cloned from it ───────────────────────────────────────
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
mkdir -p "$T/up" && git -C "$T/up" init -q -b main
mkdir -p "$T/up/coordinator"
printf 'OLD\n' > "$T/up/coordinator/server.py"
git -C "$T/up" add -A && git -C "$T/up" commit -qm old
git -C "$T/up" tag v0.0.1
git clone -q "$T/up" "$T/box"
# upstream moves on; the box fetches but, as on a real deployment box, never pulls
printf 'NEW\n' > "$T/up/coordinator/server.py"
git -C "$T/up" commit -qam new
git -C "$T/box" fetch -q --tags origin

LOCAL=$(git -C "$T/box" rev-parse --short main)
REMOTE=$(git -C "$T/box" rev-parse --short origin/main)
check "$([ "$LOCAL" != "$REMOTE" ] && echo 1)" \
      "the box reproduces the trap: local main $LOCAL != origin/main $REMOTE"

if [ "$CONTROL" = 1 ]; then
    # ⛔ The world before the guard: strip it and let `main` mean whatever git says.
    sed 's/^\(\s*\)check_ref_is_not_a_stale_local_branch$/\1true/' "$SCRIPT" > "$T/deploy.sh"
    chmod +x "$T/deploy.sh"
    SCRIPT="$T/deploy.sh"
fi

run() { REPO="$T/box" DRY_RUN=1 UNIT=nonexistent-unit "$SCRIPT" "$1" 2>&1; }

# ── 1. ⛔ THE ONE THAT MATTERS: a bare ambiguous branch name ─────────────────────────────────────
out=$(run main); rc=$?
if [ "$CONTROL" = 1 ]; then
    check "$([ $rc -eq 0 ] && echo 1)" "control: 'main' is accepted (exit $rc)"
    check "$(printf '%s' "$out" | /usr/bin/grep -q "would check out: main ($LOCAL)" && echo 1)" \
          "⛔ control: and it takes the STALE LOCAL branch $LOCAL, not $REMOTE — the production failure"
else
    check "$([ $rc -eq 1 ] && echo 1)" "an ambiguous 'main' is REFUSED (exit $rc)"
    check "$(printf '%s' "$out" | /usr/bin/grep -q "REFUSING: 'main' is ambiguous" && echo 1)" \
          "and says so plainly"
    check "$(printf '%s' "$out" | /usr/bin/grep -q "origin/main" && echo 1)" \
          "naming the exact command to use instead"
    check "$(printf '%s' "$out" | /usr/bin/grep -q "$LOCAL" && printf '%s' "$out" | /usr/bin/grep -q "$REMOTE" && echo 1)" \
          "showing BOTH commits, so the operator can see which is which"
fi

# ── 2. the unambiguous forms still work ─────────────────────────────────────────────────────────
out=$(run origin/main); rc=$?
check "$([ $rc -eq 0 ] && echo 1)" "origin/main is accepted (exit $rc)"
check "$(printf '%s' "$out" | /usr/bin/grep -q "would check out: origin/main ($REMOTE)" && echo 1)" \
      "and resolves to the remote commit $REMOTE"

out=$(run v0.0.1); rc=$?
check "$([ $rc -eq 0 ] && echo 1)" "a tag is accepted (exit $rc) — tags are never ambiguous this way"

# ── 3. a branch that agrees with its remote is not ambiguous ────────────────────────────────────
# ⚠ detach first: `branch -f` refuses to move the branch that is checked out.
git -C "$T/box" checkout -q --detach HEAD
git -C "$T/box" branch -f main origin/main
out=$(run main); rc=$?
check "$([ $rc -eq 0 ] && echo 1)" \
      "once local main == origin/main it is accepted again (exit $rc) — the guard tests DISAGREEMENT, not the name"

echo
if [ "$CONTROL" = 1 ]; then
    [ "$fails" = 0 ] && { echo "PASS (control): without the guard, 'main' silently deploys the stale local branch"; exit 0; }
    echo "FAIL (control): $fails — the control should reproduce the trap, not fail"; exit 1
fi
[ "$fails" = 0 ] && { echo "PASS (real)"; exit 0; }
echo "FAIL: $fails"; exit 1
