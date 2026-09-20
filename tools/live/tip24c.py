#!/usr/bin/env python3
"""24 hours at the tip — traces, fold, pulse, grid, ring.

⛔ MOCK. Synthetic data; the run has not happened.

The frame, left to right:
  * POD LANES — one horizontal trace per card, growing towards the green 100% line. A dark lane is a
    card retired or idle; the gaps are the fleet churn.
  * THE 100% LINE — where a card's work on this block is done.
  * THE FOLD — the finished lanes converge into one block token (the join tree, in one gesture).
  * THE PULSE — the token travels to its cell in the grid of all 144 blocks, which flips red to green.
  * THE RING, dead centre — orange outer arc is the 24-hour clock, green inner arc is completion,
    the count sits in the middle. Cost per block and running total underneath.

When the run happens, synth() is replaced by the evidence bundle: block claim/verify timestamps and
segment counts from the coordinator, fold timestamps from `submissions`, and per-pod boot/retire
times plus 1 Hz gpu_samples.csv from the controller (neither of the last two is recorded today).

    python3 tip24c.py --still 0.4
    python3 tip24c.py --frames --seconds 60 --fps 24
"""
import argparse, bisect, math, os, random
from PIL import Image, ImageDraw, ImageFont

GROUND = '#0f1012'; PANEL = '#16181b'; RULE = '#2e3135'
TEXT = '#e6e4de'; DIM = '#8b8a84'; FAINT = '#55544f'
ACCENT = '#f7931a'; OK = '#5cc77e'; FOLD = '#6fb3c4'; RED = '#d4694f'
CARD = {'4090': ('#f7931a', 0.34), 'A40': ('#e8a94f', 0.49), 'L40S': ('#6fb3c4', 0.86)}
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

W, H = 1920, 1080
DAY = 86400.0
BUDGET = 600.0
NBLOCKS = 144
LANES = 30
HOLD = 0.06
PULSE = 70.0                 # run-seconds a pulse takes to cross to the grid
FOLDW = 0.18                 # the last fraction of a block's proof that reads as the fold

# layout
PAD = 60
LX0, LX1 = 108, 560          # lane traces: 0% .. 100%
LTOP, LROW = 300, 17.0
FOLDX = 610                  # where the fold token sits, just right of the line
RCX, RCY, ROUT, RIN = 960, 560, 168, 132      # the ring, dead centre
GX0, GY0, GCOL, GCELL, GGAP = 1318, 300, 12, 36, 6


def hx(c):
    c = c.lstrip('#'); return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def mix(c, bg, a):
    c, bg = hx(c), hx(bg); return tuple(int(bg[i] + (c[i] - bg[i]) * a) for i in range(3))


def mmss(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def synth(seed=11):
    r = random.Random(seed)
    blocks, h0, step, t = [], 967_300, DAY / NBLOCKS, 0.0
    for i in range(NBLOCKS):
        t = min(DAY - 300, max(t + 30, i * step + r.gauss(0, 100)))
        segs = int(r.lognormvariate(math.log(1400), 0.42))
        cards = max(10, min(LANES, int(segs / 58 + r.gauss(0, 3))))
        prove = max(80.0, segs / (cards * 0.62) + 18 * math.log2(max(2, cards)) + r.gauss(0, 14))
        if r.random() < 0.06:
            prove *= r.uniform(1.25, 1.7)
        blocks.append(dict(h=h0 + i, arrive=t, segs=segs, cards=cards, prove=prove, done=t + prove))
    # lanes: a sequence of pods per lane, with dark gaps where nothing is rented
    lanes = []
    for _ in range(LANES):
        seq, tt = [], r.uniform(0, 5400)
        while tt < DAY:
            life = r.uniform(5400, 30000)
            seq.append(dict(kind=r.choice(['4090', '4090', 'A40', 'L40S']),
                            t0=tt, t1=min(DAY, tt + life), seed=r.random()))
            tt += life + r.uniform(900, 7200)
        lanes.append(seq)
    # cost: cards x their hourly price x the time they were on the block
    for b in blocks:
        b['cost'] = b['cards'] * 0.45 * (b['prove'] / 3600.0)
    return dict(blocks=blocks, lanes=lanes)


def build_warp(run, idle_weight=0.08, fold_weight=4.0):
    edges = [0.0]
    for b in run['blocks']:
        edges += [b['arrive'], b['done'] - FOLDW * b['prove'], b['done'], b['done'] + PULSE]
    edges.append(DAY)
    edges = sorted(set(min(DAY, max(0.0, e)) for e in edges))
    cum, s = [0.0], 0.0
    for i in range(len(edges) - 1):
        mid = (edges[i] + edges[i + 1]) / 2
        gesture = any(x['done'] - FOLDW * x['prove'] <= mid < x['done'] + PULSE for x in run['blocks'])
        busy = any(x['arrive'] <= mid < x['done'] for x in run['blocks'])
        w = fold_weight if gesture else (1.0 if busy else idle_weight)
        s += (edges[i + 1] - edges[i]) * w
        cum.append(s)

    def warp(u):
        u = max(0.0, min(1.0, u))
        if u >= 1 - HOLD:
            return DAY
        target = (u / (1 - HOLD)) * s
        i = min(max(0, bisect.bisect_right(cum, target) - 1), len(edges) - 2)
        span = (cum[i + 1] - cum[i]) or 1e-9
        return edges[i] + (edges[i + 1] - edges[i]) * ((target - cum[i]) / span)
    return warp


def load_fonts():
    f = ImageFont.truetype
    return dict(ring=f(FONTB, 76), head=f(FONTB, 36), mid=f(FONTB, 20), fig=f(FONTB, 30),
                lab=f(FONT, 17), small=f(FONT, 14), tiny=f(FONT, 12), eyebrow=f(FONT, 16))


def cell_xy(i):
    return GX0 + (i % GCOL) * (GCELL + GGAP), GY0 + (i // GCOL) * (GCELL + GGAP)


def draw(run, t, fo):
    im = Image.new('RGB', (W, H), hx(GROUND))
    d = ImageDraw.Draw(im)
    blocks, lanes = run['blocks'], run['lanes']
    done = [b for b in blocks if b['done'] + PULSE <= t]
    live = next((b for b in blocks if b['arrive'] <= t < b['done']), None)
    late = [b for b in blocks if b['done'] <= t and b['prove'] > BUDGET]
    spent = sum(b['cost'] for b in blocks if b['done'] <= t)
    upnow = [(ln, p) for ln, seq in enumerate(lanes) for p in seq if p['t0'] <= t < p['t1']]

    d.text((PAD, 44), 'HAZYNC · 24 HOURS AT THE TIP', font=fo['eyebrow'], fill=hx(DIM))
    d.text((PAD, 70), 'Every block, proved before the next one arrived', font=fo['head'], fill=hx(TEXT))
    d.line([PAD, 132, W - PAD, 132], fill=hx(RULE))

    # ---------------- pod lanes, growing towards the 100% line
    d.text((LX0, LTOP - 30), f'{len(upnow)} CARDS ON THIS BLOCK', font=fo['small'], fill=hx(FAINT))
    prog = 0.0 if not live else min(1.0, (t - live['arrive']) / live['prove'])
    for ln, seq in enumerate(lanes):
        y = LTOP + ln * LROW
        base = y + LROW - 3
        d.line([LX0, base, LX1, base], fill=mix(RULE, GROUND, .35))
        d.text((PAD, y + 1), f'{ln:02d}', font=fo['tiny'],
               fill=hx(FAINT) if any(p['t0'] <= t < p['t1'] for p in seq) else mix(FAINT, GROUND, .45))
        pod = next((p for p in seq if p['t0'] <= t < p['t1']), None)
        if pod is None:
            continue                                  # dark lane: no card rented here now
        col, _ = CARD[pod['kind']]
        p = 0.0 if live is None else max(0.0, min(1.0, prog * (0.82 + 0.34 * pod['seed'])))
        xend = LX0 + p * (LX1 - LX0)
        idle, work = [], []
        for x in range(int(LX0), int(LX1), 3):
            u = (x - LX0) / (LX1 - LX0)
            amp = 0.35 + 0.5 * abs(math.sin(u * 26 + pod['seed'] * 9))
            pt = (x, base - amp * (LROW - 5))
            (work if x <= xend else idle).append(pt)
        if len(idle) > 1:                              # rented, powered, not on this block
            d.line(idle, fill=mix(col, GROUND, .16), width=1)
        if len(work) > 1:
            d.line(work, fill=mix(col, GROUND, .85), width=1)
        if p >= 0.999:
            d.ellipse([LX1 - 3, base - 3, LX1 + 3, base + 3], fill=mix(OK, GROUND, .95))

    # the 100% line
    ytop, ybot = LTOP - 6, LTOP + LANES * LROW + 2
    d.line([LX1, ytop, LX1, ybot], fill=mix(OK, GROUND, .8), width=2)
    d.text((LX1 - 16, ybot + 8), '100%', font=fo['small'], fill=mix(OK, GROUND, .9))
    d.text((LX0, ybot + 8), 'GPU POWER PER CARD, 1 Hz', font=fo['small'], fill=hx(FAINT))

    # ---------------- the fold: finished lanes converge into one block token
    fy = (ytop + ybot) / 2
    folding = live is not None and prog > (1.0 - FOLDW)
    if folding or (live is None and blocks[0]['arrive'] <= t):
        k = 1.0 if live is None else (prog - (1.0 - FOLDW)) / FOLDW
        for ln, pod in upnow:
            y = LTOP + ln * LROW + LROW - 3
            d.line([LX1, y, LX1 + (FOLDX - LX1) * min(1.0, k), fy + (y - fy) * (1 - min(1.0, k))],
                   fill=mix(FOLD, GROUND, .20 + .5 * k), width=1)
    just = [b for b in blocks if b['done'] <= t < b['done'] + PULSE]
    if just or folding:
        g = 10 if just else 6
        d.ellipse([FOLDX - g, fy - g, FOLDX + g, fy + g], fill=mix(FOLD if not just else OK, GROUND, .9))

    # ---------------- the pulse: token -> its cell in the grid
    for b in just:
        f = (t - b['done']) / PULSE
        cx, cy = cell_xy(b['h'] - blocks[0]['h'])
        tx_, ty_ = cx + GCELL / 2, cy + GCELL / 2
        px, py = FOLDX + (tx_ - FOLDX) * f, fy + (ty_ - fy) * f
        d.line([FOLDX, fy, px, py], fill=mix(OK, GROUND, .18))
        r_ = 7 - 3 * f
        d.ellipse([px - r_, py - r_, px + r_, py + r_], fill=mix(OK, GROUND, .95))

    # ---------------- the ring, dead centre
    d.arc([RCX - ROUT, RCY - ROUT, RCX + ROUT, RCY + ROUT], 0, 360, fill=mix(RULE, GROUND, .8), width=13)
    sweep = 360.0 * min(t, DAY) / DAY
    if sweep > 0.4:
        d.arc([RCX - ROUT, RCY - ROUT, RCX + ROUT, RCY + ROUT], -90, -90 + sweep,
              fill=mix(ACCENT, GROUND, .95), width=13)
    d.arc([RCX - RIN, RCY - RIN, RCX + RIN, RCY + RIN], 0, 360, fill=mix(RULE, GROUND, .55), width=11)
    if done:
        d.arc([RCX - RIN, RCY - RIN, RCX + RIN, RCY + RIN], -90, -90 + 360.0 * len(done) / NBLOCKS,
              fill=mix(OK, GROUND, .95), width=11)
    cnt = f'{len(done)}'
    d.text((RCX - d.textlength(cnt, font=fo['ring']) / 2, RCY - 66), cnt, font=fo['ring'], fill=hx(OK))
    sub = f'/ {NBLOCKS} BLOCKS'
    d.text((RCX - d.textlength(sub, font=fo['lab']) / 2, RCY + 18), sub, font=fo['lab'],
           fill=mix(TEXT, GROUND, .7))
    tag = 'NONE LATE' if not late else f'{len(late)} LATE'
    d.text((RCX - d.textlength(tag, font=fo['mid']) / 2, RCY + 44), tag, font=fo['mid'],
           fill=mix(OK if not late else RED, GROUND, .95))
    el = f'{int(min(t,DAY)//3600):02d}:{int(min(t,DAY)%3600//60):02d} ELAPSED'
    d.text((RCX - d.textlength(el, font=fo['small']) / 2, RCY - ROUT - 26), el,
           font=fo['small'], fill=hx(FAINT))

    # money, under the ring
    c1 = f"${live['cost']:.2f}" if live else (f"${done[-1]['cost']:.2f}" if done else '—')
    d.text((RCX - d.textlength(c1, font=fo['fig']) / 2, RCY + ROUT + 20), c1,
           font=fo['fig'], fill=hx(TEXT))
    d.text((RCX - d.textlength('THIS BLOCK', font=fo['small']) / 2, RCY + ROUT + 58), 'THIS BLOCK',
           font=fo['small'], fill=hx(FAINT))
    c2 = f'${spent:,.0f}'
    d.text((RCX - d.textlength(c2, font=fo['fig']) / 2, RCY + ROUT + 92), c2,
           font=fo['fig'], fill=mix(TEXT, GROUND, .85))
    d.text((RCX - d.textlength('SPENT SO FAR', font=fo['small']) / 2, RCY + ROUT + 130), 'SPENT SO FAR',
           font=fo['small'], fill=hx(FAINT))
    hrs = sum(b['cards'] * b['prove'] for b in blocks if b['done'] <= t) / 3600.0
    ar = f'{hrs:,.0f} card-hours × $0.45'
    d.text((RCX - d.textlength(ar, font=fo['tiny']) / 2, RCY + ROUT + 152), ar,
           font=fo['tiny'], fill=mix(FAINT, GROUND, .8))

    # ---------------- the grid: every block we mean to prove
    d.text((GX0, GY0 - 30), f'ALL {NBLOCKS} BLOCKS', font=fo['small'], fill=hx(FAINT))
    for i, b in enumerate(blocks):
        cx, cy = cell_xy(i)
        if b['done'] + PULSE <= t:
            col = mix(OK, GROUND, .9)
        elif b['arrive'] <= t:
            col = mix(ACCENT, GROUND, .9)
        else:
            col = mix(RED, GROUND, .26)
        d.rectangle([cx, cy, cx + GCELL, cy + GCELL], fill=col)
        if b['done'] + PULSE > t and b['arrive'] > t:
            d.rectangle([cx, cy, cx + GCELL, cy + GCELL], outline=mix(RED, GROUND, .40))
    gy1 = GY0 + 12 * (GCELL + GGAP)
    if live:
        d.text((GX0, gy1 + 6), f"NOW: block {live['h']:,} · {live['segs']:,} segments · {mmss(t-live['arrive'])}",
               font=fo['small'], fill=mix(ACCENT, GROUND, .9))
    elif done:
        d.text((GX0, gy1 + 6), f"slowest {mmss(max(b['prove'] for b in done))} · "
                               f"median {mmss(sorted(b['prove'] for b in done)[len(done)//2])}",
               font=fo['small'], fill=hx(DIM))

    d.text((PAD, H - 42), 'MOCK · synthetic data · the 24-hour run has not happened',
           font=fo['small'], fill=mix(RED, GROUND, .85))
    note = 'waiting between blocks runs fast · proving runs at full rate'
    d.text((W - PAD - d.textlength(note, font=fo['small']), H - 42), note,
           font=fo['small'], fill=hx(FAINT))
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--still', type=float)
    ap.add_argument('--frames', action='store_true')
    ap.add_argument('--fps', type=int, default=24)
    ap.add_argument('--seconds', type=float, default=60.0)
    ap.add_argument('--out', default='frames')
    a = ap.parse_args()
    run, fo = synth(), load_fonts()
    warp = build_warp(run)
    if a.still is not None:
        draw(run, warp(a.still), fo).save('still.png')
        print(f'still.png u={a.still}')
        return
    if a.frames:
        os.makedirs(a.out, exist_ok=True)
        n = int(a.seconds * a.fps)
        for i in range(n):
            draw(run, warp(i / (n - 1)), fo).save(f'{a.out}/f{i:05d}.png')
            if i % 200 == 0:
                print(f'  {i}/{n}', flush=True)
        print('frames:', n)


if __name__ == '__main__':
    main()
