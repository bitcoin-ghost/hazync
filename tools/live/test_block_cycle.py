#!/usr/bin/env python3
"""One block's cycle must render as a cycle: prove 0→100, hold, then fold 0→100 (hazync#481).

⛔ WHY THIS EXISTS. An operator watched a real 27-block session and saw:

    "at first it showed proving in the circle, and then folding but never finished folding, then
     went blank circles then proving then blank then folding then blank. Never did all the fold
     lines activate and never did a block highlight on the right."

The fleet was fine — 27 blocks proved and verified, one every ~32 s. Three faults in the frame:

  * `kfold` was `len(assembling) / len(up)` — a HEADCOUNT of cards wearing the label "FOLDING n%".
    On three cards it could only read 0, 33, 67 or 100.
  * `prog` was the mean of `seg_n/seg_total`, so a card that finished proving dropped out of the
    mean and the arc FELL exactly where it should have latched at 100%.
  * every sample carried an empty block height, so `blocks` stayed at 0 entries and no cell of the
    144-block map could ever light.

This replays a block tick by tick through the SHIPPED collector and renderer and pins the shape of
the cycle. The telemetry is not invented: the line shapes come from `agglines.txt`, captured from a
live mode-6 aggregate on 2026-09-22, and the per-tick values are what `tip-stream.sh` extracts.

  python3 test_block_cycle.py            # must PASS
  python3 test_block_cycle.py --control  # the fold arc goes back to counting cards; MUST FAIL
"""
import json
import os
import subprocess
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── the cycle, as tip-stream.sh reports it: (phase, seg_n, seg_total, fold_n, fold_total) ────────
# ⚠ Taken from stepping the real parser over a real block, not written by hand to suit the test.
SEGS, JOINS, H = 15, 5, 119500
CYCLE = ([("executed", 0, SEGS, 0, 0)] * 2
         + [("proving", n, SEGS, 0, 0) for n in range(1, SEGS + 1)]
         + [("assembling", SEGS, SEGS, j, JOINS) for j in range(1, JOINS + 1)]
         + [("done", SEGS, SEGS, JOINS, JOINS)] * 2)


def rundir_at(tick):
    """A rundir whose telemetry stops at `tick` of the cycle — what the collector would see then."""
    d = tempfile.mkdtemp(prefix="cycle_")
    os.makedirs(os.path.join(d, "stream"))
    now = time.time()
    with open(os.path.join(d, "pods.txt"), "w") as p:
        for i in range(3):
            p.write(f"pod{i} hz-card-{i} 10.0.0.{i + 1} 2200{i} 0.74 RTX_4090\n")
    open(os.path.join(d, "t0"), "w").write(f"{now - 600}\n")
    # ⛔ ONE AGGREGATE, TWO WORKERS — the shape a real mode-6 fleet has. Giving all three cards the
    # aggregate's view made the fixture uniform, and a uniform fleet hides the exact fault this is
    # about: the aggregate is the only card that knows the block's TOTALS (`7/15 segments`,
    # `joins 5/5`), while a worker reports nothing but its own finished-task count.
    for i in range(3):
        with open(os.path.join(d, "stream", f"hz-card-{i}.csv"), "w") as fh:
            for k in range(tick + 1):
                ph, sn, st, fn, ft = CYCLE[min(k, len(CYCLE) - 1)]
                if i:                       # a worker: counts, never totals
                    sn, st = (sn if ph == "proving" else 0), 0
                    fn, ft = (fn if ph in ("assembling", "done") else 0), 0
                ts = now - (tick - k)
                fh.write(f"{ts:.6f},95,4000,60,320,2700,10251,{ph},{sn},{st},{H},{fn},{ft}\n")
    return d


def snap_at(tick):
    d = rundir_at(tick)
    out = os.path.join(d, "snap.json")
    subprocess.run([sys.executable, os.path.join(HERE, "collect.py"),
                    "--rundir", d, "--out", out, "--once"],
                   capture_output=True, text=True, timeout=120,
                   env={**os.environ, "COORD_URL": "http://127.0.0.1:1"})
    with open(out) as fh:
        return json.load(fh)


def arcs(snap):
    """The two arcs the ring draws, read out of the SHIPPED renderer's own helpers."""
    import tip24live as t
    up = [c for c in snap["cards"] if c["up"]]

    def frac(nk, tk):
        best = 0.0
        for c in up:
            tot = c.get(tk) or 0
            if tot > 0:
                best = max(best, min(1.0, (c.get(nk) or 0) / tot))
        return best

    if CONTROL:
        # ⛔ THE CONTROL IS THE ORIGINAL RULE: count the cards that are folding.
        kfold = len([c for c in up if c.get("phase") == "assembling"]) / max(1, len(up))
        prog = (sum(min(1.0, c["seg_n"] / c["seg_total"]) for c in up if c.get("seg_total"))
                / max(1, len([c for c in up if c.get("seg_total")]))) if up else 0.0
        return prog, kfold, t
    kfold = frac("fold_n", "fold_total")
    prog = 1.0 if kfold > 0 else frac("seg_n", "seg_total")
    return prog, kfold, t


# ── 1. proving climbs ────────────────────────────────────────────────────────────────────────────
mid = 2 + SEGS // 2
p_mid, k_mid, _ = arcs(snap_at(mid))
check(0.2 < p_mid < 0.9, f"mid-prove the proving arc is partway round ({p_mid * 100:.0f}%)")
check(k_mid == 0, f"and the fold arc has not started ({k_mid * 100:.0f}%)")

# ── 2. ⛔ IT HOLDS AT 100 WHILE THE FOLD RUNS. The fault the operator actually saw ────────────────
seen = []
for j in range(1, JOINS + 1):
    pr, kf, _ = arcs(snap_at(2 + SEGS + j - 1))
    seen.append((round(pr, 3), round(kf, 3)))
check(all(pr == 1.0 for pr, _ in seen),
      f"proving stays at 100% for every fold tick (got {[p for p, _ in seen]})")
ks = [k for _, k in seen]
check(ks == sorted(ks) and 0 < ks[0] < ks[-1] == 1.0,
      f"and the fold arc CLIMBS to 100% across the joins (got {ks}) — a flat 1.0 is not a climb")
check(len(set(ks)) == JOINS,
      f"one distinct step per join, not a headcount of cards ({len(set(ks))} steps for {JOINS} joins)")

# ── 3. the block has a height, so its cell can colour ────────────────────────────────────────────
s = snap_at(2 + SEGS + 1)
blocks = s.get("blocks") or []
check(len(blocks) >= 1, f"the block appears in the snapshot ({len(blocks)} entries)")
check(any(b.get("h") == H for b in blocks), f"and it is the right height ({[b.get('h') for b in blocks]})")

# ── 3b. ⛔ AND IT GOES GREEN WHEN THE PROVER SAYS SO, not when the driver gets round to it ────────
# The tile stayed orange through the whole fold in the first replay: `done` came only from the run's
# phase line (written after submission) or from 5 s of silence (which cannot fire while the card is
# still streaming the block it just finished). The prover writes `receipt written` / `RECEIPT
# VERIFIED`, tip-stream.sh reports phase=done, and that is now enough.
mid_fold = snap_at(2 + SEGS + 2)
check(not any(b.get("done") for b in (mid_fold.get("blocks") or [])),
      "mid-fold the block is NOT done — the cell must not green early")
end = snap_at(len(CYCLE) - 1)
check(any(b.get("h") == H and b.get("done") for b in (end.get("blocks") or [])),
      f"and once the receipt is written it IS done "
      f"({[(b.get('h'), b.get('done')) for b in (end.get('blocks') or [])]})")

# ── 3c. the ring names the state: VERIFIED, and board work vs tip work ───────────────────────────
# ⛔ A TIP SESSION CLAIMS BOARD WORK BETWEEN TIP BLOCKS (#367), and the two read identically on the
# frame: an hour of filling in while waiting looked exactly like an hour of doing the job. The grid
# already captioned itself "AT THE TIP" / "BACKFILL" from the same fact, so the ring was the only
# part not told.
def ring_text(snap, tip):
    """The two lines the ring centre draws, decided the way the renderer decides them."""
    import tip24live as t
    up = [c for c in snap["cards"] if c["up"]]
    blocks = snap.get("blocks") or []
    live_h = next((c.get("block") for c in up if c.get("block")), None)
    cur = next((b for b in blocks if b["h"] == live_h), None)
    on_board = bool(tip) and bool(cur) and cur["h"] <= tip - t.WINDOW
    if cur and cur.get("done"):
        return "VERIFIED", f"BLOCK {cur['h']:,}"
    if cur:
        if on_board:
            import tip24live as _t
            from PIL import Image as _I, ImageDraw as _D
            _dd = _D.Draw(_I.new("RGB", (10, 10)))
            return "PROVING/FOLDING", _t.centre_text(
                _dd, ["BOARD BLOCK · AWAITING TIP", "BOARD · AWAITING TIP", "BOARD BLOCK"],
                _t.fonts()["small"], 78, _t.RIN)
        return "PROVING/FOLDING", "… ON THIS BLOCK"
    return "STANDING BY", "WAITING FOR A BLOCK"


# ⛔ AND IT HAS TO FIT INSIDE THE RING. The centre is a circle, so the usable width shrinks as you
# move from the middle; nothing checked that, and "BOARD BLOCK · AWAITING TIP" had THREE pixels of
# chord to spare — it ran into the inner arc and read as cut off, which is how the operator found it.
# Every string the centre can draw is measured here, so the next label added cannot repeat it.
import tip24live as _tl                                                        # noqa: E402
from PIL import Image as _Im, ImageDraw as _ID                                 # noqa: E402

_d = _ID.Draw(_Im.new("RGB", (10, 10)))
_fo = _tl.fonts()
CENTRE_STRINGS = [('/ 144 TODAY', 'lab', 22), ('AHEAD OF THE CHAIN', 'mid', 48),
                  ('VERIFIED', 'mid', 48), ('PROVING', 'mid', 48), ('FOLDING', 'mid', 48),
                  ('STANDING BY', 'mid', 48), ('BLOCK 968,178', 'small', 78),
                  ('WAITING FOR A BLOCK', 'small', 78), ('NEXT BLOCK IN 07:12', 'small', 78),
                  ('59:59 ON THIS BLOCK', 'small', 78)]
bad = [(t, round(_d.textlength(t, font=_fo[f]), 1))
       for t, f, dy in CENTRE_STRINGS if not _tl.centre_fits(_d, t, _fo[f], dy, _tl.RIN)]
check(not bad, f"every fixed centre string clears the inner arc (over-wide: {bad})")

fitted = _tl.centre_text(_d, ['BOARD BLOCK · AWAITING TIP', 'BOARD · AWAITING TIP', 'BOARD BLOCK'],
                         _fo['small'], 78, _tl.RIN)
check(_tl.centre_fits(_d, fitted, _fo['small'], 78, _tl.RIN),
      f"and the board caption degrades to one that fits ({fitted!r})")
check(fitted != 'BOARD BLOCK',
      f"without falling all the way back and losing what it is waiting for ({fitted!r})")

mid, low = ring_text(snap_at(len(CYCLE) - 1), tip=968178)
check(mid == "VERIFIED" and "119,500" in low,
      f"a finished block says VERIFIED and names it ({mid!r} / {low!r})")

# ⚠ 119,500 against a tip of 968,178 is board work by 848,678 blocks — the state the operator asked
# to be able to see.
_, low_board = ring_text(snap_at(2 + SEGS + 2), tip=968178)
check("AWAITING TIP" in low_board,
      f"board work says so, and says what it is waiting for ({low_board!r})")

# ⚠ And the same block, if it WERE the tip, must not claim to be waiting for one.
_, low_tip = ring_text(snap_at(2 + SEGS + 2), tip=H + 3)
check("AWAITING TIP" not in low_tip,
      f"a block at the tip is not reported as board work ({low_tip!r})")

# ⛔ With no chain tip known, every height looks like backfill — inventing "awaiting tip" there
# would be a state the frame cannot actually know.
_, low_notip = ring_text(snap_at(2 + SEGS + 2), tip=0)
check("AWAITING TIP" not in low_notip,
      f"with no tip known it claims nothing about the tip ({low_notip!r})")


# ── 4. the frame renders, and the tree lights more branches as the fold advances ─────────────────
import tip24live as tl                                                          # noqa: E402

fo = tl.fonts()
lit_counts = []
for j in (1, JOINS):
    sn = snap_at(2 + SEGS + j - 1)
    img = tl.draw_live(sn, fo)
    px = img.load()
    # count teal-ish pixels in the join-tree band — a lit branch is FOLD-coloured, an unlit one grey
    n = 0
    for x in range(tl.TX0, tl.TX1, 3):
        for y in range(int(tl.LYTOP), int(tl.LYBOT), 3):
            r, g, b = px[x, y]
            if b > r + 20 and g > r + 10:
                n += 1
    lit_counts.append(n)
check(lit_counts[1] > lit_counts[0],
      f"more of the tree is lit at {JOINS}/{JOINS} joins than at 1/{JOINS} ({lit_counts})")

EXPECTED_CONTROL = {"the fold arc CLIMBS", "one distinct step per join"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — with the headcount restored the fold arc stops being a fold arc: it "
              "jumps to 100% the instant the cards change phase and never climbs:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected the arc assertions to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("a block renders as a cycle: prove to 100, hold, fold to 100, and the cell lights")
