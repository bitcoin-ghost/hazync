#!/usr/bin/env python3
"""The spine absorbs the WIDEST verified chunk at each step, not one block.

`extend-spine` folds `[1..N] + [N+1..M]` at the same cost for any M, but `cmd_spine` fetched
`/api/proof/{N+1}` -- one block -- every step. So the one serial job ran at one fold per block while
the fold tree it could have swallowed whole sat beside it: ~92 blocks/hr on a shared card, ~230/hr on
its own, against ~2,500/hr proven (measured on the live board, 2026-09-11).

Loads the real CLI and stubs only the network and the GPU. A fake coordinator holds the spine head,
the `/api/vranges` list and the receipts; a fake `extend-spine` enforces adjacency exactly as the host
does (the chunk must start at head+1) and writes the extended head. What is checked is WHICH chunks the
spine absorbs, that a chunk which will not absorb falls back to a narrower one instead of stalling, and
that the old one-block path still works when the range list is unavailable.

NOT covered: real folding and real STARK verification (GPU; see test_spine_fold.py's docstring).

  python3 test_spine_ranges.py            # must PASS (exit 0)
  python3 test_spine_ranges.py --control  # restores one block per step; failures MUST be detected (exit 0)
"""
import base64
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_spine_")
CONTROL = "--control" in sys.argv

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)


class _Sk:
    def sign(self, m):
        return b"\x00" * 64


hz.HOSTBIN = sys.executable                     # any existing file satisfies the path checks
hz.RCPTDIR = hz.pathlib.Path(tempfile.mkdtemp(prefix="hz_rcpt_"))
hz.identity = lambda: (_Sk(), "ab" * 32, "tester")
hz.gpu_lock = lambda what="prove": contextlib.nullcontext()


def rid(lo, hi):
    return str(lo) if lo == hi else f"{lo}-{hi}"


def tree(lo, hi, maxw=1 << 20):
    """Every node of the canonical fold tree inside [lo..hi] -- aligned, power-of-two widths."""
    out, w = [], 2
    while w <= maxw:
        for a in range(lo, hi + 1):
            if (a - 1) % w == 0 and a + w - 1 <= hi:
                out.append((a, a + w - 1))
        w *= 2
    return out


class World:
    def __init__(self, head, ranges, singles, bad=(), reject=(), vranges_down=False):
        self.head = head
        self.served = set(ranges) | {(b, b) for b in singles}
        self.bad = set(bad)             # chunks the host refuses (a seam that does not match)
        self.reject = set(reject)       # chunks whose extended head the coordinator refuses
        self.vranges_down = vranges_down
        self.fed = []                   # every chunk handed to extend-spine, in order
        self.steps = []                 # every chunk that actually advanced the head


W = None


def _404(path):
    return hz.urllib.error.HTTPError(path, 404, "not found", {}, None)


def fake_get(path):
    if path == "/api/spine":
        if not W.head:
            raise _404(path)
        return json.dumps({"lo": 1, "hi": W.head}).encode()
    if path == "/api/spine/proof":
        return f"R 1 {W.head}".encode()
    if path == "/api/vranges":
        if W.vranges_down:
            raise _404(path)
        return json.dumps({"vranges": [{"lo": lo, "hi": hi, "handle": "t", "proof": f"/api/proof/{rid(lo, hi)}"}
                                       for lo, hi in sorted(W.served)]}).encode()
    if path.startswith("/api/proof/"):
        parts = [int(x) for x in path[len("/api/proof/"):].split("-")]
        lo, hi = parts[0], parts[-1]
        if (lo, hi) not in W.served:
            raise _404(path)
        return f"R {lo} {hi}".encode()
    raise AssertionError(f"unexpected GET {path}")


def fake_post(path, body):
    assert path == "/api/spine", f"unexpected POST {path}"
    _, lo, hi = base64.b64decode(body["receipt"]).decode().split()
    lo, hi = int(lo), int(hi)
    assert lo == 1, "a spine head must start at block 1"
    chunk = (W.head + 1, hi)
    if chunk in W.reject:
        return {"error": "spine rejected — test"}
    if hi <= W.head:
        return {"error": f"spine already at [1..{W.head}]; submitted [1..{hi}] does not advance it"}
    W.head = hi
    W.steps.append(chunk)
    return {"ok": True}


def fake_run(argv, capture_output=True, text=True):
    assert argv[1] == "extend-spine", argv
    _, one, n = open(argv[2]).read().split()
    _, lo, hi = open(argv[3]).read().split()
    n, lo, hi = int(n), int(lo), int(hi)
    W.fed.append((lo, hi))
    if lo != n + 1:
        return subprocess.CompletedProcess(argv, 101, "", f"chunk is not adjacent: spine ends at {n}")
    if (lo, hi) in W.bad:
        return subprocess.CompletedProcess(argv, 101, "", "seam: chunk in_roots != spine out_roots (normalized)")
    with open(argv[4], "w") as f:
        f.write(f"R 1 {hi}")
    return subprocess.CompletedProcess(argv, 0, "ok", "")


hz.get, hz.post = fake_get, fake_post
hz.subprocess = types.SimpleNamespace(run=fake_run)

if CONTROL:
    hz.spine_chunks = lambda index, nxt: [(nxt, f"/api/proof/{nxt}")]     # the old one-block path
    print("CONTROL: one block per step restored -- the width checks below MUST fail")

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


def spine(world, args=()):
    global W
    W = world
    try:
        hz.cmd_spine(list(args))
    except SystemExit as e:
        return f"SystemExit: {e}"
    return None


# The shape of the live board on 2026-09-11: the spine at 3,322, the fold tree complete to 3,424,
# single blocks proven a little further.
TREE = tree(3321, 3424)
SINGLES = range(1, 3431)
WIDE = [(3323, 3324), (3325, 3328), (3329, 3392), (3393, 3424)] + [(b, b) for b in range(3425, 3431)]

print("widest chunk first")
w = World(3322, TREE, SINGLES)
err = spine(w)
check(err is None, f"the run ends without an error ({err})")
check(w.head == 3430, f"the spine reaches the last proven block, 3430 (got {w.head})")
check(w.steps == WIDE, f"each step absorbs the widest chunk at head+1: 10 steps, not 108 (got {len(w.steps)})")
check(w.fed == WIDE, "no chunk is tried and thrown away when every chunk absorbs")

print("a wide chunk that will not absorb")
w = World(3322, TREE, SINGLES, bad={(3329, 3392)})
err = spine(w)
check(err is None, f"a chunk the host refuses does not stop the spine ({err})")
check(w.head == 3430, f"the spine still reaches 3430 (got {w.head})")
check((3329, 3360) in w.steps and w.fed.count((3329, 3392)) == 1,
      "it falls back to the next-widest chunk, [3329..3360], after ONE failed attempt")

print("a wide head the coordinator refuses")
w = World(3322, TREE, SINGLES, reject={(3393, 3424)})
err = spine(w)
check(err is None and w.head == 3430, f"a refused head falls back rather than ending the run (head {w.head}, {err})")
check((3393, 3408) in w.steps, "the next-widest chunk, [3393..3408], is what advanced it")

print("no range list: the old path")
w = World(3322, TREE, SINGLES, vranges_down=True)
err = spine(w)
check(err is None and w.head == 3430, f"without /api/vranges it absorbs single blocks to 3430 (head {w.head}, {err})")
check(len(w.steps) == 108 and all(lo == hi for lo, hi in w.steps), "one block per step, exactly as before")

print("`hazync spine n` counts steps")
w = World(3322, TREE, SINGLES)
spine(w, ["2"])
check(w.steps == WIDE[:2] and w.head == 3328, f"`spine 2` takes two steps, to 3328 (got {w.head})")

print("bootstrap")
w = World(0, tree(1, 16), range(1, 17))
err = spine(w)
check(err is None, f"a board with no spine yet is seeded without an error ({err})")
check(w.steps == [(1, 1), (2, 2), (3, 4), (5, 8), (9, 16)],
      f"seeded with block 1, then the tree climbs 2, [3..4], [5..8], [9..16] (got {w.steps})")

print("the range index trusts nothing it cannot fetch")
idx = hz.spine_index([
    {"lo": 5, "hi": 8, "proof": "/api/proof/5-8"},
    {"lo": 5, "hi": 6, "proof": "/api/proof/5-6"},
    {"lo": 5, "hi": 5, "proof": "/api/proof/5"},             # singles are the fallback's job
    {"lo": 5, "hi": 12, "proof": "https://elsewhere/x"},       # not a coordinator proof path
    {"lo": 5, "hi": 16},                                       # no receipt served
    {"lo": 9, "hi": 7, "proof": "/api/proof/9-7"},             # empty range
    {"lo": "x", "hi": 3, "proof": "/api/proof/x-3"},           # not a number
    "junk",
])
check(idx == {5: [(8, "/api/proof/5-8"), (6, "/api/proof/5-6")]},
      f"only served multi-block ranges are indexed, widest first (got {idx})")
check(hz.spine_chunks(idx, 5)[-1] == (5, "/api/proof/5") and hz.spine_chunks({}, 7) == [(7, "/api/proof/7")],
      "the single block is always offered, and always last")

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    if fails:
        print("CONTROL OK -- one block per step was restored and the width checks caught it.")
        sys.exit(0)
    print("CONTROL FAILED -- one block per step was restored and every check still passed.")
    sys.exit(1)
sys.exit(1 if fails else 0)
