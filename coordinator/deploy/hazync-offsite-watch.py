#!/usr/bin/env python3
"""Phone notifications (ntfy) for the offsite copies in Cloudflare R2 and Backblaze B2.

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
It also downloads the newest SPINE copy from R2, checks its bytes against its sha256, runs
hazync-verify on it, and says how far it is behind the live spine. And it checks that the current sponsor
identities have an encrypted copy in R2, encrypted to the pinned operator key, and that key is not about to
expire.
Then the same for the second copy in B2: receipts, the B2 mirror's 24 h, the spine, the sponsor keys, and the
newest daily LEDGER copy (B2 has no Litestream), which is downloaded, unpacked, integrity-checked and compared
with the live ledger. OFFSITE_B2_KEYS= (empty) turns the B2 lines off; a configured keys file that is missing
is a problem, not a skip.
All good -> low priority; anything wrong -> high.

Messages go out through hazync-alert.sh with ALERT_PRIORITY / ALERT_TAGS.
"""
import argparse
import datetime
import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zlib

ALERT =os.environ.get("HAZYNC_ALERT", "/usr/local/bin/hazync-alert.sh")
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
# Litestream drives ~300 retention list calls an hour against R2, and R2 throttles a small share of
# them (measured on server 1 over 3 days: 109 429s against 12,584 successes, ~0.9%, steady). One
# throttled list is not a backup problem, but a single attempt made it indistinguishable from one --
# and this check runs every 10 min, so at that rate it pages roughly once a day for nothing.
LTX_ATTEMPTS = int(os.environ.get("OFFSITE_LTX_ATTEMPTS", "3"))
LTX_RETRY_SECS = float(os.environ.get("OFFSITE_LTX_RETRY_SECS", "5"))
# A receipt counts as missing only after it has missed a whole hourly mirror cycle. `copy` skips
# receipts younger than 120 s, so one written just before or during a run waits up to an hour for the
# next; at 1800 the 07:00 UTC summary (~23 min before the :23 run) flagged those almost every morning
# (2026-09-15: 68 "missing", every one uploaded by the next run).
GRACE = int(os.environ.get("OFFSITE_PROOF_GRACE_SECS", "7200"))
SPINE = os.environ.get("COORD_SPINE", "/var/lib/hazync/spine")
VERIFY = os.environ.get("HAZYNC_VERIFY", "/usr/local/bin/hazync-verify")
# The spine is copied every 10 min, so a newest copy an hour older than the live spine means the copy stopped.
SPINE_LAG_ALERT = int(os.environ.get("OFFSITE_SPINE_LAG_SECS", "3600"))
SPONSOR_IDENTITIES = os.environ.get("SPONSOR_IDENTITIES", "/var/lib/hazync/sponsor-bot/identities")
SPONSOR_PUBKEY = os.environ.get("OFFSITE_KEYS_PUBKEY", "/etc/hazync/backup/sponsor-keys.pub.asc")
SPONSOR_RECIPIENT = os.environ.get("OFFSITE_KEYS_RECIPIENT", "777FE81F8CC077FD3D08055E852C2B3190F5B928")
# The keys copy runs hourly, so identities that changed in the last two hours may simply be waiting for it.
KEYS_GRACE = int(os.environ.get("OFFSITE_KEYS_GRACE_SECS", "7200"))
KEY_EXPIRY_WARN_DAYS = int(os.environ.get("OFFSITE_KEY_EXPIRY_WARN_DAYS", "60"))
B2_KEYS = os.environ.get("OFFSITE_B2_KEYS", "/etc/hazync/backup/b2.keys")     # "" turns the B2 checks off
B2_BUCKET = os.environ.get("OFFSITE_B2_BUCKET", "hazync-backup")
B2_MIRROR_UNIT = "hazync-offsite-proofs-b2.service"
# The ledger copy in B2 is taken once a day, so a newest copy older than 26 h means the daily copy stopped.
B2_LEDGER_LAG_ALERT = int(os.environ.get("OFFSITE_B2_LEDGER_LAG_SECS", "93600"))

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


def ltx_transient(text):
    """True for a listing failure worth retrying, where the copy itself is fine.

    Deliberately narrow. AccessDenied, a missing bucket and a broken config are NOT transient: they
    need a human either way, so retrying them three times only delays a real alert by 10 s.
    """
    t = text.lower()
    return any(m in t for m in ("429", "slowdown", "serviceunavailable", "503", "500",
                                "timeout", "timed out", "connection reset", "unexpected eof"))


def ledger_lag(run, now):
    """(seconds since the newest ledger change reached R2, None) or (None, why R2 could not be listed).

    Retries a transient failure so a throttled listing does not page as a lost backup. A sustained
    outage still alerts once the attempts are spent -- the point is to tell the two apart, not to
    soften the alarm.
    """
    attempts = max(1, LTX_ATTEMPTS)
    why, attempt = "no output", 0
    for attempt in range(1, attempts + 1):
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
        if rc == 0 and stamps:
            return max(0.0, now - max(stamps)), None
        why = ((out.strip().splitlines() or ["no output"])[-1])[:300]
        if attempt >= attempts or not ltx_transient(why):
            break
        time.sleep(LTX_RETRY_SECS)
    return None, (f"{why}  [{attempt} attempts]" if attempt > 1 else why)


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


def store_client(m, keys, client):
    if client is not None:
        return client
    parts = open(keys).read().split()
    return m.make_client(parts[0], parts[1], parts[2], 8)


def proofs_section(now, client=None, mirror=None, keys=None, bucket=None, where="R2"):
    m = mirror or load_mirror()
    s3 = store_client(m, keys or KEYS, client)
    remote = m.list_remote(s3, bucket or BUCKET, f"proofs-{m.method_prefix(REPO)}/")
    everything, _ = m.list_local(PROOFS, 0, now)
    settled, young = m.list_local(PROOFS, GRACE, now)
    missing, differ = m.plan(settled, remote)
    line = (f"Proofs in {where}: {sum(1 for n in everything if n in remote):,} of {len(everything):,} on disk, "
            f"{sum(remote.values()) / 1e9:.1f} GB")
    if young:
        line += f" ({young:,} newer than {ago(GRACE)} go in the next hourly run)"
    if missing:
        line += f"\n  ⚠ {len(missing):,} older than {ago(GRACE)} are MISSING from {where}, e.g. {missing[0]}"
    if differ:
        line += f"\n  ⚠ {len(differ):,} differ in size from their {where} copy, e.g. {differ[0]}"
    return not missing and not differ, line


def mirror_section(run, unit=None, label="Hourly mirror"):
    unit = unit or MIRROR_UNIT
    _, out = run(["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainExitTimestamp"], timeout=30)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    _, log = run(["journalctl", "-u", unit, "--since=-24h", "--no-pager", "-o", "cat"], timeout=60)
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
    return ok, (f"{label}: last run {when}, {result} · {runs} run(s), {failed} failed, "
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


def spine_section(now, client=None, mirror=None, keys=None, bucket=None, where="R2"):
    """Download the newest spine copy in the store, check it against its own sha256, verify it, and compare
    it with the live spine. A spine copy nobody has verified is a hope, as with the ledger."""
    m = mirror or load_mirror()
    s3 = store_client(m, keys or KEYS, client)
    bucket = bucket or BUCKET
    prefix = f"spine-{m.method_prefix(REPO)}/"
    remote = m.list_remote(s3, bucket, prefix)
    with open(os.path.join(SPINE, "spine.json")) as f:
        live = json.load(f)
    pairs = []
    for key in remote:
        if key.startswith("spine_") and key.endswith(".json") and key[:-5] + ".bin" in remote:
            try:
                lo, hi = (int(x) for x in key[len("spine_"):-5].split("-"))
            except ValueError:
                continue
            pairs.append((hi, lo, key[:-5]))
    if not pairs:
        return False, f"Spine in {where}: NO copy (live spine [1..{int(live['hi']):,}])\n  ⚠ the spine is not backed up"
    hi, lo, name = max(pairs)
    data = s3.get_object(Bucket=bucket, Key=prefix + name + ".bin")["Body"].read()
    meta = json.loads(s3.get_object(Bucket=bucket, Key=prefix + name + ".json")["Body"].read())
    behind = max(0.0, float(live.get("ts") or 0) - float(meta.get("ts") or 0))
    problems = []
    if hashlib.sha256(data).hexdigest() != meta.get("sha256") or len(data) != meta.get("bytes"):
        problems.append("its bytes do not match the sha256 and size recorded beside it")
    if not os.path.exists(VERIFY):
        detail = "not verified"
        problems.append(f"cannot verify it: no hazync-verify at {VERIFY}")
    else:
        ok, detail = m.verify_spine(VERIFY, data)
        if not ok:
            problems.append(f"hazync-verify rejected it: {detail}")
    if behind > SPINE_LAG_ALERT:
        problems.append(f"it is {ago(behind)} behind the live spine (alert above {ago(SPINE_LAG_ALERT)})")
    line = (f"Spine in {where}: [{lo:,}..{hi:,}] of live [1..{int(live['hi']):,}], {ago(behind)} behind, "
            f"{len(pairs):,} copies · {detail}")
    for p in problems:
        line += f"\n  ⚠ {p}"
    return not problems, line


def keys_section(now, client=None, mirror=None, keys=None, bucket=None, where="R2"):
    """The current sponsor identities have a copy in the store, encrypted to the pinned operator key, and that
    key is not about to expire. The box cannot decrypt the copy, so it checks the recipient in its packets."""
    m = mirror or load_mirror()
    s3 = store_client(m, keys or KEYS, client)
    bucket = bucket or BUCKET
    prefix = "sponsor-keys/"
    remote = m.list_remote(s3, bucket, prefix)
    copies = sum(1 for k in remote if k.endswith(".tar.gpg"))
    _, count, name = m.identities_object(SPONSOR_IDENTITIES)
    newest = max((os.stat(os.path.join(r, f)).st_mtime for r, _, fs in os.walk(SPONSOR_IDENTITIES) for f in fs),
                 default=0)
    problems, expires = [], None
    home = tempfile.mkdtemp(prefix="hzk-")
    try:
        subs, expires, why = m.recipient_keys(home, SPONSOR_PUBKEY, SPONSOR_RECIPIENT)
        if why:
            problems.append(why)
        if name in remote:
            state = f"current copy in {where}"
            ids = m.packet_keyids(home, s3.get_object(Bucket=bucket, Key=prefix + name)["Body"].read())
            if subs and not set(ids) & set(subs):
                problems.append(f"the current copy is not encrypted to {SPONSOR_RECIPIENT} "
                                f"(it is encrypted to {', '.join(ids) or 'nothing readable'})")
        elif now - newest < KEYS_GRACE:
            state = f"changed {ago(now - newest)} ago, goes in the next hourly run"
        else:
            state = f"current identities NOT in {where}"
            problems.append(f"the current identities ({count} files, unchanged for {ago(now - newest)}) "
                            f"have no copy in {where}")
        if expires is not None and expires - now < KEY_EXPIRY_WARN_DAYS * 86400:
            problems.append(f"the encryption key expires in {ago(max(0, expires - now))}: extend it "
                            f"(gpg --quick-set-expire) and re-export the public key to {SPONSOR_PUBKEY}")
    finally:
        m.gpg_stop(home)
        shutil.rmtree(home, ignore_errors=True)
    exp = f", key expires in {ago(expires - now)}" if expires else ""
    line = f"Sponsor keys in {where}: {count} identity file(s), {state}, {copies} encrypted copies{exp}"
    for p in problems:
        line += f"\n  ⚠ {p}"
    return not problems, line


def ledger_copy_section(now, client=None, mirror=None, keys=None, bucket=None, where="B2"):
    """Download the newest daily ledger copy, unpack it, integrity-check it and compare it with the live
    ledger. The same drill as the Litestream restore, for the copy that does not come from Litestream."""
    m = mirror or load_mirror()
    s3 = store_client(m, keys or B2_KEYS, client)
    bucket = bucket or B2_BUCKET
    copies = []
    for key in m.list_remote(s3, bucket, "ledger/"):
        if key.startswith("coordinator-") and key.endswith(".db.gz"):
            try:
                t = datetime.datetime.strptime(key[len("coordinator-"):-len(".db.gz")], "%Y%m%dT%H%M%SZ")
            except ValueError:
                continue
            copies.append((t.replace(tzinfo=datetime.timezone.utc).timestamp(), key))
    if not copies:
        return False, f"Ledger in {where}: NO daily copy\n  ⚠ the ledger has no second copy"
    ts, name = max(copies)
    age = max(0.0, now - ts)
    problems = []
    if age > B2_LEDGER_LAG_ALERT:
        problems.append(f"the newest copy is {ago(age)} old (alert above {ago(B2_LEDGER_LAG_ALERT)})")
    shutil.rmtree(DRILL_DIR, ignore_errors=True)
    os.makedirs(DRILL_DIR, mode=0o700)
    try:
        out_db = os.path.join(DRILL_DIR, "coordinator-copy.db")
        body = s3.get_object(Bucket=bucket, Key="ledger/" + name)["Body"]
        with gzip.GzipFile(fileobj=body) as g, open(out_db, "wb") as f:
            shutil.copyfileobj(g, f, 1 << 20)
        q = "SELECT (SELECT COUNT(*) FROM submissions), (SELECT COUNT(*) FROM vranges)"
        r = sqlite3.connect(f"file:{out_db}?mode=ro", uri=True)
        integrity = r.execute("PRAGMA integrity_check").fetchone()[0]
        rs, rv = r.execute(q).fetchone()
        r.close()
        live = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        ls, lv = live.execute(q).fetchone()
        live.close()
        if integrity != "ok":
            problems.append(f"the restored copy fails integrity_check: {integrity[:200]}")
        detail = (f"restore {'ok' if integrity == 'ok' else 'FAILED'}, integrity {integrity[:40]}, "
                  f"{rs:,} submissions / {rv:,} verified ranges (live {ls:,} / {lv:,})")
    except (OSError, EOFError, zlib.error, sqlite3.Error) as e:
        problems.append(f"the copy is not a usable ledger: {e}")
        detail = "restore FAILED"
    finally:
        shutil.rmtree(DRILL_DIR, ignore_errors=True)
    line = f"Ledger in {where}: newest daily copy {ago(age)} old, {len(copies):,} copies · {detail}"
    for p in problems:
        line += f"\n  ⚠ {p}"
    return not problems, line


def summary(run=run, send=send, now=None, client=None, mirror=None):
    now = time.time() if now is None else now
    checks = [("proofs", lambda: proofs_section(now, client, mirror)),
              ("hourly mirror", lambda: mirror_section(run)),
              ("ledger", lambda: ledger_section(run, now)),
              ("restore drill", lambda: restore_drill(run, now)),
              ("spine", lambda: spine_section(now, client, mirror)),
              ("sponsor keys", lambda: keys_section(now, client, mirror))]
    if B2_KEYS and not os.path.exists(B2_KEYS):
        checks.append(("B2", lambda: (False, f"B2: no keys file at {B2_KEYS}, so the second copy is not checked")))
    elif B2_KEYS:
        b2 = dict(keys=B2_KEYS, bucket=B2_BUCKET, where="B2")
        checks += [("proofs in B2", lambda: proofs_section(now, client, mirror, **b2)),
                   ("hourly mirror to B2", lambda: mirror_section(run, B2_MIRROR_UNIT, "Hourly mirror to B2")),
                   ("ledger in B2", lambda: ledger_copy_section(now, client, mirror, **b2)),
                   ("spine in B2", lambda: spine_section(now, client, mirror, **b2)),
                   ("sponsor keys in B2", lambda: keys_section(now, client, mirror, **b2))]
    sections = []
    for name, fn in checks:
        try:
            sections.append(fn())
        except Exception as e:           # one broken check must not silence the others
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
