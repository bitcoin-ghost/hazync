#!/usr/bin/env python3
# Flatten a block fixture into the TSV the real-vector harness reads:
#   rawhex \t ninputs \t value,spkhex,coin_height,coin_is_coinbase,coin_mtp \t ...
#
# The coinbase is SKIPPED: it has no prevout to verify against, so verify_input does not apply.
import json, sys
d = json.load(open(sys.argv[1]))
n = 0
for t in d["txs"]:
    ps = t.get("prevouts") or []
    if not ps:            # coinbase, or a tx the fixture carries without spends
        continue
    cols = [t["raw"], str(len(ps))]
    for p in ps:
        cols.append(f'{p["value"]},{p["spk"]},{p["coin_height"]},{p["coin_is_coinbase"]},{p["coin_mtp"]}')
    print("\t".join(cols)); n += 1
print(f"  {n} spending transactions from block {d['height']}", file=sys.stderr)
