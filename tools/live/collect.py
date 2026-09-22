#!/usr/bin/env python3
"""Build snapshot.json: one live picture of the tip run, from pod telemetry + the coordinator.

INPUTS
  $RUNDIR/stream/<card>.csv   one line per second, written by tip-stream.sh over a persistent ssh:
                                epoch,util_pct,mem_mib,temp_c,power_w,sm_mhz,mem_mhz,phase,seg_n,seg_total,block
                              phase/seg come from the worker's own log, parsed with the worker's
                              regexes (dist/hazync-worker:636-640) so we cannot drift from it:
                                executed, N segments  |  segment n/N  |  assembling N segment receipts
  $RUNDIR/pods.txt            id name ip port cost gpu      (written by fleet.sh)
  api.hazync.org              chain facts (tip, frontier, proven, pct) — slim, 9.5 KB

OUTPUT  snapshot.json, rewritten atomically every tick. The renderer only ever reads this file.

⛔ Nothing here is synthesised except under --demo, which exists so the renderer can be tested with
no pods running. A card with no recent sample is reported up:false rather than given a plausible curve.
"""
import argparse, csv, json, math, os, re, time, urllib.request

API = os.environ.get("COORD_URL", "https://api.hazync.org")
WINDOW_S = 900          # rolling telemetry window kept per card (seconds)
STALE_S = 15            # no sample for this long -> the card is not "up"


def chain_facts():
    try:
        with urllib.request.urlopen(f"{API}/api/state?slim=1", timeout=10) as r:
            d = json.load(r)
        p = d.get("progress", {})
        return {"tip": p.get("tip"), "frontier": p.get("frontier"), "proven": p.get("proven"),
                "folded": p.get("folded"), "pct": p.get("pct"), "contributors": p.get("contributors"),
                "ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def read_phase(rundir):
    """What the run says it is doing, from `$RUNDIR/phase`. One short line, or None.

    ⛔ A LIVE FLEET DOING NOTHING LOOKS EXACTLY LIKE A DEAD FEED. Preparing a card means staging a
    fixture and pulling a 407 MB prover -- minutes of flat traces and an empty dial, indistinguishable
    on the frame from an idle fleet or a broken collector. That question was asked three times in one
    evening, and every time the answer was "it is preparing". The run writes what it is doing; the
    frame says it.
    """
    try:
        with open(os.path.join(rundir, "phase")) as fh:
            return (fh.read().strip() or None)
    except OSError:
        return None


def read_pods(rundir):
    """id name ip port cost gpu — cost is REAL money from RunPod, not an assumption."""
    out = {}
    p = os.path.join(rundir, "pods.txt")
    if not os.path.exists(p):
        return out
    for line in open(p):
        f = line.split()
        if len(f) >= 6:
            out[f[1]] = {"pod": f[0], "ip": f[2], "port": f[3],
                         "cost_hr": float(f[4]), "gpu": f[5].replace("_", " ")}
    return out


class StreamCursor:
    """Per-card incremental read state: how far we have counted, and what we counted.

    ⛔ WITHOUT THIS THE COLLECTOR FALLS BEHIND ITS OWN INTERVAL ON A LONG RUN. `readlines()`
    re-reads and re-parses the ENTIRE capture every tick, and the capture grows at 1 line/sec/card.
    Measured on 30 synthetic cards: 0.15 s at 1 hour, 0.38 s at 3 h, 0.73 s at 6 h, 1.43 s at 12 h,
    and 2.0-5.4 s (median 3.8 s over five reads) at 24 h -- against an --interval of 1.0 s. The
    dashboard does not break; it degrades to a frame every few seconds by the end of the day, with
    nothing on the frame to say so.

    The whole-file pass is a pure accumulator -- every row contributes once, independently and
    monotonically -- so it resumes from a byte offset. The trace pass is already bounded to the last
    WINDOW_S samples and is read from the END of the file instead.
    """

    __slots__ = ("offset", "ino", "size", "secs", "by_block", "peak", "peak_h")

    def __init__(self):
        self.offset, self.ino, self.size = 0, None, 0
        self.secs, self.by_block = 0, {}
        self.peak, self.peak_h = 0.0, None

    def reset(self):
        self.offset, self.ino, self.size = 0, None, 0
        self.secs, self.by_block = 0, {}
        self.peak, self.peak_h = 0.0, None


def _tail_lines(path, nbytes):
    """The last `nbytes` of a file as complete lines; a partial first line is dropped."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        back = min(nbytes, size)
        fh.seek(size - back)
        chunk = fh.read(back)
    lines = chunk.decode("utf8", "replace").splitlines(keepends=True)
    if back < size and lines:
        lines = lines[1:]            # cut mid-way by the seek
    return lines


def read_streams(rundir, now, cursors=None):
    """Read every card's stream.

    `cursors` is a dict name -> StreamCursor. Pass one and each file is read INCREMENTALLY from where
    the last tick stopped; omit it and every file is read whole, exactly as before -- which is what
    --once, --demo and --replay want, and what the equivalence test compares against.
    """
    cards = []
    sdir = os.path.join(rundir, "stream")
    if not os.path.isdir(sdir):
        return cards
    pods = read_pods(rundir)
    for fn in sorted(os.listdir(sdir)):
        if not fn.endswith(".csv"):
            continue
        name = fn[:-4]
        path = os.path.join(sdir, fn)
        t, w, u = [], [], []
        phase, seg_n, seg_total, block = "idle", 0, 0, None

        cur = None
        if cursors is not None:
            cur = cursors.get(name)
            if cur is None:
                cur = cursors[name] = StreamCursor()

        try:
            if cur is None:
                with open(path) as fh:
                    newrows = fh.readlines()
                secs, by_block = 0, {}
                peak, peak_h = 0.0, None
            else:
                st = os.stat(path)
                # ⛔ A SHRUNK OR REPLACED FILE MUST RESET THE CURSOR. tip-stream.sh truncates the
                # stream directory between sessions and a new session is a new inode. Seeking to a
                # stale offset in a fresh file would skip the start of the run and undercount the
                # money for the rest of it.
                if cur.ino is not None and (st.st_ino != cur.ino or st.st_size < cur.size):
                    cur.reset()
                cur.ino, cur.size = st.st_ino, st.st_size
                with open(path, "rb") as fh:
                    fh.seek(cur.offset)
                    blob = fh.read()
                # ⛔ NEVER CONSUME A PARTIAL LAST LINE. The stream is appended over a reconnecting
                # ssh, so a read landing mid-write is NORMAL, not exceptional. Both naive choices are
                # wrong and both are silent:
                #   advance past it  -> the fragment has too few fields and is skipped, and so does
                #                       its remainder on the next tick. The sample is LOST FOR EVER.
                #                       Verified: with this guard removed the count stays at 10 where
                #                       it should reach 11, and observed_s IS the money.
                #   do not advance   -> the completed line is read again and counted TWICE.
                # Advance to the last NEWLINE and leave the remainder for next time.
                cut = blob.rfind(b"\n") + 1
                cur.offset += cut
                newrows = blob[:cut].decode("utf8", "replace").splitlines(keepends=True)
                secs, by_block = cur.secs, cur.by_block
                peak, peak_h = cur.peak, cur.peak_h
        except OSError:
            continue

        # EVERY NEW ROW, for money: each 1 Hz line IS a second this card was observed running.
        # Counting samples rather than integrating a wall clock means the figure is correct on a
        # one-shot run, on a replay of a finished capture, and across a collector restart.
        for line in newrows:
            f = line.rstrip("\n").split(",")
            if len(f) < 11:
                continue
            secs += 1

            # ⛔ FINISHING MUST NOT LOOK LIKE NOT STARTING. The join tree's leaf dot is green when
            # seg_n/seg_total >= 1, but a card that has finished stops emitting `segment n/N`, so
            # seg_total falls back to 0, the ratio reads 0.0, and the dot reverts to grey -- the card
            # appears to un-finish the moment it succeeds. `peak` is the furthest this card got on the
            # height it is working, and it resets only when the card moves to a different block.
            if f[9] and f[10].strip().isdigit():
                try:
                    n_, tot_ = int(f[8] or 0), int(f[9] or 0)
                except ValueError:
                    n_ = tot_ = 0
                if tot_ > 0:
                    if f[10].strip() != peak_h:
                        peak, peak_h = 0.0, f[10].strip()
                    peak = max(peak, min(1.0, n_ / tot_))

            h = f[10].strip()
            if not h.isdigit():
                continue
            try:
                ts = float(f[0])
            except ValueError:
                continue
            # ⛔ AN IDLE SAMPLE MUST NOT OPEN A BLOCK. tip-stream falls back to
            # `ls /workspace/block_*.json` for the height, so every card reports the block from the
            # moment its FIXTURE IS STAGED -- during preparation, minutes before T0. blocks_from_cards
            # then drops the height as "began before the run" and the grid stays empty for the whole
            # run. Measured 2026-09-20 on block 741,000: the four cards first reported it 84-119 s
            # before t0, and the snapshot held ZERO blocks while all four proved it.
            #
            # A block is opened by the first sample that shows WORK on it. An idle sample still counts
            # toward `secs` above, because the card is rented whether or not it is busy.
            if f[7] == "idle" and h not in by_block:
                continue
            e = by_block.get(h)
            if e is None:
                e = by_block[h] = {"n": 0, "t0": ts, "t1": ts, "segs": 0, "prove": 0, "asm": 0}
            e["n"] += 1
            e["t0"] = min(e["t0"], ts); e["t1"] = max(e["t1"], ts)
            e["segs"] = max(e["segs"], int(f[9] or 0))
            if f[7] == "assembling":
                e["asm"] += 1
            elif f[7] in ("proving", "executed"):
                e["prove"] += 1
        if cur is not None:
            cur.secs, cur.by_block = secs, by_block
            cur.peak, cur.peak_h = peak, peak_h

        # WINDOW, for the traces: only what the waveform draws. Read from the END of the file rather
        # than slicing the whole capture -- at 24 h that slice was the thing being paid for.
        # ~70 bytes per line with a wide margin, so WINDOW_S lines are always covered.
        try:
            window = _tail_lines(path, WINDOW_S * 160)
        except OSError:
            window = []
        for line in window[-WINDOW_S:]:
            f = line.rstrip("\n").split(",")
            if len(f) < 11:
                continue
            try:
                ts = float(f[0])
            except ValueError:
                continue
            if now - ts > WINDOW_S:
                continue
            t.append(round(ts, 1)); u.append(int(float(f[1] or 0))); w.append(float(f[4] or 0))
            phase = f[7] or phase
            seg_n = int(f[8] or 0); seg_total = int(f[9] or 0)
            block = int(f[10]) if f[10].strip().isdigit() else block
        meta = pods.get(name, {})
        rate = meta.get("cost_hr", 0.0)
        cards.append({"name": name, "gpu": meta.get("gpu", "?"), "cost_hr": rate,
                      "up": bool(t) and (now - t[-1]) < STALE_S,
                      "t": t, "w": w, "u": u,
                      "phase": phase, "seg_n": seg_n, "seg_total": seg_total, "block": block,
                      "observed_s": secs, "spend_usd": round(secs * rate / 3600.0, 4),
                      "peak": round(peak, 4),
                      "block_s": by_block})
    return cards


RE_VERIFIED = re.compile(r"VERIFIED\s+block\s+(\d+)")


def verified_heights(phase_text):
    """Heights the RUN ITSELF has declared verified, from its own phase line.

    ⛔ WHY THIS EXISTS. `done` was inferred from five seconds of silence -- "nothing has reported
    this height for 5 s". The collector stops when the run ends, BEFORE its own grace period
    elapses, so the LAST block of every run never counted. On a single-block run the headline read
    "0 blocks" beside a banner reading "VERIFIED block 741000 in 267.9s" (captured 2026-09-21).

    On a 24-hour run that is an off-by-one at the exact moment someone reads the final number, and
    it is the headline figure.

    The run already writes the authoritative answer to $RUNDIR/phase. Silence stays as a FALLBACK,
    for a height that has scrolled out of the phase line -- it is not wrong, it is just late.
    """
    out = set()
    for m in RE_VERIFIED.finditer(phase_text or ""):
        out.add(int(m.group(1)))
    return out


def blocks_from_cards(cards, state, now, verified=()):
    """One entry per height the fleet worked while we were watching, from the FULL stream history.

    ⛔ It used to record only each card's CURRENT height, so a capture holding dozens of blocks
    produced three entries and the chart had three bars. Every sample carries its height, so the
    history is already on disk; this reads it.

    ⚠ prove_s/fold_s are card-seconds divided by the number of cards seen on that height. With one
    card per block (board mode) that IS wall time. With N cards sharing a block (the tip run) it is
    an average, not a measured wall span — do not quote it as one.
    """
    agg = {}
    since = state.get("since", 0.0)      # $RUNDIR/t0, when a run declares its own start
    for c in cards:
        rate = c.get("cost_hr", 0.0)
        for h, e in (c.get("block_s") or {}).items():
            # ⛔ Ignore heights whose samples all predate T0. A milestone run inherits the board
            # worker's last blocks in the logs, and they were being counted as completed blocks —
            # that is where "3 blocks" came from, not from the chunks.
            # Drop heights that BEGAN before the run: one stale board sample landing just after T0
            # was enough to keep block 75807 alive as a "completed block" when the filter tested t1.
            if since and e["t0"] < since:
                continue
            a = agg.setdefault(h, {"n": 0, "t0": e["t0"], "t1": e["t1"], "segs": 0,
                                   "prove": 0, "asm": 0, "cards": set(), "cost": 0.0})
            a["n"] += e["n"]
            a["t0"] = min(a["t0"], e["t0"]); a["t1"] = max(a["t1"], e["t1"])
            a["segs"] = max(a["segs"], e["segs"])
            a["prove"] += e["prove"]; a["asm"] += e["asm"]
            a["cards"].add(c["name"])
            a["cost"] += e["n"] * rate / 3600.0
    out = []
    for h, a in agg.items():
        n_cards = max(1, len(a["cards"]))
        out.append({"h": int(h), "arrive": a["t0"],
                    # ⛔ `done` is a BOOLEAN and the pulse needs a TIME. The renderer animates a
                    # block travelling from the join tree to its cell for PULSE seconds after it
                    # finished, so it has to know WHEN that was -- `(now - t1) > 5` cannot say.
                    # t1 is the last moment any card reported this height, which is that instant.
                    "done_at": a["t1"],
                    "prove_s": round(a["prove"] / n_cards, 1) or None,
                    "fold_s": round(a["asm"] / n_cards, 1) or None,
                    "segs": a["segs"], "cards": len(a["cards"]),
                    # Authoritative first: the run said VERIFIED. Silence is the fallback.
                      "done": int(h) in verified or (now - a["t1"]) > 5,
                    "cost": round(a["cost"], 4)})
    out.sort(key=lambda x: x["h"])
    return out


def newest_sample(rundir):
    """Epoch of the most recent sample on disk, or 0. Used by --replay so a FINISHED capture renders
    as it looked live: wall-clock staleness would otherwise mark every card down."""
    best, sdir = 0.0, os.path.join(rundir, "stream")
    if not os.path.isdir(sdir):
        return best
    for fn in os.listdir(sdir):
        if not fn.endswith(".csv"):
            continue
        try:
            lines = open(os.path.join(sdir, fn)).readlines()[-5:]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                best = max(best, float(line.split(",")[0]))
                break
            except (ValueError, IndexError):
                continue
    return best


def demo(now, at=0.62, ncards=30):
    """A faithful TIP-RUN preview: N cards on ONE block, a new block every ~10 min, 144 in a day.

    The old demo modelled BOARD mode — 30 cards each on a different block — so the frame it produced
    was not a preview of the thing we intend to stream. This models the tip shape, and its numbers
    come from tonight's real 3-card capture rather than invention:
        power 28..296 W · util 0..100 · ~2 s per segment on a 4090
        prove:fold = 1040:359 s measured, so fold ≈ 0.35 x prove
    `at` is the fraction of the 24-hour day to render, so any moment can be reviewed.
    ⛔ Still synthetic — every frame drawn from it is stamped DEMO.
    """
    NB, PERIOD = 144, 600.0                       # blocks in a day, seconds between blocks
    day_t = max(0.0, min(1.0, at)) * NB * PERIOD
    done_n = int(day_t // PERIOD)                 # blocks finished before the one in flight
    since = day_t - done_n * PERIOD               # seconds into the current block
    h0 = 967_200

    def shape(k):
        """(total segments, prove seconds, fold seconds) for block k — varied but plausible."""
        segs = 1700 + (k * 53) % 900
        per_card = max(8, segs // max(1, ncards))
        pv = per_card * 2.0
        return segs, pv, pv * 0.35

    segs, prove_s, fold_s = shape(done_n)
    per_card = max(8, segs // max(1, ncards))

    def phase_at(el, seed):
        """Phase and per-card progress `el` seconds into the block. Cards finish at slightly
        different times — the straggler spread measured tonight was 2.48 vs 1.96 s/segment."""
        p = prove_s * (0.88 + 0.30 * seed)        # this card's own prove time
        if el < 0:
            return "idle", 0
        if el < p:
            return "proving", max(1, int(per_card * el / p))
        if el < p + fold_s:
            return "assembling", per_card
        return "idle", per_card

    cards = []
    for i in range(ncards):
        seed = ((i * 37) % 100) / 100.0
        t, w, u = [], [], []
        for s in range(300):                      # last 300 s, phase-aware so transitions show
            el = since - (300 - s)
            ph, _ = phase_at(el, seed)
            if ph == "proving":
                base, spread = 265.0, 30.0
            elif ph == "assembling":
                base, spread = 165.0, 45.0
            else:
                base, spread = 40.0, 12.0
            t.append(now - 300 + s)
            w.append(max(28.0, min(296.0, base + spread * math.sin(s / 6.0 + i * 1.7)
                                   + 0.35 * spread * math.sin(s / 2.1 + i))))
            u.append(0 if ph == "idle" else min(100, max(20, int(92 + 8 * math.sin(s / 5.0 + i)))))
        ph, n = phase_at(since, seed)
        cards.append({"name": f"hz-tip-{i}", "gpu": "NVIDIA GeForce RTX 4090", "cost_hr": 0.34,
                      "up": True, "t": t, "w": w, "u": u, "phase": ph,
                      "seg_n": n, "seg_total": per_card, "block": h0 + done_n})

    rate = ncards * 0.34
    blocks = []
    for k in range(done_n):
        s_k, pv_k, fd_k = shape(k)
        blocks.append({"h": h0 + k, "arrive": now - (done_n - k) * PERIOD - since,
                       "prove_s": round(pv_k, 1), "fold_s": round(fd_k, 1),
                       "segs": s_k, "cards": ncards, "done": True,
                       "cost": round(rate * (pv_k + fd_k) / 3600.0, 4)})
    blocks.append({"h": h0 + done_n, "arrive": now - since, "prove_s": None, "fold_s": None,
                   "segs": segs, "cards": ncards, "done": False,
                   "cost": round(rate * since / 3600.0, 4)})
    return cards, blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rundir", default=os.environ.get("HAZYNC_RUNDIR", "."))
    ap.add_argument("--out", default="snapshot.json")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--once", action="store_true")
    # Looping is the DEFAULT, but README.txt and dash.html both say `--loop`; without this the
    # documented command dies on "unrecognized arguments" and the renderer silently keeps drawing
    # whatever stale snapshot is lying next to it.
    ap.add_argument("--loop", action="store_true", help="run continuously (the default)")
    ap.add_argument("--demo", action="store_true", help="synthesise a snapshot; no pods needed")
    ap.add_argument("--at", type=float, default=0.62,
                    help="demo only: fraction (0..1) of the simulated 24-hour day to render")
    ap.add_argument("--cards", type=int, default=30, help="demo only: fleet size")
    ap.add_argument("--replay", action="store_true",
                    help="anchor 'now' to the newest sample, so a finished capture renders as live")
    a = ap.parse_args()
    state = {}
    # One cursor per card, carried across ticks. A fresh cursor reads the file whole, so --once and
    # the first tick of --loop are unchanged; every later tick reads only what has been appended.
    cursors = {}
    while True:
        now = time.time()
        if a.replay and not a.demo:
            ns = newest_sample(a.rundir)
            if ns:
                now = ns + 0.5          # just after the last sample: the fleet reads as live
        if a.demo:
            cards, blocks = demo(now, a.at, a.cards)
            phase_label = None
            # the simulated chain tip IS the block in flight, or the grid mislabels its own window
            # as BACKFILL once the simulated day runs past a hardcoded height
            tip = blocks[-1]["h"]
            chain = {"tip": tip, "frontier": 74927, "proven": 75443, "folded": 62306,
                     "pct": 7.75, "contributors": 7, "ok": True}
        else:
            cards = read_streams(a.rundir, now, cursors)
            phase_label = read_phase(a.rundir)
            chain = chain_facts()
            t0f = os.path.join(a.rundir, "t0")          # written by mile3.sh at T0
            if os.path.exists(t0f) and "since" not in state:
                try:
                    state["since"] = float(open(t0f).read().strip())
                except (ValueError, OSError):
                    state["since"] = 0.0
            blocks = blocks_from_cards(cards, state, now, verified_heights(phase_label))
        up = [c for c in cards if c["up"]]
        rate = sum(c["cost_hr"] for c in up)
        # Money is DERIVED FROM THE SAMPLES, not from a wall clock. The renderer used to sum each
        # block's prove_s+fold_s, which are only set on an OBSERVED phase transition — so they were
        # almost always None and the header read $0 while real cards billed by the second. A tick
        # integrator would have been no better here: --replay pins `now` to the newest sample, so
        # every dt is zero and the total would stay $0 in exactly the mode used to verify it.
        if not a.demo:
            spend_total = sum(c.get("spend_usd", 0.0) for c in cards)   # per-block cost is set in
            pass                                                         # blocks_from_cards now
        else:
            spend_total = sum((b.get("cost") or 0) for b in blocks)
        snap = {"t": now, "demo": bool(a.demo), "phase": phase_label,
                "chain": chain, "cards": cards, "blocks": blocks,
                "fleet": {"cards": len(cards), "up": len(up), "cost_hr": round(rate, 2),
                          "spend_usd": round(spend_total, 4)}}
        tmp = a.out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snap, fh, separators=(",", ":"))
        os.replace(tmp, a.out)          # atomic: the renderer never sees a half-written file
        print(f"[{time.strftime('%H:%M:%S')}] {len(up)}/{len(cards)} up · "
              f"{len(blocks)} blocks · ${rate:.2f}/hr · spent ${spend_total:.4f} · "
              f"chain {'ok' if chain.get('ok') else chain.get('error')}",
              flush=True)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
