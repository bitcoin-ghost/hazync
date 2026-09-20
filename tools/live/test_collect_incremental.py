#!/usr/bin/env python3
"""Tests for collect.py's incremental stream reader.

The whole-file reader was correct and slow; this proves the fast one is still correct. The central
case is EQUIVALENCE: feed the same capture to both paths and require identical output, because an
incremental reader that is merely plausible produces a dashboard that is merely plausible — money
that drifts, blocks that vanish, and nothing anywhere that says so.

  python3 test_collect_incremental.py            # assertions; exit 0 on success
  python3 test_collect_incremental.py --control  # the partial-line guard is removed; MUST fail
"""
import importlib.util
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("collect", os.path.join(HERE, "collect.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


BASE = 1789900000


def row(i, h=None, phase="proving"):
    h = h if h is not None else 965500 + i // 600
    return f"{BASE+i}.0,90,21000,60,300,2100,9500,{phase},{i%30},30,{h}"


def rundir(rows_by_card):
    d = tempfile.mkdtemp(prefix="coll_")
    os.makedirs(os.path.join(d, "stream"))
    with open(os.path.join(d, "pods.txt"), "w") as p:
        for name in rows_by_card:
            p.write(f"pid {name} 10.0.0.1 22 0.3400 RTX_4090\n")
    for name, rows in rows_by_card.items():
        with open(os.path.join(d, "stream", f"{name}.csv"), "w") as fh:
            fh.write("".join(r + "\n" for r in rows))
    return d


def append(d, name, rows, newline=True):
    with open(os.path.join(d, "stream", f"{name}.csv"), "a") as fh:
        fh.write("".join(r + "\n" for r in rows) if newline else "".join(rows))


def comparable(cards):
    """Everything the dashboard actually consumes. Traces included — a wrong window is a wrong frame."""
    return [{k: v for k, v in sorted(cd.items())} for cd in cards]


if CONTROL:
    # ⛔ THE PARTIAL-LINE GUARD REMOVED: advance the offset over everything read, including an
    # incomplete final line. That line is then counted here AND again when its remainder arrives, so
    # Neither the fragment nor its remainder then has enough fields to count, so the sample is LOST
    # — `observed_s`, which IS the money, silently undercounts by one per torn read. The stream is
    # appended over a reconnecting ssh, so torn reads are normal, not exceptional.
    src = open(os.path.join(HERE, "collect.py"), encoding="utf8").read()
    assert 'cut = blob.rfind(b"\\n") + 1' in src, "the guard this control removes is not there"
    src = src.replace('cut = blob.rfind(b"\\n") + 1', "cut = len(blob)")
    c = importlib.util.module_from_spec(spec)
    exec(compile(src, "collect_control.py", "exec"), c.__dict__)

# ── 1. EQUIVALENCE: incremental == whole-file, over many ticks ────────────────────────────────────
rows = [row(i) for i in range(1800)]              # 30 minutes, spanning 3 blocks
d = rundir({"hz-a": rows[:600], "hz-b": rows[:600]})
cursors = {}
now = BASE + 600
inc = c.read_streams(d, now, cursors)             # tick 1
for start in (600, 1200):
    append(d, "hz-a", rows[start:start + 600])
    append(d, "hz-b", rows[start:start + 600])
    now = BASE + start + 600
    inc = c.read_streams(d, now, cursors)
whole = c.read_streams(d, now)                    # the same capture, read from scratch

check(comparable(inc) == comparable(whole),
      "⛔ three incremental ticks produce EXACTLY what one whole-file read produces")
check(inc[0]["observed_s"] == 1800,
      f"every sample is counted once ({inc[0]['observed_s']} of 1800) — observed_s IS the money")
check(sorted(inc[0]["block_s"]) == sorted(whole[0]["block_s"]),
      "the per-block aggregates match, block for block")
check(len(inc[0]["t"]) == len(whole[0]["t"]),
      f"and the trace window matches ({len(inc[0]['t'])} vs {len(whole[0]['t'])} points)")

# ── 2. a torn final line is not lost and not double-counted ───────────────────────────────────────
d = rundir({"hz-a": [row(i) for i in range(10)]})
cursors = {}
c.read_streams(d, BASE + 10, cursors)
before = cursors["hz-a"].secs
append(d, "hz-a", ["1789900010.0,90,21000,60,300,2100,95"], newline=False)   # cut mid-write
mid = c.read_streams(d, BASE + 11, cursors)
check(mid[0]["observed_s"] == before,
      f"⛔ a TORN final line is not counted yet ({mid[0]['observed_s']} still {before}) — it is "
      f"incomplete, and counting it would inflate the money")
append(d, "hz-a", ["00,proving,5,30,965500"], newline=True)                  # the rest arrives
done = c.read_streams(d, BASE + 12, cursors)
check(done[0]["observed_s"] == before + 1,
      f"⛔ and when the rest arrives it is counted EXACTLY ONCE ({done[0]['observed_s']}) — a naive "
      f"offset skips the fragment AND its remainder, losing the sample for ever")
check(comparable(done) == comparable(c.read_streams(d, BASE + 12)),
      "the result still equals a whole-file read after a torn line")

# ── 3. truncation and replacement reset the cursor ────────────────────────────────────────────────
# tip-stream.sh starts a new session; seeking to a stale offset in a fresh file would skip the start
# of the run and undercount the money for the rest of it.
d = rundir({"hz-a": [row(i) for i in range(100)]})
cursors = {}
c.read_streams(d, BASE + 100, cursors)
check(cursors["hz-a"].secs == 100, "100 samples counted")
with open(os.path.join(d, "stream", "hz-a.csv"), "w") as fh:                 # truncate, same inode
    fh.write("".join(row(i) + "\n" for i in range(5)))
after = c.read_streams(d, BASE + 5, cursors)
check(after[0]["observed_s"] == 5,
      f"⛔ a TRUNCATED file resets the cursor ({after[0]['observed_s']}) — a stale offset would skip "
      f"the whole of the new session")

d2 = rundir({"hz-a": [row(i) for i in range(50)]})
cur2 = {}
c.read_streams(d2, BASE + 50, cur2)
os.replace(os.path.join(d2, "stream", "hz-a.csv"), os.path.join(d2, "stream", "old.csv"))
with open(os.path.join(d2, "stream", "hz-a.csv"), "w") as fh:                # new inode
    fh.write("".join(row(i) + "\n" for i in range(80)))
rep = [x for x in c.read_streams(d2, BASE + 80, cur2) if x["name"] == "hz-a"][0]
check(rep["observed_s"] == 80, f"a REPLACED file (new inode) resets too ({rep['observed_s']})")

# ── 4. an empty tick changes nothing ──────────────────────────────────────────────────────────────
d = rundir({"hz-a": [row(i) for i in range(20)]})
cursors = {}
a1 = c.read_streams(d, BASE + 20, cursors)
a2 = c.read_streams(d, BASE + 20, cursors)
check(comparable(a1) == comparable(a2),
      "a tick with no new rows returns the same card, not an empty or doubled one")
check(a2[0]["observed_s"] == 20, f"and does not re-count ({a2[0]['observed_s']})")

# ── 5. the fast path is actually fast ─────────────────────────────────────────────────────────────
# The whole point. Asserted as a RATIO against the whole-file read on the same data, so it does not
# depend on this machine being any particular speed.
import time  # noqa: E402

big = [row(i) for i in range(40000)]
d = rundir({f"hz-{k:02d}": big for k in range(6)})
cursors = {}
c.read_streams(d, BASE + 40000, cursors)                  # prime: this one IS a whole-file read
for k in range(6):
    append(d, f"hz-{k:02d}", [row(40000 + i) for i in range(5)])
t = time.time(); c.read_streams(d, BASE + 40005, cursors); inc_s = time.time() - t
t = time.time(); c.read_streams(d, BASE + 40005); whole_s = time.time() - t
check(inc_s < whole_s / 3,
      f"an incremental tick is far cheaper than a whole-file read ({inc_s*1000:.0f} ms vs "
      f"{whole_s*1000:.0f} ms on 6 x 40k lines)")

EXPECTED_CONTROL_FAILURES = {
    "counted EXACTLY ONCE",
    "still equals a whole-file read after a torn line",
}

print()
if CONTROL:
    got = set(fails)
    hit = {e for e in EXPECTED_CONTROL_FAILURES if any(e in f for f in got)}
    if hit == EXPECTED_CONTROL_FAILURES:
        print("CONTROL OK — the partial-line guard was removed and the assertion that detects it "
              "failed, as it must:")
        for e in sorted(hit):
            print(f"  - {e}")
        sys.exit(0)
    print("CONTROL FAILED — a torn line was consumed and nothing noticed.")
    for e in sorted(EXPECTED_CONTROL_FAILURES - hit):
        print(f"  should have failed and did not: {e}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
