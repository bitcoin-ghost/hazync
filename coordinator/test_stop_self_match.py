#!/usr/bin/env python3
"""--stop must not count the shell that invoked it (hazync#491).

⛔ WHY THIS EXISTS. `pgrep -f` matches the whole command LINE, so any process whose ARGUMENTS
mention the worker patterns matches — including the shell running --stop. Observed live:

    stopped (remaining: 1)
    WARNING: 1 process(es) survived --stop. They still take the GPU lock.
    1108693 bash -c cd /workspace || exit 1 ... ./hazync-run-workers.sh 1 --stop ...

1108693 was the invoking shell. Every loop was gone, yet --stop exited 1 and sent the operator
hunting PIDs that did not exist. Same family as `pkill -f` killing its own invoking shell.

  python3 test_stop_self_match.py            # must PASS
  python3 test_stop_self_match.py --control  # the self/ancestor filter removed; MUST FAIL
"""
import os, subprocess, sys, tempfile, textwrap

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok: fails.append(what)

src = open(os.path.join(HERE, "run-workers.sh"), encoding="utf8").read()

# ── 1. the shipped script must filter self and ancestors ─────────────────────────────────────────
check("_mine=" in src and "ps -o ppid=" in src,
      "--stop walks its own parent chain and excludes those PIDs")
check("pgrep -c" not in src.split("--stop")[-1][:2000],
      "and counts with grep -c on a string, not `pgrep -c` (which prints 0 AND exits 1)")

# ── 2. ⛔ THE CENTRAL CASE, executed: an ANCESTOR whose argv mentions the pattern is excluded ─────
# ⚠ Do NOT assert "zero survivors". Other processes on the box legitimately match this (loose)
# pattern — a collector at ~/hazync-flagship-*/ run with --loop matches `hazync-.*-loop` through its
# PATH. The property #491 is about is narrower and machine-independent: the shell that invoked
# --stop, and its ancestors, must not appear.
script = textwrap.dedent('''
    _mine=" $$ "
    _p=$$
    while [ "${_p:-1}" -gt 1 ]; do
        _p=$(ps -o ppid= -p "$_p" 2>/dev/null | tr -d ' ')
        [ -n "$_p" ] || break
        _mine="$_mine $_p "
    done
    echo "MINE=$_mine"
    pgrep -f 'hazync-worker|hazync-.*-loop' 2>/dev/null %s
''')
FILTER = '''| while read -r _pid; do case "$_mine" in *" $_pid "*) ;; *) echo "$_pid" ;; esac; done'''
body = script % ("" if CONTROL else FILTER)

with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
    fh.write(body); path = fh.name
os.chmod(path, 0o755)
# ⚠ the wrapper's ARGV carries the pattern, exactly as the real --stop invocation did
# ⚠ The pattern goes in the script's OWN argv, not a wrapper's. A `bash -c` wrapper may EXEC the
# inner command and vanish, so relying on it made the control flaky — it matched itself only when
# bash chose not to optimise. Passing the pattern as an argument makes $$ match deterministically,
# which is the exact shape of #491: the process doing the counting is itself counted.
out = subprocess.run(["bash", path, "hazync-fold-loop", "hazync-worker-loop"],
                     capture_output=True, text=True, timeout=60)
lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
mine = set(next((l[5:] for l in lines if l.startswith("MINE=")), "").split())
survivors = {l for l in lines if l.isdigit()}
overlap = mine & survivors
check(bool(mine), f"the ancestor chain was captured ({len(mine)} PIDs)")
check(not overlap,
      f"no PID the script is STANDING ON is reported as a survivor "
      f"(overlap: {sorted(overlap) or 'none'}; survivors: {sorted(survivors) or 'none'})")

os.unlink(path)
EXPECTED_CONTROL = {"no PID the script is STANDING ON"}
print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — without the filter, the invoking shell counts itself as a survivor:")
        for f in fails: print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected the self-match to be counted; got {len(fails)} failure(s)")
    sys.exit(1)
if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails)); sys.exit(1)
print("--stop reports survivors it can actually do something about")
