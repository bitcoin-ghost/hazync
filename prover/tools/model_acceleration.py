#!/usr/bin/env python3
# Model the acceleration stack (#136, #137, #139) on block 962,000 using #138's fitted
# coefficients. Sources the G-cycle ladder in docs/ACCELERATION.md 'The board'.
# Usage: python3 prover/tools/model_acceleration.py
import json
d=json.load(open('prover/block_962000.json'))
# measured classification (from classify.py)
INPUTS=8006; ECDSA=7015; SCHNORR=191
BASE=34_000        # #138 measured per-input base
EC=1_950_000       # COST_PER_EC_OP, fitted and cross-confirmed by ec-bench (1,909,913)
uniq=sum(len(t['raw'])//2 + sum(len(p['spk'])//2+40 for p in t['prevouts']) for t in d['txs'])
shipped=sum((len(t['raw'])//2 + sum(len(p['spk'])//2+40 for p in t['prevouts']))*len(t['prevouts']) for t in d['txs'])
print(f"block 962,000: unique payload {uniq:,} B, shipped per-input {shipped:,} B ({shipped/uniq:.1f}x dup)\n")

def total(byte_coef, bytes_, ec_f=1.0, schnorr_f=1.0):
    return (INPUTS*BASE + bytes_*byte_coef
            + ECDSA*EC/ec_f + SCHNORR*EC/schnorr_f)

stages=[
 ("main today (182 c/B, per-input payload)", total(182, shipped)),
 ("+ #136 read_slice (36 c/B)",              total(36, shipped)),
 ("+ #137 group-by-tx (6 c/B, unique)",      total(6, uniq)),
 ("+ #139 middle path (ECDSA 6.52x)",        total(6, uniq, ec_f=6.52)),
 ("+ #139 wholesale (ECDSA 13.77x)",         total(6, uniq, ec_f=13.77)),
]
base=stages[0][1]; prev=None
print(f"{'stage':<44}{'G cycles':>10}{'vs main':>9}{'step':>8}")
for name,v in stages:
    step = f"{prev/v:.2f}x" if prev else "—"
    print(f"{name:<44}{v/1e9:>9.2f}{base/v:>8.2f}x{step:>8}")
    prev=v

print("\n--- composition after #136+#137, before any EC change ---")
t=total(6,uniq)
for lbl,val in (("ECDSA verify",ECDSA*EC),("Schnorr verify",SCHNORR*EC),
                ("per-input base",INPUTS*BASE),("payload bytes",uniq*6)):
    print(f"  {lbl:<18}{val/1e9:>7.2f} G  {val/t*100:>5.1f}%")

print("\n--- the Schnorr floor: bigint2 has no BIP340 ---")
for f,lbl in ((6.52,"middle path"),(13.77,"wholesale")):
    v=total(6,uniq,ec_f=f)
    ideal=total(6,uniq,ec_f=f,schnorr_f=f)
    print(f"  {lbl:<12} {t/v:>5.2f}x   (if Schnorr were also accelerated: {t/ideal:.2f}x)")
