#!/usr/bin/env python3
"""Mirror the coordinator's proofs to S3-compatible storage (Cloudflare R2, Backblaze B2), append-only.

    hazync-offsite-proofs.py copy    --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs
    hazync-offsite-proofs.py check   --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs
    hazync-offsite-proofs.py spine   --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs --verify /usr/local/bin/hazync-verify

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

Why not rclone: Ubuntu 24.04's rclone 1.60 reports every upload to R2 as `501 NotImplemented` (the
PUT succeeds; the HEAD it sends afterwards is refused), and with that worked around it still never
queued a transfer against the flat ~97,000-file proofs directory (measured 2026-09-15).

Keys file: one line, "<access key id> <secret> <endpoint> [bucket]", readable by root only.
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
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


def list_local(proofs, min_age, now):
    local, young = {}, 0
    for e in os.scandir(proofs):
        if not e.is_file() or not e.name.startswith("proof_"):
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


def spine_copy(s3, bucket, prefix, spine_dir, verify):
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
    pair = [(name + ".bin", data), (name + ".json", js)]     # .bin first: a .json in R2 means a complete pair
    remote = list_remote(s3, bucket, prefix)
    uploaded = 0
    for key, body in pair:
        if key in remote:
            if remote[key] != len(body):
                log(f"spine: WARNING, {prefix}{key} is {remote[key]} bytes in R2, not {len(body)}; NOT overwritten")
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
    state = "uploaded" if uploaded else "already in R2"
    log(f"spine: {prefix}{name} [{int(meta['lo']):,}..{int(meta['hi']):,}] {state}, "
        f"{'complete' if ok else 'INCOMPLETE'} in R2; {copies:,} spine copies there")
    return 0 if ok else 1


def main(argv=None, client=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["copy", "check", "spine"])
    ap.add_argument("--keys", required=True)
    ap.add_argument("--bucket")
    ap.add_argument("--proofs", default=os.environ.get("COORD_PROOFS", "/var/lib/hazync/proofs"))
    ap.add_argument("--spine", default=os.environ.get("COORD_SPINE", "/var/lib/hazync/spine"))
    ap.add_argument("--verify", help="spine: hazync-verify binary; a copy it rejects is not uploaded")
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

    if a.mode == "spine":
        return spine_copy(s3, bucket, f"spine-{method_prefix(a.repo)}/", a.spine, a.verify)

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
