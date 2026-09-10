#!/bin/bash
# Per-card prove with full telemetry, for the sub-10-minute block proof.
# Everything a timeline chart needs is captured HERE, at the time it happens -- none of it can be
# reconstructed afterwards.
#   facts.json       static: card identity, host, location, driver, clocks, clock offset
#   gpu_samples.csv  1 Hz: util, VRAM, temperature, power, SM/mem clocks
#   prove.log        the host's own per-segment progress, so each segment is placeable in time
#   result.json      the outcome and the phase boundaries
set -u
CHUNK="${1:?usage: pod-prove.sh <chunk-index>}"
BLOCK="${HAZYNC_BLOCK_NAME:-block_962000.json}"
NCH="${HAZYNC_CHUNKS:-16}"
# ⛔ A card that takes a REASSIGNED chunk used to run here too, writing the same fixed filenames
# (prove.log, result.json, chunk_N.bin) into the same directory as its first chunk -- clobbering its
# own telemetry and letting the stager pick up a half-written file. Reassigned work gets its own dir.
W=${HAZYNC_WORKDIR:-/workspace}; mkdir -p $W; cd $W || exit 1
BIN=/workspace/hazync-host-cuda

# ---- fetch (datacenter link, not the operator's uplink) ----------------------------------------
if [ ! -x "$BIN" ]; then
  curl -fsSL -o "$BIN" https://github.com/bitcoin-ghost/hazync/releases/download/v0.21.0/hazync-host-x86_64-linux-gnu-cuda || exit 1
  chmod +x "$BIN"
fi
[ -f "/workspace/$BLOCK" ] || { curl -fsSLO "https://bitcoinghost.org/hazync/repro/${BLOCK}.gz" && gunzip -f "${BLOCK}.gz"; }

# ---- static facts -------------------------------------------------------------------------------
read -r GNAME GUUID GDRV GMEM GPWR GSM GMM < <(nvidia-smi --query-gpu=name,uuid,driver_version,memory.total,power.limit,clocks.max.sm,clocks.max.mem --format=csv,noheader,nounits | tr -d ',')
python3 - > facts.json <<PY
import json, os, subprocess, time
def sh(c):
    try: return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception: return ""
json.dump({
  "captured_utc": time.time(),
  "pod_id": os.environ.get("RUNPOD_POD_ID",""),
  "datacenter": os.environ.get("RUNPOD_DC_ID",""),
  "public_ip": os.environ.get("RUNPOD_PUBLIC_IP",""),
  "gpu": {"name": "$GNAME", "uuid": "$GUUID", "driver": "$GDRV",
          "vram_total_mib": "$GMEM", "power_limit_w": "$GPWR",
          "max_sm_clock_mhz": "$GSM", "max_mem_clock_mhz": "$GMM"},
  "cpu_model": sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs"),
  "cpu_cores": os.cpu_count(),
  "ram_gb": round(os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')/1e9, 1),
  "kernel": sh("uname -r"),
  "method_id": sh("$BIN method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1"),
  "block": "$BLOCK", "chunks": $NCH, "chunk_index": $CHUNK,
}, open("/dev/stdout","w"), indent=2)
PY

# ---- 1 Hz sampler, so the chart has curves and not just endpoints -------------------------------
( echo "epoch,util_pct,mem_used_mib,temp_c,power_w,sm_clock_mhz,mem_clock_mhz"
  while true; do
    echo "$(date +%s.%N),$(nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw,clocks.sm,clocks.mem --format=csv,noheader,nounits | tr -d ' ')"
    sleep 1
  done ) > gpu_samples.csv 2>/dev/null &
SAMPLER=$!

# ---- prove --------------------------------------------------------------------------------------
export HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_ECMULT_WINDOW=21
export HAZYNC_BLOCK=/workspace/$BLOCK HAZYNC_CHUNKS=$NCH HAZYNC_OUT=$W/chunk_$CHUNK.bin
T_START=$(date +%s.%N)
# stdbuf so the per-segment lines are timestamped as they happen, not flushed in a block at the end
stdbuf -oL "$BIN" prove-chunk "$CHUNK" 2>&1 | while IFS= read -r line; do echo "$(date +%s.%N) $line"; done > prove.log
RC=${PIPESTATUS[0]}
T_END=$(date +%s.%N)
kill $SAMPLER 2>/dev/null

python3 - > result.json <<PY
import json, re, subprocess
log = open("prove.log", errors="replace").read()
m = re.search(r"chunk \d+: (\d+) inputs, (\d+) segments at po2 (\d+)", log)
seg_times = [float(t) for t in re.findall(r"^(\d+\.\d+) +segment \d+/\d+", log, re.M)]
json.dump({
  "rc": $RC, "t_start": $T_START, "t_end": $T_END, "wall_s": round($T_END-$T_START,2),
  "inputs": int(m.group(1)) if m else None,
  "segments": int(m.group(2)) if m else None,
  "po2": int(m.group(3)) if m else None,
  "s_per_segment": round(($T_END-$T_START)/int(m.group(2)),3) if m else None,
  "first_segment_at": seg_times[0] if seg_times else None,
  "segment_marks": len(seg_times),
  "retries_119": log.count("[#119]"),
  "receipt_sha256": subprocess.run("sha256sum chunk_$CHUNK.bin 2>/dev/null | cut -c1-64",
                                   shell=True, capture_output=True, text=True).stdout.strip(),
}, open("/dev/stdout","w"), indent=2)
PY
echo "DONE rc=$RC"
