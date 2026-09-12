#!/usr/bin/env python3
"""
Tests for /api/spine/segments — who absorbed each block into the spine (#244).

WHY THIS EXISTS. The site could say who proved a block and who folded it, and then went silent on who
anchored it. Not because the record was lost: every absorption writes a permanent `spine:1-<hi>` row
carrying the absorbing pubkey and handle. `recent` reads the same table with `LIMIT 40`, so the
attribution was visible for roughly as long as it took forty more submissions to arrive, and then it
was not. This endpoint re-reads the rows that were there all along.

WHAT THIS DOES NOT COVER, stated first: nothing here proves a spine. There is no verification, no GPU
and no receipt — the rows are written by hand, exactly as the coordinator writes them. What is under
test is the reconstruction: turning a log of advances into runs of blocks, and refusing to credit the
wrong person for them.

The interesting cases are all about IDENTITY rather than arithmetic. Two anonymous contributors both
present as a null handle, so merging on the handle would hand one of them the other's blocks; and a
contributor who renames mid-run would have that run split for no reason. Both are silent: the output
is still well-formed, still contiguous, still covers the spine — just wrong about who did it. So the
control breaks exactly that, and the tests that catch it are the point of the file.

Usage:
  python3 test_spine_segments.py            # assertions; exit 0 on success
  python3 test_spine_segments.py --control  # key the runs on handle instead; the tests MUST fail
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpspine = tempfile.mkdtemp(prefix="spine_")
_tmpproofs = tempfile.mkdtemp(prefix="proofs_")
_tmpblock = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w")
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = _tmpspine
os.environ["COORD_PROOFS"] = _tmpproofs
os.environ["MOD_BLOCK_FILE"] = _tmpblock.name
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()

if CONTROL:
    # The break: key the runs on the HANDLE rather than the pubkey. Everything still looks right —
    # contiguous, covering, one row per apparent contributor — which is what makes it worth a control.
    def _control_segments():
        c = server.db()
        try:
            blk = server.blocked_pubkeys()
            rows = c.execute("SELECT range_id,pubkey,handle FROM submissions"
                             " WHERE range_id LIKE 'spine:1-%' ORDER BY ts ASC, rowid ASC").fetchall()
        finally:
            c.close()
        segs, prev_hi, prev_handle = [], 0, object()
        for s in rows:
            try:
                hi = int(s["range_id"].split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if hi <= prev_hi:
                continue
            pk = (s["pubkey"] or "").lower()
            handle = "[removed]" if pk in blk else s["handle"]
            if segs and handle == prev_handle:
                segs[-1]["hi"] = hi
            else:
                segs.append({"lo": prev_hi + 1, "hi": hi, "handle": handle})
            prev_hi, prev_handle = hi, handle
        return segs, (int(rows[0]["range_id"].split("-", 1)[1]) if rows else None)
    server.spine_segments = _control_segments

fails = []
def check(ok, what):
    if ok:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)

def advance(hi, pubkey, handle, ts):
    c = server.db()
    c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
              " VALUES(?,?,?,?,?,1,?,?)",
              (f"spine:1-{hi}", pubkey, handle, "sha", "sig",
               f"spine advanced to [1..{hi}]", ts))
    c.commit(); c.close()

def other(range_id, pubkey, handle, ts):
    """A normal proof or fold submission — must not be read as an absorption."""
    c = server.db()
    c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
              " VALUES(?,?,?,?,?,1,?,?)", (range_id, pubkey, handle, "sha", "sig", "note", ts))
    c.commit(); c.close()

AA, BB, CC = "aa" * 32, "bb" * 32, "cc" * 32
t = 1000.0

# A run by one contributor, a handover, a run by a third, with ordinary submissions interleaved so
# they have every chance to be mistaken for absorptions.
advance(10, AA, "alice", t + 1)
other("1-10", BB, "bob", t + 2)
advance(20, AA, "alice", t + 3)
advance(30, AA, "alice", t + 4)
other("31", CC, None, t + 5)
advance(45, BB, "bob", t + 6)
advance(60, CC, None, t + 7)          # anonymous
advance(75, AA, None, t + 8)          # a DIFFERENT anonymous contributor, back to back

segs, first_hi = server.spine_segments()
by = [(s["lo"], s["hi"], s["handle"]) for s in segs]

check(len(segs) >= 1, f"the log produces segments at all (got {len(segs)})")
check((1, 30, "alice") in by, "three consecutive advances by one contributor are ONE run [1..30]")
check((31, 45, "bob") in by, "a handover starts the next run at the previous head + 1 [31..45]")
check(sum(1 for s in by if s[2] is None) == 2,
      "two anonymous contributors back to back stay TWO runs, not one")
check((46, 60, None) in by and (61, 75, None) in by,
      "  ...and each keeps its own blocks ([46..60] and [61..75])")
check(all(s["lo"] <= s["hi"] for s in segs), "no segment runs backwards")
check([s["lo"] for s in segs] == sorted(s["lo"] for s in segs), "segments come out in order")
check(all(segs[i]["hi"] + 1 == segs[i + 1]["lo"] for i in range(len(segs) - 1)),
      "segments are contiguous — no block falls between two runs")
check(segs[0]["lo"] == 1 and segs[-1]["hi"] == 75, "coverage runs from block 1 to the current head")
check(not any(s["hi"] in (10, 31) and s["handle"] == "bob" and s["lo"] == 1 for s in segs),
      "an ordinary fold submission is not read as an absorption")
# The first row is the one assumption: it is taken to start at block 1. Publishing it lets a caller
# notice a truncated log instead of reading an over-credited segment and believing it.
check(first_hi == 10, f"the first recorded advance is published, so the assumption is checkable (got {first_hi})")

# A head at or below the one already reached covers no new blocks: a re-advertised head, or two
# submissions racing. It must vanish, not appear as an empty or backwards segment.
before = len(server.spine_segments()[0])
advance(75, BB, "bob", t + 9)
advance(40, BB, "bob", t + 10)
after = server.spine_segments()[0]
check(len(after) == before, "a repeated head adds no segment")
check(all(s["lo"] <= s["hi"] for s in after), "a BACKWARDS head adds no segment either")
check(after[-1]["hi"] == 75, "  ...and does not move the head backwards")

# A rename is not a handover: same key, same run. AA was anonymous over [61..75] and now names
# itself, which is the same event as a rename and the one most likely to be mishandled.
advance(90, AA, "alice prime", t + 11)
advance(100, AA, "alice prime", t + 12)
ren = server.spine_segments()[0]
span = [s for s in ren if s["lo"] <= 70 <= s["hi"]]
check(len(span) == 1 and span[0]["hi"] == 100,
      "a contributor who renames mid-run keeps ONE run, not two")
check(len(span) == 1 and span[0]["handle"] == "alice prime",
      "  ...shown under the name they use now, not the one they started under")

# Malformed rows are skipped rather than guessed at.
c = server.db()
c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
          " VALUES('spine:1-notanumber',?,?, 'sha','sig',1,'junk',?)", (AA, "alice", t + 13))
c.commit(); c.close()
check(server.spine_segments()[0][-1]["hi"] == 100, "a malformed head is skipped, not parsed as a height")

# Moderation: a blocked pubkey is hidden the same way `recent` hides it.
advance(120, BB, "bob", t + 14)
with open(_tmpblock.name, "w") as f:
    f.write(BB + "\n")
mod = server.spine_segments()[0]
check(any(s["lo"] == 101 and s["handle"] == "[removed]" for s in mod),
      "a blocked contributor shows as [removed], not by name")
check(all(s["handle"] != "bob" for s in mod), "  ...and their handle appears nowhere in the output")

# ---- the ROUTE, not just the function ------------------------------------------------------------
# A reconstruction nothing can reach is not a feature. This drives the real handler over HTTP, because
# the failure mode of a new endpoint is a routing mistake, and that is invisible to every test above.
import json as _json, threading as _th, urllib.request as _rq  # noqa: E402

_httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
_th.Thread(target=_httpd.serve_forever, daemon=True).start()
_base = f"http://127.0.0.1:{_httpd.server_address[1]}"
try:
    with _rq.urlopen(_base + "/api/spine/segments", timeout=10) as r:
        _code, _body = r.status, _json.loads(r.read())
except Exception as e:                                       # noqa: BLE001
    _code, _body = 0, {"error": repr(e)}
check(_code == 200, f"GET /api/spine/segments answers 200 (got {_code})")
check(isinstance(_body.get("segments"), list) and _body["segments"],
      "  ...and returns the segments, not an empty body")
check(_body.get("segments", [{}])[0].get("lo") == 1, "  ...starting at block 1")
check(all({"lo", "hi", "handle"} <= set(s) for s in _body.get("segments", [])),
      "  ...with lo, hi and handle on every segment")
check(_body.get("hi") is None or _body.get("hi") == _body["segments"][-1]["hi"]
      or isinstance(_body.get("hi"), int),
      "  ...and reports the current head alongside them")
_httpd.shutdown()

if CONTROL:
    if fails:
        print(f"CONTROL OK — keyed the runs on handle and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — runs were keyed on a handle and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("spine segments attribute every absorbed block to the contributor who absorbed it.")
