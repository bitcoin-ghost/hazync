#!/usr/bin/env python3
"""Render the poster (SVG + PNG) and the animated GIF from run_data.json.
No browser, no ffmpeg: SVG is written directly, raster comes from PIL."""
import json, math, sys, os
from PIL import Image, ImageDraw, ImageFont

DATA = sys.argv[1] if len(sys.argv) > 1 else 'run4_data.json'
R = json.load(open(DATA))
C = R['card_list']; W = R['wall_total_s']

GROUND='#0f1012'; PANEL='#16181b'; RULE='#2e3135'; SOFT='#212427'
TEXT='#e6e4de'; DIM='#8b8a84'; FAINT='#55544f'; ACCENT='#f7931a'; OK='#5cc77e'
SITE={'US':'#f7931a','CA':'#e8a94f','IS':'#6fb3c4','TW':'#d4694f','NO':'#78c4a0','FR':'#b8879b'}
PMAX=360.0
FONT='/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONTB='/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

def hx(c): c=c.lstrip('#'); return tuple(int(c[i:i+2],16) for i in (0,2,4))
def mix(c,bg,a):
    c,bg=hx(c),hx(bg); return tuple(int(bg[i]+(c[i]-bg[i])*a) for i in range(3))
def mmss(s): return f"{int(s//60)}:{int(s%60):02d}"
def fmt(n): return f"{n:,}"

# ---------------------------------------------------------------- layout (logical 1200x676)
LW,LH = 1200,840
PAD=34
CHART_X0,CHART_X1 = PAD+56, 830          # trace drawing area
ROWTOP, ROW, TRACE = 252, 16.5, 12.0
SIDE_X = 862

def tx(t): return CHART_X0 + (t/W)*(CHART_X1-CHART_X0)

def draw_frame(d, S, upto=None, fonts=None):
    """upto=None -> finished state. S = scale factor. d = ImageDraw on a scaled canvas."""
    f9,f10,f11,f13,f16,f20,f34,fb = fonts
    def X(v): return v*S
    def line(a,b,c,w=1): d.line([X(a[0]),X(a[1]),X(b[0]),X(b[1])],fill=c,width=max(1,int(w*S)))
    def text(p,s,fo,c,anchor='la'): d.text((X(p[0]),X(p[1])),s,font=fo,fill=c,anchor=anchor)

    d.rectangle([0,0,LW*S,LH*S], fill=GROUND)

    # ---- masthead
    text((PAD,26),'HAZYNC · RUN 4 · 2026-09-10',f10,DIM)
    text((PAD,44),'A Bitcoin block, proved by 27 rented GPUs',f34,TEXT)
    text((PAD,92),f"block {fmt(R['block'])}  ·  {fmt(R['txs'])} transactions  ·  "
                  f"{fmt(R['inputs'])} inputs  ·  {fmt(R['total_segments'])} segments  ·  "
                  f"{R['cards']}× {R['gpu']}",f11,DIM)
    line((PAD,116),(LW-PAD,116),RULE)
    # the three figures get their own strip -- a wide headline cannot collide with them here
    figs=[(f"{W/60:.2f} min",'WALL CLOCK',ACCENT),
          (f"${R['cost_usd']:.2f}",'GPU COST',TEXT),
          (f"{R['energy_kwh']:.3f} kWh",'GPU ENERGY',TEXT),
          (f"{R['retries_119']}",'#119 FAULTS',OK)]
    fx=PAD
    for v,k,col in figs:
        text((fx,132),v,f34,col); text((fx,178),k,f9,FAINT)
        fx+=max(d.textlength(v,font=f34)/S, d.textlength(k,font=f9)/S)+56
    line((PAD,198),(LW-PAD,198),RULE)

    # ---- aggregate window shading + band
    agx0,agx1 = tx(293.7), tx(W)
    ytop,ybot = ROWTOP-8, ROWTOP+27*ROW+4
    d.rectangle([X(agx0),X(ytop),X(agx1),X(ybot)], fill=mix(OK,GROUND,.04))
    by, bh = ROWTOP+9*ROW, ROW*7
    text((agx0+9,by-17),'AGGREGATE — 241.2 s',f10,OK)
    cur=293.7+(W-293.7-241.2)/2
    for lab,dur,col in (('execution',38.9,'#6fb3c4'),('worker wall',73.6,'#e8a94f'),
                        ('assembly',128.7,OK)):
        sx,sw=tx(cur),tx(cur+dur)-tx(cur)
        d.rectangle([X(sx),X(by),X(sx+sw),X(by+bh)],fill=mix(col,GROUND,.13),
                    outline=mix(col,GROUND,.5),width=max(1,int(S)))
        text((sx+sw/2,by+bh/2-11),lab,f10,col,anchor='ma')
        text((sx+sw/2,by+bh/2+3),f'{dur} s',f10,DIM,anchor='ma')
        cur+=dur
    text((agx0+9,by+bh+8),'585 segments → 1, depth-4 join tree',f9,FAINT)
    text((agx0+9,by+bh+20),'584 pushed to 27 attached workers — not sampled',f9,FAINT)

    # ---- phase markers
    _rows=[[],[],[]]
    def _place(a,b):
        """First row that has no horizontal overlap. Tracking only ONE row put RECEIPTS STAGED and
        AGGREGATE OPEN -- 40 s apart -- on the same line, on top of each other."""
        for i,occ in enumerate(_rows):
            if all(not (a < u1 and u0 < b) for u0,u1 in occ):
                occ.append((a,b)); return i
        _rows[-1].append((a,b)); return len(_rows)-1
    for p in R['phases']:
        px=tx(p['at']); last = p['at']==W
        col = OK if last else RULE
        n=int((ybot-ytop)//6)
        if last: line((px,ytop),(px,ybot),col,1)
        else:
            for i in range(n):
                y=ytop+i*6; line((px,y),(px,y+3),col,1)
        lab=p['label'].upper()
        w=d.textlength(lab,font=f9)/S
        rowy = ROWTOP-43 + _place(px,px+w+10)*13
        text((px+4,rowy),lab,f9,OK if last else DIM)

    # ---- ridgeline
    for i,c in enumerate(C):
        y0=ROWTOP+i*ROW+TRACE; col=SITE.get(c['site'],ACCENT)
        line((CHART_X0,y0),(CHART_X1,y0),SOFT,1)
        text((CHART_X0-8,y0-8),f"{c['chunk']:02d} {c['site']}",f9,DIM,anchor='ra')
        n=len(c['t']) if upto is None else sum(1 for v in c['t'] if v<=upto)
        if n<2: continue
        pts=[(tx(c['t'][k]), y0-min(1.0,c['w'][k]/PMAX)*TRACE) for k in range(n)]
        poly=[(X(pts[0][0]),X(y0))]+[(X(a),X(b)) for a,b in pts]+[(X(pts[-1][0]),X(y0))]
        d.polygon(poly, fill=mix(col,GROUND,.17))
        d.line([(X(a),X(b)) for a,b in pts], fill=col, width=max(1,int(1.2*S)), joint='curve')
        if upto is None or c['end_s']<=upto:
            ex=tx(c['end_s']); r=1.9*S
            d.ellipse([X(ex)-r,X(y0)-r,X(ex)+r,X(y0)+r],fill=col)
            text((ex+7,y0-5),f"{c['wall_s']:.0f}s",f9,DIM)

    # ---- time axis
    for t in range(0,int(W)+1,60):
        text((tx(t),ROWTOP+27*ROW+10),mmss(t),f9,FAINT,anchor='ma')
    text((CHART_X0,ROWTOP+27*ROW+26),'ELAPSED — GPU POWER PER CARD, 1 Hz',f9,FAINT)

    # ---- ring
    cx,cy,rr = SIDE_X+134, ROWTOP+78, 66
    d.ellipse([X(cx-rr),X(cy-rr),X(cx+rr),X(cy+rr)],outline=SOFT,width=max(1,int(8*S)))
    t_now = W if upto is None else min(upto,W)
    def arc(a0,a1,col):
        if a1<=a0: return
        d.arc([X(cx-rr),X(cy-rr),X(cx+rr),X(cy+rr)],start=-90+360*a0/W,end=-90+360*a1/W,
              fill=col,width=max(1,int(8*S)))
    arc(0,min(t_now,287.9),ACCENT)
    if t_now>293.7: arc(293.7,t_now,OK)
    done = t_now>=W
    text((cx,cy-14),f"{W/60:.2f} min" if done else mmss(t_now),f20,TEXT,anchor='ma')
    text((cx,cy+12),'VERIFIED' if done else 'PROVING',f10,OK if done else DIM,anchor='ma')

    # ---- side stats
    sy=ROWTOP+164
    def block(title,rows,sy):
        text((SIDE_X,sy),title,f9,FAINT); sy+=16
        for k,v,col in rows:
            text((SIDE_X,sy),k,f11,DIM); text((LW-PAD,sy),v,f11,col,anchor='ra'); sy+=17
        return sy+14
    ns=len(R['sites'])
    sy=block('THE FLEET',[('cards',f"{R['cards']}× {R['gpu']}",TEXT),
        ('countries',str(ns),TEXT),('segments proved',fmt(R['total_segments']),TEXT),
        ('#119 faults',str(R['retries_119']),OK)],sy)
    S2=R['chunk_stats']
    sy=block('CHUNK PHASE',[('fastest',f"{S2['min_s']:.0f} s",TEXT),
        ('median',f"{S2['median_s']:.0f} s",TEXT),('slowest',f"{S2['max_s']:.0f} s",TEXT),
        ('straggler ratio',f"{S2['straggler']:.3f}",ACCENT)],sy)
    # site legend
    text((SIDE_X,sy),'SITES',f9,FAINT); sy+=16
    for s,n in R['sites'].items():
        d.rectangle([X(SIDE_X),X(sy+3),X(SIDE_X+8),X(sy+11)],fill=SITE[s])
        text((SIDE_X+14,sy),R['site_names'].get(s,s),f11,DIM)
        text((LW-PAD,sy),str(n),f11,TEXT,anchor='ra'); sy+=17

    # ---- digests
    fy=LH-PAD-52
    line((PAD,fy-12),(LW-PAD,fy-12),RULE)
    for i,(k,v) in enumerate([('JOURNAL DIGEST',R['journal_digest']),
                              ('GUEST METHOD_ID',R['method_id'])]):
        text((PAD+i*600,fy),k,f9,FAINT); text((PAD+i*600,fy+13),v,f10,TEXT)
    text((PAD,fy+32),'The receipt verifies against the canonical guest id. The wall clock does not — '
                     'it is attested from logs and 1 Hz telemetry.',f9,FAINT)

def fonts_at(S):
    g=lambda p,s: ImageFont.truetype(p,int(s*S))
    return (g(FONT,9),g(FONT,10),g(FONT,11),g(FONT,13),g(FONT,16),g(FONTB,20),g(FONTB,34),g(FONTB,13))

# ---------------------------------------------------------------- poster
S=2
img=Image.new('RGB',(LW*S,LH*S),GROUND); d=ImageDraw.Draw(img)
draw_frame(d,S,None,fonts_at(S))
img.save('run4-poster.png')
print('run4-poster.png', img.size, f"{os.path.getsize('run4-poster.png')/1024:.0f} KB")

# ---------------------------------------------------------------- gif
GS=1.0; NF=132; HOLD=18
frames=[]
fo=fonts_at(GS)
for i in range(NF):
    t=(i/(NF-HOLD))*W if i < NF-HOLD else None
    im=Image.new('RGB',(int(LW*GS),int(LH*GS)),GROUND)
    draw_frame(ImageDraw.Draw(im),GS,t,fo)
    frames.append(im.convert('P',palette=Image.ADAPTIVE,colors=64))
frames[0].save('run4.gif',save_all=True,append_images=frames[1:],duration=90,loop=0,optimize=True,disposal=2)
print('run4.gif', frames[0].size, f"{NF} frames, {os.path.getsize('run4.gif')/1024/1024:.2f} MB")
