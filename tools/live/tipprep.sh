#!/bin/bash
# Build the block file a TIP trial needs, locally. No pods, no coordinator, no spend.
#
#   ./tipprep.sh            # current tip minus 6 (safely confirmed)
#   ./tipprep.sh 967400     # a specific height
#
# ⛔ MEASURED 2026-09-17 01:3x — NO TIP BUNDLES ARE SERVED AT ALL.
#   /api/witness/<h> serves BRIDGE_DIR/bundle_<h>.json first, then WITNESS/block_<h>.json
#   (bundle_path(), deliberately shared with the bulk endpoint so they cannot drift). So this is a
#   true test of bundle availability, and:
#       tip-0 .. tip-50,000  -> 404   (incl. 917,339, far outside the 100-block finality lag)
#       76,130 / 76,129 / 74,928 -> 200 (7 KB .. 2.2 MB)   <- positive control, probe is sound
#   hazync-bridge.service is documented as tip-following (advances to tip-HAZYNC_BRIDGE_FINALITY=100,
#   polls every 30 s), yet nothing near the tip is served. The coordinator MIGRATED on 2026-09-16.
#   ⇒ CHECK whether HAZYNC_BRIDGE_OUT / the bundle directory came across to the new box. Not
#     diagnosed from here — the box was not inspected. This blocks the 24-hour tip run regardless
#     of the dashboard.
#
# WHY THIS EXISTS, and why the tip trial cannot use the board path:
#   * /api/witness/<tip> is 404 — the coordinator's rolling window covers the BACKFILL (~76k), not
#     the tip. Measured 2026-09-17: 75,000 and 76,000 return 200, 967,328 returns 404.
#   * `hazync run <h>` calls fetch_witnesses(1..h), i.e. ~967,000 requests at tip height. Not viable.
#   So a tip run goes the MILESTONE path: one self-contained block_<h>.json handed to each card,
#   HAZYNC_CHUNKS=N, then the aggregate. Exactly what block_962000.json did tonight.
#
# ⛔ MEASURED 2026-09-17 on block 967,332 (3,671 txs): the fetch runs at 1.67 items/s and needs
#   ~11,400 requests — prevouts 3,671 then meta 7,741 — for a total of about 93 MINUTES PER BLOCK.
#   The tip produces a block every ~10 minutes, so this fetcher is ~9x too slow to FOLLOW the tip.
#   That is inherent to the scaffold (public explorers + a 0.3 s throttle), not a tuning problem.
#     * a ONE-OFF tip-block trial: fine, just slow to prepare
#     * a 24-hour tip-following run: NOT POSSIBLE this way — the archive-node bridge is a
#       prerequisite, not an optimisation
#   PARTIALLY resumable — measured after a kill at meta 1200/7741: the .meta2.json cache survives
#   (all 1,201 entries), but the PREVOUTS phase is NOT cached and restarts from 0, so every kill
#   costs ~20 min of redone work. Forward progress, but not free. Budget for it if the box is
#   under memory pressure: this WSL2 host killed the fetch once at ~25 min in.
set -u
REPO=/home/defenwycke/dev/projects/hazync
OUT=${OUT:-/home/defenwycke/tipblocks}
mkdir -p "$OUT"

H=${1:-}
if [ -z "$H" ]; then
  H=$(curl -s -m 20 "https://api.hazync.org/api/state?slim=1" \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['progress']['tip']-6)")
fi
[ -n "$H" ] || { echo "could not determine a height"; exit 1; }
F="$OUT/block_$H.json"

echo "=== building $F ==="
if [ -s "$F" ]; then
  echo "  already present ($(stat -c%s "$F") bytes) — skipping fetch"
else
  time python3 "$REPO/prover/fetch_block.py" "$H" "$F" || { echo "⛔ fetch failed"; exit 1; }
fi

# Verify before anyone pays a GPU to read it: it must parse, and carry txs and prevouts.
python3 - "$F" <<'PY'
import json, sys, os
p = sys.argv[1]
d = json.load(open(p))
ntx = len(d.get("txs") or d.get("transactions") or [])
keys = sorted(d.keys())[:8]
print(f"  parses OK · {os.path.getsize(p)/1e6:.1f} MB · {ntx} txs · keys: {keys}")
if ntx == 0:
    sys.exit("⛔ no transactions in the block file — do not ship this to a fleet")
PY
echo "  sha256: $(sha256sum "$F" | cut -c1-16)"

cat <<EOF

=== next steps (when you want to spend) ===
  1) deploy N cards:
       cd /home/defenwycke/hazync-board-fleet
       S=/home/defenwycke/tiprun-tip N=3 OFF=40 MIXED=0 bash fleet.sh
  2) put the block where pod-prove.sh will find it (it fetches from hazync.org/repro by name, so a
     tip block must be COPIED, not downloaded):
       for each pod: scp $F root@IP:/workspace/
  3) prep + run, CHUNKS = number of cards:
       S=/home/defenwycke/tiprun-tip BLOCK=block_$H.json CHUNKS=3 ./mile3.sh prep
       S=... ./mile3.sh prove ; ./mile3.sh watch ; ./mile3.sh stage ; ./mile3.sh agg ; ./mile3.sh watchagg
  4) dashboard: HAZYNC_RUNDIR=/home/defenwycke/tiprun-tip ./tip-stream.sh start
       then collect.py --rundir ... --loop  and  tip24live.py --loop

⛔ mile3.sh prep currently DOWNLOADS the block from hazync.org/repro, which has only 962000/966256/
   966280. For a tip height it must copy the local file instead — that edit is pending.
EOF
