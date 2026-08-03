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

# Defaults target a node on this machine. Overridable so the same script can pull fixtures from a
# node reachable over the network (an SSH tunnel, or an RPC port bound to a private interface) without
# a second copy of this logic drifting from the first:
#
#   HAZYNC_RPC_HOST / HAZYNC_RPC_PORT   where bitcoind's RPC listens
#   HAZYNC_RPC_COOKIE                   path to .cookie, or
#   HAZYNC_RPC_AUTH                     literal "user:password" when cookie auth is not available
#
# Read-only either way: this issues getblockhash and getblock and nothing else.
import os
HOST = os.environ.get("HAZYNC_RPC_HOST", "127.0.0.1")
PORT = int(os.environ.get("HAZYNC_RPC_PORT", "8332"))
COOKIE = pathlib.Path(os.environ.get("HAZYNC_RPC_COOKIE",
                                     str(pathlib.Path.home() / ".bitcoin" / ".cookie")))

def _auth():
    literal = os.environ.get("HAZYNC_RPC_AUTH")
    if literal:
        return base64.b64encode(literal.strip().encode()).decode()
    if not COOKIE.exists():
        raise SystemExit(
            f"no RPC credentials: {COOKIE} does not exist and HAZYNC_RPC_AUTH is unset.\n"
            f"Set HAZYNC_RPC_COOKIE to the node's cookie path, or HAZYNC_RPC_AUTH to 'user:pass'.")
    return base64.b64encode(COOKIE.read_text().strip().encode()).decode()

def rpc(method, params=None):
    auth = _auth()
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

    # RETARGET SUPPORT (hazync#83). The guest computes the expected nBits at every height where
    # height % 2016 == 0, using the timestamp of the first block of the PREVIOUS epoch. A fixture that
    # does not carry it forces the host to fabricate one, and the retarget check then compares against
    # a target derived from a made-up timestamp — which is exactly how block 481824 came back
    # `block_valid=true retarget_ok=false`: the block was fine, the fixture could not express it.
    #
    # This is not an edge case to skip. BIP9 soft forks activate ON retarget boundaries by design, so
    # the activation heights that most need a fixture (CSV 419328, segwit 481824) are all retarget
    # blocks. Emitted for every height, since it costs one extra call and a non-retarget block simply
    # carries it unused.
    epoch_first = ((height - 1) // 2016) * 2016 if height > 0 else 0
    eb = rpc("getblock", [rpc("getblockhash", [epoch_first]), 1])
    prev = rpc("getblock", [b["previousblockhash"], 1]) if height > 0 else None
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
           "recent_times": recent_times,
           # #83: the in-boundary retarget inputs, so a block at a retarget height can be validated
           # standalone. `epoch_start` is the timestamp of the first block of the PREVIOUS epoch —
           # what Core's CalculateNextWorkRequired takes as nFirstBlockTime. `prev_time`/`prev_bits`
           # are the previous block's, not this one's minus a guess.
           "epoch_start": eb["time"],
           "prev_time": prev["time"] if prev else b["time"],
           "prev_bits": int(prev["bits"], 16) if prev else int(b["bits"], 16),
           "txs": []}

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
