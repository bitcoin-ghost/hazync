#!/usr/bin/env python3
# What #139 does to the chunk packer.
#
# `predicted_ec_ops` counts signature verifications without distinguishing ECDSA from Schnorr,
# because today they cost the same. After #139 they do not: ECDSA drops to ~141K cycles and Schnorr
# stays at ~1.95M, a 13.8x divergence the packer cannot see. Since a block's wall-clock is its
# slowest chunk, that mis-pricing is not cosmetic.
#
# SIMULATION, not the real packer: this reproduces its SHAPE (contiguous chunks, balanced on
# predicted cost) rather than calling it. The finding does not depend on the approximation -- it
# follows from the 13.8x per-type divergence, which the real packer is equally blind to.
#
# Usage: python3 prover/tools/pack_after_139.py [prover/block_962000.json]
import json,sys,os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_inputs import parse_tx, count_sigops   # reuse the verified classifier
d=json.load(open(sys.argv[1] if len(sys.argv)>1 else 'prover/block_962000.json'))
EC=1_950_000; BASE=34_000; BYTE=6
# F is the ECDSA speedup the packer is blind to. DEFAULT IS THE *PROVING* figure: this script's
# subject is WALL-CLOCK, and execute-mode cycles are not proving cost. Measured on a B200
# 2026-08-21, n=256: 574.5s -> 56.3s = 10.20x proved, where execute mode said 14.19x.
# HZ_F re-derives the old execute-mode framing for comparison.
F=float(os.environ.get('HZ_F', '10.20'))

per=[]   # (ec_ops_ecdsa, ec_ops_schnorr, bytes)
for t in d['txs']:
    try: vins,wits=parse_tx(t['raw'])
    except Exception: continue
    txb=len(t['raw'])//2 + sum(len(p['spk'])//2+40 for p in t['prevouts'])
    for i,po in enumerate(t['prevouts']):
        spk=bytes.fromhex(po['spk']); w=wits[i] if i<len(wits) else []
        e=s=0
        if spk[:2]==b'\x00\x14' and len(spk)==22: e=1
        elif spk[:2]==b'\x00\x20' and len(spk)==34: e=count_sigops(w[-1]) if w else 1
        elif spk[:2]==b'\x51\x20' and len(spk)==34:
            ww=w[:-1] if (len(w)>=2 and w[-1][:1]==b'\x50') else w
            s = 1 if len(ww)==1 else (count_sigops(ww[-2]) if len(ww)>=2 else 0)
        elif spk==bytes.fromhex('51024e73'): pass
        elif spk[:3]==b'\x76\xa9\x14' and len(spk)==25: e=1
        elif spk[:2]==b'\xa9\x14' and len(spk)==23: e=(count_sigops(w[-1]) if len(w)>1 else 1)
        elif len(spk)>1 and spk[-1]==0xac: e=1
        per.append((e,s,txb//max(1,len(t['prevouts']))))

def cost_model(x):    # what the PACKER believes: all EC ops equal
    e,s,b=x; return BASE + EC*(e+s) + BYTE*b
def cost_pre(x):      # truth before #139
    e,s,b=x; return BASE + EC*(e+s) + BYTE*b
def cost_post(x):     # truth after #139: ECDSA accelerated, Schnorr NOT
    e,s,b=x; return BASE + EC*e/F + EC*s + BYTE*b

def pack(items,n,cost):   # contiguous equal-cost partition, same shape as the real packer
    tot=sum(cost(i) for i in items); tgt=tot/n
    out=[];cur=[];acc=0
    for it in items:
        cur.append(it); acc+=cost(it)
        if acc>=tgt and len(out)<n-1: out.append(cur);cur=[];acc=0
    out.append(cur); return out

for label,cost_truth in (("BEFORE #139",cost_pre),("AFTER  #139",cost_post)):
    chunks=pack(per,16,cost_model)          # packed by the CURRENT model either way
    real=[sum(cost_truth(i) for i in c) for c in chunks]
    mean=sum(real)/len(real)
    print(f"{label}: straggler {max(real)/mean:.2f}x   (chunk max {max(real)/1e6:,.0f}M vs mean {mean/1e6:,.0f}M)")

# and what it SHOULD be if the packer knew the types
chunks=pack(per,16,cost_post)
real=[sum(cost_post(i) for i in c) for c in chunks]
print(f"AFTER  #139, type-aware packer: straggler {max(real)/(sum(real)/len(real)):.2f}x")

# A block's wall-clock is its SLOWEST chunk, so the straggler is what the speedup actually is.
naive = max(sum(cost_post(i) for i in c) for c in pack(per,16,cost_model))
aware = max(sum(cost_post(i) for i in c) for c in pack(per,16,cost_post))
before= max(sum(cost_pre(i)  for i in c) for c in pack(per,16,cost_model))
print(f"\nWALL-CLOCK (16 chunks, one card each — the slowest chunk IS the block)")
print(f"  before #139                {before/1e6:>8,.0f}M cycles      1.00x")
print(f"  after, packer unchanged    {naive/1e6:>8,.0f}M cycles   {before/naive:>6.2f}x")
print(f"  after, packer type-aware   {aware/1e6:>8,.0f}M cycles   {before/aware:>6.2f}x")
print(f"  >>> refitting the packer is worth {naive/aware:.2f}x — it is a PREREQUISITE, not a follow-up")

e_tot=sum(x[0] for x in per); s_tot=sum(x[1] for x in per)
print(f"\nper-verify cost after #139:  ECDSA {EC/F:>10,.0f}   Schnorr {EC:>10,.0f}   ratio {F:.1f}x")
print(f"block 962,000 is only {s_tot/(e_tot+s_tot)*100:.1f}% Schnorr, which is why the damage is small HERE")
