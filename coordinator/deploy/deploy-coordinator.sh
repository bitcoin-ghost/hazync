#!/usr/bin/env bash
# Deploy the coordinator by moving its CHECKOUT to a tag — not by copying files into it.
#
#   ./coordinator/deploy/deploy-coordinator.sh v0.13.1          # on the coordinator box
#   REPO=/root/hazync ./coordinator/deploy/deploy-coordinator.sh v0.13.1
#   DRY_RUN=1 ./coordinator/deploy/deploy-coordinator.sh v0.13.1
#
# WHY THIS EXISTS
#
# Deploys used to be `scp server.py` + `systemctl restart`. That works, and it is how the box ended up
# 144 commits behind its own HEAD with three "modified" tracked files that were really newer copies
# pasted over an old tree (#48). The box then could not answer "what is running?" — `git describe`
# described the checkout, and the checkout described nothing.
#
# The danger is not untidiness. Before the spine/fold deploy the live server.py had to be diffed
# against every plausible commit to establish that overwriting it would not destroy a production fix.
# It happened to match `93b9bff` exactly, so the deploy was provably additive — but that was luck, and
# the next time the answer could be "matches nothing", with no way to tell a stale copy from a
# deliberate one.
#
# So: the checkout is the deployment. Per-box differences live in the systemd unit's environment,
# where they already do (COORD_DB, COORD_PROOFS, HAZYNC_HOST, TIP_HEIGHT, …), never in edited files.
#
# WHAT IT REFUSES TO DO
#
#   * deploy over local modifications to tracked files — that is the drift this exists to prevent, and
#     silently discarding someone's emergency fix would be worse than stopping. --force overrides.
#   * restart when nothing the service reads has changed. A restart is not free: it interrupts a live
#     proving fleet mid-submission. The v0.13.1 checkout on 2026-08-01 changed no served file, so the
#     right number of restarts was zero.
set -uo pipefail

TAG="${1:-}"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
UNIT="${UNIT:-hazync-coordinator}"
DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"
[ "${2:-}" = "--force" ] && FORCE=1

die() { echo "FATAL: $*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

# ⛔ A BARE BRANCH NAME RESOLVES TO THE BOX'S LOCAL BRANCH, AND NOTHING EVER UPDATES THAT.
# `git fetch` moves refs/remotes/origin/*; it does not touch refs/heads/*. Nobody pulls on a
# deployment box, so a local `main` stays wherever it was when the checkout was made -- while
# origin/main moves on. Every check downstream still passes: the ref resolves, the checkout succeeds,
# server.py "changes", the service restarts and reports active, and /api/state answers 200.
#
# Measured 2026-09-30: `deploy-coordinator.sh main` deployed 03541e3 (v0.21.6-4) while origin/main was
# f413da2 (v0.22.1-17). The two fixes being deployed were in neither the file nor the running service,
# and the deploy reported success. The ONLY tell was `git describe` printing a version that made no
# sense -- this script's own closing claim is what exposed it.
#
# ⚠ Refuse rather than silently preferring the remote: guessing which one the operator meant is how a
# deploy script earns distrust. Naming the exact command costs one re-run and no ambiguity.
check_ref_is_not_a_stale_local_branch() {
    git show-ref --verify --quiet "refs/heads/$TAG" || return 0
    git show-ref --verify --quiet "refs/remotes/origin/$TAG" || return 0
    local lo ro
    lo=$(git rev-parse "refs/heads/$TAG")
    ro=$(git rev-parse "refs/remotes/origin/$TAG")
    [ "$lo" = "$ro" ] && return 0
    echo "REFUSING: '$TAG' is ambiguous on this box and the two do not agree:" >&2
    echo "    local  $TAG        $(git rev-parse --short "$lo")  $(git describe --tags --always "$lo" 2>/dev/null)" >&2
    echo "    origin/$TAG $(git rev-parse --short "$ro")  $(git describe --tags --always "$ro" 2>/dev/null)" >&2
    echo >&2
    echo "git checkout '$TAG' would take the LOCAL branch, which no fetch ever updates, and every" >&2
    echo "check after it would still pass. Deploy the remote ref or a tag, explicitly:" >&2
    echo "  · $0 origin/$TAG" >&2
    echo "  · $0 <a tag>" >&2
    exit 1
}

[ -n "$TAG" ] || die "usage: $0 <tag|ref> [--force]   (e.g. $0 v0.13.1)"
[ -d "$REPO/.git" ] || die "REPO=$REPO is not a git checkout — this script deploys BY checkout, so there must be one"
cd "$REPO" || die "cannot cd to $REPO"

# The file the service actually executes. Restart is decided on this, not on "did anything change".
SERVED="coordinator/server.py"

echo "== deploying $TAG into $REPO =="
say "currently at: $(git describe --tags --always 2>/dev/null || echo unknown) ($(git rev-parse --short HEAD 2>/dev/null))"

# ── drift: report it in a dry run, refuse it in a real one ────────────────────────────────────────
#
# The order here matters and it was wrong to begin with. The refusal used to run BEFORE the dry-run
# branch, so `DRY_RUN=1` exited 1 on a drifted checkout without saying what a deploy would do — which
# is backwards, because a drifted box is precisely the one you most want to preview before touching.
# A dry run must be read-only and must always answer the question it was asked.
drift=$(git status --porcelain --untracked-files=no | wc -l)
if [ "$drift" -ne 0 ] && [ "$DRY_RUN" != 1 ] && [ "$FORCE" != 1 ]; then
    echo "REFUSING: $drift tracked file(s) modified in the checkout:" >&2
    git status --porcelain --untracked-files=no >&2
    echo >&2
    echo "Someone edited the deployment in place. That is exactly the state this script exists to" >&2
    echo "end, and discarding it blind could throw away a production fix." >&2
    echo "  · inspect:  git -C $REPO diff" >&2
    echo "  · preview:  DRY_RUN=1 $0 $TAG   (read-only; works even in this state)" >&2
    echo "  · keep it:  commit it upstream, then deploy the tag that contains it" >&2
    echo "  · drop it:  re-run with --force (it is backed up below either way)" >&2
    exit 1
fi

before_sha=$(sha256sum "$SERVED" 2>/dev/null | cut -d' ' -f1)
say "$SERVED before: ${before_sha:0:16}"

if [ "$DRY_RUN" = 1 ]; then
    git fetch --tags --quiet origin || die "fetch failed"
    check_ref_is_not_a_stale_local_branch
    target=$(git rev-parse --verify --quiet "${TAG}^{commit}") || die "no such tag/ref: $TAG"
    after_sha=$(git show "$TAG:$SERVED" 2>/dev/null | sha256sum | cut -d' ' -f1)
    say "would check out: $TAG ($(git rev-parse --short "$target"))"
    say "$SERVED after:  ${after_sha:0:16}"
    [ "$before_sha" = "$after_sha" ] && say "-> served file UNCHANGED: no restart would be needed" \
                                     || say "-> served file CHANGES: a restart would be required"
    if [ "$drift" -ne 0 ]; then
        say ""
        say "-> $drift tracked file(s) are modified in this checkout, so a real deploy would REFUSE:"
        git status --porcelain --untracked-files=no | sed 's/^/       /'
        # Whether that drift matters is answerable, so answer it rather than leaving it as a warning.
        if git diff --quiet "$TAG" -- "$SERVED" 2>/dev/null; then
            say "   the local $SERVED is IDENTICAL to $TAG — the edit is already committed upstream,"
            say "   so --force would discard nothing."
        else
            say "   the local $SERVED DIFFERS from $TAG — inspect before forcing:"
            say "     git -C $REPO diff $TAG -- $SERVED"
        fi
    fi
    echo "== dry run only, nothing done =="
    exit 0
fi

# ── backup, then move the checkout ────────────────────────────────────────────────────────────────
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="${BACKUP_DIR:-$(dirname "$REPO")}/hazync-deploy-backup-$TS.tar.gz"
tar czf "$BACKUP" -C "$REPO" coordinator/ 2>/dev/null && say "backup: $BACKUP ($(du -h "$BACKUP" | cut -f1))" \
    || say "WARNING: backup failed — continuing, the checkout is recoverable from git regardless"

git fetch --tags --quiet origin || die "fetch failed — not deploying against a stale remote"
check_ref_is_not_a_stale_local_branch
git rev-parse --verify --quiet "${TAG}^{commit}" >/dev/null || die "no such tag/ref: $TAG"
git -c advice.detachedHead=false checkout -f "$TAG" --quiet || die "checkout failed"
say "now at: $(git describe --tags --always) ($(git rev-parse --short HEAD))"

still=$(git status --porcelain --untracked-files=no | wc -l)
[ "$still" -eq 0 ] || die "checkout left $still modified file(s) — refusing to report success"
say "tracked drift: 0"

# ── restart only if the service's own file moved ──────────────────────────────────────────────────
after_sha=$(sha256sum "$SERVED" 2>/dev/null | cut -d' ' -f1)
if [ "$before_sha" = "$after_sha" ]; then
    say "$SERVED unchanged — NOT restarting (a live fleet is mid-submission; a needless restart costs them)"
else
    say "$SERVED changed ${before_sha:0:12} -> ${after_sha:0:12} — restarting $UNIT"
    python3 -c "import ast,sys; ast.parse(open('$SERVED').read())" || die "the new $SERVED does not parse — NOT restarting"
    systemctl restart "$UNIT" || die "restart failed"
    sleep 5
fi

# ── verify, rather than assume ────────────────────────────────────────────────────────────────────
active=$(systemctl is-active "$UNIT" 2>/dev/null)
[ "$active" = active ] || die "$UNIT is '$active' after deploy"
say "$UNIT: active"

PORT="${COORD_PORT:-8899}"
if command -v curl >/dev/null; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://127.0.0.1:$PORT/api/state?slim=1" || echo 000)
    [ "$code" = 200 ] && say "/api/state: 200" || die "/api/state returned $code — the box is at $TAG but not serving"
fi

echo "== deployed $TAG =="
echo "   git -C $REPO describe --tags   now answers what is running, truthfully."
