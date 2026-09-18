#!/usr/bin/env python3
"""Mirror the coordinator's proofs to S3-compatible storage (Cloudflare R2, Backblaze B2), append-only.

    hazync-offsite-proofs.py copy    --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs
    hazync-offsite-proofs.py check   --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs
    hazync-offsite-proofs.py spine   --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs --verify /usr/local/bin/hazync-verify
    hazync-offsite-proofs.py keys    --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs --pubkey KEY.asc --recipient FINGERPRINT
    hazync-offsite-proofs.py ledger  --keys /etc/hazync/backup/b2.keys --bucket hazync-backup --db /var/lib/hazync/coordinator.db

A receipt is immutable once written, so the mirror only ever ADDS: a file already present remotely is
never re-uploaded or deleted. Keys are namespaced by guest id (`proofs-<first 8 of METHOD_ID>/`),
the same layout backup.sh uses, because file names repeat across re-baselines with different bytes.

`copy` lists the remote once, uploads every local proof older than --min-age that is missing, and
exits 1 if any upload failed. `check` compares names and sizes and exits 1 if any proof older than
--min-age is missing or differs, so a timer can alert on it.

`spine` copies the genesis-anchored spine (`spine.bin` + `spine.json`), which the coordinator rewrites
every ~15-30 s and which is the one proof that cannot be rebuilt cheaply. Each copy is kept under its
own height, `spine-<first 8 of METHOD_ID>/spine_<lo>-<hi>.{bin,json}`, and never overwritten. A copy is
uploaded only if spine.bin matches the sha256 and size in spine.json (the two files are replaced one
after the other, so a read can land between them) and, with --verify, hazync-verify accepts it. The
.bin goes up before the .json, so a .json in R2 means its pair is complete. Exits 1 on any failure.

`keys` copies the sponsor bot's signing identities (`identities/<sponsorship>/key.hex` + `handle`), ENCRYPTED.
Each sponsorship proves under its own ed25519 key; lose one and that sponsor's blocks can never be signed
for again. The directory is packed into a tar that is byte-identical for identical contents, encrypted
with gpg to the encryption subkey of --recipient, and uploaded as `sponsor-keys/identities-<sha256 of the
tar, 16 hex>.tar.gpg`, never overwritten. Only the PUBLIC key is on the box: --pubkey is imported into a
scratch keyring each run and refused unless its primary fingerprint is --recipient, and the ciphertext is
refused unless its packets name that key's encryption subkey. Neither this box nor R2 can decrypt a copy.

`ledger` takes an online SQLite backup of the coordinator ledger (safe while the coordinator writes, and it
includes what is still in the WAL), refuses it unless `PRAGMA integrity_check` is ok and it has a `ranges`
table, gzips it and uploads `ledger/coordinator-<UTC stamp>.db.gz`, never overwritten. It is the ledger's
copy in B2: Litestream 0.5 allows one replica per database, and that one goes to R2. Exits 1 on any failure.

The same script serves both stores; log lines name the store from the endpoint (R2, B2).

Why not rclone: Ubuntu 24.04's rclone 1.60 reports every upload to R2 as `501 NotImplemented` (the
PUT succeeds; the HEAD it sends afterwards is refused), and with that worked around it still never
queued a transfer against the flat ~97,000-file proofs directory (measured 2026-09-15).

Keys file: one line, "<access key id> <secret> <endpoint> [bucket]", readable by root only.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

SPINE_READ_TRIES = 5
SPINE_READ_PAUSE = 1.0


def log(msg):
    print(f"[offsite-proofs] {msg}", flush=True)


def method_prefix(repo):
    with open(os.path.join(repo, "reproduce", "METHOD_ID")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tok = "".join(ch for ch in line if ch in "0123456789abcdef")
                if len(tok) >= 64:
                    return tok[:8]
    raise SystemExit("could not read a 64-hex METHOD_ID from reproduce/METHOD_ID")


def store_name(endpoint):
    """How log lines name the store: R2, B2, or the endpoint's host."""
    if "r2.cloudflarestorage.com" in endpoint:
        return "R2"
    if "backblazeb2.com" in endpoint:
        return "B2"
    return endpoint.split("//")[-1].split("/")[0]


def utc_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def make_client(kid, secret, endpoint, threads):
    import boto3                                   # imported here so the planning logic tests without it
    from botocore.config import Config
    url = endpoint if endpoint.startswith("http") else "https://" + endpoint
    region = "auto" if "r2.cloudflarestorage.com" in url else None
    return boto3.client("s3", endpoint_url=url, aws_access_key_id=kid, aws_secret_access_key=secret,
                        region_name=region,
                        config=Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=15,
                                      read_timeout=60, max_pool_connections=threads + 4))


def plan(local, remote):
    """(missing, differ): names to upload, and names whose remote size disagrees. Append-only: a name
    already present remotely is never scheduled for upload, whatever its size."""
    missing = sorted(n for n in local if n not in remote)
    differ = sorted(n for n in local if n in remote and remote[n] != local[n])
    return missing, differ


def list_local(root, min_age, now, prefix="proof_", suffix=""):
    """{name: size} for files under root named prefix*suffix and older than min_age, plus a young count.

    ⛔ `suffix` exists for the checkpoints mirror. The archiver writes `state_<h>.bin.tmp` and renames
    (hazync#347 6.6), and the 230,000 rung was streamed onto the box as exactly that. Matching on the
    prefix alone would upload a half-written rung — and because this mirror is APPEND-ONLY, the complete
    file would then never replace it. A partial rung that can never be corrected is worse than no rung.
    """
    local, young = {}, 0
    for e in os.scandir(root):
        if not e.is_file() or not e.name.startswith(prefix) or not e.name.endswith(suffix):
            continue
        st = e.stat()
        if now - st.st_mtime < min_age:
            young += 1
            continue
        local[e.name] = st.st_size
    return local, young


def list_remote(s3, bucket, prefix):
    remote = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            remote[o["Key"][len(prefix):]] = o["Size"]
    return remote


def read_spine(spine_dir, tries=None, pause=None):
    """(spine.bin bytes, spine.json bytes, meta, None) for a pair that agrees, or (None, None, None, why).
    The coordinator replaces spine.bin and then spine.json, so a read between the two is retried."""
    tries = SPINE_READ_TRIES if tries is None else tries
    pause = SPINE_READ_PAUSE if pause is None else pause
    why = "no spine"
    for i in range(max(1, tries)):
        try:
            with open(os.path.join(spine_dir, "spine.json"), "rb") as f:
                js = f.read()
            with open(os.path.join(spine_dir, "spine.bin"), "rb") as f:
                data = f.read()
            meta = json.loads(js)
            if hashlib.sha256(data).hexdigest() == meta.get("sha256") and len(data) == meta.get("bytes"):
                return data, js, meta, None
            why = "spine.bin does not match the sha256 and size in spine.json"
        except (OSError, ValueError) as e:
            why = f"cannot read the spine: {e!r}"
        if i + 1 < tries:
            time.sleep(pause)
    return None, None, None, why


def spine_name(meta):
    return f"spine_{int(meta['lo'])}-{int(meta['hi'])}"


def verify_spine(verify, data):
    """(ok, first line of hazync-verify's output). ok only when it exits 0 on these exact bytes."""
    with tempfile.NamedTemporaryFile(suffix=".hzk") as f:
        f.write(data)
        f.flush()
        try:
            p = subprocess.run([verify, f.name], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"could not run {verify}: {e!r}"
    lines = [ln for ln in ((p.stdout or "") + (p.stderr or "")).splitlines() if ln.strip()]
    return p.returncode == 0, ((lines[0].strip() if lines else f"exit {p.returncode}")[:300])


class RateLimit:
    """Bytes per second across all upload threads (token bucket, 1 s burst)."""
    def __init__(self, bps):
        self.bps, self.tokens, self.t, self.lock = bps, bps, time.monotonic(), threading.Lock()

    def take(self, n):
        if self.bps <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.bps, self.tokens + (now - self.t) * self.bps)
                self.t = now
                if self.tokens >= n or self.tokens >= self.bps:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.bps
            time.sleep(min(wait, 1.0))


def upload_all(s3, bucket, prefix, root, names, sizes, threads, bwlimit_mbit):
    """Upload root/<name> to prefix+name for each name. (done, failed, bytes sent)."""
    rl = RateLimit(bwlimit_mbit * 1e6 / 8)
    done = failed = sent = 0
    last = t1 = time.monotonic()

    def up(name):
        size = sizes[name]
        rl.take(size)
        with open(os.path.join(root, name), "rb") as f:
            s3.put_object(Bucket=bucket, Key=prefix + name, Body=f, ContentType="application/octet-stream")
        return size

    with ThreadPoolExecutor(max(1, threads)) as ex:
        futs = {ex.submit(up, n): n for n in names}
        for fut in as_completed(futs):
            try:
                sent += fut.result()
                done += 1
            except Exception as e:                  # counted, reported, and the run exits 1
                failed += 1
                if failed <= 10:
                    log(f"FAILED {futs[fut]}: {e!r}")
            if time.monotonic() - last >= 30:
                last = time.monotonic()
                el = last - t1
                rate = done / el if el else 0
                eta = (len(names) - done - failed) / rate if rate else 0
                log(f"progress: {done}/{len(names)} uploaded, {failed} failed, {sent / 1e9:.2f} GB, "
                    f"{rate:.1f} files/s, {sent * 8 / 1e6 / el:.0f} Mbit/s, ETA {eta / 60:.0f} min")
    log(f"done: {done} uploaded, {failed} failed, {sent / 1e9:.2f} GB in {(time.monotonic() - t1) / 60:.1f} min")
    return done, failed, sent


def spine_copy(s3, bucket, prefix, spine_dir, verify, where="R2"):
    data, js, meta, why = read_spine(spine_dir)
    if data is None:
        log(f"spine: NOT uploaded, {why}")
        return 1
    name = spine_name(meta)
    if verify:
        ok, detail = verify_spine(verify, data)
        if not ok:
            log(f"spine: NOT uploaded, hazync-verify rejected {name}: {detail}")
            return 1
        log(f"spine: {detail}")
    else:
        log("spine: WARNING, no --verify given, so this copy is uploaded without being verified")
    pair = [(name + ".bin", data), (name + ".json", js)]     # .bin first: a .json in the store means a complete pair
    remote = list_remote(s3, bucket, prefix)
    uploaded = 0
    for key, body in pair:
        if key in remote:
            if remote[key] != len(body):
                log(f"spine: WARNING, {prefix}{key} is {remote[key]} bytes in {where}, not {len(body)}; NOT overwritten")
            continue
        try:
            s3.put_object(Bucket=bucket, Key=prefix + key, Body=io.BytesIO(body),
                          ContentType="application/json" if key.endswith(".json") else "application/octet-stream")
            uploaded += 1
        except Exception as e:
            log(f"spine: FAILED {prefix}{key}: {e!r}")
            return 1
    remote = list_remote(s3, bucket, prefix)
    ok = all(remote.get(k) == len(b) for k, b in pair)
    copies = sum(1 for k in remote if k.endswith(".json"))
    state = "uploaded" if uploaded else f"already in {where}"
    log(f"spine: {prefix}{name} [{int(meta['lo']):,}..{int(meta['hi']):,}] {state}, "
        f"{'complete' if ok else 'INCOMPLETE'} in {where}; {copies:,} spine copies there")
    return 0 if ok else 1


def identities_tar(src):
    """(tar bytes, file count) of every regular file under src as identities/<relative path>. Sorted, with
    mtime and owner zeroed, so identical contents give identical bytes and the sha256 names the state."""
    names = []
    for root, _, files in os.walk(src):
        for n in files:
            p = os.path.join(root, n)
            if os.path.isfile(p) and not os.path.islink(p):
                names.append(os.path.relpath(p, src).replace(os.sep, "/"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(names):
            with open(os.path.join(src, rel), "rb") as f:
                data = f.read()
            ti = tarfile.TarInfo("identities/" + rel)
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0, 0o600, 0, 0, "", ""
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue(), len(names)


def identities_object(src):
    tar, count = identities_tar(src)
    return tar, count, f"identities-{hashlib.sha256(tar).hexdigest()[:16]}.tar.gpg"


def gpg(home, args, data=None):
    p = subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", home] + list(args), input=data,
                       capture_output=True, timeout=120)
    return p.returncode, p.stdout, p.stderr.decode(errors="replace")


def gpg_stop(home):
    subprocess.run(["gpgconf", "--homedir", home, "--kill", "all"], capture_output=True, timeout=30)


def recipient_keys(home, pubkey, fpr):
    """Import pubkey into the scratch keyring `home`. ([usable encryption subkey ids], expiry, None), or
    ([], None, why). The primary fingerprint must be fpr: a swapped public key file is refused."""
    rc, _, err = gpg(home, ["--import", pubkey])
    if rc != 0:
        return [], None, f"cannot import {pubkey}: {err.strip()[-200:]}"
    rc, out, _ = gpg(home, ["--with-colons", "--list-keys", fpr])
    if rc != 0:
        return [], None, f"{fpr} is not in {pubkey}"
    primary, primary_exp, subs, last = [], None, [], None
    for line in out.decode(errors="replace").splitlines():
        f = line.split(":")
        if f[0] == "pub":
            last, primary_exp = "pub", (int(f[6]) if f[6] else None)
        elif f[0] == "sub":
            last = "sub"
            if "e" in f[11] and f[1] not in ("e", "r"):           # validity: e = expired, r = revoked
                subs.append((f[4], int(f[6]) if f[6] else None))
        elif f[0] == "fpr" and last == "pub":
            primary.append(f[9].upper())
    if fpr.upper() not in primary:
        return [], None, f"{fpr} is not in {pubkey}"
    if not subs:
        return [], None, f"{fpr} has no usable encryption subkey"
    sub_exp = None if any(e is None for _, e in subs) else max(e for _, e in subs)
    ends = [e for e in (primary_exp, sub_exp) if e]
    return [k.upper() for k, _ in subs], (min(ends) if ends else None), None


def packet_keyids(home, cipher):
    """The key ids a gpg ciphertext is encrypted to, read from its packets (no secret key needed)."""
    with tempfile.NamedTemporaryFile(suffix=".gpg") as f:
        f.write(cipher)
        f.flush()
        _, out, err = gpg(home, ["--list-packets", f.name])
    text = out.decode(errors="replace") + err
    return sorted(set(k.upper() for k in re.findall(r"pubkey enc packet: version \d+, algo \d+, keyid ([0-9A-Fa-f]{16})", text)))


def keys_copy(s3, bucket, prefix, src, pubkey, fpr, where="R2"):
    if not os.path.isdir(src):
        log(f"keys: NOT uploaded, no identities directory at {src}")
        return 1
    tar, count, name = identities_object(src)
    home = tempfile.mkdtemp(prefix="hzk-")
    try:
        subs, _, why = recipient_keys(home, pubkey, fpr)
        if why:
            log(f"keys: NOT uploaded, {why}")
            return 1
        remote = list_remote(s3, bucket, prefix)
        copies = sum(1 for k in remote if k.endswith(".tar.gpg"))
        if name in remote:
            log(f"keys: {prefix}{name} ({count} files) already in {where}; {copies} encrypted copies there")
            return 0
        rc, cipher, err = gpg(home, ["--trust-model", "always", "--recipient", fpr, "--encrypt"], data=tar)
        if rc != 0 or not cipher:
            log(f"keys: NOT uploaded, gpg --encrypt failed: {err.strip()[-300:]}")
            return 1
        if not set(packet_keyids(home, cipher)) & set(subs):
            log(f"keys: NOT uploaded, the ciphertext is not encrypted to an encryption subkey of {fpr}")
            return 1
        if b"identities/" in cipher:
            log("keys: NOT uploaded, the ciphertext contains plaintext file names")
            return 1
        try:
            s3.put_object(Bucket=bucket, Key=prefix + name, Body=io.BytesIO(cipher),
                          ContentType="application/pgp-encrypted")
        except Exception as e:
            log(f"keys: FAILED {prefix}{name}: {e!r}")
            return 1
        ok = list_remote(s3, bucket, prefix).get(name) == len(cipher)
        log(f"keys: {prefix}{name} ({count} files, encrypted to {', '.join(subs)}) uploaded, "
            f"{'complete' if ok else 'INCOMPLETE'} in {where}; {copies + 1} encrypted copies there")
        return 0 if ok else 1
    finally:
        gpg_stop(home)
        shutil.rmtree(home, ignore_errors=True)


def ledger_snapshot(db, out):
    """Online-backup the live ledger db into out. (rows in ranges, None) or (None, why it is refused).
    Opened read-only, so a wrong path fails instead of creating an empty database."""
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(out)
        src.backup(dst)
        src.close()
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        ranges = dst.execute("SELECT COUNT(*) FROM ranges").fetchone()[0]
        dst.close()
    except sqlite3.Error as e:
        return None, f"not a usable coordinator ledger: {e}"
    if integrity != "ok":
        return None, f"not a usable coordinator ledger: integrity_check says {integrity[:200]!r}"
    return ranges, None


def ledger_copy(s3, bucket, prefix, db, where="B2"):
    name = f"coordinator-{utc_stamp()}.db.gz"
    work = tempfile.mkdtemp(prefix="hzl-")
    try:
        snap = os.path.join(work, "coordinator.db")
        ranges, why = ledger_snapshot(db, snap)
        if why:
            log(f"ledger: NOT uploaded, {db} is {why}")
            return 1
        gz = snap + ".gz"
        with open(snap, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        size = os.path.getsize(gz)
        remote = list_remote(s3, bucket, prefix)
        if name in remote:
            log(f"ledger: {prefix}{name} already in {where} ({remote[name]} bytes); NOT overwritten")
            return 0 if remote[name] == size else 1
        try:
            with open(gz, "rb") as f:
                s3.put_object(Bucket=bucket, Key=prefix + name, Body=f, ContentType="application/gzip")
        except Exception as e:
            log(f"ledger: FAILED {prefix}{name}: {e!r}")
            return 1
        remote = list_remote(s3, bucket, prefix)
        ok = remote.get(name) == size
        copies = sum(1 for k in remote if k.endswith(".db.gz"))
        log(f"ledger: {prefix}{name} ({os.path.getsize(snap) / 1e6:.0f} MB, {ranges:,} ranges, "
            f"{size / 1e6:.0f} MB gzipped) uploaded, {'complete' if ok else 'INCOMPLETE'} in {where}; "
            f"{copies:,} ledger copies there")
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def checkpoints_copy(s3, bucket, prefix, root, min_age, threads, bwlimit_mbit, where="R2"):
    """Mirror the bridge's archived checkpoint rungs, append-only. Uploads and verifies in one pass.

    WHY THIS EXISTS (hazync#386). `prune_bundles.py` will not delete a bundle unless an archived rung
    below it exists to rebuild from — that is the entire safety argument for pruning. The rungs live on
    /srv/bulk, which no offsite unit touched, so the thing authorising deletion was itself single-copy.
    Lose a rung and the bundles pruned against it are not rebuildable, and nothing would notice until
    someone tried to regenerate one.

    Unlike `copy`/`check`, this uploads and verifies in a single mode: there are tens of rungs, not the
    ~100,000 files the proofs directory holds, so a split pass buys nothing.

    ⛔ THE LOWEST RUNG IS NOT JUST ANOTHER FILE. Regeneration seeds from the nearest rung STRICTLY BELOW
    the target, so the lowest one bounds what can be rebuilt at all. Today that is state_230000.bin, the
    only seed below the 418,269-967,499 bundle gap, rescued from the retiring coordinator. If it is
    missing remotely this returns 1 even when every other rung is present.
    """
    if not os.path.isdir(root):
        log(f"checkpoints: NOT uploaded, no directory at {root}")
        return 1
    local, young = list_local(root, min_age, time.time(), prefix="state_", suffix=".bin")
    # ⛔ The prefix and suffix are not enough: `state_abc.bin` and `state_.bin` satisfy both and are not
    # heights. The height is parsed below to find the lowest rung, so a malformed name is a crash rather
    # than a skipped file. hazync-archive-checkpoint.sh guards the same hazard on the shell side
    # (`case "$_b" in ''|*[!0-9]*) continue`); this is that guard, on this side.
    local = {n: sz for n, sz in local.items() if n[len("state_"):-len(".bin")].isdigit()}
    if not local:
        log(f"checkpoints: nothing to mirror in {root} ({young} younger than {min_age:.0f} s skipped)")
        return 0
    remote = list_remote(s3, bucket, prefix)
    missing, differ = plan(local, remote)
    total = sum(local[n] for n in missing)
    log(f"{bucket}/{prefix}: {len(remote)} remote, {len(local)} local rungs "
        f"({young} younger than {min_age:.0f} s skipped), {len(missing)} to upload ({total / 1e9:.2f} GB)")
    if differ:
        log(f"WARNING: {len(differ)} rung(s) differ in size from the remote copy and are NOT overwritten "
            f"(append-only); first: {differ[0]}")
    failed = 0
    if missing:
        _, failed, _ = upload_all(s3, bucket, prefix, root, missing, local, threads, bwlimit_mbit)

    remote = list_remote(s3, bucket, prefix)
    absent = sorted(n for n in local if remote.get(n) != local[n])
    lowest = min(local, key=lambda n: int(n[len("state_"):-len(".bin")]))
    lowest_ok = remote.get(lowest) == local[lowest]
    log(f"checkpoints: {len(local) - len(absent)}/{len(local)} rungs complete in {where}; "
        f"lowest is {lowest} ({local[lowest] / 1e9:.2f} GB), {'present' if lowest_ok else 'MISSING'}")
    if not lowest_ok:
        log("checkpoints: the LOWEST rung is the seed everything below the bundle gap regenerates from — "
            "without it those heights cannot be rebuilt at all")
    for n in absent[:10]:
        log(f"  missing or wrong size: {n}")
    return 1 if (failed or absent) else 0


def main(argv=None, client=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["copy", "check", "spine", "keys", "ledger", "checkpoints"])
    ap.add_argument("--keys", required=True)
    ap.add_argument("--bucket")
    ap.add_argument("--proofs", default=os.environ.get("COORD_PROOFS", "/var/lib/hazync/proofs"))
    ap.add_argument("--spine", default=os.environ.get("COORD_SPINE", "/var/lib/hazync/spine"))
    ap.add_argument("--checkpoints", default=os.environ.get("HAZYNC_CKPT_ARCHIVE", "/srv/bulk/hazync/checkpoints"),
                    help="checkpoints: the bridge's archived rungs (state_<height>.bin)")
    ap.add_argument("--verify", help="spine: hazync-verify binary; a copy it rejects is not uploaded")
    ap.add_argument("--identities", default=os.environ.get("SPONSOR_IDENTITIES", "/var/lib/hazync/sponsor-bot/identities"))
    ap.add_argument("--pubkey", help="keys: the recipient's armored PUBLIC key file")
    ap.add_argument("--recipient", help="keys: the recipient's primary key fingerprint (pinned)")
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"),
                    help="ledger: the live coordinator database")
    ap.add_argument("--repo", default=os.environ.get("HZ_REPO", "/opt/hazync"))
    ap.add_argument("--min-age", type=float, default=120, help="skip proofs modified in the last N seconds")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--bwlimit-mbit", type=float, default=64)
    ap.add_argument("--limit", type=int, default=0, help="upload at most N files (testing)")
    a = ap.parse_args(argv)

    parts = open(a.keys).read().split()
    if len(parts) < 3:
        raise SystemExit("keys file must hold: <access key id> <secret> <endpoint> [bucket]")
    kid, secret, endpoint = parts[:3]
    bucket = a.bucket or (parts[3] if len(parts) > 3 else None)
    if not bucket:
        raise SystemExit("no bucket: pass --bucket or put it 4th in the keys file")
    s3 = client if client is not None else make_client(kid, secret, endpoint, a.threads)
    where = store_name(endpoint)

    if a.mode == "spine":
        return spine_copy(s3, bucket, f"spine-{method_prefix(a.repo)}/", a.spine, a.verify, where)

    if a.mode == "checkpoints":
        # ⛔ NOT namespaced by guest id, unlike proofs and the spine. A rung is bridge state — the UTXO
        # forest at a height — and a re-baseline does not change its bytes. Namespacing it would orphan
        # every existing rung the next time the guest id moved, which is the opposite of the point.
        return checkpoints_copy(s3, bucket, "checkpoints/", a.checkpoints, a.min_age,
                                a.threads, a.bwlimit_mbit, where)

    if a.mode == "ledger":
        return ledger_copy(s3, bucket, "ledger/", a.db, where)

    if a.mode == "keys":
        if not a.pubkey or not a.recipient:
            raise SystemExit("keys needs --pubkey and --recipient")
        return keys_copy(s3, bucket, "sponsor-keys/", a.identities, a.pubkey, a.recipient, where)

    prefix = f"proofs-{method_prefix(a.repo)}/"
    t0 = time.monotonic()
    remote = list_remote(s3, bucket, prefix)
    local, young = list_local(a.proofs, a.min_age, time.time())
    log(f"{bucket}/{prefix}: {len(remote)} remote, {len(local)} local older than {a.min_age:.0f} s "
        f"({young} younger skipped), listed in {time.monotonic() - t0:.1f} s")
    missing, differ = plan(local, remote)

    if a.mode == "check":
        extra = len([n for n in remote if n not in local])
        log(f"check: missing {len(missing)}, size differs {len(differ)}, remote-only {extra} (kept, append-only)")
        for n in (missing + differ)[:10]:
            log(f"  {'missing' if n in missing else 'differs'}: {n}")
        return 1 if (missing or differ) else 0

    if differ:
        log(f"WARNING: {len(differ)} proofs differ in size from the remote copy and are NOT overwritten "
            f"(append-only); first: {differ[0]}")
    todo = missing[: a.limit] if a.limit else missing
    total = sum(local[n] for n in todo)
    log(f"copy: {len(todo)} to upload ({total / 1e9:.2f} GB), {a.threads} threads, cap {a.bwlimit_mbit:g} Mbit/s")
    _, failed, _ = upload_all(s3, bucket, prefix, a.proofs, todo, local, a.threads, a.bwlimit_mbit)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
