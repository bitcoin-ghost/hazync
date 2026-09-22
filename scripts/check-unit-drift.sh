#!/usr/bin/env bash
# Does the DEPLOYED unit contain anything this repo does not know about? (hazync#168 part C)
#
#   ./scripts/check-unit-drift.sh hazync-proof      # or `localhost`, running on the box itself
#
# ⚠ This said `hazync-coord` until 2026-09-18. That alias points at 152.53.93.164, the ORIGINAL
# coordinator, which was retired that day. The coordinator has been 159.195.207.224 (`hazync-proof`)
# since the 2026-09-16 cutover, so anyone following the old usage line checked a box that is gone.
#
# WHY THIS DIRECTION. The obvious check is "every path a doc names must exist". That check would
# NOT have caught the incident this script exists for. On 2026-08-25 the production coordinator's
# base unit turned out to be a hand-edited hybrid: dead pre-#58 /root paths sitting alongside two
# settings that were live, load-bearing, and present nowhere in this repo --
#
#     COORD_BIND=0.0.0.0        TRUSTED_PROXIES=83.136.255.218
#
# Replacing that base with the repo copy silently dropped both. COORD_BIND fell back to 127.0.0.1,
# which would have stopped the coordinator accepting the nginx proxy from the web box. Every path
# in the repo's unit existed; the danger ran the other way. So the useful question is not "does
# what we wrote down exist" but "is anything running that we never wrote down".
#
# systemd makes the failure silent twice over: `systemctl cat` prints superseded lines as if they
# were live, and `daemon-reload` does not restart, so a bad edit detonates at some arbitrary later
# restart with nothing wrong in any log.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

HOST="${1:-}"
[ -n "$HOST" ] || { echo "usage: $0 <ssh-host>   e.g. $0 hazync-proof   (or localhost, on the box)" >&2; exit 2; }
DROPINS_DIR=coordinator/deploy/dropins
# ⛔ DISCOVER THE UNITS, DO NOT HARDCODE TWO OF THEM.
# This was `hazync-coordinator hazync-bridge` — two units — while the repo declares 30 service files
# and the boxes carry 32. It then printed "no drift: everything running on localhost is declared in
# this repo", which is a claim about the whole box made after checking 1/16th of it.
#
# The unchecked thirty are where quiet damage lives: hazync-coordinator-backup (a changed BACKUP_DIR
# sends backups somewhere nobody looks), the hazync-offsite-*-b2 jobs, hazync-prune-bundles (first
# real --apply 2026-09-27), hazync-sponsor-bot (spends money).
#
# ⚠ The discovery runs ON THE HOST, because that is the only side that knows what is installed. A
# repo-side list would miss exactly the case this check exists for: a unit live on the box and
# absent from the repo.
discover_units() {
    local probe='systemctl list-unit-files "hazync-*.service" --no-pager --plain 2>/dev/null | awk "/\\.service/{sub(/\\.service\$/,\"\",\$1); print \$1}"'
    if [ "$HOST" = localhost ]; then bash -c "$probe" 2>/dev/null
    else ssh -n -o ConnectTimeout=15 "$HOST" "$probe" 2>/dev/null; fi
}
UNITS="${HAZYNC_UNITS:-}"
if [ -z "$UNITS" ]; then
    UNITS="$(discover_units | sort -u | tr '\n' ' ')"
    # ⛔ DISCOVERING NOTHING IS "COULD NOT CHECK", NOT "NOTHING TO CHECK". A host that answers with an
    # empty list is one we failed to ask, and reporting "no drift" for it is the silent pass this
    # whole script exists to prevent.
    [ -n "${UNITS// /}" ] || { echo "COULD NOT CHECK: no hazync-* units discovered on $HOST"; exit 2; }
fi
N_UNITS=$(printf '%s\n' $UNITS | grep -c .)
ALLOW="${HAZYNC_DRIFT_ALLOW:-coordinator/deploy/unit-drift-allow.txt}"

fail=0
cannot=0
note() { echo "  $*"; }
bad()  { echo "DRIFT $*"; fail=1; }
# ⛔ "could not read" IS NOT "no drift", AND IT IS NOT DRIFT EITHER. Until 2026-09-18 an unreadable
# host went through bad(), so a box this script could not reach at all printed "DRIFT FOUND — the box
# is running configuration this repo does not contain" and exited 1. It had read nothing. The check
# contract has a code for this: 0 holds, 1 drift, 2 could not check.
cant() { echo "COULD NOT CHECK $*"; cannot=1; }

# The remote side is read-only and answers in one round trip per unit: `systemctl show` rather than
# `systemctl cat`, because cat prints the FILE and show prints what actually runs.
# ⛔ DIRECTIVES THIS CHECK COULD NOT SEE AT ALL UNTIL 2026-09-22 (hazync#452).
#
# The probe read Environment, ExecStart, User and drop-in FILENAMES -- nothing else. So MemoryHigh,
# MemoryMax and the hardening directives were invisible, and the check reported "no drift:
# everything running on localhost is declared" while hazync-proof ran MemoryHigh=48G against a
# declared 53G.
#
# That is not a cosmetic gap. A MemoryHigh below what the bridge needs is exactly what caused the
# bridge to be OOM-killed 23 times and make ZERO progress overnight 2026-09-18/19 (#413) -- the
# single most expensive incident this repo records. The setting whose drift costs the most was the
# one nothing compared.
#
# Keep this list to directives where a wrong value BREAKS something, not every knob systemd has: a
# check that reports noise gets muted, and a muted check is worse than none.
GUARDED="MemoryHigh MemoryMax MemorySwapMax TasksMax LimitNOFILE Restart RestartSec ProtectSystem ProtectHome PrivateTmp NoNewPrivileges"
export GUARDED

for u in $UNITS; do
    echo
    echo "=== $u on $HOST ==="

    # ⚠ ONLY *.conf. systemd loads drop-ins matching *.conf and ignores everything else, so a parked
    # copy like `height-cap.conf.parked.bak` (found on the bridge 2026-09-18) is inert -- reporting it
    # as drift is a false alarm, and false alarms are how a check like this gets muted.
    # ⛔ A MASKED UNIT IS RETIRED, NOT DRIFTED. systemd reports Restart=no (and empty everything) for a
    # masked unit, so hazync-coordinator on the retired coordinator box -- masked on purpose when the
    # bridge took that machine over -- reported "Restart is no on the box but the repo declares always"
    # on EVERY run. A permanent false positive on a deliberately retired service is precisely how a
    # check like this gets muted, which costs more than the blind spot it was added to close.
    probe="
        echo \"STATE \$(systemctl show $u -p LoadState --value 2>/dev/null)\"
        systemctl show $u -p Environment --value | tr ' ' '\n' | grep -v '^\$' | sed 's/^/ENV /'
        systemctl show $u -p ExecStart --value | grep -oE 'argv\[\]=[^;]*' | sed 's/^/EXEC /'
        systemctl show $u -p User --value | sed 's/^/USER /'
        for d in $GUARDED; do
            v=\$(systemctl show $u -p \$d --value 2>/dev/null)
            [ -n \"\$v\" ] && [ \"\$v\" != infinity ] && [ \"\$v\" != 0 ] && echo \"GUARD \$d=\$v\"
        done
        ls -1 /etc/systemd/system/$u.service.d/ 2>/dev/null | grep '\.conf\$' | sed 's/^/DROPIN /'
        for c in /etc/systemd/system/$u.service.d/*.conf; do
            [ -f \"\$c\" ] || continue
            b=\$(basename \"\$c\")
            echo \"DROPINBODY \$b\"; cat \"\$c\"; echo \"DROPINEND \$b\"
        done
    "
    # ⛔ localhost IS NOT AN SSH HOST. The timer that runs this check runs ON the coordinator, and root
    # there has no authorized_key for root@localhost -- measured 2026-09-18: Permission denied
    # (publickey). Going through ssh anyway would fail every single run with "could not read unit
    # state", which reads as an infrastructure problem, gets muted, and leaves real drift unreported:
    # precisely the silent failure this script exists to catch. So localhost runs the probe directly.
    if [ "$HOST" = localhost ]; then
        remote=$(bash -c "$probe" 2>/dev/null)
    else
        remote=$(ssh -n -o ConnectTimeout=15 "$HOST" "$probe" 2>/dev/null)
    fi
    if [ -z "$remote" ]; then cant "$u: no unit state from $HOST (unreachable, unit absent, or ssh refused)"; continue; fi

    if printf '%s\n' "$remote" | grep -qx 'STATE masked'; then
        note "skip $u is MASKED on $HOST — retired, not drifted"
        continue
    fi
    # ⛔ A UNIT THAT DOES NOT EXIST IS "COULD NOT CHECK", NOT "NO DRIFT". `systemctl show` answers for
    # a name it has never heard of — empty values, exit 0 — so $remote is non-empty and the earlier
    # cant() never fires. Caught by this script's own control (HAZYNC_UNITS=hazync-does-not-exist),
    # which reported "no drift: 1 unit(s) checked, all declared" and exited 0. A check that passes
    # on a unit it cannot see would pass on a whole box named wrongly.
    if printf '%s\n' "$remote" | grep -qx 'STATE not-found'; then
        cant "$u: not installed on $HOST (LoadState=not-found)"
        continue
    fi

    # --- 1. drop-in FILES the repo does not ship ------------------------------------------------
    # A drop-in nobody has committed is config that exists only on one disk. `ratelimit.conf` was
    # exactly this: it is what holds RATE_MAX at 120 rather than the base unit's 1000000, and
    # rebuilding the box from this repo would have quietly restored the million.
    # ⚠ A DROP-IN THAT SETS ONLY ALLOW-LISTED KEYS IS LEGITIMATE BY DEFINITION. The allow-list holds
    # keys that genuinely differ per box (a datadir, a sync destination); a file whose whole purpose
    # is to set them cannot be committed without creating drift on the OTHER box. Flagging it anyway
    # left 6 permanent findings after the scope widened to every unit — and a check that is
    # permanently red gets muted, which is the failure this whole script exists to prevent.
    #
    # ⛔ The exemption is narrow ON PURPOSE: EVERY Environment key in the file must be allow-listed.
    # One unlisted key and the file is reported, because that key is exactly what would be lost.
    dropin_is_all_allowed() {   # $1 = unit, $2 = drop-in filename
        [ -f "$ALLOW" ] || return 1
        local body keys k
        body="$(printf '%s\n' "$remote" | sed -n "/^DROPINBODY $2\$/,/^DROPINEND $2\$/p")"
        keys="$(printf '%s\n' "$body" | grep -E '^Environment=' | sed 's/^Environment=//' \
                 | sed 's/^"//' | cut -d= -f1)"
        [ -n "$keys" ] || return 1          # no Environment keys at all -> not an allow-list case
        while read -r k; do
            [ -n "$k" ] || continue
            grep -qxF "$k" "$ALLOW" || return 1
        done < <(printf '%s\n' "$keys")
        return 0
    }
    while read -r _ f; do
        [ -n "${f:-}" ] || continue
        if [ -f "$DROPINS_DIR/$u-$f" ]; then
            note "ok   drop-in $f is declared"
        elif dropin_is_all_allowed "$u" "$f"; then
            note "ok   drop-in $f sets only allow-listed per-box keys"
        else
            bad "$u: drop-in '$f' is on the box but NOT in $DROPINS_DIR/$u-$f"
        fi
    done < <(printf '%s\n' "$remote" | grep '^DROPIN ')

    # --- 1b. GUARDED directives: the VALUE must match, not merely the key ------------------------
    # ⛔ A DIFFERING VALUE HERE IS A FAILURE, NOT A NOTE. For Environment (below) a per-box value is
    # often legitimate -- paths and proxy lists genuinely differ. For MemoryHigh it is not: the box
    # either has the cap the repo says it needs, or it is one incident away from #413 again.
    #
    # ⛔ BUT IT MUST NORMALISE FIRST, OR IT CRIES WOLF ON CORRECT CONFIG. systemd reports MemoryHigh
    # in BYTES and the repo writes "53G"; it reports ProtectHome as "yes" where the repo writes
    # "true". Comparing those raw marks every healthy unit as drift, and a check that always fails
    # gets muted -- which is strictly worse than the blind spot this replaces.
    #
    # ⚠ A guarded directive the repo does NOT declare is a systemd DEFAULT (TasksMax=77099,
    # LimitNOFILE=524288), not somebody's edit. Those are noted, never failed: we guard the values we
    # have committed to, not every knob systemd exposes.
    norm_val() {
        local v="${1,,}"
        case "$v" in
            true|on|yes)  echo yes; return;;
            false|off|no) echo no;  return;;
        esac
        case "$v" in
            *g) echo $(( ${v%g} * 1024 * 1024 * 1024 ));;
            *m) echo $(( ${v%m} * 1024 * 1024 ));;
            *k) echo $(( ${v%k} * 1024 ));;
            *)  echo "$v";;
        esac
    }
    gdeclared=$( { cat "coordinator/deploy/$u.service" 2>/dev/null
                   cat "$DROPINS_DIR/$u-"*.conf 2>/dev/null; } \
                 | grep -E "^($(echo "$GUARDED" | tr ' ' '|'))=" | sort -u )
    while read -r _ kv; do
        [ -n "${kv:-}" ] || continue
        gk="${kv%%=*}"; gv=$(norm_val "${kv#*=}")
        # ⚠ ANY declared value may be the winning one. systemd's drop-in precedence is alphabetical
        # and this check deliberately does not simulate it (see "Union, not precedence" below), so a
        # directive set in two files must not be called drift just because the first one differs.
        # ⚠ AN ALLOW-LISTED GUARDED DIRECTIVE IS PER-BOX, NOT DRIFT. MemoryHigh/MemoryMax have to be
        # sized against what ELSE lives on the box: the coordinator runs bitcoind at 11.6 GB with no
        # swap and needs 48G/52G, while the tip-bridge box has 24 GB of swap and bitcoind at 3.2 GB
        # and needs 58G/60G. Reporting one as drift led to "fixing" it by deploying the other value,
        # which put MemoryMax=60G on a 62 GB box with nothing to spill to.
        if [ -f "$ALLOW" ] && grep -qxF "$gk" "$ALLOW"; then
            note "ok   $gk=${kv#*=} is an accepted per-box setting (see $(basename "$ALLOW"))"
            continue
        fi
        wants=$(printf '%s\n' "$gdeclared" | grep "^$gk=" | sed "s/^$gk=//")
        if [ -z "$wants" ]; then
            note "note $gk=${kv#*=} is a systemd default (not declared, not guarded)"
        else
            hit=no
            while read -r w; do
                [ -n "$w" ] || continue
                [ "$gv" = "$(norm_val "$w")" ] && { hit=yes; break; }
            done < <(printf '%s\n' "$wants")
            if [ "$hit" = yes ]; then
                note "ok   $gk matches the repo (${kv#*=})"
            else
                bad "$u: $gk is ${kv#*=} on the box but the repo declares $(printf '%s\n' "$wants" | paste -sd'|' -)"
            fi
        fi
    done < <(printf '%s\n' "$remote" | grep '^GUARD ')

    # --- 2. effective settings the repo cannot account for --------------------------------------
    # Union, not precedence: we are asking "could the repo have produced this value at all", which
    # is the question that matters and needs no simulation of systemd's override order.
    declared=$( { cat "coordinator/deploy/$u.service" 2>/dev/null
                  cat "$DROPINS_DIR/$u-"*.conf 2>/dev/null; } \
                | grep -E '^Environment=' | sed 's/^Environment=//' | sort -u )
    while read -r _ kv; do
        [ -n "${kv:-}" ] || continue
        k="${kv%%=*}"
        if printf '%s\n' "$declared" | grep -qxF "$kv"; then
            continue                                   # exact key=value is in the repo
        elif printf '%s\n' "$declared" | grep -q "^$k="; then
            note "note $k differs from every declared value (per-box override)"
        elif [ -f "$ALLOW" ] && grep -qxF "$k" "$ALLOW"; then
            note "ok   $k is an accepted per-box setting (see $(basename "$ALLOW"))"
        else
            bad "$u: $k is set on the box and appears NOWHERE in the repo   ($kv)"
        fi
    done < <(printf '%s\n' "$remote" | grep '^ENV ')

    # --- 3. the binary it actually executes -----------------------------------------------------
    exec_line=$(printf '%s\n' "$remote" | grep '^EXEC ' | head -1 | sed 's/^EXEC argv\[\]=//')
    note "runs: ${exec_line:-<none>}   (user: $(printf '%s\n' "$remote" | grep '^USER ' | head -1 | cut -d' ' -f2-))"
done

echo
if [ "$fail" != 0 ]; then
    echo "DRIFT FOUND — the box is running configuration this repo does not contain."
    echo "Fix by committing it (a drop-in under $DROPINS_DIR), not by deleting it from the box:"
    echo "a setting that is live and undeclared is load-bearing until proven otherwise."
    # A real finding outranks an unreadable unit: drift is definite, and exiting 2 would hide it.
    [ "$cannot" != 0 ] && echo "(and at least one unit could not be read at all — see above)"
    exit 1
fi
if [ "$cannot" != 0 ]; then
    echo "COULD NOT CHECK — no unit state was readable, so this says NOTHING about drift."
    exit 2
fi
# ⛔ SAY WHAT WAS ACTUALLY CHECKED. "everything running on $HOST is declared" was printed after
# inspecting two units out of thirty-two. A count is the difference between a verified claim
# and a slogan, and it is what makes a narrowed run (HAZYNC_UNITS=...) obvious in a log.
echo "no drift: $N_UNITS unit(s) checked on $HOST, all declared in this repo."
exit 0
