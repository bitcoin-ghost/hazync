#!/usr/bin/env python3
"""24 hours at the tip — the marketing cut.

⛔ MOCK. Synthetic data; the run has not happened. Model + time warp come from tip24c.

Composition (1920x1080), built to fill the frame:
  HEADER  0..130    title, then ONE live line of block facts (said once, nowhere else)
  STAGE   150..690  traces + ring (left), join tree (middle), all 144 blocks (right)
  STATS   700..755  a single row: proving/idle clock, cost, spend, idle total
  BARS    775..1022 proving and folding time for every block, full width
  FOOTER  1050      the mock stamp

Design notes: hero numbers are large, labels are few, and every element grows during the film —
the grid fills, the bars march right, the ring sweeps. Nothing is explained twice.
"""
import argparse, math, os
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont
from tip24c import synth, build_warp, mmss, hx, mix, PULSE, DAY, BUDGET, NBLOCKS, LANES, CARD

GROUND = '#0f1012'; RULE = '#2a2d31'
TEXT = '#e6e4de'; DIM = '#8b8a84'; FAINT = '#55544f'
ACCENT = '#f7931a'; OK = '#5cc77e'; FOLD = '#6fb3c4'; RED = '#d4694f'; VIOLET = '#b8879b'
# The three block states, back in the original green/orange family (the site's pink/yellow read as
# neon and were dropped). One language everywhere: ring, tree, map, bars.
#   proving = orange   folding = teal (the join tree's own colour)   done = green
MAP_PROVING = ACCENT       # #f7931a
MAP_FOLDING = FOLD         # #6fb3c4
MAP_DONE = OK              # #5cc77e
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

W, H = 1920, 1080
PAD = 92

TITLE_Y, SUB_Y, RULE_Y = 38, 92, 130

# stage — left: the fleet, with the ring dead centre of it
LX0, LX1 = 92, 830
LTOP, LROW = 168, 18.2
LYTOP, LYBOT = LTOP - 6, LTOP + LANES * LROW + 2          # 162 .. 716
RCX, RCY = (LX0 + LX1) // 2, int((LTOP - 6 + LTOP + LANES * LROW + 2) / 2)
ROUT, RIN = 182, 142

# stage — middle: the join tree, root landing on RCY
TX0, TX1 = 872, 1292

# stage — right: all 144, centred on RCY
GCOL, GCELL, GGAP = 12, 36, 4
GROWS = NBLOCKS // GCOL
# derive both edges rather than hard-coding: at 12x34+11x4 the grid is 452 wide, and a literal
# GX0 of 1390 ran 14px past the 1828 padding line the rule and footer align to
GX0 = W - PAD - (GCOL * GCELL + (GCOL - 1) * GGAP)
GY0 = RCY - (GROWS * GCELL + (GROWS - 1) * GGAP) // 2
GKEY_Y = GY0 + GROWS * GCELL + (GROWS - 1) * GGAP + 16     # the map's OWN key, under the map

# stats row
SL_Y, SV_Y = 736, 754

# bars
BX0, BX1, BH = 92, 1828, 194
BHEAD, BY1 = 806, 1026

FOOT_Y = 1050
START = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)   # ⛔ MOCK clock


def fonts():
    f = ImageFont.truetype
    return dict(ring=f(FONTB, 88), head=f(FONTB, 40), stat=f(FONTB, 34), mid=f(FONTB, 20),
                big=f(FONTB, 52), lab=f(FONT, 17), small=f(FONT, 14), tiny=f(FONT, 12))


def cell_xy(i):
    return GX0 + (i % GCOL) * (GCELL + GGAP), GY0 + (i // GCOL) * (GCELL + GGAP)


def enrich(run):
    """Split each block into proving and folding, and give it the deadline the title claims.

    Folding leans toward a fixed cost rather than a share, echoing run 4 where the join tree did
    not shrink as cards were added. ⚠ NOT a clean floor here: synth makes blocks whose total is
    ~110 s, so the 0.5x clamp pulls folding down. Do not caption these bars as proving a floor.
    """
    if run.get('enriched'):
        return run
    bs = run['blocks']
    for i, b in enumerate(bs):
        b['deadline'] = bs[i + 1]['arrive'] if i + 1 < len(bs) else DAY
        r = ((b['h'] * 2654435761) % 997) / 997.0
        b['fold_s'] = min(105.0 + 50.0 * r, 0.5 * b['prove'])
        b['prove_s'] = b['prove'] - b['fold_s']
        b['grow'] = b['prove_s'] / b['prove']
        b['txs'] = int(b['segs'] * 2.35 * (0.90 + 0.20 * r))       # run 4's shape
        b['inputs'] = int(b['segs'] * 4.50 * (0.90 + 0.20 * r))
    run['enriched'] = True
    return run


def gaps_of(blocks):
    gs, end = [], 0.0
    for b in blocks:
        if b['arrive'] > end:
            gs.append((end, b['arrive']))
        end = max(end, b['done'])
    if end < DAY:
        gs.append((end, DAY))
    return gs


def idle_state(gaps, t):
    now, total = None, 0.0
    for g0, g1 in gaps:
        if t >= g1:
            total += g1 - g0
        elif t > g0:
            total += t - g0
            now = t - g0
    return now, total


def bloom(d, box, a0, a1, col, width):
    """A soft halo under the crisp arc — cheap bloom, and it makes the ring the focal point."""
    for w, a in ((width + 14, .08), (width + 7, .16), (width, .95)):
        d.arc(box, a0, a1, fill=mix(col, GROUND, a), width=w)


def stat(d, fo, x, label, value, col, sub=None):
    d.text((x, SL_Y), label, font=fo['small'], fill=hx(FAINT))
    d.text((x, SV_Y), value, font=fo['stat'], fill=col)
    if sub:
        d.text((x + d.textlength(value, font=fo['stat']) + 14, SV_Y + 16), sub,
               font=fo['small'], fill=hx(DIM))


def draw(run, t, fo):
    base = Image.new('RGB', (W, H), hx(GROUND))
    d = ImageDraw.Draw(base)
    blocks, lanes = enrich(run)['blocks'], run['lanes']
    done = [b for b in blocks if b['done'] + PULSE <= t]
    live = next((b for b in blocks if b['arrive'] <= t < b['done']), None)
    late = [b for b in blocks if b['done'] <= t and b['done'] > b['deadline']]
    spent = sum(b['cost'] for b in blocks if b['done'] <= t)
    up = [(ln, p) for ln, seq in enumerate(lanes) for p in seq if p['t0'] <= t < p['t1']]
    prog = 0.0 if not live else min(1.0, (t - live['arrive']) / live['prove'])
    grow = live['grow'] if live else 1.0
    kfold = 0.0 if (not live or prog <= grow) else min(1.0, (prog - grow) / (1.0 - grow))
    inow, itot = idle_state(run.setdefault('gaps', gaps_of(blocks)), t)

    # ---------------- header: the claim, then the facts ONCE
    # the title's money is the RUNNING spend, counting up to the day's total. It used to show the
    # final figure while a stat tile showed the running one — two different numbers wearing the
    # same label. Now there is exactly one "spent" figure in the frame, and it is this.
    x = PAD
    for s, col in [('Hazync zkVM bitcoin block proofs', hx(TEXT)), (' : ', hx(FAINT)),
                   (f'{NBLOCKS} blocks', mix(ACCENT, GROUND, .95)), (' · ', hx(FAINT)),
                   ('24 hours', mix(ACCENT, GROUND, .95)), (' · ', hx(FAINT)),
                   (f'${spent:,.0f}', hx(TEXT))]:
        d.text((x, TITLE_Y), s, font=fo['head'], fill=col)
        x += d.textlength(s, font=fo['head'])
    when = START + timedelta(seconds=min(t, DAY))
    sub = (f"{when:%H:%M:%S} UTC · block {live['h']:,} · {live['segs']:,} segments · "
           f"{live['txs']:,} txs · {live['inputs']:,} inputs · "
           f"{len(up)} card{'' if len(up) == 1 else 's'}" if live else
           f"{when:%H:%M:%S} UTC · {len(done)} of {NBLOCKS} proved · waiting for the next block")
    d.text((PAD, SUB_Y), sub, font=fo['lab'], fill=hx(DIM))
    d.line([PAD, RULE_Y, W - PAD, RULE_Y], fill=hx(RULE))

    # ---------------- stage left: the fleet
    leaves = []
    for ln, seq in enumerate(lanes):
        b0 = LTOP + ln * LROW + LROW - 3
        d.line([LX0, b0, LX1, b0], fill=mix(RULE, GROUND, .22))
        pod = next((p for p in seq if p['t0'] <= t < p['t1']), None)
        if pod is None:
            continue
        col, _ = CARD[pod['kind']]
        p = 0.0 if live is None else max(0.0, min(1.0, prog / (0.62 + 0.34 * pod['seed'])))
        leaves.append((b0, p >= 1.0))
        xend = LX0 + p * (LX1 - LX0)
        rest, work = [], []
        for xx in range(int(LX0), int(LX1), 3):
            u = (xx - LX0) / (LX1 - LX0)
            amp = 0.34 + 0.52 * abs(math.sin(u * 22 + pod['seed'] * 9))
            (work if xx <= xend else rest).append((xx, b0 - amp * (LROW - 4)))
        if len(rest) > 1:
            d.line(rest, fill=mix(col, GROUND, .18), width=1)
        if len(work) > 1:
            d.polygon([(work[0][0], b0)] + work + [(work[-1][0], b0)], fill=mix(col, GROUND, .10))
            d.line(work, fill=mix(col, GROUND, .95), width=2)
    d.line([LX1, LYTOP, LX1, LYBOT], fill=mix(OK, GROUND, .7), width=2)

    # ---------------- the ring, in front of the fleet
    ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).ellipse([RCX - ROUT - 52, RCY - ROUT - 52, RCX + ROUT + 52, RCY + ROUT + 52],
                               fill=(15, 16, 18, 190))
    base = Image.alpha_composite(base.convert('RGBA'), ov).convert('RGB')
    d = ImageDraw.Draw(base)
    ob = [RCX - ROUT, RCY - ROUT, RCX + ROUT, RCY + ROUT]
    ib = [RCX - RIN, RCY - RIN, RCX + RIN, RCY + RIN]
    d.arc(ob, 0, 360, fill=mix(RULE, GROUND, .85), width=13)
    d.arc(ib, 0, 360, fill=mix(RULE, GROUND, .55), width=11)
    finished = not live and len(done) >= NBLOCKS
    # the ring speaks the map's language too: orange was "proving" here while it meant "done" on
    # the map, in the same frame
    if finished:            # the closing frame earns a full ring, not two empty grey circles
        bloom(d, ob, -90, 270, MAP_DONE, 13)
        bloom(d, ib, -90, 270, MAP_FOLDING, 11)
    else:
        if prog > 0.002:
            bloom(d, ob, -90, -90 + 360 * prog, MAP_PROVING, 13)
        if kfold > 0.002:
            bloom(d, ib, -90, -90 + 360 * kfold, MAP_FOLDING, 11)
    cnt = f'{len(done)}'
    d.text((RCX - d.textlength(cnt, font=fo['ring']) / 2, RCY - 76), cnt, font=fo['ring'], fill=hx(OK))
    for s, f_, dy, c in ((f'/ {NBLOCKS}', 'lab', 22, mix(TEXT, GROUND, .7)),
                         ('NONE LATE' if not late else f'{len(late)} LATE', 'mid', 48,
                          mix(OK if not late else RED, GROUND, .95)),
                         (f'{int(min(t,DAY)//3600):02d}:{int(min(t,DAY)%3600//60):02d} ELAPSED',
                          'small', 78, hx(DIM))):
        d.text((RCX - d.textlength(s, font=fo[f_]) / 2, RCY + dy), s, font=fo[f_], fill=c)
    pl = '24 HOURS ELAPSED' if finished else (f'PROVING {prog*100:.0f}%' if live else 'PROVING')
    d.text((RCX - d.textlength(pl, font=fo['small']) / 2, RCY - ROUT - 34), pl, font=fo['small'],
           fill=mix(MAP_DONE if finished else MAP_PROVING, GROUND, .95 if (live or finished) else .35))
    fl = f'{NBLOCKS} BLOCKS FOLDED' if finished else (f'FOLDING {kfold*100:.0f}%' if kfold > 0 else 'FOLDING')
    d.text((RCX - d.textlength(fl, font=fo['small']) / 2, RCY + ROUT + 18), fl, font=fo['small'],
           fill=mix(MAP_FOLDING, GROUND, .95 if (kfold > 0 or finished) else .35))

    # ---------------- stage middle: the join tree
    if live:
        landed = [y for y, ok in leaves if ok]
        for y, ok in leaves:
            lc = OK if ok else RULE
            d.line([LX1, y, TX0, y], fill=mix(lc, GROUND, .26), width=1)
            d.ellipse([TX0 - 4, y - 4, TX0 + 4, y + 4], fill=mix(lc, GROUND, .9))
        level, x = landed or [RCY], TX0
        depth = max(1, math.ceil(math.log2(max(2, len(leaves)))))
        dx = (TX1 - TX0) / depth
        for lv in range(depth):
            nxt, pull = [], (lv + 1) / depth
            njoin = max(1, math.ceil(len(level) / 2))
            for i in range(0, len(level), 2):
                pair = level[i:i + 2]
                ym = sum(pair) / len(pair) * (1 - pull) + RCY * pull
                x2 = x + dx
                if kfold >= (lv + (i / 2 + 1) / njoin) / depth:   # only when THAT join lands
                    for y in pair:
                        d.line([x, y, x2, ym], fill=mix(FOLD, GROUND, .8), width=2)
                    d.ellipse([x2 - 4, ym - 4, x2 + 4, ym + 4], fill=mix(FOLD, GROUND, .95))
                nxt.append(ym)
            level, x = nxt, x + dx
        if kfold >= 1.0:
            bloom(d, [TX1 - 13, RCY - 13, TX1 + 13, RCY + 13], 0, 360, OK, 6)
    elif len(done) >= NBLOCKS:
        d.text((TX0, RCY - 58), 'ALL 144 BLOCKS PROVED', font=fo['mid'], fill=mix(OK, GROUND, .95))
        d.text((TX0, RCY - 22), 'NONE LATE', font=fo['big'], fill=mix(OK, GROUND, .95))
    else:
        d.text((TX0, RCY - 52), 'NOTHING TO PROVE', font=fo['mid'], fill=mix(VIOLET, GROUND, .9))
        d.text((TX0, RCY - 16), mmss(inow or 0), font=fo['big'], fill=mix(VIOLET, GROUND, .95))
        d.text((TX0, RCY + 46), 'the next block is still being mined', font=fo['lab'], fill=hx(DIM))

    # ---------------- the pulse: the fold's last point -> its cell
    for b in [z for z in blocks if z['done'] <= t < z['done'] + PULSE]:
        f = (t - b['done']) / PULSE
        cx, cy = cell_xy(b['h'] - blocks[0]['h'])
        tx_, ty_ = cx + GCELL / 2, cy + GCELL / 2
        fade = 1.0 - f
        d.line([TX0 + (TX1 - TX0) * .62, RCY, TX1, RCY], fill=mix(FOLD, GROUND, .4 * fade), width=2)
        d.ellipse([TX1 - 10, RCY - 10, TX1 + 10, RCY + 10], fill=mix(OK, GROUND, .25 + .7 * fade))
        px, py = TX1 + (tx_ - TX1) * f, RCY + (ty_ - RCY) * f
        d.line([TX1, RCY, px, py], fill=mix(OK, GROUND, .45))
        r = 10 - 4 * f
        d.ellipse([px - r - 4, py - r - 4, px + r + 4, py + r + 4], fill=mix(OK, GROUND, .22))
        d.ellipse([px - r, py - r, px + r, py + r], fill=mix(OK, GROUND, .95))

    # ---------------- stage right: all 144
    d.text((GX0, GY0 - 26), f'ALL {NBLOCKS} BLOCKS', font=fo['small'], fill=hx(FAINT))
    for i, b in enumerate(blocks):
        cx, cy = cell_xy(i)
        if b['done'] + PULSE <= t:
            col = mix(RED, GROUND, .95) if b['done'] > b['deadline'] else mix(MAP_DONE, GROUND, .92)
        elif b['arrive'] <= t:                       # live: proving, then folding
            col = mix(MAP_FOLDING if (t - b['arrive']) / b['prove'] > b['grow'] else MAP_PROVING,
                      GROUND, .95)
        else:
            col = mix(TEXT, GROUND, .07)
        d.rectangle([cx, cy, cx + GCELL, cy + GCELL], fill=col)
    kx = GX0                                          # the map's own key, in the map's colours
    for lbl, c in (('proving', MAP_PROVING), ('folding', MAP_FOLDING), ('done', MAP_DONE)):
        d.rectangle([kx, GKEY_Y + 2, kx + 11, GKEY_Y + 12], fill=mix(c, GROUND, .92))
        d.text((kx + 18, GKEY_Y), lbl, font=fo['small'], fill=hx(DIM))
        kx += 18 + d.textlength(lbl, font=fo['small']) + 28

    # ---------------- stats: one row, each fact said once
    if live:
        stat(d, fo, PAD, 'PROVING', mmss(t - live['arrive']), mix(MAP_PROVING, GROUND, .95))
    else:
        stat(d, fo, PAD, 'NOTHING TO PROVE', mmss(inow or 0), mix(VIOLET, GROUND, .95))
    c1 = f"${live['cost']:.2f}" if live else (f"${done[-1]['cost']:.2f}" if done else '—')
    stat(d, fo, 372, 'THIS BLOCK' if live else 'LAST BLOCK', c1, hx(TEXT))
    stat(d, fo, 700, 'IDLE — ANCHOR WARP COULD USE IT',
         f'{int(itot//3600)}h {int(itot%3600//60):02d}m', mix(VIOLET, GROUND, .9),
         sub=f'{100.0*itot/max(60.0, min(t, DAY)):.0f}% of the day')

    # ---------------- bars: proving and folding for every block
    # legend belongs BESIDE its own chart: parked at the far right it sat under the block map and
    # read as the map's key
    d.text((BX0, BHEAD), 'TIME PER BLOCK', font=fo['small'], fill=hx(FAINT))
    lg = BX0 + 190
    d.rectangle([lg, BHEAD + 2, lg + 11, BHEAD + 12], fill=mix(MAP_PROVING, GROUND, .95))
    d.text((lg + 18, BHEAD), 'proving', font=fo['small'], fill=hx(DIM))
    d.rectangle([lg + 100, BHEAD + 2, lg + 111, BHEAD + 12], fill=mix(MAP_FOLDING, GROUND, .95))
    d.text((lg + 118, BHEAD), 'folding', font=fo['small'], fill=hx(DIM))
    # Scale to what is actually DRAWN. Using the block's total time here left ~a third of the box
    # as dead headroom (no single bar is ever that tall), which is why a taller box produced no
    # taller bars — the v7 complaint. The tallest bar now reaches ~96% of the box.
    ymax = max(max(b['prove_s'], b['fold_s']) for b in blocks) * 1.04
    d.line([BX0, BY1, BX1, BY1], fill=mix(RULE, GROUND, .9))
    for sec in range(60, int(ymax), 60):              # minute gridlines give the empty headroom
        gy = BY1 - BH * sec / ymax                    # a scale instead of leaving it blank
        d.line([BX0 + 40, gy, BX1, gy], fill=mix(RULE, GROUND, .6))
        d.text((BX0, gy - 7), f'{sec // 60}m', font=fo['small'], fill=hx(FAINT))
    slot = (BX1 - BX0) / NBLOCKS
    for i, b in enumerate(blocks):
        x0 = BX0 + i * slot
        if b['arrive'] > t:                      # a dim placeholder keeps the full day in frame
            d.rectangle([x0 + 1, BY1 - 3, x0 + 10, BY1], fill=mix(RULE, GROUND, .55))
            continue
        el = t - b['arrive']
        ps, fs = min(b['prove_s'], el), min(b['fold_s'], max(0.0, el - b['prove_s']))
        # same two states as the map, so the same two colours — otherwise orange means "proving"
        # here and "done" over there, in one frame
        if ps > 0:
            d.rectangle([x0 + 1, BY1 - BH * ps / ymax, x0 + 5.4, BY1], fill=mix(MAP_PROVING, GROUND, .92))
        if fs > 0:
            d.rectangle([x0 + 6.2, BY1 - BH * fs / ymax, x0 + 10.6, BY1], fill=mix(MAP_FOLDING, GROUND, .92))

    d.text((PAD, FOOT_Y), 'MOCK · synthetic data · the 24-hour run has not happened',
           font=fo['small'], fill=mix(RED, GROUND, .8))
    note = 'waiting between blocks runs fast · proving runs at full rate'
    d.text((W - PAD - d.textlength(note, font=fo['small']), FOOT_Y), note,
           font=fo['small'], fill=hx(FAINT))
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stills')
    ap.add_argument('--frames', action='store_true')
    ap.add_argument('--fps', type=int, default=24)
    ap.add_argument('--seconds', type=float, default=75.0)
    ap.add_argument('--out', default='frames')
    a = ap.parse_args()
    run, fo = synth(), fonts()
    warp = build_warp(run, idle_weight=0.26, fold_weight=4.0)
    if a.stills:
        for s in a.stills.split(','):
            draw(run, warp(float(s)), fo).save(f'e_{s}.png')
        print('stills:', a.stills)
        return
    if a.frames:
        os.makedirs(a.out, exist_ok=True)
        n = int(a.seconds * a.fps)
        for i in range(n):
            draw(run, warp(i / (n - 1)), fo).save(f'{a.out}/f{i:05d}.png')
            if i % 300 == 0:
                print(f'  {i}/{n}', flush=True)
        print('frames:', n)


if __name__ == '__main__':
    main()
