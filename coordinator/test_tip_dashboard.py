#!/usr/bin/env python3
"""Tests for tip_dashboard.py — the fleet→dashboard feed (Phase 5 ⑨).

Every failure this file pins is SILENT in production: the dashboard renders, the numbers look
plausible, and they are wrong. The round-trip cases are checked against `collect.py`'s ACTUAL reader
where it is available, so this fails if the contract on the other side ever moves.

  python3 test_tip_dashboard.py            # assertions; exit 0 on success
  python3 test_tip_dashboard.py --control  # GPU-name escaping is removed; MUST fail
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_dashboard as tdash   # noqa: E402

if CONTROL:
    # ⛔ THE ESCAPE REMOVED: write the GPU name with its spaces intact. The line then has seven
    # whitespace-separated fields, `len(f) >= 6` still passes, and the reader takes `gpu` as the first
    # word — silently, for every card.
    tdash.escape_gpu = lambda gpu: str(gpu)

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


CARDS = [
    {"cid": "hz-a", "ip": "10.0.0.1", "port": 22, "price": 0.34, "gpu": "NVIDIA GeForce RTX 4090",
     "pod_id": "abc123"},
    {"cid": "hz-b", "ip": "10.0.0.2", "port": 40022, "price": 0.69, "gpu": "NVIDIA L40S"},
]

# ── 1. the round trip, against the reader that actually consumes this ─────────────────────────────
txt = tdash.pods_txt(CARDS)
got = tdash.parse_pods_txt(txt)
check(set(got) == {"hz-a", "hz-b"}, f"every card survives the round trip ({sorted(got)})")
check(got["hz-a"]["gpu"] == "NVIDIA GeForce RTX 4090",
      f"⛔ a multi-word GPU name comes back INTACT ({got['hz-a']['gpu']!r}) — unescaped it is read as "
      f"its first word and the rest is discarded")
check(got["hz-a"]["cost_hr"] == 0.34 and got["hz-b"]["cost_hr"] == 0.69,
      "the real RunPod price survives, and stays a float")
check(got["hz-b"]["port"] == "40022", "a non-default ssh port survives")

# ⛔ EVERY LINE MUST HAVE EXACTLY SIX FIELDS. Seven passes `len(f) >= 6` and corrupts `gpu` quietly.
for line in txt.splitlines():
    check(len(line.split()) == 6,
          f"the line has exactly 6 fields (got {len(line.split())}): {line!r}")

# ── 2. the silent drop: a 5-field line vanishes without an error ──────────────────────────────────
# Demonstrated against the reader, so the refusal is justified by behaviour rather than by assertion.
short = "pid hz-c 10.0.0.3 22 0.34\n"
check(tdash.parse_pods_txt(short) == {},
      "⛔ a 5-field line is silently DROPPED by the reader — this is why a missing GPU is refused")
try:
    tdash.pods_line("hz-c", "10.0.0.3", 22, price=0.34, gpu=None)
    check(False, "a card with no GPU string must be refused")
except tdash.FeedRefused as e:
    check("vanish" in str(e), f"a card with no GPU is refused, with the reason ({str(e)[:52]}…)")

# ── 3. one bad price stops the dashboard for EVERY card ───────────────────────────────────────────
try:
    tdash.parse_pods_txt("pid hz-d 10.0.0.4 22 n/a RTX_4090\n")
    check(False, "the reader must raise on a non-numeric price (that is the point)")
except ValueError:
    check(True, "⛔ the reader raises on a non-numeric price — one card would stop the whole collector")
try:
    tdash.pods_line("hz-d", "10.0.0.4", 22, price=None, gpu="RTX 4090")
    check(False, "a card with no price must be refused")
except tdash.FeedRefused as e:
    check("ALL of them" in str(e), "a card with no price is refused before it can stop the collector")

# ── 4. the name is a field AND a path ─────────────────────────────────────────────────────────────
for bad, why in [("hz a", "whitespace splits into two fields and shifts every field after it"),
                 ("../esc", "a path component escapes the stream directory"),
                 ("a/b", "a slash writes outside stream/"),
                 ("", "an empty name is not addressable")]:
    try:
        tdash.pods_line(bad, "10.0.0.5", 22, price=0.1, gpu="RTX 4090")
        check(False, f"card name {bad!r} must be refused — {why}")
    except tdash.FeedRefused:
        check(True, f"card name {bad!r} is refused — {why}")

# ── 5. all or nothing ─────────────────────────────────────────────────────────────────────────────
try:
    tdash.pods_txt(CARDS + [{"cid": "hz-e", "ip": "10.0.0.9", "port": 22, "price": 0.3, "gpu": ""}])
    check(False, "one unrepresentable card must fail the whole file")
except tdash.FeedRefused:
    check(True, "⛔ one unrepresentable card fails the WHOLE file — a partial pods.txt silently "
                "shrinks the fleet the run is paying for")
try:
    tdash.pods_txt([CARDS[0], dict(CARDS[0])])
    check(False, "a duplicate card name must be refused")
except tdash.FeedRefused as e:
    check("overwrite" in str(e), "a duplicate name is refused — both would share one stream file")

# ── 6. writing: atomic, and never partial ─────────────────────────────────────────────────────────
d = tempfile.mkdtemp(prefix="feed_")
n = tdash.write_feed(d, CARDS)
check(n == 2, f"write_feed reports the cards written ({n})")
check(os.path.isdir(os.path.join(d, "stream")), "stream/ is created — tip-stream.sh does not mkdir it")
check(not os.path.exists(os.path.join(d, "pods.txt.tmp")), "no .tmp is left behind")

# ⛔ A BAD CARD MUST NOT LEAVE A TRUNCATED pods.txt UNDER A RUNNING COLLECTOR.
before = open(os.path.join(d, "pods.txt")).read()
try:
    tdash.write_feed(d, CARDS + [{"cid": "hz-f", "ip": "1.2.3.4", "port": 22, "price": 0.1, "gpu": ""}])
except tdash.FeedRefused:
    pass
check(open(os.path.join(d, "pods.txt")).read() == before,
      "a refused write leaves the PREVIOUS pods.txt untouched, not a truncated one")

# ── 7. t0 is the SESSION clock, and moving it destroys the history ───────────────────────────────
tdash.write_t0(d, 1789918763.353)
check(abs(float(open(os.path.join(d, "t0")).read()) - 1789918763.353) < 0.001,
      "t0 round-trips to the millisecond — the dashboard's whole elapsed clock counts from it")

# ⛔ MEASURED AGAINST THE REAL COLLECTOR, not assumed. collect.py's blocks_from_cards does
#     if since and e["t0"] < since: continue
# so every height that began before t0 is DROPPED. On a two-block capture, t0=1000 yields [100, 101]
# and t0=1020 yields [101]. Advancing t0 per block would make a 24-hour session read "1 block today"
# for its whole duration, with one bar on the chart and the cost of one block — all looking healthy.
back = tdash.write_t0(d, 1789999999.0)
check(abs(back - 1789918763.353) < 0.001,
      f"⛔ a SECOND mark does NOT move t0 ({back}) — moving it deletes every block already proved "
      f"in the session")
check(abs(float(open(os.path.join(d, "t0")).read()) - 1789918763.353) < 0.001,
      "and the file on disk still holds the session's original start")
check(abs(tdash.write_t0(d, 1789999999.0, once=False) - 1789999999.0) < 0.001,
      "once=False is the explicit override, for a genuinely new session")

# ── 8. the streamer is invoked by argv, never a shell string ──────────────────────────────────────
argv, env = tdash.stream_cmd("/run dir", "start", script="./tip-stream.sh", key="/k/id", log_dir="/L")
check(isinstance(argv, list) and argv == ["./tip-stream.sh", "start"],
      "tip-stream.sh is invoked as argv")
check(env["HAZYNC_RUNDIR"] == "/run dir",
      "⛔ a rundir with a space stays ONE value — via a shell string it would become two arguments "
      "and the streamer would write somewhere else entirely")
check(env["HAZYNC_SSH_KEY"] == "/k/id" and env["LOG_DIR"] == "/L",
      "the key and the remote log dir are passed through the environment")
try:
    tdash.stream_cmd("/r", "restart", script="./tip-stream.sh")
    check(False, "an unknown action must be refused")
except tdash.FeedRefused:
    check(True, "an unknown tip-stream action is refused rather than passed through")

# ── 9. a dead feed is not a quiet card ────────────────────────────────────────────────────────────
st = tdash.staleness({"a": 1000.0, "b": 940.0, "c": None}, now=1001.0, limit_s=15.0)
check(st["live"] == ["a"] and st["stale"] == ["b"] and st["never"] == ["c"],
      f"live/stale/never are told apart ({st['live']}/{st['stale']}/{st['never']})")
check(not st["ok"], "and the verdict is not ok while any card is stale or unseen")
check(tdash.staleness({"a": 1000.0}, now=1001.0)["ok"], "a fully live feed is ok")

# ── 9b. last_epochs reads real files, including the awkward ones ─────────────────────────────────
sd = os.path.join(d, "stream")
open(os.path.join(sd, "hz-a.csv"), "w").write(
    "1789918760.100,55,21000,61,310,2100,9500,proving,4,530,965500\n"
    "1789918761.200,57,21000,61,312,2100,9500,proving,5,530,965500\n")
# ⛔ A TORN LAST LINE IS NORMAL. tip-stream.sh appends over a reconnecting ssh, so the tail can end
# mid-write. Taking the last line blindly yields a ValueError or a bogus epoch; walk back instead.
open(os.path.join(sd, "hz-b.csv"), "w").write(
    "1789918700.000,50,20000,60,300,2100,9500,proving,1,530,965500\n"
    "17899187")
open(os.path.join(sd, "hz-d.csv"), "w").write("")          # created, never written to
ep = tdash.last_epochs(d, ["hz-a", "hz-b", "hz-c", "hz-d"])
check(abs(ep["hz-a"] - 1789918761.2) < 0.01, f"the newest epoch is read ({ep['hz-a']})")
check(abs(ep["hz-b"] - 1789918700.0) < 0.01,
      f"⛔ a TORN final line is skipped and the last COMPLETE one is used ({ep['hz-b']}) — the stream "
      f"is appended over a reconnecting ssh and can end mid-write")
check(ep["hz-c"] is None, "a card with no stream file at all reads None, not 0")
check(ep["hz-d"] is None, "an empty stream file reads None, not 0 — 0 would look infinitely stale")

st = tdash.staleness(ep, now=1789918763.0, limit_s=15.0)
check(st["live"] == ["hz-a"] and st["stale"] == ["hz-b"] and sorted(st["never"]) == ["hz-c", "hz-d"],
      f"the real files give the right verdict (live={st['live']} stale={st['stale']})")

# ── 9c. DashboardFeed: the order that matters ────────────────────────────────────────────────────
calls = []
d2 = tempfile.mkdtemp(prefix="feed2_")
fd = tdash.DashboardFeed(d2, script="./tip-stream.sh", run=lambda a, e: calls.append((a, e)),
                         key="/k/id")
check(fd.start(CARDS) == 2, "start() writes pods.txt for every card")
check(calls and calls[0][0] == ["./tip-stream.sh", "start"],
      "and starts the streamer by argv")
check(os.path.exists(os.path.join(d2, "pods.txt")), "pods.txt is on disk")
check(not os.path.exists(os.path.join(d2, "t0")),
      "⛔ start() does NOT write t0 — the streamer runs BEFORE the clock, and a t0 written here would "
      "back-date the run by the whole of phase 0")
fd.mark_t0(1789918763.353)
check(os.path.exists(os.path.join(d2, "t0")), "mark_t0() is what declares the clock")
check(abs(fd.mark_t0(1789999999.0) - 1789918763.353) < 0.001,
      "a later block in the SAME session does not move the clock")

# ⛔ BUT A NEW SESSION MUST NOT INHERIT THE LAST ONE'S CLOCK, or it counts the previous session's
# blocks as its own. start() is the session boundary and clears t0.
fd.start(CARDS)
check(not os.path.exists(os.path.join(d2, "t0")),
      "start() clears t0 — that is what makes mark_t0 write-once safe across sessions")
check(abs(fd.mark_t0(1789999999.0) - 1789999999.0) < 0.001,
      "and the new session sets its own clock")
fd.stop()
check(calls[-1][0] == ["./tip-stream.sh", "stop"], "stop() stops the streamer")

# ⛔ A BAD CARD MUST NOT START A STREAMER AGAINST A FILE THAT WAS NEVER WRITTEN.
calls2 = []
fd2 = tdash.DashboardFeed(tempfile.mkdtemp(prefix="feed3_"), script="./s.sh",
                          run=lambda a, e: calls2.append(a))
try:
    fd2.start(CARDS + [{"cid": "bad", "ip": "1.2.3.4", "port": 22, "price": 0.1, "gpu": ""}])
    check(False, "an unrepresentable card must stop start()")
except tdash.FeedRefused:
    check(calls2 == [], "a refused feed never starts the streamer — it would stream into nothing")

# ── 9d. the join: addressing from the Card, money from the fleet ─────────────────────────────────
class FakeCard:
    def __init__(self, cid, ip, port): self.cid, self.ip, self.port = cid, ip, port


RUN = [FakeCard("hz-a", "10.0.0.1", 22), FakeCard("hz-b", "10.0.0.2", 40022)]
FLEET = [{"id": "hz-a", "price": 0.34, "gpu": "NVIDIA GeForce RTX 4090", "pod_id": "pod1"},
         {"id": "hz-b", "price": 0.69, "gpu": "NVIDIA L40S"}]

j = tdash.feed_records(RUN, FLEET)
check([r["cid"] for r in j["records"]] == ["hz-a", "hz-b"], "every running card gets a record")
check(j["records"][0]["price"] == 0.34 and j["records"][0]["gpu"] == "NVIDIA GeForce RTX 4090",
      "the price and GPU come from the fleet entry")
check(j["records"][0]["ip"] == "10.0.0.1" and j["records"][1]["port"] == 40022,
      "the address comes from the Card")
check(j["unassigned"] == [], "nothing is unassigned when the run uses the whole fleet")
check(tdash.pods_txt(j["records"]).count("\n") == 2, "and the join feeds pods_txt directly")

# ⛔ NEITHER SILENT FALLBACK IS ACCEPTABLE. price=0 under-reports a run with no budget cap; dropping
# the card hides a pod that is proving and being billed.
try:
    tdash.feed_records(RUN + [FakeCard("hz-z", "10.0.0.9", 22)], FLEET)
    check(False, "a card with no fleet entry must be refused")
except tdash.FeedRefused as e:
    check("no budget cap" in str(e) and "being billed" in str(e),
          f"a card with no fleet entry is refused, naming both bad fallbacks ({str(e)[:44]}…)")

# ⚠ A SPARE IS REPORTED, NOT REFUSED — holding spares is legitimate, but they are being paid for and
# will not appear in the dashboard's cost.
j2 = tdash.feed_records([RUN[0]], FLEET)
check(j2["unassigned"] == ["hz-b"],
      f"a rented card not in the run is REPORTED as unassigned ({j2['unassigned']}) — it is still "
      f"being billed and will not show in the dashboard's cost")
check(len(j2["records"]) == 1, "and it is not silently added to the run")

# ── 10. cross-check against the REAL reader, when it is present ───────────────────────────────────
# ⚠ tools/live/ arrives with #427. Until it merges this cannot run, and a silent skip would be a check
# that cannot fail — so it reports its own status explicitly and is NOT counted as a pass.
live = os.path.join(HERE, os.pardir, "tools", "live", "collect.py")
if os.path.exists(live):
    import importlib.util
    spec = importlib.util.spec_from_file_location("hz_collect", live)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = os.path.join(d, "pods.txt")
    real = mod.read_pods(os.path.dirname(p))
    check(real == tdash.parse_pods_txt(open(p).read()),
          "⛔ our mirror of read_pods matches collect.py's ACTUAL reader, field for field")
    check(real["hz-a"]["gpu"] == "NVIDIA GeForce RTX 4090",
          "and the real reader recovers the full GPU name")
else:
    print("  ....  DEFERRED: tools/live/collect.py absent (arrives with #427) — the cross-check "
          "against the real reader did NOT run and is not counted as a pass")

EXPECTED_CONTROL_FAILURES = {
    "the line has exactly 6 fields",
    "a multi-word GPU name comes back INTACT",
}

print()
if CONTROL:
    got_f = set(fails)
    hit = {e for e in EXPECTED_CONTROL_FAILURES if any(e in f for f in got_f)}
    if hit == EXPECTED_CONTROL_FAILURES:
        print("CONTROL OK — GPU-name escaping was removed and the assertions that detect it failed, "
              "as they must:")
        for e in sorted(hit):
            print(f"  - {e}")
        sys.exit(0)
    print("CONTROL FAILED — an unescaped GPU name went undetected.")
    for e in sorted(EXPECTED_CONTROL_FAILURES - hit):
        print(f"  should have failed and did not: {e}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
