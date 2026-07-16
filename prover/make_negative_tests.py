#!/usr/bin/env python3
"""Generate negative-test fixtures for the security regression (SEC-neg).

These prove the guest REJECTS malicious inputs, not just that it accepts valid ones.

  block_741000_badwit.json — one byte flipped inside a transaction's *witness* data. This changes the
  wtxid (so the BIP141 witness-commitment check must fail) but NOT the txid (so the base merkle root is
  unaffected). Running `check-full` on it must report `merkle_ok=true, witness_ok=false, all_ok=false`
  — i.e. the block is rejected specifically on the witness commitment. This is the check SEC-1 hardened
  (the guest recomputes `has_witness` in-guest, so a prover can't claim "no witness" to skip it).

Usage:  python3 make_negative_tests.py           # reads block_741000.json, writes the fixture(s)
"""
import json
import sys


def rd_varint(b, o):
    x = b[o]; o += 1
    if x < 0xfd:
        return x, o
    if x == 0xfd:
        return int.from_bytes(b[o:o + 2], 'little'), o + 2
    if x == 0xfe:
        return int.from_bytes(b[o:o + 4], 'little'), o + 4
    return int.from_bytes(b[o:o + 8], 'little'), o + 8


def witness_region(raw):
    """Return (start, end, total_len) byte offsets of the witness section of a segwit tx, or None."""
    b = bytes.fromhex(raw)
    if len(b) < 6 or b[4] != 0x00 or b[5] != 0x01:
        return None  # not segwit (no marker/flag)
    o = 6
    nin, o = rd_varint(b, o)
    for _ in range(nin):
        o += 32 + 4                       # prevout (txid + vout)
        sl, o = rd_varint(b, o); o += sl  # scriptSig
        o += 4                            # sequence
    nout, o = rd_varint(b, o)
    for _ in range(nout):
        o += 8                            # value
        pl, o = rd_varint(b, o); o += pl  # scriptPubKey
    wit_start = o
    for _ in range(nin):
        nitems, o = rd_varint(b, o)
        for _ in range(nitems):
            il, o = rd_varint(b, o); o += il
    return wit_start, o, len(b)           # [wit_start, o) is witness; then 4 bytes locktime


def make_badwit(src='block_741000.json', dst='block_741000_badwit.json'):
    d = json.load(open(src))
    for i, tx in enumerate(d['txs']):
        r = witness_region(tx['raw'])
        if r and r[1] - r[0] > 4:
            ws, we, _ = r
            flip = ws + (we - ws) // 2    # middle of the witness region, safely before locktime
            b = bytearray.fromhex(tx['raw'])
            b[flip] ^= 0xff
            d['txs'][i]['raw'] = b.hex()
            json.dump(d, open(dst, 'w'))
            print(f"{dst}: flipped witness byte at tx[{i}] offset {flip} "
                  f"(witness [{ws},{we}), locktime last 4) -> wtxid changes, txid unchanged")
            return
    print("no segwit tx with a witness found", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    make_badwit()
