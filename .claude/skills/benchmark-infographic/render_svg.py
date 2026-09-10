#!/usr/bin/env python3
"""Vector poster. Same layout and numbers as render.py, emitted as SVG so it stays crisp at any size."""
import json,sys
R=json.load(open(sys.argv[1] if len(sys.argv)>1 else 'run4_data.json'))
C=R['card_list']; W=R['wall_total_s']
GROUND='#0f1012';RULE='#2e3135';SOFT='#212427';TEXT='#e6e4de';DIM='#8b8a84';FAINT='#55544f'
ACCENT='#f7931a';OK='#5cc77e'
SITE={'US':'#f7931a','CA':'#e8a94f','IS':'#6fb3c4','TW':'#d4694f','NO':'#78c4a0','FR':'#b8879b'}
PMAX=360.0; LW,LH=1200,840; PAD=34
X0,X1=PAD+56,830; ROWTOP,ROW,TRACE=252,16.5,12.0; SIDE=862
MONO='IBM Plex Mono, DejaVu Sans Mono, monospace'
def tx(t): return X0+(t/W)*(X1-X0)
def mmss(s): return f"{int(s//60)}:{int(s%60):02d}"
def fmt(n): return f"{n:,}"
def esc(s): return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
o=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {LW} {LH}" width="{LW}" height="{LH}" '
   f'font-family="{MONO}"><rect width="{LW}" height="{LH}" fill="{GROUND}"/>']
def T(x,y,s,size,fill,anchor='start',weight='400'):
    o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
             f'text-anchor="{anchor}" font-weight="{weight}">{esc(s)}</text>')
def L(x1,y1,x2,y2,st,w=1,dash=''):
    da=f' stroke-dasharray="{dash}"' if dash else ''
    o.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{st}" stroke-width="{w}"{da}/>')
T(PAD,34,'HAZYNC · RUN 4 · 2026-09-10',10,DIM)
T(PAD,74,'A Bitcoin block, proved by 27 rented GPUs',34,TEXT,weight='700')
T(PAD,100,f"block {fmt(R['block'])}  ·  {fmt(R['txs'])} transactions  ·  {fmt(R['inputs'])} inputs  ·  "
          f"{fmt(R['total_segments'])} segments  ·  {R['cards']}× {R['gpu']}",11,DIM)
L(PAD,116,LW-PAD,116,RULE)
fx=PAD
for v,k,col in ((f"{W/60:.2f} min",'WALL CLOCK',ACCENT),(f"${R['cost_usd']:.2f}",'GPU COST',TEXT),
                (f"{R['energy_kwh']:.3f} kWh",'GPU ENERGY',TEXT),(str(R['retries_119']),'#119 FAULTS',OK)):
    T(fx,166,v,34,col,weight='700'); T(fx,186,k,9,FAINT); fx+=max(len(v)*20.5,len(k)*5.4)+56
L(PAD,198,LW-PAD,198,RULE)
agx0,ybot=tx(293.7),ROWTOP+27*ROW+4
o.append(f'<rect x="{agx0:.1f}" y="{ROWTOP-8:.1f}" width="{tx(W)-agx0:.1f}" height="{ybot-ROWTOP+8:.1f}" fill="{OK}" fill-opacity=".04"/>')
by,bh=ROWTOP+9*ROW,ROW*7
T(agx0+9,by-17,'AGGREGATE — 241.2 s',10,OK)
cur=293.7+(W-293.7-241.2)/2
for lab,dur,col in (('execution',38.9,'#6fb3c4'),('worker wall',73.6,'#e8a94f'),('assembly',128.7,OK)):
    sx,sw=tx(cur),tx(cur+dur)-tx(cur)
    o.append(f'<rect x="{sx:.1f}" y="{by:.1f}" width="{sw:.1f}" height="{bh:.1f}" fill="{col}" '
             f'fill-opacity=".13" stroke="{col}" stroke-opacity=".5"/>')
    T(sx+sw/2,by+bh/2-4,lab,10,col,'middle'); T(sx+sw/2,by+bh/2+11,f'{dur} s',10,DIM,'middle'); cur+=dur
T(agx0+9,by+bh+16,'585 segments → 1, depth-4 join tree',9,FAINT)
T(agx0+9,by+bh+29,'584 pushed to 27 attached workers — not sampled',9,FAINT)
rows=[[],[],[]]
for p in R['phases']:
    px=tx(p['at']); last=p['at']==W; lab=p['label'].upper(); w=len(lab)*5.4+10
    L(px,ROWTOP-8,px,ybot,OK if last else RULE,1,'' if last else '2 4')
    r=next((i for i,occ in enumerate(rows) if all(not(px<u1 and u0<px+w) for u0,u1 in occ)),2)
    rows[r].append((px,px+w)); T(px+4,ROWTOP-43+r*13,lab,9,OK if last else DIM)
for i,c in enumerate(C):
    y0=ROWTOP+i*ROW+TRACE; col=SITE.get(c['site'],ACCENT)
    L(X0,y0+.5,X1,y0+.5,SOFT)
    T(X0-8,y0-1,f"{c['chunk']:02d} {c['site']}",9,DIM,'end')
    pts=' '.join(f"{tx(c['t'][k]):.1f},{y0-min(1.0,c['w'][k]/PMAX)*TRACE:.1f}" for k in range(len(c['t'])))
    if not pts: continue
    o.append(f'<polygon points="{tx(c["t"][0]):.1f},{y0:.1f} {pts} {tx(c["t"][-1]):.1f},{y0:.1f}" fill="{col}" fill-opacity=".17"/>')
    o.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.2" stroke-linejoin="round"/>')
    ex=tx(c['end_s']); o.append(f'<circle cx="{ex:.1f}" cy="{y0:.1f}" r="1.9" fill="{col}"/>')
    T(ex+7,y0+3,f"{c['wall_s']:.0f}s",9,DIM)
for t in range(0,int(W)+1,60): T(tx(t),ROWTOP+27*ROW+18,mmss(t),9,FAINT,'middle')
T(X0,ROWTOP+27*ROW+34,'ELAPSED — GPU POWER PER CARD, 1 Hz',9,FAINT)
cx,cy,rr=SIDE+134,ROWTOP+78,66; import math
Cc=2*math.pi*rr
o.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{SOFT}" stroke-width="8"/>')
o.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{ACCENT}" stroke-width="8" '
         f'transform="rotate(-90 {cx} {cy})" stroke-dasharray="{Cc*287.9/W:.1f} {Cc:.1f}"/>')
o.append(f'<circle cx="{cx}" cy="{cy}" r="{rr}" fill="none" stroke="{OK}" stroke-width="8" '
         f'transform="rotate(-90 {cx} {cy})" stroke-dasharray="{Cc*(W-293.7)/W:.1f} {Cc:.1f}" '
         f'stroke-dashoffset="{-Cc*293.7/W:.1f}"/>')
T(cx,cy+2,f"{W/60:.2f} min",22,TEXT,'middle',weight='700'); T(cx,cy+24,'VERIFIED',10,OK,'middle')
sy=ROWTOP+164
def blk(title,rws,sy):
    T(SIDE,sy,title,9,FAINT); sy+=16
    for k,v,col in rws: T(SIDE,sy,k,11,DIM); T(LW-PAD,sy,v,11,col,'end'); sy+=17
    return sy+14
S2=R['chunk_stats']
sy=blk('THE FLEET',[('cards',f"{R['cards']}× {R['gpu']}",TEXT),('countries',str(len(R['sites'])),TEXT),
    ('segments proved',fmt(R['total_segments']),TEXT),('#119 faults',str(R['retries_119']),OK)],sy)
sy=blk('CHUNK PHASE',[('fastest',f"{S2['min_s']:.0f} s",TEXT),('median',f"{S2['median_s']:.0f} s",TEXT),
    ('slowest',f"{S2['max_s']:.0f} s",TEXT),('straggler ratio',f"{S2['straggler']:.3f}",ACCENT)],sy)
T(SIDE,sy,'SITES',9,FAINT); sy+=16
for s_,n in R['sites'].items():
    o.append(f'<rect x="{SIDE}" y="{sy-8}" width="8" height="8" fill="{SITE[s_]}"/>')
    T(SIDE+14,sy,R['site_names'].get(s_,s_),11,DIM); T(LW-PAD,sy,str(n),11,TEXT,'end'); sy+=17
fy=LH-PAD-52; L(PAD,fy-12,LW-PAD,fy-12,RULE)
for i,(k,v) in enumerate((('JOURNAL DIGEST',R['journal_digest']),('GUEST METHOD_ID',R['method_id']))):
    T(PAD+i*600,fy,k,9,FAINT); T(PAD+i*600,fy+13,v,10,TEXT)
T(PAD,fy+32,'The receipt verifies against the canonical guest id. The wall clock does not — it is '
             'attested from logs and 1 Hz telemetry.',9,FAINT)
o.append('</svg>')
open('run4-poster.svg','w').write('\n'.join(o))
import os; print('run4-poster.svg', f"{os.path.getsize('run4-poster.svg')/1024:.0f} KB")
