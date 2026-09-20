#!/usr/bin/env python3
"""Tests for the Share button on the live page.

`share_harness.js` runs the SHIPPED live.js against a mock browser, so these exercise the real
handler rather than a re-implementation of it — a test that reimplements the logic passes happily
while the page is broken.

  python3 test_share.py            # assertions; exit 0 on success
  python3 test_share.py --control  # the cancelled-share check is removed; MUST fail
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


if not shutil.which("node"):
    print("  ⛔ node is not installed, so the shipped live.js cannot be executed at all.")
    print("     Skipping would make this suite green while proving nothing about the button.")
    sys.exit(1)

WORK = tempfile.mkdtemp(prefix="share_")
RUNDIR = HERE

if CONTROL:
    # ⛔ THE CANCELLED-SHARE CHECK REMOVED. Dismissing the share sheet then falls through to the
    # download fallback, so someone who changed their mind gets a file they did not ask for and a
    # clipboard they did not ask to have overwritten.
    src = open(os.path.join(HERE, "live.js"), encoding="utf8").read()
    assert "err.name === 'AbortError'" in src, "the guard this control removes is not there"
    src = src.replace("if (err && err.name === 'AbortError') { say(''); return; }", "")
    RUNDIR = WORK
    for f in ("share_harness.js",):
        shutil.copy(os.path.join(HERE, f), os.path.join(WORK, f))
    open(os.path.join(WORK, "live.js"), "w", encoding="utf8").write(src)


def run(scenario):
    r = subprocess.run(["node", os.path.join(RUNDIR, "share_harness.js"), scenario],
                       capture_output=True, text=True, cwd=RUNDIR, timeout=60)
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return {"errors": [f"harness produced nothing: {(r.stderr or '')[:160]}"]}
    return json.loads(line[-1])


# ── 1. the happy path shares the IMAGE and a link, together ──────────────────────────────────────
ok = run("share-ok")
check(not ok.get("errors"), f"the harness ran the shipped live.js ({ok.get('errors')})")
sh = ok.get("shared") or {}
check(sh.get("fileType") == "image/png",
      f"⛔ the actual PNG bytes are shared, not a URL to them ({sh.get('fileType')}) — frame.png is "
      f"overwritten about once a second, so a link to it is not a link to this moment")
check(sh.get("url") == "https://hazync.org/live/",
      f"and a link to the live page goes with it ({sh.get('url')})")
check(bool(re.match(r"hazync-\d{8}T\d{6}Z\.png$", sh.get("fileName") or "")),
      f"the file is named for the FRAME's time, so it stays self-dating once passed on "
      f"({sh.get('fileName')})")
check(ok.get("buttonReEnabled") is True, "the button is re-enabled afterwards")

# ── 2. ⚠ A CANCELLED SHARE IS NOT A FAILURE ──────────────────────────────────────────────────────
c = run("share-cancelled")
check(c.get("downloaded") is None and c.get("clipboard") is None,
      f"⚠ dismissing the share sheet downloads NOTHING and touches no clipboard "
      f"(downloaded={c.get('downloaded')}, clipboard={c.get('clipboard')})")
check((c.get("messages") or [""])[0] in ("", None),
      f"and says nothing — reporting an error when someone simply changed their mind is worse than "
      f"silence ({c.get('messages')})")

# ── 3. a browser without file sharing still gets both halves ─────────────────────────────────────
n = run("no-file-share")
check((n.get("downloaded") or "").endswith(".png"),
      f"without navigator.share the PNG is downloaded instead ({n.get('downloaded')})")
check(n.get("clipboard") == "https://hazync.org/live/",
      f"and the link goes to the clipboard, so both halves still arrive ({n.get('clipboard')})")

# ── 4. a failed capture says so ──────────────────────────────────────────────────────────────────
f = run("fetch-fails")
check(f.get("shared") is None and f.get("downloaded") is None,
      "a frame that cannot be fetched shares nothing")
check("Could not capture" in (f.get("messages") or [""])[0],
      f"and the page says so rather than appearing to have worked ({f.get('messages')})")

# ── 5. ⛔ A STALE FRAME MUST NOT BE SHARED AS LIVE ────────────────────────────────────────────────
# The image carries its own UTC timestamp, but a recipient reads whatever the sharer's text says.
st = run("stale")
txt = (st.get("shared") or {}).get("text") or ""
check("not live" in txt,
      f"⛔ sharing a stale frame says it is NOT LIVE ({txt!r}) — passed along without that, an old "
      f"frame reads as the present")
live_txt = (ok.get("shared") or {}).get("text") or ""
check("live right now" in live_txt, f"and a fresh frame says it is live ({live_txt!r})")

# ── 6. the page itself stays within the CSP the site sends ───────────────────────────────────────
html = open(os.path.join(HERE, "public.html"), encoding="utf8").read()
check("<button id=\"share\"" in html, "the control is a real <button>, focusable and keyboard-usable")
check("aria-label" in html and 'role="status"' in html,
      "it is labelled, and its feedback is an aria-live region rather than a silent change")
check(not re.search(r"on(click|load)\s*=", html),
      "⛔ no inline handlers — script-src 'self' has no 'unsafe-inline', so an onclick would never "
      "run and the button would look present and do nothing")
js = open(os.path.join(RUNDIR, "live.js"), encoding="utf8").read()
check("fetch('frame.png?t=" in js,
      "the capture is same-origin and cache-busted — connect-src 'self' permits nothing else, and a "
      "cached frame would share something other than what is on screen")

EXPECTED_CONTROL_FAILURES = {
    "dismissing the share sheet downloads NOTHING",
}

print()
if CONTROL:
    hit = {e for e in EXPECTED_CONTROL_FAILURES if any(e in x for x in fails)}
    if hit == EXPECTED_CONTROL_FAILURES:
        print("CONTROL OK — the cancelled-share check was removed and the assertion that detects it "
              "failed, as it must:")
        for e in sorted(hit):
            print(f"  - {e}")
        sys.exit(0)
    print("CONTROL FAILED — a cancelled share silently downloaded a file.")
    for e in sorted(EXPECTED_CONTROL_FAILURES - hit):
        print(f"  should have failed and did not: {e}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
