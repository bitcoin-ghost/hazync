#!/usr/bin/env python3
# Classify every input of a block JSON by script type and count ECDSA vs Schnorr
# verifications. Sources the input-mix table in docs/ACCELERATION.md 'The board'.
# Usage: python3 prover/tools/classify_inputs.py [prover/block_962000.json]
import json, collections
d=json.load(open(__import__('sys').argv[1] if len(__import__('sys').argv)>1 else 'prover/block_962000.json'))

def rd(b,o,n): return b[o:o+n], o+n
def varint(b,o):
    x=b[o]; o+=1
    if x<0xfd: return x,o
    if x==0xfd: return int.from_bytes(b[o:o+2],'little'),o+2
    if x==0xfe: return int.from_bytes(b[o:o+4],'little'),o+4
    return int.from_bytes(b[o:o+8],'little'),o+8

def parse_tx(raw):
    b=bytes.fromhex(raw); o=4
    segwit=False
    if b[o]==0x00 and b[o+1]==0x01: segwit=True; o+=2
    nin,o=varint(b,o); vins=[]
    for _ in range(nin):
        o+=36
        sl,o=varint(b,o); script=b[o:o+sl]; o+=sl; o+=4
        vins.append(script)
    nout,o=varint(b,o)
    for _ in range(nout):
        o+=8; sl,o=varint(b,o); o+=sl
    wits=[[] for _ in range(nin)]
    if segwit:
        for i in range(nin):
            n,o=varint(b,o); items=[]
            for _ in range(n):
                l,o=varint(b,o); items.append(b[o:o+l]); o+=l
            wits[i]=items
    return vins,wits

# count CHECKSIG-family ops in a script
CHECKSIG={0xac,0xad,0xba}  # CHECKSIG, CHECKSIGVERIFY, CHECKSIGADD
def count_sigops(s):
    n=0;i=0
    while i<len(s):
        op=s[i]
        if op<=0x4b: i+=1+op; continue
        if op==0x4c: i+=2+(s[i+1] if i+1<len(s) else 0); continue
        if op==0x4d: i+=3+int.from_bytes(s[i+1:i+3],'little'); continue
        if op==0x4e: i+=5+int.from_bytes(s[i+1:i+5],'little'); continue
        if op in CHECKSIG: n+=1
        elif op in (0xae,0xaf):  # CHECKMULTISIG(VERIFY)
            # look back for the N push
            n+= 20
        i+=1
    return n

cnt=collections.Counter(); ec=collections.Counter(); inputs=0
for t in d['txs']:
    try: vins,wits=parse_tx(t['raw'])
    except Exception: continue
    for i,po in enumerate(t['prevouts']):
        inputs+=1
        spk=bytes.fromhex(po['spk']); w=wits[i] if i<len(wits) else []
        if spk[:2]==b'\x00\x14' and len(spk)==22: cnt['P2WPKH']+=1; ec['ECDSA']+=1
        elif spk[:2]==b'\x00\x20' and len(spk)==34:
            cnt['P2WSH']+=1; ec['ECDSA']+= (count_sigops(w[-1]) if w else 1)
        elif spk[:2]==b'\x51\x20' and len(spk)==34:
            if len(w)>=2 and w[-1][:1]==b'\x50': w=w[:-1]  # annex
            if len(w)==1: cnt['P2TR-keypath']+=1; ec['SCHNORR']+=1
            elif len(w)>=2: cnt['P2TR-script']+=1; ec['SCHNORR']+=count_sigops(w[-2])
            else: cnt['P2TR-other']+=1
        elif spk==bytes.fromhex('51024e73'): cnt['P2A-anchor']+=1
        elif spk[:3]==b'\x76\xa9\x14' and len(spk)==25: cnt['P2PKH']+=1; ec['ECDSA']+=1
        elif spk[:2]==b'\xa9\x14' and len(spk)==23:
            cnt['P2SH']+=1
            if w: ec['ECDSA']+= (count_sigops(w[-1]) if len(w)>1 else 1)
            else: ec['ECDSA']+=1
        elif len(spk)>1 and spk[-1]==0xac: cnt['P2PK']+=1; ec['ECDSA']+=1
        elif len(spk)>=4 and spk[0]==0x51 and len(spk)<=42: cnt['witness-vN(unencumbered)']+=1
        else: cnt['other']+=1; ec['ECDSA']+=count_sigops(spk) or 0

print(f"block 962,000 — {inputs:,} inputs, {len(d['txs']):,} txs\n")
print(f"{'type':<28}{'inputs':>8}{'share':>9}")
for k,v in cnt.most_common(): print(f"{k:<28}{v:>8,}{v/inputs*100:>8.1f}%")
tot=ec['ECDSA']+ec['SCHNORR']
print(f"\n{'signature verifications':<28}{'count':>8}{'share':>9}")
for k in ('ECDSA','SCHNORR'): print(f"{k:<28}{ec[k]:>8,}{ec[k]/tot*100:>8.1f}%")
print(f"{'TOTAL':<28}{tot:>8,}")
print(f"\ninputs verifying nothing: {cnt['P2A-anchor']+cnt['witness-vN(unencumbered)']:,} "
      f"({(cnt['P2A-anchor']+cnt['witness-vN(unencumbered)'])/inputs*100:.1f}%)")
