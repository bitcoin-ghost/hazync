#!/usr/bin/env python3
"""Render the mp4's frame from LIVE data — same renderer, so the composition cannot drift.

Every layout constant and helper is imported from tip24e (the file the v9 mp4 was rendered with), so
this is the same picture. The ONLY substantive change is that the trace waveform is the card's real
1 Hz `power_w` samples instead of a sine, and every figure comes from snapshot.json.

  python3 collect.py --demo --once && python3 tip24live.py --once   # no pods needed
  python3 tip24live.py --loop                                        # frame.png once a second

⛔ A value with no source is NOT drawn. txs/inputs are absent from pod telemetry, so the subtitle
   does not claim them; cost IS real (RunPod costPerHr, via fleet.sh -> pods.txt).
"""
import argparse, json, math, os, time
from datetime import datetime, timezone
from PIL import Image, ImageDraw

from tip24e import (W, H, PAD, LX0, LX1, LTOP, LROW, LYTOP, LYBOT, RCX, RCY, ROUT, RIN,
                    TX0, TX1, GCELL, GGAP, GCOL, GROWS, GX0, GY0, GKEY_Y,
                    SL_Y, SV_Y, BX0, BX1, BH, BHEAD, BY1, FOOT_Y,
                    GROUND, RULE, TEXT, DIM, FAINT, ACCENT, OK, FOLD, RED, VIOLET,
                    MAP_PROVING, MAP_FOLDING, MAP_DONE,
                    fonts, bloom, cell_xy, hx, mix, mmss, NBLOCKS, LANES)

WINDOW = GCOL * GROWS          # 144 cells, exactly as the film

# ── the ring sits BESIDE the traces, not on top of them ──────────────────────────────────────────
# Asked for 2026-09-22, and it supersedes the earlier "the ring stays IN FRONT OF the traces": a
# 364px disc in the middle of a 738px band meant every lane ran behind it, and the parts of a
# waveform a viewer most wants (the busy middle) were the parts hidden.
#
# ⚠ REBOUND HERE, NOT EDITED IN tip24e. That module is the geometry the v9 mp4 was rendered with and
# the docstring above promises "the same picture" -- changing it would silently re-cut the film.
# Overriding in the live renderer keeps the two honestly separate.
#
# ⚠ RCY IS DELIBERATELY UNTOUCHED. `GY0` and the join tree's root both derive from it, so moving it
# would drag the block map and the tree off their shared centre line.
RING_GAP = 44                       # breathing room between the ring's edge and the first lane
RCX = PAD + ROUT                    # 274: the ring's left edge lands on the page's padding line
LX0 = RCX + ROUT + RING_GAP         # 500: lanes start clear of it, and are narrower for it
# Seconds a completed block spends crossing to its cell. The film used 70 because its
# clock compressed a day into a minute; live, this is real seconds and the renderer
# draws about once a second, so 6 gives roughly six frames of travel.
PULSE_S = float(os.environ.get('HAZYNC_PULSE_S', '6'))

# One colour per worker, so a lane is identifiable at a glance. Same green/orange family as the
# rest of the frame — colouring by GPU type made every card of the same model indistinguishable.
TRACE = ['#f7931a', '#6fb3c4', '#5cc77e', '#e8a94f', '#b8879b', '#7aa2f7',
         '#d4694f', '#9ece6a', '#c0a36e', '#89ddff', '#e0af68', '#73daca']


def stat(d, fo, x, label, value, col, sub=None):
    d.text((x, SL_Y), label, font=fo['small'], fill=hx(FAINT))
    d.text((x, SV_Y), value, font=fo['stat'], fill=col)
    if sub:
        d.text((x + d.textlength(value, font=fo['stat']) + 14, SV_Y + 16), sub,
               font=fo['small'], fill=hx(DIM))


# Words that mean the run is in trouble. Matched case-insensitively against the phase line the run
# writes for itself, so the frame inherits the run's own vocabulary rather than guessing.
BAD_WORDS = ('failed', 'fail', 'error', 'panic', 'stranded', 'refusing', 'aborted', 'stopping',
             'could not', 'cannot', 'no capacity', 'timed out')
GOOD_WORDS = ('verified', 'submitted')


def phase_tone(phase_txt):
    """RED for trouble, green for a finished proof, accent for work in progress.

    ⛔ WHY. The banner was hardcoded to ACCENT, so FAILED, PROVING and VERIFIED rendered
    pixel-identically -- measured (235,140,25) for all three. A status readout with one colour
    cannot report status; it is decoration. That is the fourth such readout found on this frame.

    ⚠ Trouble is checked FIRST. "FAILED on block 741000 after it verified 3 others" must read as a
    failure, not as a success, and a phase line can easily contain both words.
    """
    low = (phase_txt or '').lower()
    if any(w in low for w in BAD_WORDS):
        return RED
    if any(w in low for w in GOOD_WORDS):
        return OK
    return ACCENT


def fleet_colour(up, total):
    """⛔ A DEGRADED FLEET MUST NOT LOOK LIKE A HEALTHY ONE.

    This read `2/3 up` in exactly the same neutral colour as `3/3 up`. Over an unattended 24-hour run
    the entire point of the frame is that someone glances at it, and a card that died four hours ago
    is the thing they need to see. A named function so the rule is testable without rendering.
    """
    return hx(TEXT) if up >= total else mix(ACCENT, GROUND, .95)


def fleet_sub(up, total, cost_hr):
    """The $/hr, plus how many cards are down.

    ⚠ THE RATE DOES NOT DROP WHEN A CARD DIES. A rented pod bills whether it answers or not, so a
    reduced rate would understate what the run is actually costing -- the opposite of what a cost
    readout is for.
    """
    sub = f'${cost_hr:.2f}/hr'
    return sub if up >= total else f'{sub}  ·  {total - up} down'


# A 1 Hz feed that has said nothing for this long is not slow, it is broken.
STALE_S = 60


def feed_age(snap, wall_now):
    """Seconds since the collector last wrote, from the WALL clock.

    ⚠ Falls back to 0 for a snapshot written before `wall` existed; such a snapshot cannot report
    staleness at all, which is the old behaviour and no worse than it was.
    """
    stamp = snap.get('wall')
    if stamp is None:
        return 0.0
    return max(0.0, wall_now - stamp)


def feed_line(n_up, age):
    """The footer's left-hand text. Says STALE loudly rather than quietly reading 0s."""
    if age >= STALE_S:
        return f'STALE — no update for {age / 60.0:.0f}m · the feed may be dead'
    return f'live · {n_up} cards streaming · updated {age:.0f}s ago'


def draw_live(snap, fo):
    base = Image.new('RGB', (W, H), hx(GROUND))
    d = ImageDraw.Draw(base)
    now = snap.get('t') or time.time()
    chain = snap.get('chain', {})
    cards = snap.get('cards', [])
    blocks = snap.get('blocks', [])
    fleet = snap.get('fleet', {})
    up = [c for c in cards if c.get('up')]
    tip = chain.get('tip') or 0

    live_h = next((c.get('block') for c in up if c.get('block')), None)
    cur = next((b for b in blocks if b['h'] == live_h), None)
    proving = [c for c in up if c.get('phase') == 'proving']
    assembling = [c for c in up if c.get('phase') == 'assembling']
    prog = (sum(min(1.0, (c['seg_n'] / c['seg_total'])) for c in up if c.get('seg_total'))
            / max(1, len([c for c in up if c.get('seg_total')]))) if up else 0.0
    kfold = (len(assembling) / len(up)) if up else 0.0
    # ⛔ The readout used to key on `cur` (does a block record exist?) rather than on what the cards
    # are DOING, so the gap between blocks rendered "PROVING 8:48" with 30/30 cards at rest.
    # MEASURED: 30 cards finish a 1,900-segment block in ~194 s of a 600 s block period, so the
    # fleet is idle ~70% of a tip-following day. That state is the one viewers see most — it has to
    # read as "ahead of the chain", not as a dead dashboard.
    working = bool(proving or assembling)
    idle = bool(up) and not working
    if idle:
        prog = kfold = 0.0          # a stale segment count from the last block is not progress

    # elapsed on the current block, and what it has cost so far — REAL money: card-hours x price
    el = (now - cur['arrive']) if cur else 0.0
    # Both figures come from the collector's INTEGRATED spend. Deriving them from block phase
    # timings (prove_s + fold_s) made the header read $0 while cards were billing by the second,
    # because those fields are only set on an observed phase transition.
    cost_now = (cur or {}).get('cost') or (sum(c.get('cost_hr', 0.0) for c in up) * (el / 3600.0))
    spent = fleet.get('spend_usd') or sum((b.get('cost') or 0) for b in blocks)
    # bitcoin's own cadence — the number every other figure here is racing
    PERIOD = 600.0
    eta = max(0.0, PERIOD - el) if cur else 0.0
    # how far ahead of the chain the fleet actually is, from MEASURED block totals (not a claim)
    tot = sorted((b.get('prove_s') or 0) + (b.get('fold_s') or 0)
                 for b in blocks if b.get('prove_s'))
    med = tot[len(tot) // 2] if tot else 0.0
    ahead = (PERIOD / med) if med > 0 else 0.0

    # ---------------- header
    x = PAD
    done_n = sum(1 for b in blocks if b.get('done'))
    for s, col in [('Hazync zkVM bitcoin block proofs', hx(TEXT)), (' : ', hx(FAINT)),
                   (f"{done_n} block{'' if done_n == 1 else 's'}", mix(ACCENT, GROUND, .95)),
                   (' · ', hx(FAINT)),
                   (f"{len(up)} card{'' if len(up) == 1 else 's'}", mix(ACCENT, GROUND, .95)), (' · ', hx(FAINT)),
                   # ⛔ ':,.0f' rounded a real $0.46 to "$0" — the total looked broken when it was
                   # only being formatted away. Cents below $100, whole dollars above.
                   (f'${spent:,.2f}' if spent < 100 else f'${spent:,.0f}', hx(TEXT))]:
        d.text((x, 40), s, font=fo['head'], fill=col)
        x += d.textlength(s, font=fo['head'])
    ts = datetime.fromtimestamp(now, timezone.utc).strftime('%H:%M:%S')
    # ⛔ card count and $/hr live in the header and the FLEET stat respectively. Saying either here
    # made three places on one frame state "30 cards" — the same duplication already called out for
    # the block number.
    sub = (f"{ts} UTC · block {live_h:,} · {cur['segs']:,} segments"
           if (live_h and cur) else
           f"{ts} UTC · tip {tip:,} · waiting for the next block")
    d.text((PAD, 90), sub, font=fo['lab'], fill=hx(DIM))
    d.line([PAD, 122, W - PAD, 122], fill=hx(RULE))

    # ---------------- left: one lane per card, REAL power samples
    leaves = []
    # Lane height follows the fleet. Three cards used to get three thin lanes at the top with 27
    # empty below; now they get three tall ones across the whole band — which also gives the 1 Hz
    # waveform room to show its detail instead of flattening to a line.
    nlanes = max(1, min(LANES, len(cards)))
    # no upper cap: capping at 64px left a 3-card fleet with three lanes crammed at the top of a
    # 512px band and two thirds of it empty. Fill the band.
    # ⛔ Cap the lane height. Giving a 3-card trial a third of the band each turned every trace into
    # a 180px colour slab. Capped and centred, a small fleet reads as a small fleet rather than as a
    # broken one — and the band is still filled at the 16-30 cards a tip run actually uses.
    row = min(96.0, max(6.0, (LYBOT - LTOP) / nlanes))
    ytop = LTOP + ((LYBOT - LTOP) - row * nlanes) / 2.0
    for i in range(nlanes):
        b0 = ytop + i * row + row - 3
        d.line([LX0, b0, LX1, b0], fill=mix(RULE, GROUND, .22))
        if i >= len(cards):
            continue
        c = cards[i]
        segt = c.get('seg_total') or 0
        p = min(1.0, (c.get('seg_n') or 0) / segt) if segt else 0.0
        # ⛔ USE THE PEAK, NOT THE INSTANT. A card that has finished stops emitting `segment n/N`, so
        # seg_total falls back to 0 and p reads 0.0 -- the leaf dot reverted to grey the moment the
        # card succeeded, which looks exactly like a card that never started. collect.py carries the
        # furthest this card got on the block it is on.
        reached = max(p, float(c.get('peak') or 0.0))
        leaves.append((b0, reached >= 1.0 or c.get('phase') == 'assembling'))
        if not c.get('up'):
            d.text((LX0 + 6, b0 - row + 3), c['name'][:18] + ' · down',
                   font=fo['tiny'] if 'tiny' in fo else fo['small'], fill=mix(RED, GROUND, .6))
            continue
        w = c.get('w') or []
        if not w:
            continue
        col = TRACE[i % len(TRACE)]      # one colour per worker, so a lane is identifiable at a glance
        # ⛔ x is TIME, not segment progress. Mapping x to seg_n/seg_total collapsed the whole
        # waveform to ZERO WIDTH whenever a card sat between units (progress 0): 129 real samples
        # drew a single tick. run4_data.json's traces are time-based for exactly this reason.
        tt = c.get('t') or []
        t0, t1 = (tt[0], tt[-1]) if len(tt) > 1 else (0.0, 1.0)
        span = max(1e-6, t1 - t0)
        lo_w, hi_w = min(w), max(max(w), min(w) + 1.0)
        pts = []
        n = len(w)
        for k in range(0, n, max(1, n // 240)):
            frac = ((tt[k] - t0) / span) if k < len(tt) else (k / max(1, n - 1))
            xx = LX0 + frac * (LX1 - LX0)
            amp = (w[k] - lo_w) / (hi_w - lo_w)
            pts.append((xx, b0 - (0.25 + 0.7 * amp) * (row - 4)))
        if len(pts) > 1:
            d.polygon([(pts[0][0], b0)] + pts + [(pts[-1][0], b0)], fill=mix(col, GROUND, .10))
            d.line(pts, fill=mix(col, GROUND, .95), width=2)
        if p > 0:                       # progress is a MARKER now, not the axis
            xp = LX0 + p * (LX1 - LX0)
            d.line([xp, b0 - (row - 4), xp, b0], fill=mix(OK, GROUND, .85), width=2)
    # span the LANES, not the whole band: with the lane height capped, a 3-card fleet left a
    # full-height green rule with nothing attached to two thirds of it
    d.line([LX1, ytop, LX1, ytop + row * nlanes], fill=mix(OK, GROUND, .7), width=2)

    # ---------------- the ring
    # ⚠ THE RING NO LONGER OVERLAPS ANYTHING (see the geometry at the top of this file). It used to
    # sit in front of the traces; the feathered backing below is what stopped that reading as a grey
    # smudge with trace ghosts swimming through it. It is KEPT because it still gives the medallion
    # its solid centre against the page, but it is no longer load-bearing: nothing is occluded now,
    # and if the ring is ever moved back over the lanes this is the machinery that has to come with
    # it. The two earlier notes about how far to feather (ROUT+74 erased 70% of the stage, and at
    # 3 cards the lanes vanished entirely) are why it backs only from RIN.
    ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for pad, alpha in ((34, 55), (26, 120), (18, 195), (10, 240), (2, 255)):
        od.ellipse([RCX - RIN - pad, RCY - RIN - pad, RCX + RIN + pad, RCY + RIN + pad],
                   fill=(15, 16, 18, alpha))
    base = Image.alpha_composite(base.convert('RGBA'), ov).convert('RGB')
    d = ImageDraw.Draw(base)
    ob = [RCX - ROUT, RCY - ROUT, RCX + ROUT, RCY + ROUT]
    ib = [RCX - RIN, RCY - RIN, RCX + RIN, RCY + RIN]
    d.arc(ob, 0, 360, fill=mix(RULE, GROUND, .85), width=13)
    d.arc(ib, 0, 360, fill=mix(RULE, GROUND, .55), width=11)
    if prog > 0.002:
        bloom(d, ob, -90, -90 + 360 * prog, MAP_PROVING, 13)
    if kfold > 0.002:
        bloom(d, ib, -90, -90 + 360 * kfold, MAP_FOLDING, 11)
    cnt = str(done_n)
    d.text((RCX - d.textlength(cnt, font=fo['ring']) / 2, RCY - 76), cnt, font=fo['ring'], fill=hx(OK))
    # 'NONE LATE' meant nothing to a viewer. The centre now says what the fleet is doing right now:
    # a live block timer while working, a countdown to the next block while idle.
    if idle:
        mid_s, mid_c = 'AHEAD OF THE CHAIN', mix(OK, GROUND, .95)
        low_s = f'NEXT BLOCK IN {mmss(eta)}'
    elif cur:
        mid_s, mid_c = ('FOLDING' if kfold > 0 else 'PROVING'), mix(
            MAP_FOLDING if kfold > 0 else MAP_PROVING, GROUND, .95)
        low_s = f'{mmss(el)} ON THIS BLOCK'
    else:
        mid_s, mid_c, low_s = 'STANDING BY', hx(DIM), 'WAITING FOR A BLOCK'
    for s, f_, dy, c_ in ((f'/ {WINDOW} TODAY', 'lab', 22, mix(TEXT, GROUND, .7)),
                          (mid_s, 'mid', 48, mid_c),
                          (low_s, 'small', 78, hx(DIM))):
        d.text((RCX - d.textlength(s, font=fo[f_]) / 2, RCY + dy), s, font=fo[f_], fill=c_)
    # ⛔ a bare grey 'FOLDING' with no number is a label with no value — draw each arc's caption only
    # when that arc exists.
    def ring_label(text, y, col):
        # ⛔ These two captions sit OUTSIDE the ring (ROUT±34) while the medallion backing now stops
        # at RIN+34, so shrinking the backing left the traces running straight through them and they
        # became unreadable. Each caption carries its own small plate.
        tw = d.textlength(text, font=fo['small'])
        d.rectangle([RCX - tw / 2 - 10, y - 5, RCX + tw / 2 + 10, y + 19], fill=hx(GROUND))
        d.text((RCX - tw / 2, y), text, font=fo['small'], fill=col)

    if prog > 0.002:
        ring_label(f'PROVING {prog*100:.0f}%', RCY - ROUT - 34, mix(MAP_PROVING, GROUND, .95))
    if kfold > 0.002:
        ring_label(f'FOLDING {kfold*100:.0f}%', RCY + ROUT + 18, mix(MAP_FOLDING, GROUND, .95))

    # ---------------- the join tree: leaves are cards that finished proving
    for y, ok in leaves:
        lc = OK if ok else RULE
        d.line([LX1, y, TX0, y], fill=mix(lc, GROUND, .26), width=1)
        d.ellipse([TX0 - 4, y - 4, TX0 + 4, y + 4], fill=mix(lc, GROUND, .9))
    # ⛔ The tree used to be drawn ONLY as folds completed, so for ~70% of a tip day the 420px
    # between the lanes and the map was empty except an orphan column of leaf dots. The structure is
    # now always present but unlit; a join LIGHTS UP as it completes, which is the behaviour asked
    # for ("each line should only appear between points when it completes that single fold") without
    # leaving a hole in the frame the rest of the time.
    level, xx = ([y for y, _ in leaves] or [RCY]), TX0
    depth = max(1, math.ceil(math.log2(max(2, len(leaves) or 2))))
    dx = (TX1 - TX0) / depth
    for lv in range(depth):
        nxt, pull = [], (lv + 1) / depth
        njoin = max(1, math.ceil(len(level) / 2))
        for i in range(0, len(level), 2):
            pair = level[i:i + 2]
            ym = sum(pair) / len(pair) * (1 - pull) + RCY * pull
            x2 = xx + dx
            lit = kfold >= (lv + (i / 2 + 1) / njoin) / depth
            for y in pair:
                d.line([xx, y, x2, ym],
                       fill=mix(FOLD, GROUND, .8) if lit else mix(RULE, GROUND, .40),
                       width=2 if lit else 1)
            d.ellipse([x2 - 4, ym - 4, x2 + 4, ym + 4],
                      fill=mix(FOLD, GROUND, .95) if lit else mix(RULE, GROUND, .55))
            nxt.append(ym)
        level, xx = nxt, xx + dx
    if kfold >= 1.0:
        bloom(d, [TX1 - 13, RCY - 13, TX1 + 13, RCY + 13], 0, 360, OK, 6)

    # ---------------- right: the last 144 heights at the tip
    # The window follows the WORK, not the tip. A backfill card at 75,449 against a tip of 967,328
    # renders 144 empty cells — the same dead-window bug already found and fixed in the HTML build.
    # Flips to tip mode by itself as soon as any block inside the tip window shows up.
    # ⛔ The window used to be anchored to the CHAIN TIP (lo = tip-143), so the run's own blocks —
    # always the newest ones — piled into the bottom rows while the top two thirds showed heights
    # from before the run began. It read as a mostly-broken grid. Anchor to the RUN instead: cell 0
    # is the first block this run proved, and the map fills left-to-right, top-to-bottom across the
    # day. The empty cells then mean "the rest of the day", which is the story.
    hs = [b['h'] for b in blocks]
    at_tip = bool(tip) and any(h > tip - WINDOW for h in hs)
    lo = min(hs) if hs else (tip - WINDOW + 1 if tip else 1)
    if hs and max(hs) - lo >= WINDOW:       # a run longer than the window slides to keep the tip
        lo = max(hs) - WINDOW + 1
    hi = lo + WINDOW - 1
    by_h = {b['h']: b for b in blocks}
    # the ring already says "N / 144 TODAY" — printing the same fraction here made it twice on one frame
    d.text((GX0, GY0 - 26),
           f'BLOCKS {lo:,}–{hi:,}' + (' · AT THE TIP' if at_tip else ' · BACKFILL'),
           font=fo['small'], fill=mix(MAP_DONE, GROUND, .9) if at_tip else hx(FAINT))
    for i in range(WINDOW):
        h = lo + i
        cx, cy = cell_xy(i)
        b = by_h.get(h)
        if b and b.get('done'):
            col = mix(MAP_DONE, GROUND, .92)
        elif b and h == live_h:
            col = mix(MAP_FOLDING if kfold > 0 else MAP_PROVING, GROUND, .95)
        elif b:
            col = mix(MAP_PROVING, GROUND, .6)
        else:
            col = mix(TEXT, GROUND, .07)
        d.rectangle([cx, cy, cx + GCELL, cy + GCELL], fill=col)
    # ---------------- the pulse: a finished block travels from the tree to its own cell
    # Ported from the film (tip24c.py), which had it and this renderer never did. It is the only
    # thing on the frame that marks the MOMENT a block finishes: every other element shows a state,
    # so a block completing looked identical to a block that had completed some time ago.
    #
    # ⛔ IT NEEDS A TIME, AND `done` IS A BOOLEAN. collect.py carries `done_at` (the last sample for
    # that height) for exactly this. A snapshot from an older collector has no `done_at`, so the
    # pulse simply does not fire rather than the frame failing.
    #
    # ⛔ ONLY FOR A BLOCK THAT HAS A CELL. Outside [lo, hi] there is nowhere for it to land and
    # cell_xy would place it over some other block's square.
    # ⚠ Guarded against a card clock that runs ahead of ours: a negative age is not a fresh block.
    for b in blocks:
        t_done = b.get('done_at')
        if not t_done or not (lo <= b['h'] <= hi):
            continue
        age = now - t_done
        if not (0.0 <= age < PULSE_S):
            continue
        f = age / PULSE_S
        cx, cy = cell_xy(b['h'] - lo)
        tx_, ty_ = cx + GCELL / 2, cy + GCELL / 2
        px, py = TX1 + (tx_ - TX1) * f, RCY + (ty_ - RCY) * f
        d.line([TX1, RCY, px, py], fill=mix(OK, GROUND, .18))
        r_ = 7 - 3 * f
        d.ellipse([px - r_, py - r_, px + r_, py + r_], fill=mix(OK, GROUND, .95))

    kx = GX0
    for lbl, c_ in (('proving', MAP_PROVING), ('folding', MAP_FOLDING), ('done', MAP_DONE)):
        d.rectangle([kx, GKEY_Y + 2, kx + 11, GKEY_Y + 12], fill=mix(c_, GROUND, .92))
        d.text((kx + 18, GKEY_Y), lbl, font=fo['small'], fill=hx(DIM))
        kx += 18 + d.textlength(lbl, font=fo['small']) + 28

    # ---------------- the phase tile: what the run says it is doing
    # ⛔ A LIVE FLEET DOING NOTHING LOOKS EXACTLY LIKE A DEAD FEED. Preparation is minutes of flat
    # traces and an empty dial -- indistinguishable on the frame from an idle fleet or a broken
    # collector. Three times in one evening the question was "why is the dashboard empty", and every
    # time the answer was "it is preparing". When the run says what it is doing, the frame says it.
    #
    # ⚠ It sits on the SUBTITLE line, right-aligned. The first attempt put it above the footer, where
    # it landed on top of the time-per-block bars and was unreadable against them.
    phase_txt = (snap.get('phase') or '').strip()
    if phase_txt:
        tw = d.textlength(phase_txt, font=fo['lab'])
        tx, ty = W - PAD - tw, 90
        # ⛔ THE BANNER MUST SAY WHICH KIND OF NEWS IT IS. This was hardcoded to ACCENT, so
        # "FAILED on block 741000" rendered in exactly the same orange as "PROVING block 741000" --
        # verified pixel-identical, (235,140,25) for all three. A failed run was indistinguishable
        # from normal progress at a glance, which on an overnight run is the only glance it gets.
        tone = phase_tone(phase_txt)        # ⚠ NOT `base`: that name is the PIL image
        d.rectangle([tx - 12, ty - 5, tx + tw + 8, ty + 21], fill=mix(tone, GROUND, .12))
        d.rectangle([tx - 12, ty - 5, tx - 9, ty + 21], fill=mix(tone, GROUND, .95))
        d.text((tx, ty), phase_txt, font=fo['lab'], fill=mix(tone, GROUND, .95))

    # ---------------- stats (cost is real: card-hours x RunPod price)
    if idle:
        stat(d, fo, PAD, 'NEXT BLOCK', mmss(eta), mix(MAP_DONE, GROUND, .9), sub='fleet idle')
    elif cur:
        stat(d, fo, PAD, 'FOLDING' if kfold > 0 else 'PROVING', mmss(el),
             mix(MAP_FOLDING if kfold > 0 else MAP_PROVING, GROUND, .95))
    else:
        stat(d, fo, PAD, 'WAITING', '—', hx(FAINT))
    # while the fleet is idle the block is finished — "THIS BLOCK" kept ticking up against a block
    # nobody was working on
    stat(d, fo, 372, 'LAST BLOCK' if idle else 'THIS BLOCK',
         f'${cost_now:.2f}' if cur else '—', hx(TEXT))
    stat(d, fo, 700, 'FLEET', f'{len(up)}/{len(cards)} up',
         fleet_colour(len(up), len(cards)),
         sub=fleet_sub(len(up), len(cards), fleet.get('cost_hr', 0)))
    # ⛔ was CHAIN pct/proven — the BACKFILL campaign's progress, which never moves during a tip run
    # and is hardcoded under --demo. The run's own headline instead, from measured block totals:
    # 30 cards close a block in ~194 s against bitcoin's 600 s.
    if ahead > 0:
        # ⛔ This was hardcoded green. A 3-card fleet takes 1,863 s against the chain's 600 s — 0.3x,
        # i.e. LOSING — and rendered as a green success. The headline figure must be able to say the
        # fleet is behind, or it is a stat that cannot fail.
        keeping = ahead >= 1.0
        stat(d, fo, 1180, 'AHEAD OF CHAIN' if keeping else 'BEHIND THE CHAIN', f'{ahead:.1f}×',
             mix(MAP_DONE if keeping else RED, GROUND, .9),
             sub=f'{med:.0f}s per block · 600s between blocks')
    else:
        stat(d, fo, 1180, 'AHEAD OF CHAIN', '—', hx(FAINT), sub='no block timed yet')

    # ---------------- bars: measured prove/fold per block
    d.text((BX0, BHEAD), 'TIME PER BLOCK', font=fo['small'], fill=hx(FAINT))
    lg = BX0 + 190
    d.rectangle([lg, BHEAD + 2, lg + 11, BHEAD + 12], fill=mix(MAP_PROVING, GROUND, .95))
    d.text((lg + 18, BHEAD), 'proving', font=fo['small'], fill=hx(DIM))
    d.rectangle([lg + 100, BHEAD + 2, lg + 111, BHEAD + 12], fill=mix(MAP_FOLDING, GROUND, .95))
    d.text((lg + 118, BHEAD), 'folding', font=fo['small'], fill=hx(DIM))
    timed = [b for b in blocks if b.get('prove_s')][-WINDOW:]
    ymax = max([b['prove_s'] for b in timed] + [b.get('fold_s') or 0 for b in timed] + [60]) * 1.04
    d.line([BX0, BY1, BX1, BY1], fill=mix(RULE, GROUND, 1.0), width=2)
    # the step scales with the range: a fixed 60 s drew 18 labels down the axis at a 1,040 s bar and
    # they overprinted into a grey smear
    step = next((x for x in (15, 30, 60, 120, 300, 600, 900, 1800, 3600) if ymax / x <= 6), 3600)
    for s in range(step, int(ymax), step):
        gy = BY1 - BH * s / ymax
        d.line([BX0 + 40, gy, BX1, gy], fill=mix(RULE, GROUND, .6))
        lbl = f'{s // 60}m' if step >= 60 else f'{s}s'
        d.text((BX0, gy - 7), lbl, font=fo['small'], fill=hx(FAINT))
    # GUT: the first bars used to start at BX0 and paint over the y-axis labels, clipping the
    # leading digit ("15m" rendered as "5m")
    GUT = 44
    slot = (BX1 - (BX0 + GUT)) / WINDOW
    for i, b in enumerate(timed):
        x0 = BX0 + GUT + i * slot
        ps, fs = b.get('prove_s') or 0, b.get('fold_s') or 0
        if ps:
            d.rectangle([x0 + 1, BY1 - BH * ps / ymax, x0 + 5.4, BY1], fill=mix(MAP_PROVING, GROUND, .92))
        if fs:
            d.rectangle([x0 + 6.2, BY1 - BH * fs / ymax, x0 + 10.6, BY1], fill=mix(MAP_FOLDING, GROUND, .92))
    # the axis is the DAY (144 slots), so an empty right-hand side reads as "hours still to run"
    # rather than as a chart that failed to draw. Without these ticks it just looked broken.
    for q in range(1, 4):
        qx = BX0 + GUT + (WINDOW * q / 4) * slot
        d.line([qx, BY1, qx, BY1 + 5], fill=mix(RULE, GROUND, 1.0))
        d.text((qx - 12, BY1 + 8), f'{q * 6}h', font=fo['small'], fill=hx(FAINT))
    d.text((BX0 + GUT + WINDOW * slot - 20, BY1 + 8), '24h', font=fo['small'], fill=hx(FAINT))
    if not timed:
        d.text((BX0 + 60, BY1 - BH / 2), 'waiting for the first block to complete',
               font=fo['lab'], fill=hx(FAINT))

    # ---------------- footer
    # ⛔ AGE MUST COME FROM THE WALL CLOCK, NOT FROM THE SNAPSHOT'S OWN `t`.
    # This read `now - snap['t']`, and `now` is SET to `snap['t']` at the top of draw_live -- so the
    # age was ZERO by construction in every frame ever rendered. Twelve minutes after the collector
    # died the footer still read "live · 3 cards streaming · updated 0s ago".
    #
    # On an unattended 24-hour run that is the worst failure the frame can have: a dead feed that
    # looks perfectly healthy. Nobody checks a dashboard that always says it is fine.
    age = feed_age(snap, time.time())
    left = ('DEMO DATA — no pods are running' if snap.get('demo')      # no emoji: DejaVu draws tofu
            else feed_line(len(up), age))
    d.text((PAD, FOOT_Y), left, font=fo['small'],
           fill=mix(RED, GROUND, .85) if (snap.get('demo') or age >= STALE_S) else hx(FAINT))
    note = 'GPU power is measured at 1 Hz on each card'
    d.text((W - PAD - d.textlength(note, font=fo['small']), FOOT_Y), note,
           font=fo['small'], fill=hx(FAINT))
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snap', default='snapshot.json')
    ap.add_argument('--out', default='frame.png')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--interval', type=float, default=1.0)
    a = ap.parse_args()
    fo = fonts()
    while True:
        try:
            with open(a.snap) as fh:
                snap = json.load(fh)
            img = draw_live(snap, fo)
            tmp = a.out + '.tmp.png'
            img.save(tmp)
            os.replace(tmp, a.out)      # atomic, so a viewer never sees a torn frame
            print(f"[{time.strftime('%H:%M:%S')}] {a.out} · {len(snap.get('cards', []))} cards", flush=True)
        except FileNotFoundError:
            print(f'waiting for {a.snap} …', flush=True)
        except Exception as e:
            print(f'render error: {e}', flush=True)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == '__main__':
    main()
