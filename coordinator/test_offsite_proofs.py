#!/usr/bin/env python3
"""The offsite receipt mirror is append-only, namespaced by guest id, and its check can fail.

deploy/hazync-offsite-proofs.py copies proof receipts to R2 (later B2). Receipts are the artifacts the
"you don't have to trust us" claim rests on, and until 2026-09-15 they had no off-box copy at all. What
must hold:

  1. `check` exits 1 while any receipt older than --min-age is missing remotely, and 0 once none is;
  2. `copy` uploads exactly the missing receipts, under proofs-<first 8 of METHOD_ID>/;
  3. append-only: a remote receipt is never overwritten (even when its size differs), and remote-only
     receipts are never deleted;
  4. a receipt younger than --min-age is left for the next run (it may still be being written);
  5. a failed upload makes `copy` exit 1, and `check` still reports the receipt missing.

Runs against an in-memory S3 stand-in, so it needs no network and no boto3.

  python3 test_offsite_proofs.py            # must PASS
  python3 test_offsite_proofs.py --control  # plan() re-uploads everything (not append-only); MUST FAIL
"""
import importlib.util
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("offsite", os.path.join(HERE, "deploy", "hazync-offsite-proofs.py"))
off = importlib.util.module_from_spec(spec)
spec.loader.exec_module(off)

if CONTROL:
    off.plan = lambda local, remote: (sorted(local), [])      # overwrite every receipt, every run
    print("CONTROL: plan() re-uploads everything -- the checks below MUST fail")

MID = "37987b85" + "0" * 56


class FakeS3:
    def __init__(self, fail_names=()):
        self.objects = {}                 # (bucket, key) -> bytes
        self.puts = []
        self.fail_names = set(fail_names)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for (b, k) in s3.objects if b == Bucket and k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), 2):          # tiny pages, to exercise pagination
                    yield {"Contents": [{"Key": k, "Size": len(s3.objects[(Bucket, k)])} for k in keys[i:i + 2]]}
        return P()

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if os.path.basename(Key) in self.fail_names:
            raise RuntimeError("simulated upload failure")
        self.objects[(Bucket, Key)] = Body.read()
        self.puts.append(Key)


fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


def run(s3, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = off.main(list(args) + ["--keys", KEYS, "--bucket", "b", "--proofs", PROOFS, "--repo", REPO,
                                     "--bwlimit-mbit", "0", "--threads", "3"], client=s3)
    return rc, buf.getvalue()


tmp = tempfile.mkdtemp(prefix="offsite_")
PROOFS, REPO = os.path.join(tmp, "proofs"), os.path.join(tmp, "repo")
os.makedirs(PROOFS)
os.makedirs(os.path.join(REPO, "reproduce"))
with open(os.path.join(REPO, "reproduce", "METHOD_ID"), "w") as f:
    f.write("# guest id\n" + MID + "\n")
KEYS = os.path.join(tmp, "keys")
with open(KEYS, "w") as f:
    f.write("kid secret https://example.invalid\n")
old = time.time() - 3600
for n in ("proof_1.bin", "proof_2.bin", "proof_3-4.bin", "proof_5.bin"):
    p = os.path.join(PROOFS, n)
    with open(p, "wb") as f:
        f.write(("receipt " + n).encode())
    os.utime(p, (old, old))
with open(os.path.join(PROOFS, "notes.txt"), "w") as f:
    f.write("not a receipt")
os.utime(os.path.join(PROOFS, "notes.txt"), (old, old))
P = "proofs-37987b85/"

s3 = FakeS3()
s3.objects[("b", P + "proof_1.bin")] = b"receipt proof_1.bin"          # already mirrored, same size
s3.objects[("b", P + "proof_2.bin")] = b"an older, different-sized copy"  # present, size differs
s3.objects[("b", P + "proof_0-0.bin")] = b"remote-only receipt"          # remote-only

# 1. check fails while receipts are missing
rc, out = run(s3, "check")
check(rc == 1 and "missing 2" in out and "size differs 1" in out,
      f"check exits 1 with 2 missing and 1 differing (rc={rc})")

# 2 + 3. copy uploads exactly the missing receipts, namespaced, and overwrites/deletes nothing
rc, out = run(s3, "copy")
check(rc == 0, f"copy exits 0 when every upload succeeds (rc={rc})")
check(sorted(s3.puts) == [P + "proof_3-4.bin", P + "proof_5.bin"],
      f"copy uploads exactly the missing receipts under {P} (put {sorted(s3.puts)})")
check(s3.objects[("b", P + "proof_2.bin")] == b"an older, different-sized copy",
      "append-only: a remote receipt whose size differs is NOT overwritten")
check(("b", P + "proof_0-0.bin") in s3.objects, "append-only: a remote-only receipt is not deleted")
check(not any(k.endswith("notes.txt") for (_, k) in s3.objects), "only proof_* files are mirrored")
check(s3.objects.get(("b", P + "proof_5.bin")) == b"receipt proof_5.bin", "uploaded bytes match the local receipt")

# the differing receipt still fails check: a human has to look at it, the mirror must not paper over it
rc, out = run(s3, "check")
check(rc == 1 and "size differs 1" in out and "missing 0" in out,
      f"check still exits 1 for the differing receipt, with nothing missing (rc={rc})")

# 4. a receipt younger than --min-age waits for the next run
young = os.path.join(PROOFS, "proof_6.bin")
with open(young, "wb") as f:
    f.write(b"being written")
s3.puts.clear()
rc, out = run(s3, "copy", "--min-age", "600")
check(P + "proof_6.bin" not in s3.puts and "1 younger skipped" in out,
      "a receipt younger than --min-age is not uploaded yet")
os.utime(young, (old, old))

# 5. a failed upload fails the run, and check still sees the receipt missing
s3f = FakeS3(fail_names={"proof_6.bin"})
s3f.objects = dict(s3.objects)
rc, out = run(s3f, "copy")
check(rc == 1 and "FAILED proof_6.bin" in out, f"a failed upload makes copy exit 1 and names the receipt (rc={rc})")
del s3f.objects[("b", P + "proof_2.bin")]         # remove the differing one so only proof_6 can fail check
rc, out = run(s3f, "check")
check(rc == 1 and "missing: proof_6.bin" in out, "after a failed upload, check reports that receipt missing")
s3f.fail_names.clear()
s3f.puts.clear()
rc, _ = run(s3f, "copy")
rc2, out = run(s3f, "check")
check(rc == 0 and sorted(s3f.puts) == [P + "proof_2.bin", P + "proof_6.bin"],
      f"the next run uploads the failed receipt and one that vanished remotely (put {sorted(s3f.puts)})")
check(rc2 == 0 and "missing 0" in out and "size differs 0" in out,
      f"...after which check passes (rc={rc2})")

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
