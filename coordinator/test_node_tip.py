#!/usr/bin/env python3
"""Tests for the published-node-height path feeding chain_tip().

The interesting case is not "does it read a number". It is what happens when the writer STOPS: a
last-known height left on disk looks exactly like a live one, and trusting it would replace a constant
that goes stale with a file that goes stale — the same bug wearing a different hat. So the staleness
guard gets the most attention here.

Run: python3 coordinator/test_node_tip.py     (silent success, non-zero exit on failure)
"""
import os, sys, time, tempfile, subprocess

_d = tempfile.mkdtemp(prefix="nodetip_")
os.environ["COORD_DB"]   = os.path.join(_d, "c.db")
os.environ["TIP_FILE"]   = os.path.join(_d, "node_tip")
os.environ["TIP_HEIGHT"] = "958301"
os.environ["TIP_FILE_MAX_AGE"] = "3600"
os.environ["HAZYNC_BRIDGE_OUT"] = os.path.join(_d, "bundles")
os.environ["WITNESS_DIR"] = os.path.join(_d, "witnesses")
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
os.makedirs(os.environ["HAZYNC_BRIDGE_OUT"], exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

FAILS = []
def check(cond, what):
    if not cond:
        FAILS.append(what); print(f"[FAIL] {what}")

TIP = os.environ["TIP_FILE"]

def write_tip(text, age=0.0):
    with open(TIP, "w") as f:
        f.write(text)
    if age:
        t = time.time() - age
        os.utime(TIP, (t, t))

def clear_caches():
    server._tip_cache.update(t=0.0, v=None)

def bundle(h):
    open(os.path.join(os.environ["HAZYNC_BRIDGE_OUT"], f"bundle_{h}.json"), "w").close()

# ---------------------------------------------------------------- reading -------------------------
try: os.remove(TIP)
except OSError: pass
clear_caches()
check(server.node_tip() is None, "a missing tip file reads as None, not an exception")
check(server.chain_tip() == 958301, "with no file and no bundles, chain_tip is the floor")

write_tip("962795\n")
check(server.node_tip() == 962795, "a published height is read")
clear_caches()
check(server.chain_tip() == 962796, "chain_tip becomes node height + 1 (exclusive bound)")

# ---------------------------------------------------------------- the staleness guard -------------
write_tip("962795\n", age=7200)          # writer died two hours ago
check(server.node_tip() is None, "a tip file older than TIP_FILE_MAX_AGE is not trusted")
clear_caches()
check(server.chain_tip() == 958301, "a stale file falls back to the floor rather than freezing high")

write_tip("962795\n", age=1800)          # half an hour old: still inside the window
check(server.node_tip() == 962795, "a file inside the window is still trusted")

# ---------------------------------------------------------------- garbage in ----------------------
for junk in ("", "   ", "not-a-number", "12.5", "-4", "0", "99999999999999999999x"):
    write_tip(junk)
    check(server.node_tip() is None, f"garbage tip file {junk!r} reads as None")
clear_caches()
check(server.chain_tip() == 958301, "garbage falls back to the floor")

# ---------------------------------------------------------------- the max() ------------------------
# Each source is a lower bound on the truth; chain_tip must take the highest and never regress.
bundle(220000)
clear_caches()
write_tip("962795\n")
check(server.chain_tip() == 962796, "node height wins when it is ahead of the bundles")

write_tip("100\n")                        # node absurdly behind (e.g. a fresh resync)
clear_caches()
check(server.chain_tip() == 958301,
      f"a node BEHIND the floor cannot drag the denominator down (got {server.chain_tip()})")

write_tip("500000\n")
clear_caches()
check(server.chain_tip() == 958301, "a node behind the floor still cannot lower it")

# provable_tip is allocation and must stay pinned to what can actually be SERVED, not to the node.
write_tip("962795\n")
clear_caches()
check(server.provable_tip() == 220001,
      f"provable_tip still follows servable bundles, not the node (got {server.provable_tip()})")

# ---------------------------------------------------------------- the publisher script ------------
sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "hazync-node-tip.sh")
if os.path.exists(sh):
    fake = os.path.join(_d, "bin"); os.makedirs(fake, exist_ok=True)
    def fake_cli(body):
        p = os.path.join(fake, "bitcoin-cli")
        with open(p, "w") as f: f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(p, 0o755)
    env = dict(os.environ, PATH=fake + ":" + os.environ["PATH"],
               TIP_FILE=os.path.join(_d, "published"), TIP_FILE_OWNER=f"{os.getuid()}:{os.getgid()}")
    out = os.path.join(_d, "published")

    fake_cli('echo 962795')
    r = subprocess.run(["bash", sh], env=env, capture_output=True)
    check(r.returncode == 0, f"publisher succeeds on a good height (rc={r.returncode})")
    check(open(out).read().strip() == "962795", "publisher writes the height")

    # A node that is still starting prints nothing on stdout. The previous good value must survive.
    fake_cli('exit 1')
    r = subprocess.run(["bash", sh], env=env, capture_output=True)
    check(r.returncode != 0, "publisher fails loudly when bitcoin-cli errors")
    check(open(out).read().strip() == "962795",
          "a failed run leaves the last good height intact rather than truncating it")

    fake_cli('echo "error: couldn\'t connect"')
    r = subprocess.run(["bash", sh], env=env, capture_output=True)
    check(r.returncode != 0, "publisher rejects non-numeric output")
    check(open(out).read().strip() == "962795", "non-numeric output does not overwrite the good value")

    fake_cli('echo 0')
    r = subprocess.run(["bash", sh], env=env, capture_output=True)
    check(r.returncode != 0, "publisher refuses to publish height 0")
else:
    print("note: deploy/hazync-node-tip.sh not found — publisher checks skipped")

import shutil; shutil.rmtree(_d, ignore_errors=True)
if FAILS:
    print(f"\nnode tip: {len(FAILS)} FAILED")
    sys.exit(1)
print("node tip: all checks passed")
