#!/usr/bin/env python3
"""Phase 1 of #31: prove the binding between hazed blocks and a Hazync proof.

A hazed block has had its witnesses, scriptSigs, OP_RETURN payloads and coinbase scriptSig stripped.
That destroys the ability to re-verify the signatures — so on its own, a hazed block cannot tell you the
transactions in it were ever VALID. It can only tell you what they were.

The claim ghostd would rely on is that these two things compose:

    merkle root + header chain  ->  IDENTITY   (these are the real blocks, and the real txs in them)
    the Hazync proof           ->  VALIDITY   (those txs satisfied consensus rules)

Identity is exactly what survives stripping. Validity is exactly what does not. This checks all four
links end to end so that claim is demonstrated standalone, before any of it is wired into consensus
code where a mistake is expensive.

    1. each block's merkle root recomputes from its txids          (txs <-> header)
    2. each header links to its predecessor, and its PoW is valid  (block N <-> N-1)
    3. the chain tip equals the tip the proof commits to           (chain <-> proof)
    4. the Hazync proof verifies, genesis-anchored                 (validity)

WHAT THIS DOES NOT YET USE: real .gsb files. ghostd is not built here and no hazed blocks exist yet, so
txids come from a bitcoind RPC instead of from a stripped block. That is a faithful stand-in for links
1-3 — a hazed block stores exactly these txids, and `haze::VerifyStrippedBlock` performs exactly check
1 — but it is NOT a test of Ghost's serialisation. Point `--txid-source gsb` at real files when they
exist; the checks do not change.

    ./hazed-chain-verify.py --proof fold_1000.snark --verifier ./hazync-verify
"""
import argparse, hashlib, json, subprocess, sys


def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def merkle_root(txids_le: list[bytes]) -> bytes:
    """Bitcoin merkle root over txids in internal byte order.

    The duplicated-odd-node rule is CVE-2012-2459 territory: a block with an odd number of nodes
    duplicates the last one, and naive implementations let an attacker mutate a block to the same root.
    We only recompute, never accept a claimed root, so we are not exposed — but the rule still has to be
    reproduced exactly or honest blocks fail to match.
    """
    if not txids_le:
        raise ValueError("no txids")
    layer = list(txids_le)
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


# ── GSB parsing ────────────────────────────────────────────────────────────────────────────────────
# Ghost's on-disk hazed format, from ghost-core/src/haze/stripped_block.h:
#
#   GSB frame        : "GSB\0" magic (4) | size uint32-LE (4) | CStrippedBlock
#   CStrippedBlock   : CBlockHeader (80) | compactsize n | n * CStrippedTransaction
#   CStrippedTx      : flags u8 | [stored txid 32 if flags&1] | version i32 | inputs | outputs | locktime u32
#   CStrippedInput   : prevout (32 hash + u32 index) | 0x00 (empty scriptSig length) | sequence u32
#   CStrippedOutput  : value i64 | compactsize-prefixed scriptPubKey
#
# The stored-txid flag is the crux. Stripping scriptSigs, OP_RETURN payloads and non-standard scripts
# changes the txid preimage, so those txids cannot be recomputed and are stored verbatim. A stored txid
# is NOT taken on trust: the merkle root is recomputed from whatever txids the block yields and must
# match the header, so a forged one fails.

class _R:
    def __init__(self, b): self.b, self.i = b, 0
    def take(self, n):
        if self.i + n > len(self.b): raise EOFError("gsb truncated")
        v = self.b[self.i:self.i + n]; self.i += n; return v
    def u8(self):  return self.take(1)[0]
    def u32(self): return int.from_bytes(self.take(4), "little")
    def i32(self): return int.from_bytes(self.take(4), "little", signed=True)
    def i64(self): return int.from_bytes(self.take(8), "little", signed=True)
    def compact(self):
        n = self.u8()
        if n < 0xfd: return n
        if n == 0xfd: return int.from_bytes(self.take(2), "little")
        if n == 0xfe: return self.u32()
        return int.from_bytes(self.take(8), "little")


def _tx_txid(r: _R) -> bytes:
    """Parse one CStrippedTransaction; return its txid in internal byte order."""
    flags = r.u8()
    stored = r.take(32) if (flags & 0x01) else None
    start = r.i                                  # non-witness preimage begins at the version field
    r.i32()                                      # version
    for _ in range(r.compact()):                 # inputs
        r.take(32); r.u32()                      # prevout
        n = r.compact()                          # scriptSig length — always 0 in a stripped block
        r.take(n)
        r.u32()                                  # sequence
    for _ in range(r.compact()):                 # outputs
        r.i64()                                  # value
        r.take(r.compact())                      # scriptPubKey
    r.u32()                                      # locktime
    if stored is not None:
        return stored                            # scriptSig/payload stripped: preimage is gone
    return sha256d(r.b[start:r.i])               # native SegWit: scriptSig was already empty


def parse_gsb(path, xor_key: bytes = b""):
    """Yield (header_bytes, [txid…]) for every stripped block in a .gsb file.

    Block files are XOR-obfuscated with the key in blocks/xor.dat — a Core feature that stops naive
    virus scanners and grep from matching on chain contents at rest. It is NOT encryption and carries no
    security weight, but nothing parses without undoing it first.

    Worth knowing: this is also why the node's crash reported magic `f0542088`. That is not corruption,
    it is the XOR key itself showing through a region of zeros that was never written.
    """
    data = open(path, "rb").read()
    if xor_key:
        data = bytes(b ^ xor_key[i % len(xor_key)] for i, b in enumerate(data))
    # Records are NOT contiguous from offset 0: the file is preallocated, so it opens with unwritten
    # space and may contain gaps. Scan for the magic rather than assuming a packed sequence — the
    # block index is what normally supplies positions, and we deliberately do not depend on it here.
    out, magic, i = [], b"GSB\0", data.find(b"GSB\0")
    while i != -1:
        try:
            r = _R(data[i:])
            r.take(4)
            size = r.u32()
            body = _R(r.take(size))
            header = body.take(80)
            out.append((header, [_tx_txid(body) for _ in range(body.compact())]))
        except (EOFError, ValueError, IndexError):
            pass          # a magic-looking byte run inside payload data; skip it
        i = data.find(magic, i + 1)
    return out


def pow_ok(header_hash_le: bytes, bits: int) -> bool:
    """Does the header hash meet its own difficulty target?"""
    exp, mant = bits >> 24, bits & 0xFFFFFF
    target = mant * (1 << (8 * (exp - 3))) if exp > 3 else mant >> (8 * (3 - exp))
    return int.from_bytes(header_hash_le, "little") <= target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proof", required=True)
    ap.add_argument("--verifier", default="verifier/target/release/hazync-verify")
    ap.add_argument("--cli", default="bitcoin-cli", help="bitcoin-cli, or a wrapper e.g. 'ssh box bitcoin-cli'")
    ap.add_argument("--txid-source", choices=["rpc", "gsb"], default="rpc")
    ap.add_argument("--gsb-dir", help="directory of .gsb files (when --txid-source gsb)")
    a = ap.parse_args()

    def cli(*args):
        out = subprocess.run(a.cli.split() + list(args), capture_output=True, text=True, timeout=120)
        if out.returncode:
            sys.exit(f"FAIL bitcoin-cli {' '.join(args)}: {out.stderr.strip()[:200]}")
        return out.stdout.strip()

    print("═══ 4. VALIDITY — the Hazync proof ═══")
    r = subprocess.run([a.verifier, "--json", a.proof], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAIL the proof did not verify — nothing below is meaningful\n{r.stderr.strip()[:300]}")
    st = json.loads(r.stdout)
    N, proven_tip = st["height"], st["tip_hash"]
    print(f"    ✓ verified, genesis-anchored, blocks 1..{N}")
    print(f"      proof commits tip = {proven_tip}")
    print(f"      ({st['proof_bytes']} bytes; attests every tx in 1..{N} satisfied consensus rules)")
    print()

    if a.txid_source == "gsb":
        if not a.gsb_dir:
            sys.exit("FAIL --txid-source gsb needs --gsb-dir <blocks dir containing gsb*.dat>")
        import glob, os
        files = sorted(glob.glob(os.path.join(a.gsb_dir, "gsb*.dat")))
        if not files:
            sys.exit(f"FAIL no gsb*.dat under {a.gsb_dir} — nothing to verify")
        xor_path = os.path.join(a.gsb_dir, "xor.dat")
        xor_key = open(xor_path, "rb").read() if os.path.exists(xor_path) else b""
        blocks = {}
        for f in files:
            for header, txids in parse_gsb(f, xor_key):
                blocks[sha256d(header)] = (header, txids)
        print(f"═══ 1+2. IDENTITY — from REAL hazed storage ({len(blocks)} stripped blocks) ═══")
        print(f"    (txids read from {', '.join(os.path.basename(f) for f in files)} — witnesses and")
        print("     scriptSigs are gone; these are the txids the hazed archive actually retains)")
        # walk forward from genesis using each header's prev pointer
        by_prev = {h[4:36]: (bh, h, t) for bh, (h, t) in blocks.items()}
        genesis = bytes.fromhex("000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f")[::-1]
        cur, height, bad = genesis, 0, 0
        while height < N and cur in by_prev:
            bh, header, txids = by_prev[cur]
            height += 1
            got = merkle_root(txids)
            if got != header[36:68]:
                print(f"    ✗ block {height}: merkle mismatch — {got[::-1].hex()[:16]}… vs header {header[36:68][::-1].hex()[:16]}…")
                bad += 1
            if not pow_ok(bh, int.from_bytes(header[72:76], "little")):
                print(f"    ✗ block {height}: PoW does not meet its stated target")
                bad += 1
            cur = bh
            if height % 200 == 0 or height == N:
                print(f"      …{height}/{N} blocks checked")
        if height < N:
            sys.exit(f"FAIL hazed archive only reaches height {height}, proof covers {N}")
        if bad:
            sys.exit(f"FAIL {bad} identity failure(s) — the hazed archive does not reconstruct")
        print(f"    ✓ all {N} merkle roots recompute from the hazed archive's own txids")
        print(f"    ✓ all {N} headers link, and every PoW meets its target")
        print()
        print("═══ 3. THE JOIN — does the hazed chain end where the proof says? ═══")
        tip = cur[::-1].hex()
        print(f"    hazed archive tip at {N}  {tip}")
        print(f"    proof commits             {proven_tip}")
        if tip != proven_tip:
            sys.exit("FAIL the hazed archive does not end at the proven tip")
        print("    ✓ match\n")
        print("═══ CONCLUSION ═══")
        print(f"    A node holding ONLY hazed blocks — witnesses and scriptSigs destroyed — established")
        print(f"    that blocks 1..{N} are the real chain, and a {st['proof_bytes']}-byte proof established")
        print("    that every transaction in them was valid. No signature was available to check.")
        return 0

    print(f"═══ 1+2. IDENTITY — merkle roots and header chain, blocks 1..{N} ═══")
    print("    (txids from bitcoind — the stand-in for a hazed block's stored txids)")
    prev_expect = None
    bad = 0
    for h in range(1, N + 1):
        bh = cli("getblockhash", str(h))
        blk = json.loads(cli("getblock", bh, "1"))
        txids_le = [bytes.fromhex(t)[::-1] for t in blk["tx"]]
        got = merkle_root(txids_le)[::-1].hex()
        if got != blk["merkleroot"]:
            print(f"    ✗ block {h}: merkle mismatch — recomputed {got[:16]}… vs header {blk['merkleroot'][:16]}…")
            bad += 1
        if prev_expect is not None and blk["previousblockhash"] != prev_expect:
            print(f"    ✗ block {h}: does not link to {prev_expect[:16]}…")
            bad += 1
        if not pow_ok(bytes.fromhex(bh)[::-1], int(blk["bits"], 16)):
            print(f"    ✗ block {h}: PoW does not meet its stated target")
            bad += 1
        prev_expect = bh
        if h % 200 == 0 or h == N:
            print(f"      …{h}/{N} blocks checked")
    if bad:
        sys.exit(f"FAIL {bad} identity failure(s) — the hazed chain does not reconstruct")
    print(f"    ✓ all {N} merkle roots recompute from txids alone")
    print(f"    ✓ all {N} headers link, and every PoW meets its target")
    print()

    print("═══ 3. THE JOIN — does the chain end where the proof says? ═══")
    print(f"    chain tip at {N}  {prev_expect}")
    print(f"    proof commits    {proven_tip}")
    if prev_expect != proven_tip:
        sys.exit("FAIL the verified chain does not end at the proven tip — proof and blocks disagree")
    print("    ✓ match\n")

    print("═══ CONCLUSION ═══")
    print(f"    Blocks 1..{N} are the real chain (identity, from merkle roots + PoW headers)")
    print(f"    AND every transaction in them was valid (validity, from a {st['proof_bytes']}-byte proof).")
    print()
    print("    A node holding hazed blocks — witnesses and scriptSigs destroyed — can establish both,")
    print("    despite being unable to re-check a single signature.")
    print()
    print("    NOT shown: this used bitcoind-derived txids, not real .gsb files. Links 1-3 are exactly")
    print("    what haze::VerifyStrippedBlock does, so the reasoning holds — but Ghost's serialisation")
    print("    is untested here until ghostd can emit hazed blocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
