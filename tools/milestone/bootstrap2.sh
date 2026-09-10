#!/bin/bash
# Fleet bootstrap + screening control run.
# ⛔ compat removal is REQUIRED on consumer cards (CUDA Error 804) -- see stage 1.
set -u
W=/workspace; mkdir -p $W; cd $W || exit 1
for d in /usr/local/cuda*/compat; do [ -d "$d" ] && mv "$d" "${d}.disabled"; done
ldconfig 2>/dev/null
BIN=$W/hazync-host-cuda
[ -x "$BIN" ] || { curl -fsSL -o "$BIN" https://github.com/bitcoin-ghost/hazync/releases/download/v0.21.0/hazync-host-x86_64-linux-gnu-cuda && chmod +x "$BIN"; }
[ -f $W/block_966280.json ] || { curl -fsSLO https://bitcoinghost.org/hazync/repro/block_966280.json.gz && gunzip -f block_966280.json.gz; }
BSHA=$(sha256sum $W/block_966280.json | cut -d' ' -f1)
[ "$BSHA" = "9f2124553d5a18f994ec2b95d91dfd251d59c0e9abc61f18be26564a132538be" ] || { echo "BLOCK SHA MISMATCH $BSHA"; exit 1; }
MID=$($BIN method-id 2>&1 | grep -oE '[0-9a-f]{64}' | head -1)
[ "$MID" = "37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd" ] || { echo "METHOD_ID MISMATCH $MID"; exit 1; }

# SCREENING: the real milestone block at a high chunk count -- representative work, ~30 segments.
export HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_ECMULT_WINDOW=21
export HAZYNC_BLOCK=$W/block_966280.json HAZYNC_CHUNKS=64 HAZYNC_OUT=$W/screen.bin
T0=$(date +%s.%N)
stdbuf -oL "$BIN" prove-chunk 0 > screen.log 2>&1
RC=$?
T1=$(date +%s.%N)
SEGS=$(grep -oE '[0-9]+ segments at po2' screen.log | grep -oE '^[0-9]+' | head -1)
python3 - <<PY > screen.json
import json,subprocess
def sh(c):
    try: return subprocess.run(c,shell=True,capture_output=True,text=True,timeout=15).stdout.strip()
    except Exception: return ""
segs=int("${SEGS:-0}" or 0)
wall=$T1-$T0
json.dump({"rc":$RC,"wall_s":round(wall,2),"segments":segs,
  "s_per_segment": round(wall/segs,3) if segs else None,
  "gpu": sh("nvidia-smi --query-gpu=name,uuid,driver_version --format=csv,noheader"),
  "cpu": sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs"),
  "peak_vram_mib": sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits")},
  open("/dev/stdout","w"))
PY
cat screen.json
