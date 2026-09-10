#!/usr/bin/env python3
"""Credits card: the four values that let someone who was not there check the claim. Nothing else."""
import json,os,sys
from PIL import Image,ImageDraw,ImageFont
R=json.load(open(sys.argv[1] if len(sys.argv)>1 else 'run4_data.json'))
GROUND='#0f1012';TEXT='#e6e4de';FAINT='#55544f';ACCENT='#f7931a'
F='/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
LW,LH=1600,900; PAD=56          # must match the poster, so the GIF can cut between them
def fmt(n): return f"{n:,}"

ENTRIES=[('journal digest',R['journal_digest'],
          'what the guest committed — a function of the block, identical across every run'),
         ('guest METHOD_ID',R['method_id'],
          'which program was proved; the canonical v0.21.0 guest'),
         ('receipt sha256',f"{R['receipt_sha']}  ({fmt(R['receipt_bytes'])} bytes)",
          'this particular receipt file — seal bytes differ every run, by design'),
         ('chain tip',R['tip_hash'],
          'the header this proof advances to')]

def build(S=2):
    img=Image.new('RGB',(int(LW*S),int(LH*S)),GROUND); d=ImageDraw.Draw(img)
    g=lambda p,s: ImageFont.truetype(p,max(1,int(s*S)))
    f12,f14,f18=g(F,12),g(F,14),g(F,18)
    def T(x,y,s,fo,c): d.text((x*S,y*S),s,font=fo,fill=c)
    STEP=150
    y=(LH-(3*STEP+62))/2
    for label,value,note in ENTRIES:
        T(PAD,y,label,f14,ACCENT)
        T(PAD,y+34,value,f18,TEXT)
        T(PAD,y+62,note,f12,FAINT)
        y+=STEP
    return img

if __name__=='__main__':
    im=build(2); im.save('run4-credits.png')
    print('run4-credits.png',im.size,f"{os.path.getsize('run4-credits.png')/1024:.0f} KB")
