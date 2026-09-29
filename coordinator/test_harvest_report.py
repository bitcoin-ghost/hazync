#!/usr/bin/env python3
"""The harvest must not call an absent file a dying pod, and must count what landed (hazync#588).

⛔ WHAT HAPPENED. Tip hour 4's teardown printed:

    harvested 39 log file(s) from 38 card(s)
    ⚠ not fetched (a dying pod refuses connections):
      {'hz-smoke-1': ['agg.err','run.log','prove.log','facts.json'], ...every card...}

Three things wrong with it, and **no data was actually lost**:

  1. `run.log`, `prove.log` and `facts.json` are written by `pod-prove.sh`, the CHUNK-mode path. Mode
     6 never runs it, so those files never existed. Their absence was reported as a pod failure.
  2. The count said 39; **77** files were on disk.
  3. It listed `agg.err` for hz-smoke-1, which HAD been fetched.

⚠ THE TELL was in the report itself: the "missing" files were the LAST N of the list, identically for
every card. A dying-pod race is random; list order is not. I read it, believed it, and told the
operator we had lost per-card evidence — the opposite of the truth.

⇒ A message that names the wrong cause is worse than no message, because it gets quoted.

    python3 test_harvest_report.py             # absent != unreachable; the count is from disk
    python3 test_harvest_report.py --control   # fetch blind and call every failure a dying pod
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_harvest  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


class Card:
    def __init__(self, cid, has):
        self.cid = cid
        self.has = set(has)          # the files that actually exist on this pod
        self.workdir = "/workspace"


class FakeSSH:
    """A pod that has only some files, and can be made unreachable."""
    def __init__(self, unreachable=()):
        self.unreachable = set(unreachable)

    def run(self, card, body, **kw):
        if card.cid in self.unreachable:
            return None
        if "ls -1" in body:
            asked = body.split("ls -1", 1)[1].split("2>/dev/null")[0].split()
            return "\n".join(n for n in asked if n in card.has)
        return ""

    def fetch(self, card, remote, local, **kw):
        if card.cid in self.unreachable:
            return False
        n = remote.rsplit("/", 1)[-1]
        if n not in card.has:
            return False
        with open(local, "w") as fh:
            fh.write("x")
        return True


def run_harvest(cards, ssh, outdir):
    agg = cards[0]
    cmap = {i: c for i, c in enumerate(cards)}
    if CONTROL:
        # The shipped-before shape: fetch blind, everything that fails is "missing".
        got, missed = {}, {}
        for i, card in cmap.items():
            names = tip_harvest.AGG_LOGS if card is agg else tip_harvest.WORKER_LOGS
            d = os.path.join(outdir, "logs", str(card.cid))
            os.makedirs(d, exist_ok=True)
            for n in names:
                if ssh.fetch(card, f"/workspace/{n}", os.path.join(d, n)):
                    got.setdefault(str(card.cid), []).append(n)
                else:
                    missed.setdefault(str(card.cid), []).append(n)
        return {"fetched": got, "missing": missed, "absent": {},
                "cards": len(cmap), "files": sum(len(v) for v in got.values())}
    return tip_harvest.harvest(ssh, cmap, agg, outdir)


# ── the real tip-hour-4 shape: mode 6, so the chunk-mode files do not exist ─────────────────────
AGG_HAS = ["agg.log", "agg.err", "agg-history.log"]
WORKER_HAS = ["aggw.log", "aa.log"]

out = tempfile.mkdtemp(prefix="harv-")
cards = [Card("hz-1", AGG_HAS)] + [Card(f"hz-{i}", WORKER_HAS) for i in range(2, 12)]
man = run_harvest(cards, FakeSSH(), out)

on_disk = sum(len(f) for _r, _d, f in os.walk(os.path.join(out, "logs")))
check(on_disk == 3 + 10 * 2, f"{on_disk} files really landed (3 aggregate + 10 x 2 worker)")

if CONTROL:
    n_miss = sum(len(v) for v in man["missing"].values())
    check(n_miss == 11 * 3,
          f"control reproduces it: {n_miss} files reported as failures when none of them exist")
    check(all("run.log" in v for v in man["missing"].values()),
          "⛔ and every card 'lost' run.log — files that mode 6 never writes")
else:
    check(man["files"] == on_disk,
          f"the count is what landed on disk ({man['files']}), not what was attempted")
    n_abs = sum(len(v) for v in man["absent"].values())
    check(n_abs == 11 * 3, f"{n_abs} files reported as ABSENT — not as a pod failure")
    check(not man["missing"], f"and nothing is reported as unfetchable ({man['missing']})")
    # ⛔ A file that was fetched must never appear in either negative list.
    fetched = {f for v in man["fetched"].values() for f in v}
    neg = {f for v in man["absent"].values() for f in v} | {f for v in man["missing"].values() for f in v}
    check(not (fetched & neg),
          f"⛔ no file appears as both fetched and not-fetched ({fetched & neg})")

# ── a pod that IS unreachable must still be reported as such ────────────────────────────────────
if not CONTROL:
    out2 = tempfile.mkdtemp(prefix="harv2-")
    cards2 = [Card("hz-1", AGG_HAS)] + [Card(f"hz-{i}", WORKER_HAS) for i in range(2, 6)]
    man2 = tip_harvest.harvest(FakeSSH(unreachable={"hz-3"}), {i: c for i, c in enumerate(cards2)},
                               cards2[0], out2)
    check("hz-3" in man2["missing"],
          "⛔ a genuinely unreachable pod IS still reported as missing — the fix must not silence it")
    check("hz-3" not in man2["absent"],
          "and is not filed as 'absent', which would hide real evidence loss")
    check(all(c not in man2["missing"] for c in ("hz-2", "hz-4")),
          "while its healthy neighbours are unaffected")
    shutil.rmtree(out2, ignore_errors=True)

shutil.rmtree(out, ignore_errors=True)
print()
if fails:
    print(f"FAIL: {fails}")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")
