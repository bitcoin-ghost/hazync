#!/usr/bin/env python3
"""Phone notifications (ntfy) for the offsite copies in Cloudflare R2.

    hazync-offsite-watch.py watch     # every 10 min: problems as they happen, and once when they clear
    hazync-offsite-watch.py summary   # daily: what is backed up, and whether a restore actually works

`watch` pushes when:
  * Litestream is not running                    -> the ledger is not being copied            (high)
  * the newest ledger change in R2 is older than OFFSITE_LAG_ALERT_SECS (15 min), or R2 cannot
    be listed                                    -> the copy has stalled                      (high)
  * Litestream logged WARN or ERROR lines        -> batched, at most one push an hour      (default)
An ongoing problem is re-sent every OFFSITE_REALERT_SECS (6 h), and one RECOVERED (low) is sent when
it clears. The hourly receipt mirror pages through its own OnFailure=, so it is not repeated here.

`summary` reports receipts in R2 against the disk, the hourly mirror's last 24 h, how far behind
the ledger copy is, and a RESTORE DRILL: the ledger is restored from R2 into a scratch file,
integrity-checked, compared with the live ledger, and deleted. A copy nobody has restored is a hope.
All good -> low priority; anything wrong -> high.

Messages go out through hazync-alert.sh with ALERT_PRIORITY / ALERT_TAGS.
"""
import argparse
import datetime
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

ALERT = os.environ.get("HAZYNC_ALERT", "/usr/local/bin/hazync-alert.sh")
STATE_DIR = os.environ.get("OFFSITE_STATE_DIR", "/var/lib/hazync-offsite")
LS_CONFIG = os.environ.get("LITESTREAM_CONFIG", "/etc/litestream.yml")
DB = os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db")
PROOFS = os.environ.get("COORD_PROOFS", "/var/lib/hazync/proofs")
REPO = os.environ.get("HZ_REPO", "/opt/hazync")
KEYS = os.environ.get("OFFSITE_KEYS", "/etc/hazync/backup/r2.keys")
BUCKET = os.environ.get("OFFSITE_BUCKET", "hazync-proofs")
MIRROR = os.environ.get("OFFSITE_PROOFS_SCRIPT", "/usr/local/sbin/hazync-offsite-proofs")
MIRROR_UNIT = "hazync-offsite-proofs.service"
DRILL_DIR = os.environ.get("OFFSITE_DRILL_DIR", "/var/lib/hazync/restore-drill")
DRILL_USER = os.environ.get("OFFSITE_DRILL_USER", "hazync")
LAG_ALERT = int(os.environ.get("OFFSITE_LAG_ALERT_SECS", "900"))
REALERT = int(os.environ.get("OFFSITE_REALERT_SECS", "21600"))
LOG_REALERT = int(os.environ.get("OFFSITE_LOG_REALERT_SECS", "3600"))
GRACE = int(os.environ.get("OFFSITE_PROOF_GRACE_SECS", "1800"))

TITLES = {
    "litestream-down": "Litestream is not running",
    "ledger-unlistable": "cannot list the ledger copy in R2",
    "ledger-stale": "the ledger copy in R2 is stale",
}


def run(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, repr(e)


def send(title, body, priority="high", tags="rotating_light"):
    env = dict(os.environ, ALERT_PRIORITY=priority, ALERT_TAGS=tags)
    rc = subprocess.run([ALERT, title, body], env=env, timeout=120).returncode
    if rc != 0:                      # fail the unit, so OnFailure= still gets a word out
        raise RuntimeError(f"{ALERT} exited {rc}: the notification was not delivered")


def ago(secs):
    secs = int(secs)
    if secs < 120:
        return f"{secs} s"
    if secs < 7200:
        return f"{secs // 60} min"
    if secs < 172800:
        return f"{secs / 3600:.1f} h"
    return f"{secs / 86400:.1f} days"


def ledger_lag(run, now):
    """(seconds since the newest ledger change reached R2, None) or (None, why R2 could not be listed)."""
    rc, out = run(["litestream", "ltx", "-config", LS_CONFIG, DB], timeout=120)
    stamps = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0].isdigit():
            try:
                stamps.append(datetime.datetime.strptime(f[-1], "%Y-%m-%dT%H:%M:%SZ")
                              .replace(tzinfo=datetime.timezone.utc).timestamp())
            except ValueError:
                pass
    if rc != 0 or not stamps:
        return None, ((out.strip().splitlines() or ["no output"])[-1])[:300]
    return max(0.0, now - max(stamps)), None


def litestream_state(run):
    _, out = run(["systemctl", "is-active", "litestream"], timeout=30)
    return out.strip() or "unknown"


def log_problems(run, cursor_file):
    """WARN/ERROR lines Litestream logged since the previous call; journalctl keeps the cursor."""
    cmd = ["journalctl", "-u", "litestream", "--no-pager", "-o", "cat", f"--cursor-file={cursor_file}"]
    if not os.path.exists(cursor_file):
        cmd.append("--since=-15min")             # first run: not the whole history
    _, out = run(cmd, timeout=60)
    return [line for line in out.splitlines() if "level=WARN" in line or "level=ERROR" in line]


def load_state():
    try:
        with open(os.path.join(STATE_DIR, "watch.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, "watch.json.tmp")
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, os.path.join(STATE_DIR, "watch.json"))


def watch_problems(run, now):
    """{key: message} for problems that last until something fixes them."""
    problems = {}
    state = litestream_state(run)
    if state != "active":
        problems["litestream-down"] = (f"Litestream is {state}, so the coordinator ledger is NOT being copied "
                                       f"to R2.\nCheck: systemctl status litestream; journalctl -u litestream -n 50")
    lag, why = ledger_lag(run, now)
    if lag is None:
        problems["ledger-unlistable"] = f"Could not list the ledger copy in R2: {why}"
    elif lag > LAG_ALERT:
        problems["ledger-stale"] = (f"The newest ledger change in R2 is {ago(lag)} old (alert above "
                                    f"{ago(LAG_ALERT)}). Litestream may be stuck or unable to upload.")
    return problems


def watch(run=run, send=send, now=None):
    now = time.time() if now is None else now
    st = load_state()
    ongoing = st.setdefault("ongoing", {})
    problems = watch_problems(run, now)
    for key, msg in problems.items():
        entry = ongoing.get(key, {})
        if now - entry.get("sent", 0) >= REALERT:
            since = entry.get("since", now)
            extra = f"\n\nOngoing for {ago(now - since)}." if now - since >= 60 else ""
            send(f"Hazync backups: {TITLES.get(key, key)}", msg + extra, "high", "rotating_light,floppy_disk")
            ongoing[key] = {"since": since, "sent": now}
    for key in [k for k in ongoing if k not in problems]:
        send(f"Hazync backups: RECOVERED, {TITLES.get(key, key)}",
             f"Cleared after {ago(now - ongoing[key]['since'])}.", "low", "white_check_mark")
        del ongoing[key]
    pending = st.get("log_pending", []) + log_problems(run, os.path.join(STATE_DIR, "litestream.cursor"))
    if pending and now - st.get("log_sent", 0) >= LOG_REALERT:
        body = (f"{len(pending)} warning/error line(s) from Litestream since the last message:\n\n"
                + "\n".join(line[:300] for line in pending[-8:]))
        send("Hazync backups: Litestream warnings", body, "default", "warning")
        st["log_sent"], pending = now, []
    st["log_pending"] = pending[-50:]
    save_state(st)
    return 0


def load_mirror():
    loader = importlib.machinery.SourceFileLoader("offsite_proofs", MIRROR)
    spec = importlib.util.spec_from_loader("offsite_proofs", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def proofs_section(now, client=None, mirror=None):
    m = mirror or load_mirror()
    parts = open(KEYS).read().split()
    s3 = client if client is not None else m.make_client(parts[0], parts[1], parts[2], 8)
    remote = m.list_remote(s3, BUCKET, f"proofs-{m.method_prefix(REPO)}/")
    everything, _ = m.list_local(PROOFS, 0, now)
    settled, young = m.list_local(PROOFS, GRACE, now)
    missing, differ = m.plan(settled, remote)
    line = (f"Proofs in R2: {sum(1 for n in everything if n in remote):,} of {len(everything):,} on disk, "
            f"{sum(remote.values()) / 1e9:.1f} GB")
    if young:
        line += f" ({young:,} newer than {ago(GRACE)} go in the next hourly run)"
    if missing:
        line += f"\n  ⚠ {len(missing):,} older than {ago(GRACE)} are MISSING from R2, e.g. {missing[0]}"
    if differ:
        line += f"\n  ⚠ {len(differ):,} differ in size from their R2 copy, e.g. {differ[0]}"
    return not missing and not differ, line


def mirror_section(run):
    _, out = run(["systemctl", "show", MIRROR_UNIT, "-p", "Result", "-p", "ExecMainExitTimestamp"], timeout=30)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    _, log = run(["journalctl", "-u", MIRROR_UNIT, "--since=-24h", "--no-pager", "-o", "cat"], timeout=60)
    runs = uploaded = 0
    for line in log.splitlines():
        if "[offsite-proofs] done:" in line:
            runs += 1
            n = line.split("done:", 1)[1].split()[0]
            uploaded += int(n) if n.isdigit() else 0
    failed = sum(1 for line in log.splitlines() if "Failed with result" in line)
    result = props.get("Result", "unknown")
    ok = result == "success" and failed == 0 and runs > 0
    when = props.get("ExecMainExitTimestamp") or "never"
    return ok, (f"Hourly mirror: last run {when}, {result} · {runs} run(s), {failed} failed, "
                f"{uploaded:,} proofs uploaded in 24 h")


def ledger_section(run, now):
    state = litestream_state(run)
    lag, why = ledger_lag(run, now)
    _, log = run(["journalctl", "-u", "litestream", "--since=-24h", "--no-pager", "-o", "cat"], timeout=60)
    warns = sum(1 for line in log.splitlines() if "level=WARN" in line or "level=ERROR" in line)
    ok = state == "active" and lag is not None and lag <= LAG_ALERT
    lagtxt = f"{ago(lag)} behind" if lag is not None else f"R2 not listable ({why})"
    return ok, f"Ledger in R2: Litestream {state}, {lagtxt} · {warns} warning/error line(s) in 24 h"


def restore_drill(run, now):
    shutil.rmtree(DRILL_DIR, ignore_errors=True)
    os.makedirs(DRILL_DIR, mode=0o700)
    try:
        if DRILL_USER:
            shutil.chown(DRILL_DIR, DRILL_USER, DRILL_USER)
        out_db = os.path.join(DRILL_DIR, "coordinator.db")
        cmd = ["litestream", "restore", "-config", LS_CONFIG, "-o", out_db, DB]
        if DRILL_USER:
            cmd = ["runuser", "-u", DRILL_USER, "--"] + cmd
        t = time.monotonic()
        rc, out = run(cmd, timeout=900)
        took = time.monotonic() - t
        if rc != 0 or not os.path.exists(out_db):
            return False, (f"Restore drill: FAILED, litestream restore exited {rc}: "
                           f"{((out.strip().splitlines() or [''])[-1])[:300]}")
        q = ("SELECT (SELECT COUNT(*) FROM submissions), (SELECT MAX(ts) FROM submissions), "
             "(SELECT COUNT(*) FROM vranges)")
        try:
            r = sqlite3.connect(f"file:{out_db}?mode=ro", uri=True)
            integrity = r.execute("PRAGMA integrity_check").fetchone()[0]
            rs, rts, rv = r.execute(q).fetchone()
            r.close()
            live = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
            ls, lts, lv = live.execute(q).fetchone()
            live.close()
        except sqlite3.Error as e:
            return False, f"Restore drill: FAILED, the restored file is not a usable ledger: {e}"
        behind = max(0.0, float(lts or 0) - float(rts or 0))
        ok = integrity == "ok" and behind <= 600 and rv >= 0.99 * lv
        return ok, (f"Restore drill: {'ok' if ok else 'FAILED'} in {took:.0f} s, integrity {integrity}, "
                    f"{rs:,} submissions / {rv:,} verified ranges (live {ls:,} / {lv:,}), "
                    f"newest {ago(behind)} behind live")
    finally:
        shutil.rmtree(DRILL_DIR, ignore_errors=True)


def summary(run=run, send=send, now=None, client=None, mirror=None):
    now = time.time() if now is None else now
    checks = (("proofs", lambda: proofs_section(now, client, mirror)),
              ("hourly mirror", lambda: mirror_section(run)),
              ("ledger", lambda: ledger_section(run, now)),
              ("restore drill", lambda: restore_drill(run, now)))
    sections = []
    for name, fn in checks:
        try:
            sections.append(fn())
        except Exception as e:           # one broken check must not silence the other three
            sections.append((False, f"{name}: could not be checked: {e!r}"))
    bad = sum(1 for ok, _ in sections if not ok)
    body = "\n".join(line for _, line in sections)
    if bad:
        send(f"Hazync backups: {bad} problem(s) in the daily check", body, "high", "warning,floppy_disk")
    else:
        send("Hazync backups: all good", body, "low", "white_check_mark,floppy_disk")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="ntfy for the Hazync offsite copies")
    ap.add_argument("mode", choices=["watch", "summary"])
    a = ap.parse_args(argv)
    return watch() if a.mode == "watch" else summary()


if __name__ == "__main__":
    sys.exit(main())
