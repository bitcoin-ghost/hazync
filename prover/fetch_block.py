#!/usr/bin/env python3
# Fetch a full block's witness (header + per-tx raw bytes + per-input prevouts) into the JSON the
# host reads via HAZYNC_BLOCK. Low request count to stay under blockstream's rate limit:
#   1x block/raw (ALL tx bytes in one request, parsed locally) + ~ntx/25 paginated /txs (prevouts).
# For a 2700-tx block that's ~110 requests instead of ~5400.
#   usage: python3 fetch_block.py <height> <out.json>
import json, sys, time, urllib.request

# NOTE: this explorer fetcher is a SCAFFOLD for testing the guest without a full node. The real path
# is the archive-node bridge (ConnectBlock -> witness from CCoinsViewCache, zero requests). See
# HAZYNC_ARCHITECTURE.md "archive-node bridge". Kept resumable so rate-limit deaths don't lose progress.
BASES = ["https://blockstream.info/api/", "https://mempool.space/api/"]

def get(u, tries=10):
    for i in range(tries):
        base = BASES[i % len(BASES)]  # alternate sources so one rate-limit doesn't stall us
        try:
            r = urllib.request.urlopen(base + u, timeout=120).read()
            time.sleep(0.3)  # gentle throttle
            return r
        except Exception:
            time.sleep(min(2 ** (i // len(BASES)), 60))  # exponential backoff up to 60s
    raise SystemExit("FETCH FAILED: " + u)

# ---- minimal segwit-aware raw-tx / raw-block parser (to split block/raw into per-tx byte ranges) ----
def rd_varint(b, o):
    x = b[o]; o += 1
    if x < 0xfd: return x, o
    if x == 0xfd: return int.from_bytes(b[o:o+2], "little"), o + 2
    if x == 0xfe: return int.from_bytes(b[o:o+4], "little"), o + 4
    return int.from_bytes(b[o:o+8], "little"), o + 8

def parse_tx(b, o):
    start = o
    o += 4  # version
    segwit = (b[o] == 0x00 and b[o+1] == 0x01)
    if segwit: o += 2  # marker + flag
    nin, o = rd_varint(b, o)
    for _ in range(nin):
        o += 36                                   # prevout (txid + vout)
        sl, o = rd_varint(b, o); o += sl          # scriptSig
        o += 4                                    # sequence
    nout, o = rd_varint(b, o)
    for _ in range(nout):
        o += 8                                    # value
        sl, o = rd_varint(b, o); o += sl          # scriptPubKey
    if segwit:
        for _ in range(nin):
            nit, o = rd_varint(b, o)
            for _ in range(nit):
                il, o = rd_varint(b, o); o += il  # witness item
    o += 4  # locktime
    return b[start:o], o

def parse_block(raw):
    o = 80                                        # skip the 80-byte header
    ntx, o = rd_varint(raw, o)
    out = []
    for _ in range(ntx):
        tx, o = parse_tx(raw, o)
        out.append(tx)
    return out

def main():
    h, out_path = sys.argv[1], sys.argv[2]
    H = get("block-height/" + h).decode()
    b = json.loads(get("block/" + H))
    ntx = b["tx_count"]
    print(f"block {h} ({H[:16]}..): {ntx} txs", flush=True)

    # ALL tx bytes in ONE request.
    tx_bytes = parse_block(get("block/" + H + "/raw"))
    assert len(tx_bytes) == ntx, f"parsed {len(tx_bytes)} != {ntx}"

    # Prevouts via paginated /txs (25 tx objects per request, each with vin[].prevout).
    tx_json, i = [], 0
    while i < ntx:
        page = json.loads(get(f"block/{H}/txs/{i}"))
        if not page: break
        tx_json.extend(page)
        i += len(page)
        print(f"  prevouts {len(tx_json)}/{ntx}", flush=True)
    assert len(tx_json) == ntx, f"txs {len(tx_json)} != {ntx}"

    # S2: real per-coin metadata (creating height + coinbase flag + creating-block timestamp) so
    # maturity + BIP68-height fire on real data, and so the accumulator leaf (which commits coin_mtp)
    # matches between a coin's creation and its spend. coin_mtp = the CREATING block's timestamp — the
    # same value the prover's build path uses when it adds the output (must match, or the spent coin is
    # not found in the accumulator). One /tx lookup per UNIQUE funding txid (deduped); block_time is in
    # the tx status, so no extra request. The real bridge sources all of this from its own block index.
    meta = {}
    for idx in range(1, ntx):
        for v in tx_json[idx]["vin"]:
            meta.setdefault(v["txid"], None)
    # Resume from an on-disk cache so a rate-limit death never re-fetches what we already have.
    cache_path = out_path + ".meta.json"
    try:
        cached = json.load(open(cache_path))
        for k, v in cached.items():
            if k in meta: meta[k] = tuple(v)
    except Exception:
        cached = {}
    todo = [pt for pt in meta if meta[pt] is None]
    print(f"prevout metadata: {len(meta)} unique funding txs ({len(cached)} cached, {len(todo)} to fetch)", flush=True)
    for k, pt in enumerate(todo):
        j = json.loads(get("tx/" + pt))
        st = j["status"]
        meta[pt] = (st.get("block_height", 0), 1 if j["vin"][0].get("is_coinbase") else 0, st.get("block_time", 0))
        if k % 100 == 0:
            json.dump({p: meta[p] for p in meta if meta[p] is not None}, open(cache_path, "w"))
            print(f"  meta {k}/{len(todo)}", flush=True)
    json.dump({p: meta[p] for p in meta if meta[p] is not None}, open(cache_path, "w"))

    # Previous-11 block timestamps (blocks h-11..h-1); their median = MTP(h-1) — the spend block's
    # BIP68-time / BIP113 window. Walk back via previousblockhash (<=11 block/<hash> requests). Fewer
    # than 11 near genesis is fine (Core's GetMedianTimePast uses min(11, height); order-independent).
    recent_times = []
    ph = b.get("previousblockhash")
    for _ in range(11):
        if not ph: break
        pj = json.loads(get("block/" + ph))
        recent_times.append(pj["timestamp"])
        ph = pj.get("previousblockhash")
    recent_times.reverse()

    out = {"height": b["height"], "version": b["version"], "time": b["timestamp"],
           "bits": b["bits"], "nonce": b["nonce"], "prev": b["previousblockhash"],
           "merkle": b["merkle_root"], "coinbase_hex": tx_bytes[0].hex(),
           "recent_times": recent_times, "txs": []}
    for idx in range(1, ntx):
        assert len(tx_json[idx]["vin"]) == _nin(tx_bytes[idx]), f"input-count misalignment at {idx}"
        prevs = []
        for v in tx_json[idx]["vin"]:
            h, cb, ct = meta[v["txid"]]
            prevs.append({"value": v["prevout"]["value"], "spk": v["prevout"]["scriptpubkey"],
                          "coin_height": h, "coin_is_coinbase": cb, "coin_mtp": ct})
        out["txs"].append({"raw": tx_bytes[idx].hex(), "prevouts": prevs})
    json.dump(out, open(out_path, "w"))
    reqs = 3 + (ntx + 24) // 25 + len(meta)
    print(f"saved {out_path}: {len(out['txs'])} txs, {len(meta)} coin-meta (~{reqs} requests)", flush=True)

# tx's declared input count (cheap alignment check against the paginated /txs JSON).
def _nin(tx_bytes):
    o = 4
    if tx_bytes[o] == 0x00 and tx_bytes[o+1] == 0x01: o += 2
    n, _ = rd_varint(tx_bytes, o)
    return n

if __name__ == "__main__":
    main()
