#!/usr/bin/env python3
"""Integrity check 1: the genesis proof verifies and ends on Bitcoin's real chain.

The spine is the board's headline claim: one file that proves every block from 1 to N. Every step of it is verified
once, when it is submitted. Nothing re-checked it afterwards, and nothing compared it with Bitcoin itself, so a
corrupted file, a bad restore or a coordinator bug would have gone unnoticed. This runs every 10 minutes.

    ./coordinator/check-spine.py                     # coordinator: local files, our own archive node
    ./coordinator/check-spine.py --url https://api.hazync.org \\
        --reference https://mempool.space/api --reference https://blockstream.info/api     # web box

What it checks (all held on the live board on 2026-09-15, blocks 1 to 41,539):
  1. The bytes hash to the head record's `sha256`, and hazync-verify accepts them: verified, genesis-anchored, and
     built by the canonical guest (reproduce/METHOD_ID).
  2. The proof's height is the head record's `hi`, and the head's `out_tip` is the proof's tip.
  3. The proof's tip hash is Bitcoin's block hash at that height: from our archive node on the coordinator, from an
     independent explorer on the web box (which shares no machine with the coordinator).
  4. Coordinator only: the proof's cumulative work equals the node's chainwork at that block. Only Bitcoin's
     most-work chain can match it. Explorers do not publish chainwork, so the web box checks the hash only.
  5. Coordinator only: the spine is not stalled. It fails if the spine has not advanced for SPINE_STALL_SECS (2 h)
     while a proof starting at block hi+1 is already on the board, waiting to be absorbed.

Exit status: 0 everything holds; 1 an integrity failure; 2 something needed for a check could not be reached (the
node, the verifier, the URL, every explorer). A check that could not run says so and exits non-zero. It never passes
quietly.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

UA = "hazync-check-spine"


class Report:
    def __init__(self):
        self.fails, self.cannot = [], []

    def ok(self, what):
        print(f"  ok   {what}")

    def fail(self, what):
        print(f"  FAIL {what}")
        self.fails.append(what)

    def cannot_check(self, what):
        print(f"  COULD NOT CHECK {what}")
        self.cannot.append(what)

    def finish(self):
        print()
        if self.fails:
            print(f"INTEGRITY FAILURE: {len(self.fails)} check(s) on the genesis proof failed.")
            return 1
        if self.cannot:
            print(f"COULD NOT CHECK: {len(self.cannot)} part(s) could not be checked, so this run proves nothing "
                  f"about them.")
            return 2
        print("The genesis proof holds.")
        return 0


def canonical_method_id(given, repo):
    """The canonical guest id: --method-id, else reproduce/METHOD_ID without its comments. None if neither."""
    if given:
        return given.strip().lower()
    try:
        with open(os.path.join(repo, "reproduce", "METHOD_ID")) as f:
            body = "".join(ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#"))
    except OSError:
        return None
    return body.lower() or None


def http_get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.read()


def read_local(spine_dir, tries=3, pause=1.0):
    """(bytes, head, consistent). The coordinator writes spine.bin and then spine.json, so one read can land between
    the two; only a mismatch that survives every retry is reported."""
    data = head = None
    for i in range(tries):
        with open(os.path.join(spine_dir, "spine.json")) as f:
            head = json.load(f)
        with open(os.path.join(spine_dir, "spine.bin"), "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() == head.get("sha256"):
            return data, head, True
        if i + 1 < tries:
            time.sleep(pause)
    return data, head, False


def read_remote(url, tries=3, pause=2.0):
    """As read_local, over HTTP: the head can advance between the two requests."""
    data = head = None
    base = url.rstrip("/")
    for i in range(tries):
        head = json.loads(http_get(base + "/api/spine"))
        data = http_get(base + "/api/spine/proof", timeout=60)
        if hashlib.sha256(data).hexdigest() == head.get("sha256"):
            return data, head, True
        if i + 1 < tries:
            time.sleep(pause)
    return data, head, False


def run_verify(verify, data):
    """(exit code, parsed --json output or None, output lines). Raises OSError or TimeoutExpired if it cannot run."""
    with tempfile.NamedTemporaryFile(suffix=".hzk") as f:
        f.write(data)
        f.flush()
        p = subprocess.run([verify, "--json", f.name], capture_output=True, text=True, timeout=180)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = None
    return p.returncode, out, ((p.stderr or "") + "\n" + (p.stdout or "")).strip().splitlines()


def node_block(cli, datadir, height):
    """(block hash, chainwork as int) at `height` from bitcoind. Raises RuntimeError if the node does not answer."""
    base = [cli] + ([f"-datadir={datadir}"] if datadir else [])
    h = subprocess.run(base + ["getblockhash", str(height)], capture_output=True, text=True, timeout=60)
    if h.returncode != 0:
        raise RuntimeError(f"getblockhash {height}: {(h.stderr or h.stdout).strip()[:200]}")
    block = h.stdout.strip()
    hd = subprocess.run(base + ["getblockheader", block], capture_output=True, text=True, timeout=60)
    if hd.returncode != 0:
        raise RuntimeError(f"getblockheader {block}: {(hd.stderr or hd.stdout).strip()[:200]}")
    return block.lower(), int(json.loads(hd.stdout)["chainwork"], 16)


def explorer_hash(bases, height):
    """(base, block hash) from the first Esplora API that answers, or (None, None). Plus the errors seen."""
    errors = []
    for b in bases:
        try:
            v = http_get(f"{b.rstrip('/')}/block-height/{height}").decode().strip().lower()
        except Exception as e:                           # any failure just moves on to the next explorer
            errors.append(f"{b}: {e}")
            continue
        if len(v) == 64 and all(c in "0123456789abcdef" for c in v):
            return b, v, errors
        errors.append(f"{b}: unexpected answer {v[:60]!r}")
    return None, None, errors


def spine_exists_or_board_empty(spine_dir, db):
    """True when there is no spine AND nothing starts at block 1, so there is genuinely nothing to check yet."""
    if os.path.exists(os.path.join(spine_dir, "spine.json")):
        return False
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        anchored = c.execute("SELECT 1 FROM vranges WHERE lo = 1 LIMIT 1").fetchone()
        c.close()
    except sqlite3.Error:
        return False
    return anchored is None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Integrity check 1: the genesis proof verifies and ends on Bitcoin's "
                                             "real chain.")
    ap.add_argument("--url", help="check the copy this coordinator URL serves (web box), instead of local files")
    ap.add_argument("--reference", action="append", default=[],
                    help="with --url: an Esplora API base such as https://mempool.space/api; tried in order")
    ap.add_argument("--spine-dir", default=os.environ.get("COORD_SPINE", "/var/lib/hazync/spine"))
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"))
    ap.add_argument("--verify", default=os.environ.get("HAZYNC_VERIFY", "/usr/local/bin/hazync-verify"))
    ap.add_argument("--repo", default=os.environ.get("HZ_REPO", "/opt/hazync"))
    ap.add_argument("--method-id", default=os.environ.get("HAZYNC_METHOD_ID"))
    ap.add_argument("--bitcoin-cli", default=os.environ.get("BITCOIN_CLI", "bitcoin-cli"))
    ap.add_argument("--bitcoin-datadir", default=os.environ.get("HAZYNC_BITCOIN_DATADIR"))
    ap.add_argument("--stall-secs", type=int, default=int(os.environ.get("SPINE_STALL_SECS", "7200")))
    # ⛔ FOLDING IS NOT NOTHING HAPPENING (hazync#515). A spine that is behind while folds keep arriving
    # is an operator deliberately folding first and absorbing later -- which is the CHEAP order, because
    # `extend-spine` costs the same whatever the chunk width, so a deep tree buys far more per step.
    # Measured 2026-09-24: absorbing into the collapsed backlog ran at 11.7 blocks/s (chunks up to 1,024),
    # and absorbing near the fold frontier ran at 0.34 blocks/s (chunks of 2 and 4). Same fleet, 34x apart.
    # Calling the first state an INTEGRITY FAILURE fired 75 alerts in a row on a healthy spine.
    ap.add_argument("--fold-idle-secs", type=int, default=int(os.environ.get("SPINE_FOLD_IDLE_SECS", "7200")),
                    help="if no new folded range has arrived in this long either, the spine really is stalled")
    # ⚠ A HARD CEILING, so "folding is live" cannot excuse an unbounded backlog for ever.
    ap.add_argument("--behind-max-secs", type=int, default=int(os.environ.get("SPINE_BEHIND_MAX_SECS", "259200")),
                    help="fail once the spine is this far behind even while folding continues (default 72 h)")
    a = ap.parse_args(argv)
    rep = Report()
    remote = bool(a.url)
    print(f"genesis proof: {a.url if remote else a.spine_dir}")

    if not remote and spine_exists_or_board_empty(a.spine_dir, a.db):
        print("  no genesis proof yet, and nothing on the board starts at block 1: nothing to check.")
        return 0
    try:
        data, head, consistent = read_remote(a.url) if remote else read_local(a.spine_dir)
    except Exception as e:
        rep.cannot_check(f"reading the genesis proof and its head record: {e}")
        return rep.finish()

    hi = int(head.get("hi") or 0)
    got_sha = hashlib.sha256(data).hexdigest()
    if consistent:
        rep.ok(f"the proof's bytes hash to its head record (blocks 1 to {hi:,}, sha256 {got_sha[:12]}…)")
    else:
        rep.fail(f"the proof's bytes do not hash to its head record after 3 reads: {got_sha[:12]}… against "
                 f"{str(head.get('sha256'))[:12]}…")

    try:
        rc, v, lines = run_verify(a.verify, data)
    except (OSError, subprocess.TimeoutExpired) as e:
        rep.cannot_check(f"hazync-verify could not run ({a.verify}): {e}")
        return rep.finish()
    if rc != 0 or not isinstance(v, dict):
        rep.fail(f"hazync-verify rejected the proof (exit {rc}): {' | '.join(lines[:3])[:300]}")
        return rep.finish()
    if v.get("verified") is True and v.get("genesis_anchored") is True:
        rep.ok("hazync-verify accepts it as a verified, genesis-anchored proof")
    else:
        rep.fail(f"hazync-verify did not report it verified and genesis-anchored "
                 f"(verified={v.get('verified')}, genesis_anchored={v.get('genesis_anchored')})")

    gid = str(v.get("guest_image_id", "")).lower()
    mid = canonical_method_id(a.method_id, a.repo)
    if mid:
        if gid == mid:
            rep.ok(f"built by the canonical guest {mid[:8]}")
        else:
            rep.fail(f"built by guest {gid[:8]}, not the canonical {mid[:8]}")
    elif remote:
        rep.ok(f"guest {gid[:8]} is the one this hazync-verify embeds (installed from a signed release)")
    else:
        rep.cannot_check(f"the guest id: no --method-id, and no reproduce/METHOD_ID under {a.repo}")

    height = int(v.get("height", -1))
    tip = str(v.get("tip_hash", "")).lower()
    if height == hi:
        rep.ok(f"the proof ends at block {height:,}, as its head record says")
    else:
        rep.fail(f"the proof ends at block {height:,} but its head record says {hi:,}")
    try:
        head_tip = bytes.fromhex(str(head.get("out_tip", "")))[::-1].hex()
    except ValueError:
        head_tip = None
    if head_tip == tip:
        rep.ok("the head record's out_tip is the proof's tip")
    else:
        rep.fail(f"the head record's out_tip ({str(head.get('out_tip'))[:16]}…, stored byte-reversed) is not the "
                 f"proof's tip {tip[:16]}…")

    if remote:
        if not a.reference:
            rep.cannot_check("the tip against Bitcoin: no --reference explorer given")
        else:
            base, ref, errors = explorer_hash(a.reference, height)
            if ref is None:
                rep.cannot_check(f"the tip against Bitcoin: no explorer answered for block {height:,}: "
                                 f"{'; '.join(errors)[:300]}")
            elif ref == tip:
                rep.ok(f"the tip is Bitcoin's block {height:,} according to {base}")
            else:
                rep.fail(f"the tip {tip[:16]}… is NOT Bitcoin's block {height:,}: {base} says {ref[:16]}…")
        return rep.finish()

    try:
        block, work = node_block(a.bitcoin_cli, a.bitcoin_datadir, height)
    except Exception as e:
        rep.cannot_check(f"the tip against Bitcoin: our node did not answer for block {height:,}: {e}")
    else:
        if block == tip:
            rep.ok(f"the tip is Bitcoin's block {height:,} according to our archive node")
        else:
            rep.fail(f"the tip {tip[:16]}… is NOT Bitcoin's block {height:,}: our node says {block[:16]}…")
        proof_work = int(v.get("cumulative_work", -1))
        if proof_work == work:
            rep.ok(f"the proof's cumulative work equals the node's chainwork at that block ({work:,})")
        else:
            rep.fail(f"the proof's cumulative work {proof_work:,} is not the node's chainwork {work:,} at block "
                     f"{height:,}")

    age = time.time() - float(head.get("ts") or 0)
    if age <= a.stall_secs:
        rep.ok(f"the spine advanced {int(age // 60)} min ago")
    else:
        try:
            c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
            waiting = c.execute("SELECT id FROM vranges WHERE lo = ? LIMIT 1", (hi + 1,)).fetchone()
            c.close()
        except sqlite3.Error as e:
            rep.cannot_check(f"whether proofs are waiting above the spine ({a.db}): {e}")
        else:
            if not waiting:
                rep.ok(f"the spine last advanced {age / 3600:.1f} h ago, but nothing proven starts at block "
                       f"{hi + 1:,} yet")
            else:
                # ⛔ IS ANYTHING FEEDING IT? A spine that is behind while folds keep arriving is the
                # fold-then-absorb workflow, not a fault. A spine that is behind while NOTHING has
                # arrived either is the failure this check was written for -- and the two look
                # identical if you only ask whether the spine moved.
                fold_age = None
                try:
                    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
                    row = c.execute("SELECT MAX(ts) FROM vranges").fetchone()
                    c.close()
                    if row and row[0]:
                        fold_age = time.time() - float(row[0])
                except sqlite3.Error:
                    fold_age = None            # ⚠ unknown is not "folding" -- fall through to the fail
                if fold_age is not None and fold_age <= a.fold_idle_secs and age <= a.behind_max_secs:
                    rep.ok(f"the spine is {age / 3600:.1f} h behind (proof {waiting[0]} waits at block "
                           f"{hi + 1:,}), but folding is LIVE — newest range {fold_age / 60:.0f} min ago. "
                           f"Absorbing later is cheaper: a step costs the same at any width, so a "
                           f"collapsed tree buys more per step")
                elif fold_age is not None and fold_age <= a.fold_idle_secs:
                    rep.fail(f"the spine is {age / 3600:.1f} h behind — past the {a.behind_max_secs / 3600:.0f} h "
                             f"ceiling — while proof {waiting[0]} waits at block {hi + 1:,}. Folding is still "
                             f"live, so nothing is broken; the backlog just needs a spine pass")
                else:
                    seen = "no folded range has EVER been recorded" if fold_age is None \
                        else f"the newest folded range is {fold_age / 3600:.1f} h old"
                    rep.fail(f"the spine has not advanced for {age / 3600:.1f} h, while proof {waiting[0]} "
                             f"(starting at block {hi + 1:,}) is waiting to be absorbed — and {seen}, so "
                             f"nothing is feeding it either")
    return rep.finish()


if __name__ == "__main__":
    sys.exit(main())
