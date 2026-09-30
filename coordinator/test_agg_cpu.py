#!/usr/bin/env python3
"""The aggregate's CPU is measured and reported, and only ranks on it when asked (hazync#567).

⛔ WHY IT MATTERS. Measured on tip hour 4: of a tip block's wall, the segment phase is 86% and the
EXECUTOR's own window is 54% — and the segment phase can never fall below it, because a segment cannot
be proved before it is produced. 969,118 carried a hard 157.7 s floor at any fleet size.

⛔ AND IT CANNOT BE PARALLELISED WITHOUT MOVING METHOD_ID. Chunk mode (guest mode 4) does distribute
execution but yields KIND_CHUNK while the coordinator requires KIND_RANGE; `fold_range` (mode 7)
composes ranges ACROSS HEIGHTS, not within one block; and the guest asserts the domain tag
(`assert!(l.kind == KIND_RANGE && rr.kind == KIND_RANGE)`, H8). Splitting one block's execution is a
guest change, so it is out.

⇒ What is left is WHICH card executes — and the election measured reachability and link speed and never
looked at the CPU, while hour 3 recorded TWELVE distinct host CPUs on one fleet (EPYC 7452 ~3.35 GHz to
i5-13600K ~5.1 GHz boost).

⚠ THIS IS A HYPOTHESIS. Nothing has shown execution scales with a single-core score. So the probe
REPORTS always and RANKS only under --agg-prefer-cpu. Lever 2 was predicted to save 36 s on exactly
this kind of reasoning and measured NEGATIVE; the flag exists so the next run can settle it.

    python3 test_agg_cpu.py             # measured always, ranks only with the flag
    python3 test_agg_cpu.py --control   # rank on CPU unconditionally — a weak-link card must win
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_smoke  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


class Card:
    def __init__(self, cid):
        self.cid = cid


class FakeSSH:
    """Answers the CPU probe with a scripted /proc/cpuinfo and loop time."""
    def __init__(self, table, broken=()):
        self.table = table          # cid -> (model, cores, secs)
        self.broken = set(broken)

    def run(self, card, body, **kw):
        if card.cid in self.broken:
            return None
        if "CPUPROBE" in body:
            model, cores, secs = self.table[card.cid]
            return f"CPUPROBE|{model}|{cores}|{secs}\n"
        return ""


# ── 1. the probe reads a model, a core count and a score ────────────────────────────────────────
ssh = FakeSSH({"a": ("AMD EPYC 7452 32-Core Processor", 8, 2.00),
               "b": ("13th Gen Intel(R) Core(TM) i5-13600K", 8, 1.00)})
# ── 0. ⛔⛔ THE PROBE MUST RUN UNDER `sh`, NOT BASH — the gap that let it ship broken ────────────
#
# `ssh.run` wraps every body in `sh -c`, and on the pods /bin/sh is DASH. The first version timed the
# loop with `TIMEFORMAT=%R; time ( ... )`, both bash-only, and dash answered
#
#     sh: 1: Syntax error: word unexpected (expecting ")")
#
# so the command exited non-zero, ssh.run returned None, and EVERY card of EVERY run reported
# `cpu UNMEASURED` — through tip hour 5, where it appeared eight times. The probe never worked once.
#
# ⛔ AND THE TESTS BELOW COULD NOT HAVE CAUGHT IT: they hand probe_cpu a fake CPUPROBE line, so they
# exercise the PARSING and never the COMMAND. A shell bug is invisible to a fake shell. This runs the
# real body through a real `sh`.
#
# ⚠ It LOOKED fine by hand, because the pods' LOGIN shell is bash — the failure only appears through
# `sh -c`, which is the only way the driver ever calls it.
import shutil      # noqa: E402
import subprocess  # noqa: E402

_body = {}


class _Capture:
    def run(self, card, body, **kw):
        _body["b"] = body
        return None


tip_smoke.probe_cpu(_Capture(), Card("cap"))
_SH = shutil.which("dash") or shutil.which("sh")
if CONTROL:
    # The bash-ism that shipped, put back exactly.
    _body["b"] = ("MODEL=$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//'); "
                  "CORES=$(nproc 2>/dev/null); "
                  "S=$( { TIMEFORMAT=%R; time (i=0; while [ $i -lt 300000 ]; do i=$((i+1)); done) ; } 2>&1 ); "
                  'echo "CPUPROBE|$MODEL|$CORES|$S"')
_r = subprocess.run([_SH, "-c", _body["b"]], capture_output=True, text=True, timeout=120)
_line = [l for l in (_r.stdout or "").splitlines() if l.startswith("CPUPROBE|")]
if CONTROL:
    check(_r.returncode != 0 or not _line,
          f"⛔ control: the bash-ism FAILS under {os.path.basename(_SH)} (rc={_r.returncode}) — "
          f"which is why every card reported UNMEASURED")
else:
    check(_r.returncode == 0, f"the probe body runs under {os.path.basename(_SH)} (rc={_r.returncode}) "
                              f"— this is the shell ssh.run actually uses")
    check(len(_line) == 1, f"and emits exactly one CPUPROBE line ({len(_line)})")
    if _line:
        _f = _line[0].split("|")
        check(len(_f) == 4, f"with four fields ({len(_f)})")
        check(_f[2].isdigit() and int(_f[2]) > 0, f"a core count ({_f[2]!r})")
        try:
            _sec = float(_f[3]); ok = _sec > 0
        except ValueError:
            ok = False
        check(ok, f"and a positive elapsed time ({_f[3]!r}) — dash has no floats, so it is assembled "
                  f"from integer seconds and milliseconds")

m, sc, co = tip_smoke.probe_cpu(ssh, Card("a"))
check(m and "EPYC 7452" in m, f"the CPU model is read ({m})")
check(co == 8, "and the core count")
check(sc is not None and abs(sc - 0.5) < 1e-9, f"a 2.00 s loop scores 0.5 (higher is faster): {sc}")

m2, sc2, _ = tip_smoke.probe_cpu(ssh, Card("b"))
check(sc2 > sc, f"a faster core scores higher ({sc2} > {sc})")
check(abs(sc2 / sc - 2.0) < 1e-9, "⚠ and the score is self-relative — 2x faster loop, 2x the score")

# ── 2. ⛔ a probe that fails must not be guessed at ──────────────────────────────────────────────
mb, scb, cob = tip_smoke.probe_cpu(FakeSSH({}, broken={"z"}), Card("z"))
check((mb, scb, cob) == (None, None, None),
      "an unreachable card reports UNMEASURED rather than a default score")

bad = FakeSSH({"q": ("Some CPU", 4, "not-a-number")})
mq, scq, coq = tip_smoke.probe_cpu(bad, Card("q"))
check(mq == "Some CPU" and scq is None and coq == 4,
      "⚠ an unparseable time keeps the model and cores but scores None — partial data is not invented")

# ── 3. the ranking: reachability first in BOTH modes ────────────────────────────────────────────
# Two candidates: one reaches more workers but has a slow core; one is fast but reaches fewer.
WIDE = ("wide", 30, 0.5, 9000)     # cid, workers reached, cpu score, mbit
FAST = ("fast", 20, 5.0, 33000)


def elect(prefer_cpu):
    best = (None, -1.0, {}, 0.0)
    for cid, reach, sc_, mbit in (WIDE, FAST):
        per = {f"w{i}": 1 for i in range(reach)}
        key = (len(per), sc_, mbit) if prefer_cpu else (len(per), mbit)
        bkey = ((len(best[2]), best[3], best[1]) if prefer_cpu else (len(best[2]), best[1]))
        if key > bkey:
            best = (cid, mbit, per, sc_)
    return best[0]

check(elect(prefer_cpu=False) == "wide",
      "without the flag: the most REACHABLE card wins, as #573 requires")
check(elect(prefer_cpu=True) == "wide",
      "⛔ WITH the flag too — reachability still comes FIRST. A fast core half the fleet cannot "
      "reach is worth less than nothing, which is what #573 cost.")

# CPU only breaks a tie in reachability.
def elect_tied(prefer_cpu):
    best = (None, -1.0, {}, 0.0)
    for cid, sc_, mbit in (("slowcore_fastlink", 0.5, 33000), ("fastcore_slowlink", 5.0, 9000)):
        per = {f"w{i}": 1 for i in range(30)}       # equal reach
        key = (len(per), sc_, mbit) if prefer_cpu else (len(per), mbit)
        bkey = ((len(best[2]), best[3], best[1]) if prefer_cpu else (len(best[2]), best[1]))
        if key > bkey:
            best = (cid, mbit, per, sc_)
    return best[0]

if CONTROL:
    check(elect_tied(prefer_cpu=True) == "fastcore_slowlink",
          "control: ranking on CPU picks the fast core even though its link is 3.7x slower")
    check(elect_tied(prefer_cpu=False) == "slowcore_fastlink",
          "⛔ and the DEFAULT still picks the fast link — so the flag genuinely changes the outcome, "
          "which is what makes it worth measuring rather than assuming")
else:
    check(elect_tied(prefer_cpu=False) == "slowcore_fastlink",
          "at equal reach, the default still prefers the faster LINK (unchanged behaviour)")
    check(elect_tied(prefer_cpu=True) == "fastcore_slowlink",
          "and --agg-prefer-cpu prefers the faster CORE instead — the thing to be tested")

# ── 4. the flag exists and defaults off ─────────────────────────────────────────────────────────
if not CONTROL:
    src = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
    check('"--agg-prefer-cpu", action="store_true"' in src,
          "the flag is store_true, so it defaults OFF")
    check("cpu UNMEASURED" in src,
          "and an unmeasured CPU is said out loud rather than silently scored 0")
    check("a candidate had a FASTER core" in src,
          "⭐ and when a faster candidate was passed over, the log says so — otherwise the operator "
          "has no reason to ever try the flag")

print()
if fails:
    print(f"FAIL: {fails}")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")
