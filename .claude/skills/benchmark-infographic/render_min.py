#!/usr/bin/env python3
"""Minimal poster: title block, then one row per card carrying its whole run -- proving in its site
colour, aggregation in green -- with the ring in front, dead centre."""
import json, os, sys
from PIL import Image, ImageDraw, ImageFont

R=json.load(open(sys.argv[1] if len(sys.argv)>1 else 'run4_data.json'))
C=R['card_list']; W=R['wall_total_s']; AGG=R['aggregate']
GROUND='#0f1012'; RULE='#22262a'; SOFT='#1b1e21'
TEXT='#e6e4de'; DIM='#8b8a84'; FAINT='#55544f'; ACCENT='#f7931a'; OK='#5cc77e'
SITE={'US':'#f7931a','CA':'#e8a94f','IS':'#6fb3c4','TW':'#d4694f','NO':'#78c4a0','FR':'#b8879b'}
AGGC='#5cc77e'; PMAX=360.0
F='/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FB='/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'

LW,LH=1600,900; PAD=56          # 16:9 for X
X0,X1=PAD,LW-PAD
TTOP,TROW,TRH=214,17.0,13.2
RR=118
OPACITY=0.25
FG=float(os.environ.get('FG_OPACITY','0.75'))   # title + ring opacity

def hx(c): c=c.lstrip('#'); return tuple(int(c[i:i+2],16) for i in (0,2,4))
def mix(c,bg,a):
    c,bg=hx(c),hx(bg); return tuple(int(bg[i]+(c[i]-bg[i])*a) for i in range(3))
def mmss(s): return f"{int(s//60)}:{int(s%60):02d}"
def fmt(n): return f"{n:,}"
def tx(t): return X0+(t/W)*(X1-X0)

def build(S=2, upto=None):
    img=Image.new('RGB',(int(LW*S),int(LH*S)),GROUND); d=ImageDraw.Draw(img)
    g=lambda p,s: ImageFont.truetype(p,max(1,int(s*S)))
    f11,f12,f13,f16,f17,f38,f44=g(F,11.5),g(F,12),g(FB,13),g(F,16),g(F,17),g(FB,38),g(FB,44)
    def T(x,y,s,fo,c,a='la'): d.text((x*S,y*S),s,font=fo,fill=c,anchor=a)
    def LN(x1,y1,x2,y2,c,w=1): d.line([x1*S,y1*S,x2*S,y2*S],fill=c,width=max(1,int(w*S)))
    t_now = W if upto is None else upto

    # ---- title line: name and the three headline figures, one centred line
    parts=[('Hazync zkVM bitcoin block proof',TEXT),(' : ',FAINT),
           (f"{W/60:.2f} min",ACCENT),(' · ',FAINT),
           (f"${R['cost_usd']:.2f}",TEXT),(' · ',FAINT),
           (f"{R['energy_kwh']:.3f} kWh",TEXT)]
    # auto-fit: the title is now 62 characters and overruns the frame at 44 px, so step the size
    # down until it fits rather than letting it run off the edge
    size=44
    while size>24:
        ft=g(F,size)
        if sum(d.textlength(p,font=ft)/S for p,_ in parts) <= (X1-X0): break
        size-=1
    ft=g(F,size)
    total=sum(d.textlength(p,font=ft)/S for p,_ in parts)
    cx=(LW-total)/2
    for p,col in parts:
        T(cx,58,p,ft,mix(col,GROUND,FG)); cx+=d.textlength(p,font=ft)/S
    T(LW/2,120,f"block {fmt(R['block'])} · {fmt(R['txs'])} transactions · {fmt(R['inputs'])} inputs · "
               f"{fmt(R['total_segments'])} segments · {R['cards']}× {R['gpu']}",f17,mix(DIM,GROUND,FG),'ma')
    LN(PAD,160,LW-PAD,160,RULE)

    # ---- section titles, each sitting over the span it names
    AW={w['w']:w for w in AGG['workers']}
    YROOT=TTOP+27*TROW/2                     # where the tree folds to a single receipt
    AGSPAN=max(w['wall_s'] for w in AGG['workers']); AGOFF=W-AGSPAN
    # The aggregate's own three phases are measured by the coordinator and they line up exactly with
    # what the workers report: they attach after execution, push for 73.6 s -- which is where their
    # logged work stops -- and then sit through 128.7 s of assembly. That assembly IS the join tree
    # folding 584 receipts into 1, and unlabelled it just reads as a dead half.
    AGST=W-AGG['total_s']                       # aggregate begins
    PUSH0=AGST+AGG['execution_s']               # workers attach and start pushing
    ASM0=PUSH0+AGG['worker_wall_s']             # join tree begins
    T(X0,190,'PROVING',f13,ACCENT)
    T(tx(AGST),190,'AGGREGATION',f13,AGGC)
    for xb in (PUSH0,ASM0):
        LN(tx(xb),TTOP-6,tx(xb),TTOP+27*TROW+4,mix(AGGC,GROUND,.22))
    # ⛔ the execution window is only 38.9 s wide -- a long label there runs straight into the next
    # ---- one row per card: proving, then aggregating
    for i,c in enumerate(C):
        y0=TTOP+i*TROW+TRH; col=SITE.get(c['site'],ACCENT)
        n=len(c['t']) if upto is None else sum(1 for v in c['t'] if v<=t_now)
        if n>=2:
            pts=[(tx(c['t'][k]),y0-min(1.0,c['w'][k]/PMAX)*TRH) for k in range(n)]
            d.polygon([(pts[0][0]*S,y0*S)]+[(a*S,b*S) for a,b in pts]+[(pts[-1][0]*S,y0*S)],
                      fill=mix(col,GROUND,OPACITY*.28))
            d.line([(a*S,b*S) for a,b in pts],fill=mix(col,GROUND,OPACITY),width=max(1,int(1.6*S)))
        w=AW.get(c['chunk'])
        if not w: continue          # the coordinator ran seg-serve, not a worker
        el = AGSPAN if upto is None else t_now-AGOFF
        if el<=0: continue
        last=max(p[0] for p in w['measured']) if w['measured'] else 0
        b=min(last,el)
        if b>0: d.line([tx(AGOFF)*S,y0*S,tx(AGOFF+b)*S,y0*S],
                       fill=mix(AGGC,GROUND,.62),width=max(1,int(3.0*S)))
        if el>last:
            # attached-but-unlogged runs flat until assembly starts, then the rows CONVERGE: the
            # join tree folds every worker's work into one receipt. Both ends are measured -- 26
            # workers at assembly start, 1 receipt at VERIFIED -- the curve between is the tree's
            # shape, not a claim about when each individual join landed.
            gnow=AGOFF+min(w['wall_s'],el)
            flat_end=min(gnow,ASM0)
            if flat_end>AGOFF+last:
                d.line([tx(AGOFF+last)*S,y0*S,tx(flat_end)*S,y0*S],
                       fill=mix(AGGC,GROUND,.16),width=max(1,int(1.9*S)))
            if gnow>ASM0:
                pts=[]
                N=26
                for k in range(N+1):
                    gt=ASM0+(gnow-ASM0)*k/N
                    f=((gt-ASM0)/(W-ASM0))**2.4      # hold the rows, then rush to the root
                    pts.append((tx(gt), y0+(YROOT-y0)*f))
                d.line([(a*S,b*S) for a,b in pts],fill=mix(AGGC,GROUND,.22),
                       width=max(1,int(1.6*S)))
        for p in w['measured']:
            if p[0]<=el: d.line([tx(AGOFF+p[0])*S,(y0-4.4)*S,tx(AGOFF+p[0])*S,(y0+2.0)*S],
                                fill=mix(AGGC,GROUND,.85),width=max(1,int(S)))

    # the root: one receipt
    if t_now>=W:
        d.line([tx(W-6)*S,YROOT*S,tx(W)*S,YROOT*S],fill=mix(AGGC,GROUND,.95),width=max(1,int(3.0*S)))

    # ---- time axis
    ry=TTOP+27*TROW+10
    LN(X0,ry,X1,ry,RULE)
    for t in (0,60,120,180,240,300,360,420,480,W):
        LN(tx(t),ry,tx(t),ry+4,RULE)
        T(tx(t),ry+11,mmss(t),f11,FAINT,'ra' if t==W else ('la' if t==0 else 'ma'))

    # ---- ring, in front of the traces, dead centre
    CX=(X0+X1)/2; CY=TTOP+27*TROW/2
    ov=Image.new('RGBA',img.size,(0,0,0,0)); od=ImageDraw.Draw(ov)
    for k in range(16):
        rr=(RR+30)-k*2.0
        od.ellipse([(CX-rr)*S,(CY-rr)*S,(CX+rr)*S,(CY+rr)*S],
                   fill=hx(GROUND)+(int(255*(0.18+0.82*k/15)),))
    img=Image.alpha_composite(img.convert('RGBA'),ov).convert('RGB'); d=ImageDraw.Draw(img)
    d.ellipse([(CX-RR)*S,(CY-RR)*S,(CX+RR)*S,(CY+RR)*S],outline=mix(SOFT,GROUND,FG),width=max(1,int(16*S)))
    def arc(a0,a1,col):
        if a1>a0: d.arc([(CX-RR)*S,(CY-RR)*S,(CX+RR)*S,(CY+RR)*S],
                        start=-90+360*a0/W,end=-90+360*a1/W,fill=mix(col,GROUND,FG),width=max(1,int(16*S)))
    arc(0,min(t_now,287.9),ACCENT)
    if t_now>293.7: arc(293.7,min(t_now,W),OK)
    done=t_now>=W
    T(CX,CY-28,f"{W/60:.2f}" if done else mmss(t_now),f38,mix(TEXT,GROUND,FG),'ma')
    T(CX,CY+18,'MIN · VERIFIED' if done else 'PROVING',f13,mix(OK if done else DIM,GROUND,FG),'ma')

    # ---- credits, two per line: one left justified, one right
    ENTRIES=[('journal digest',R['journal_digest']),
             ('guest METHOD_ID',R['method_id']),
             ('receipt sha256',f"{R['receipt_sha']}  ({fmt(R['receipt_bytes'])} bytes)"),
             ('chain tip',R['tip_hash'])]
    for i,(label,value) in enumerate(ENTRIES):
        row,rightcol = i//2, i%2
        y = 742 + row*70
        x,anchor = (X1,'ra') if rightcol else (X0,'la')
        T(x,y,label,f12,ACCENT,anchor)
        T(x,y+24,value,f16,TEXT,anchor)
    return img

if __name__=='__main__':
    im=build(2); im.save(os.environ.get('OUT','run4-min.png'))
    o=os.environ.get('OUT','run4-min.png'); print(o,im.size,f"{os.path.getsize(o)/1024:.0f} KB")
