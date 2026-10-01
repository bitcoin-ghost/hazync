#!/usr/bin/env python3
"""A transient GitHub 500 must not page a human, and a failed run must not re-rent (sponsor bot).

📏 WHAT HAPPENED, 2026-10-01 15:05:46. The sponsor bot died three seconds into `prepare()`:

    File "/opt/hazync/coordinator/sponsor_bot.py", line 814, in prepare
      urllib.request.urlretrieve(base + f, os.path.join(self.dir, f))
    urllib.error.HTTPError: HTTP Error 500: Internal Server Error

fetching `releases/download/v0.22.1/SHA256SUMS.txt` — GitHub's release CDN, nothing of ours. The
same assets fetched fine minutes later. It cost nothing, because `prepare()` runs before a cent is
spent, and it still woke somebody up.

⛔⛔ AND THE TIMER RE-RAN IT EVERY FIVE MINUTES. Measured on the box: NINE starts in one hour —
14:28, 14:33, 14:39, 14:44, 14:49, 14:55, 15:00, 15:05, 15:11, 15:16. The unit's own comment says
`Restart=no` means it "stops, pages the phone, and waits for a person". It did not wait. `Restart=`
governs systemd's own restarting; a timer is a separate mechanism and ignores it.

⇒ That failure was harmless only because of WHERE it happened. A failure after renting would have
re-rented every five minutes against `--max-usd 50` that each fresh run re-reads as unspent — the
exact scenario `Restart=no` is documented to prevent.

Two guards, and this checks both:
  * `http_retry` — retry 5xx and transport errors, NEVER 4xx, bounded well under the timer interval.
  * a FAILURE LATCH — `ExecStopPost` writes it, `ConditionPathExists=!` makes the next timer fire
    skip quietly instead of starting the bot again.

    python3 test_sponsor_resilience.py             # both guards present and correct
    python3 test_sponsor_resilience.py --control   # neither: one 500 kills the run, nothing latches
"""
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


import sponsor_bot  # noqa: E402

# ⚠ Never actually sleep: the backoff is 2+8+20 s and this must stay a unit test.
slept = []
sponsor_bot.time.sleep = lambda s: slept.append(s)


def http_error(code):
    return urllib.error.HTTPError("https://example/x", code, "boom", None, None)


def flaky(fail_times, exc):
    """A callable that raises `exc` the first `fail_times` calls, then returns 'ok'."""
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] <= fail_times:
            raise exc
        return "ok"
    fn.calls = lambda: state["n"]
    return fn


# ⛔ THE CONTROL IS THE SHIPPED BEHAVIOUR: one attempt, no retry, any error kills the run.
def once(fn, what):
    return fn()


retry = once if CONTROL else sponsor_bot.http_retry

# ── 1. a transient 500 must survive ─────────────────────────────────────────────────────────────
slept.clear()
f = flaky(1, http_error(500))
try:
    got = retry(f, "download SHA256SUMS.txt")
    err = None
except BaseException as e:                                   # noqa: BLE001
    got, err = None, e
if CONTROL:
    check(isinstance(err, urllib.error.HTTPError) and err.code == 500,
          f"⛔ control: a single 500 kills the run ({type(err).__name__}) — this is the state that "
          f"paged a phone at 15:05:46 for a CDN hiccup")
else:
    check(got == "ok" and err is None,
          f"one 500 then success is survived ({got!r}, {err!r})")
    check(f.calls() == 2, f"and it took exactly two attempts, not more ({f.calls()})")
    check(slept == [2], f"sleeping the first backoff only ({slept}s)")

# ── 2. ⛔ A 404 IS AN ANSWER, NOT A HICCUP, and must NOT be retried ──────────────────────────────
# This project has published an EMPTY release before (hazync#909: release.sh waited 600 s, published
# nothing and called it verified). Retrying a 404 turns "the release is broken" into "the bot is
# slow", which is far the worse failure.
if not CONTROL:
    slept.clear()
    f404 = flaky(99, http_error(404))
    try:
        retry(f404, "download SHA256SUMS.txt")
        raised = None
    except BaseException as e:                               # noqa: BLE001
        raised = e
    check(isinstance(raised, urllib.error.HTTPError) and raised.code == 404,
          f"a 404 propagates immediately ({type(raised).__name__})")
    check(f404.calls() == 1, f"⛔ and it was tried ONCE — a missing asset is not retried ({f404.calls()})")
    check(slept == [], f"with no backoff at all ({slept})")

    # ── 3. bounded, and bounded under the timer ─────────────────────────────────────────────────
    slept.clear()
    fdead = flaky(99, http_error(503))
    try:
        retry(fdead, "download SHA256SUMS.txt")
        raised = None
    except BaseException as e:                               # noqa: BLE001
        raised = e
    check(isinstance(raised, SystemExit),
          f"a persistent 5xx ends as SystemExit, not a traceback ({type(raised).__name__})")
    check("nothing was spent" in str(raised),
          f"and says nothing was spent, which is true in prepare() ({str(raised)[-40:]!r})")
    check(fdead.calls() == sponsor_bot.HTTP_TRIES,
          f"attempts are bounded at HTTP_TRIES ({fdead.calls()} of {sponsor_bot.HTTP_TRIES})")
    # ⚠ The timer re-fires 5 min after the run goes inactive, so the retry budget must stay well
    # under that or a run could still be waiting when the next one is due.
    check(sum(slept) <= 120,
          f"⚠ total backoff {sum(slept)}s stays far under the timer's 5 min (OnUnitInactiveSec)")

    # ── 4. a transport error retries too — a CDN can refuse the connection rather than answer ───
    slept.clear()
    furl = flaky(2, urllib.error.URLError("connection reset"))
    check(retry(furl, "x") == "ok" and furl.calls() == 3,
          f"a URLError is transient as well ({furl.calls()} attempts)")

# ── 5. the FAILURE LATCH, in the unit ───────────────────────────────────────────────────────────
UNIT = open(os.path.join(HERE, "deploy", "hazync-sponsor-bot.service"), encoding="utf8").read()
cond = "ConditionPathExists=!/var/lib/hazync/sponsor-bot/.failed" in UNIT
post = "ExecStopPost=" in UNIT and "SERVICE_RESULT" in UNIT
if CONTROL:
    check(True, "⛔ control: (the latch is asserted only in the real arm)")
else:
    check(cond, "⛔ the unit refuses to start while the latch file exists")
    check(post, "and writes the latch on any non-success exit, via $SERVICE_RESULT")
    # ⛔ The latch must be written for ANY non-success, not just a non-zero exit: a SIGKILL or a
    # TimeoutStopSec kill must latch too, which is exactly what $SERVICE_RESULT covers and
    # $EXIT_STATUS does not.
    check("$SERVICE_RESULT" in UNIT and "$EXIT_STATUS" not in UNIT,
          "⚠ keyed on $SERVICE_RESULT, so a signal or a timeout latches as well as a bad exit")
    # ⚠ The latch lives under the one path the sandbox can write.
    check("ReadWritePaths=/var/lib/hazync/sponsor-bot" in UNIT,
          "and the latch path is inside ReadWritePaths, or ExecStopPost could not write it")
    # ⚠ A failure to write the latch must not itself page anyone.
    line = [l for l in UNIT.splitlines() if l.startswith("ExecStopPost=")][0]
    check(line.startswith("ExecStopPost=-"),
          f"⚠ and the latch write is prefixed `-`, so failing to latch does not fail the unit ({line[:28]}…)")
    check("rm /var/lib/hazync/sponsor-bot/.failed" in UNIT,
          "⚠ and the unit documents how to clear it, since a latched unit starts silently doing nothing")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — the shipped shape must die on one 500")
        sys.exit(1)
    print("PASS (control): no retry, so a CDN hiccup ends the run and pages a phone")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")
