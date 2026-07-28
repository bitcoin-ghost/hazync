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
        sys.exit("FAIL --txid-source gsb not implemented: no .gsb files exist yet and ghostd is not "
                 "built. This tool deliberately refuses rather than silently falling back to RPC and "
                 "reporting a pass that did not test what you asked for.")

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
