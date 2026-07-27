#!/usr/bin/env python3
# Build a block's witness fixture (the JSON the host reads via HAZYNC_BLOCK) from a LOCAL archive node,
# instead of public explorers. Same output schema as fetch_block.py — this is a drop-in replacement for
# anyone who has a full node.
#
#   usage: python3 fetch_block_rpc.py <height> <out.json>
#
# Why: fetch_block.py talks to blockstream/mempool and needs roughly one request per unique funding tx
# (thousands for a modern block). That is rate-limited into hours, and 429s make it unreliable. A full
# node answers the same questions locally in seconds.
#
# Requirements: bitcoind with `txindex=1` and `prune=0`, and Core >= 25 for `getblock` verbosity 3 —
# which returns each input's `prevout` (height / generated / value / scriptPubKey) inline, so the entire
# per-funding-tx lookup loop collapses into a single call.
import json, sys, http.client, base64, pathlib
from decimal import Decimal

COOKIE = pathlib.Path.home() / ".bitcoin" / ".cookie"
HOST, PORT = "127.0.0.1", 8332

def rpc(method, params=None):
    auth = base64.b64encode(COOKIE.read_text().strip().encode()).decode()
    body = json.dumps({"jsonrpc": "2.0", "id": "h", "method": method, "params": params or []})
    c = http.client.HTTPConnection(HOST, PORT, timeout=600)
    c.request("POST", "/", body, {"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    r = c.getresponse().read()
    c.close()
    # Decimal, not float: values arrive in BTC and must convert to satoshis EXACTLY. json's default
    # float parse silently rounds (e.g. 0.1+0.2), which would corrupt the amounts the guest checks.
    d = json.loads(r, parse_float=Decimal)
    if d.get("error"): raise SystemExit(f"RPC {method} failed: {d['error']}")
    return d["result"]

def sats(btc):
    return int((Decimal(btc) * 100_000_000).to_integral_value())

def main():
    height, out_path = int(sys.argv[1]), sys.argv[2]
    h = rpc("getblockhash", [height])
    b = rpc("getblock", [h, 3])
    txs = b["tx"]
    print(f"block {height} ({h[:16]}..): {len(txs)} txs", flush=True)

    # coin_mtp = MTP(coin_height-1) — the median-time-past of the block BEFORE the coin's block. This is
    # what the prover's build path commits and what the guest's BIP68 time check consumes (Core's
    # nCoinTime = GetMedianTimePast(coinHeight-1)). Using the coin block's own nTime is WRONG: nTime >=
    # MTP(h-1), so it over-shoots and false-rejects boundary-tight BIP68 time locks. Cached per height,
    # since many funding txs share a creation block.
    mtp_at = {}
    def block_mtp(hh):
        if hh < 0: return 0
        if hh not in mtp_at:
            mtp_at[hh] = rpc("getblockheader", [rpc("getblockhash", [hh])])["mediantime"]
        return mtp_at[hh]

    # Previous-11 block timestamps (h-11..h-1), oldest first; their median = MTP(h-1), the spend block's
    # BIP68-time / BIP113 window. Fewer than 11 near genesis is fine (Core uses min(11, height)).
    recent_times, ph = [], b.get("previousblockhash")
    for _ in range(11):
        if not ph: break
        pj = rpc("getblockheader", [ph])
        recent_times.append(pj["time"])
        ph = pj.get("previousblockhash")
    recent_times.reverse()

    out = {"height": b["height"], "version": b["version"], "time": b["time"],
           # bitcoind returns nBits as a hex STRING ("18009645"); the fixture schema (and esplora) use
           # the integer. Convert, or the guest sees a wrong difficulty target.
           "bits": int(b["bits"], 16), "nonce": b["nonce"], "prev": b["previousblockhash"],
           "merkle": b["merkleroot"], "coinbase_hex": txs[0]["hex"],
           "recent_times": recent_times, "txs": []}

    n_meta = 0
    for idx in range(1, len(txs)):
        t = txs[idx]
        prevs = []
        for v in t["vin"]:
            p = v.get("prevout")
            if p is None:
                raise SystemExit(f"tx {idx} input has no prevout — is this node pruned, or Core < 25?")
            ch = p["height"]
            prevs.append({"value": sats(p["value"]), "spk": p["scriptPubKey"]["hex"],
                          "coin_height": ch,
                          "coin_is_coinbase": 1 if p.get("generated") else 0,
                          "coin_mtp": block_mtp(ch - 1)})
            n_meta += 1
        out["txs"].append({"raw": t["hex"], "prevouts": prevs})

    json.dump(out, open(out_path, "w"))
    print(f"saved {out_path}: {len(out['txs'])} txs, {n_meta} prevouts, "
          f"{len(mtp_at)} unique coin heights ({2 + len(mtp_at) * 2 + len(recent_times)} RPCs)", flush=True)

if __name__ == "__main__":
    main()
