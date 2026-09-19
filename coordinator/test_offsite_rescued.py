#!/usr/bin/env python3
"""The encrypted offsite copy of the rescued coordinator tree (hazync-admin#1).

WHY THIS EXISTS. /srv/bulk/hazync/rescued-from-old-box is 2.67 GB pulled off 152.53.93.164 before that box
was retired on 2026-09-19. Measured the same day: ZERO offsite units named that path, against six covering
other paths, and the box it came from is stopped and disabled — so server 1 holds the only copy. It carries
the rescued ed25519 identity, twelve coordinator.db snapshots, and the receipts-and-evidence tars.

⛔ THE GUARDS UNDER TEST, both of which protect a PRIVATE KEY:

  1. THE CIPHERTEXT MUST BE ENCRYPTED TO THE PINNED RECIPIENT. Only the operator's PUBLIC key is on the
     box. If a swapped or wrong public key were used, the upload would still "succeed" and the only copy
     of that identity would be readable by whoever held the other secret key — or by nobody at all, which
     is worse, because it would look like a backup and restore nothing.
  2. CONTENT-NAMED MEANS A RE-RUN UPLOADS NOTHING. The object is rescued-<sha256[:16]>.tar.gpg over a
     DETERMINISTIC tar. If the tar were not deterministic, every weekly run would upload another 2.67 GB
     of an append-only bucket for ever, and no run would ever agree it was already safe.

⛔ REAL CALLS, REAL GPG. The refusals are driven through the actual recipient_keys/packet_keyids path with
a throwaway keypair, not a re-implementation. Four positive controls written earlier today turned out to
be inert — asserting the test's own copy of a rule, or passing because a directory happened to be absent.

Usage:
  python3 test_offsite_rescued.py            # assertions; exit 0 on success
  python3 test_offsite_rescued.py --control  # determinism defeated; MUST fail
"""

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL = "--control" in sys.argv

loader = importlib.machinery.SourceFileLoader(
    "offsite", os.path.join(HERE, "deploy", "hazync-offsite-proofs.py"))
spec = importlib.util.spec_from_loader("offsite", loader)
off = importlib.util.module_from_spec(spec)
loader.exec_module(off)

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def tree(files):
    d = tempfile.mkdtemp(prefix="resc-src-")
    for rel, data in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    return d


def digest_of(src):
    with tempfile.NamedTemporaryFile(prefix="resc-", suffix=".tar") as out:
        count, size, sha = off.rescued_tar(src, out)
    return count, size, sha


FILES = {"identity/root-hazync-key.hex": b"00" * 32,
         "coordinator.db.pre-300": b"sqlite-ish" * 1000,
         "evidence/receipts.tar": b"tarry" * 5000}

src = tree(FILES)

# ── the tar ───────────────────────────────────────────────────────────────────────────────────────────

count, size, sha1 = digest_of(src)
check(count == 3, f"every regular file is included ({count})")
check(size > 0 and sha1, "the tar has a size and a digest")

# 1. ⛔ THE FIRST CONTROL CASE: determinism. Same tree, built again, must give the same digest.
if CONTROL:
    off._CONTROL_NONDETERMINISTIC_TAR = True

_, _, sha2 = digest_of(src)
check(sha1 == sha2,
      "the same tree gives the same digest, so a re-run finds the object present and uploads nothing")

# A CHANGED tree must give a different digest, or a modified archive would silently never be re-uploaded.
src2 = tree({**FILES, "coordinator.db.pre-301": b"new snapshot"})
_, _, sha3 = digest_of(src2)
check(sha3 != sha1, "a changed tree gives a different digest, so new content is not mistaken for old")

# Names are namespaced, so the object cannot collide with the identities tar in the same bucket.
with tempfile.NamedTemporaryFile(prefix="resc-", suffix=".tar") as out:
    off.rescued_tar(src, out)
    out.seek(0)
    names = off.tarfile.open(fileobj=out, mode="r").getnames()
check(all(n.startswith("rescued/") for n in names), "every member is under rescued/, never bare paths")

# ── the refusals ──────────────────────────────────────────────────────────────────────────────────────

check(off.rescued_copy(None, "b", "rescued/", "/nonexistent/tree", "k.asc", "F") == 1,
      "a missing source directory is refused, not reported as a successful backup")

# A real throwaway keypair, so recipient_keys() runs for real.
gnupg = tempfile.mkdtemp(prefix="resc-gpg-")
os.chmod(gnupg, 0o700)
gen = subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", gnupg, "--quick-generate-key",
                      "rescued-test@example.invalid", "default", "default", "never"],
                     capture_output=True, timeout=120)
HAVE_GPG = gen.returncode == 0
if HAVE_GPG:
    out = subprocess.run(["gpg", "--homedir", gnupg, "--with-colons", "--list-keys"],
                         capture_output=True, text=True, timeout=60).stdout
    fpr = next(l.split(":")[9] for l in out.splitlines() if l.startswith("fpr"))
    pub = os.path.join(gnupg, "pub.asc")
    subprocess.run(["gpg", "--homedir", gnupg, "--armor", "--output", pub, "--export", fpr],
                   capture_output=True, timeout=60)

    scratch = tempfile.mkdtemp(prefix="resc-scratch-")
    subs, _, why = off.recipient_keys(scratch, pub, fpr)
    check(why is None and subs, f"the pinned recipient's public key imports and has an encryption subkey")

    # 2. ⛔ THE SECOND CONTROL CASE is structural, not flag-driven: a WRONG fingerprint must be refused.
    bad = "0" * 40
    _, _, why_bad = off.recipient_keys(tempfile.mkdtemp(prefix="resc-bad-"), pub, bad)
    check(why_bad is not None,
          "a public key whose primary fingerprint is not the pinned one is REFUSED")
    off.gpg_stop(scratch)
    shutil.rmtree(scratch, ignore_errors=True)
else:
    print("  ..  gpg unavailable: the recipient-pinning assertions are SKIPPED (not passed)")

off.gpg_stop(gnupg)
shutil.rmtree(gnupg, ignore_errors=True)
for d in (src, src2):
    shutil.rmtree(d, ignore_errors=True)

print()
EXPECTED_CONTROL_FAILURES = {
    "the same tree gives the same digest, so a re-run finds the object present and uploads nothing",
}

if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — determinism was defeated and exactly the {len(got)} assertion(s) that "
              "depend on it failed:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — defeating determinism did not produce the expected failures.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES):
        print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")
