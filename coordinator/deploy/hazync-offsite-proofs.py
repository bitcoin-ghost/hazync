#!/usr/bin/env python3
"""Mirror the coordinator's proof receipts to S3-compatible storage (Cloudflare R2, Backblaze B2), append-only.

    hazync-offsite-proofs.py copy  --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs
    hazync-offsite-proofs.py check --keys /etc/hazync/backup/r2.keys --bucket hazync-proofs

A receipt is immutable once written, so the mirror only ever ADDS: a file already present remotely is
never re-uploaded or deleted. Keys are namespaced by guest id (`proofs-<first 8 of METHOD_ID>/`),
the same layout backup.sh uses, because file names repeat across re-baselines with different bytes.

`copy` lists the remote once, uploads every local proof older than --min-age that is missing, and
exits 1 if any upload failed. `check` compares names and sizes and exits 1 if any proof older than
--min-age is missing or differs, so a timer can alert on it.

Why not rclone: Ubuntu 24.04's rclone 1.60 reports every upload to R2 as `501 NotImplemented` (the
PUT succeeds; the HEAD it sends afterwards is refused), and with that worked around it still never
queued a transfer against the flat ~97,000-file proofs directory (measured 2026-09-15).

Keys file: one line, "<access key id> <secret> <endpoint> [bucket]", readable by root only.
"""
import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


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


def main(argv=None, client=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["copy", "check"])
    ap.add_argument("--keys", required=True)
    ap.add_argument("--bucket")
    ap.add_argument("--proofs", default=os.environ.get("COORD_PROOFS", "/var/lib/hazync/proofs"))
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
    rl = RateLimit(a.bwlimit_mbit * 1e6 / 8)
    done = failed = sent = 0
    last = t1 = time.monotonic()

    def up(name):
        size = local[name]
        rl.take(size)
        with open(os.path.join(a.proofs, name), "rb") as f:
            s3.put_object(Bucket=bucket, Key=prefix + name, Body=f, ContentType="application/octet-stream")
        return size

    with ThreadPoolExecutor(max(1, a.threads)) as ex:
        futs = {ex.submit(up, n): n for n in todo}
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
                eta = (len(todo) - done - failed) / rate if rate else 0
                log(f"progress: {done}/{len(todo)} uploaded, {failed} failed, {sent / 1e9:.2f} GB, "
                    f"{rate:.1f} files/s, {sent * 8 / 1e6 / el:.0f} Mbit/s, ETA {eta / 60:.0f} min")
    log(f"done: {done} uploaded, {failed} failed, {sent / 1e9:.2f} GB in {(time.monotonic() - t1) / 60:.1f} min")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
