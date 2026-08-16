#!/usr/bin/env python3
"""Build a block_full.json for `host prove-chunk` straight from bitcoind.

WHY THIS EXISTS. Guest mode 4 (`chunk_prove`) verifies scripts and nothing else — it reads the
height, the block hash and a per-input tuple, and never touches the accumulator. So pricing or
stress-testing a block needs NO proven prior state, no Utreexo forest and no archive bridge: just
the block and the coins it spends, which bitcoind already has.

That makes any block in history measurable in about twenty seconds of setup, including blocks far
beyond the bridge's current reach. What it produces is deliberately NOT submittable — there is no
accumulator transition in it — so this is a stopwatch and a repro harness, never a path to the board.

REQUIREMENTS. bitcoind with txindex=1, synced past the target height. `getblock <hash> 3` supplies
the prevouts (value, scriptPubKey, height, coinbase flag) directly, so no per-input lookups are
needed; only the coin-creation MTPs cost extra RPCs, and those are cached per height.

USAGE
    ./mkblock.py 962000                  # whole block
    ./mkblock.py 962000 400              # first 400 non-coinbase txs (quick sample)

    export HAZYNC_BLOCK=$PWD/block_962000.json HAZYNC_CHUNKS=16
    ./host prove-chunk 2
"""
import json, subprocess, sys

def cli(*a):
    return subprocess.run(["bitcoin-cli", *a], capture_output=True, text=True, check=True).stdout

def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    height = int(sys.argv[1])
    maxtx = int(sys.argv[2]) if len(sys.argv) > 2 else 10**9

    bh = cli("getblockhash", str(height)).strip()
    blk = json.loads(cli("getblock", bh, "3"))

    mtp_cache = {}
    def mtp_at(h):
        if h not in mtp_cache:
            mtp_cache[h] = json.loads(cli("getblockheader", cli("getblockhash", str(h)).strip()))["mediantime"]
        return mtp_cache[h]

    txs, ninputs = [], 0
    for t in blk["tx"][1:]:                       # skip the coinbase: it spends nothing
        if len(txs) >= maxtx:
            break
        prevouts = []
        for vin in t["vin"]:
            p = vin["prevout"]
            ch = p["height"]
            prevouts.append({
                "value": int(round(p["value"] * 1e8)),
                "spk": p["scriptPubKey"]["hex"],
                "coin_height": ch,
                "coin_is_coinbase": 1 if p.get("generated") else 0,
                # BIP68 reads the MTP of the block BEFORE the coin's creation block: GetAncestor(h-1)
                "coin_mtp": mtp_at(max(0, ch - 1)),
            })
        txs.append({"raw": t["hex"], "prevouts": prevouts})
        ninputs += len(t["vin"])

    recent, h = [], height - 1
    for _ in range(11):                           # the 11 timestamps GetMedianTimePast needs
        recent.append(json.loads(cli("getblockheader", cli("getblockhash", str(h)).strip()))["time"])
        h -= 1

    out = {
        "height": height, "bits": int(blk["bits"], 16), "time": blk["time"], "nonce": blk["nonce"],
        "version": blk["version"], "prev": blk["previousblockhash"], "merkle": blk["merkleroot"],
        "coinbase_hex": blk["tx"][0]["hex"], "txs": txs, "recent_times": recent,
    }
    path = f"block_{height}.json"
    with open(path, "w") as f:
        json.dump(out, f)
    print(f"block {height}: {len(txs)} txs, {ninputs} inputs -> {path}")

if __name__ == "__main__":
    main()
