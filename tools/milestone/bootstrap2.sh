#!/bin/bash
# Fleet bootstrap + screening control run.
# ⛔ compat removal is REQUIRED on consumer cards (CUDA Error 804) -- see stage 1.
set -u
W=/workspace; mkdir -p $W; cd $W || exit 1
for d in /usr/local/cuda*/compat; do [ -d "$d" ] && mv "$d" "${d}.disabled"; done
ldconfig 2>/dev/null
BIN=$W/hazync-host-cuda
[ -x "$BIN" ] || { curl -fsSL -o "$BIN" "${HAZYNC_HOST_URL:-https://github.com/bitcoin-ghost/hazync/releases/download/v0.21.7/hazync-host-x86_64-linux-gnu-cuda}" && chmod +x "$BIN"; }
# ⛔ THE FIXTURE COMES FROM A CHECKOUT, NOT A URL -- the same fix pod-prove.sh already carries
#    (hazync#395). This used to fetch https://hazync.org/repro/block_966280.json.gz, and that path has
#    been 404 since at least 2026-09-18: the files AND the /repro/ directory return the 1358-byte error
#    page, and /var/www/hazync has no repro/ at all. With `curl -f` that is a hard failure, so the
#    screening died on every pod that did not already have the fixture.
#
# ⛔ AND IT SCREENS ON A COMMITTED BLOCK NOW. block_966280.json is not in the repo, so there was
#    nowhere left to take it from. block_965500.json is the same near-tip class (4.4 MB) and IS
#    committed, which is what makes the checkout fallback able to work at all.
BLOCK=block_965500.json
if [ ! -f "$W/$BLOCK" ]; then
    for _d in "${HAZYNC_REPO:-}/prover" /hazync-zkvm/prover /repo/prover "$HOME/hazync-zkvm/prover" ./prover .; do
        [ -n "$_d" ] && [ -f "$_d/$BLOCK" ] && { cp "$_d/$BLOCK" "$W/$BLOCK"; break; }
    done
fi
[ -f "$W/$BLOCK" ] || { echo "no $BLOCK at $W and no checkout to take it from."; \
    echo "It is committed in the repo - copy it over, or set HAZYNC_REPO to a checkout."; exit 1; }
BSHA=$(sha256sum "$W/$BLOCK" | cut -d' ' -f1)
[ "$BSHA" = "5667f06abcf04c18945cee0115e9ada55c1a4771d6e0dba748feb4841ddb8cf0" ] || { echo "BLOCK SHA MISMATCH $BSHA"; exit 1; }
MID=$($BIN method-id 2>&1 | grep -oE '[0-9a-f]{64}' | head -1)
[ "$MID" = "37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd" ] || { echo "METHOD_ID MISMATCH $MID"; exit 1; }

# SCREENING: the real milestone block at a high chunk count -- representative work, ~30 segments.
export HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_ECMULT_WINDOW=21
export HAZYNC_BLOCK=$W/$BLOCK HAZYNC_CHUNKS=64 HAZYNC_OUT=$W/screen.bin
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
