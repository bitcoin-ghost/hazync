#!/bin/bash
# Per-card prove with full telemetry, for the sub-10-minute block proof.
# Everything a timeline chart needs is captured HERE, at the time it happens -- none of it can be
# reconstructed afterwards.
#   facts.json       static: card identity, host, location, driver, clocks, clock offset
#   gpu_samples.csv  1 Hz: util, VRAM, temperature, power, SM/mem clocks
#   prove.log        the host's own per-segment progress, so each segment is placeable in time
#   result.json      the outcome and the phase boundaries
set -u
# ⚠ pipefail added alongside the -u these two already carry (hazync#462). It cannot abort anything
# on its own — there is no `set -e` here — it only stops a failed producer in `a | b` reading as
# success. See tools/milestone/README.md § Shell posture.
set -o pipefail
CHUNK="${1:?usage: pod-prove.sh <chunk-index>}"
BLOCK="${HAZYNC_BLOCK_NAME:-block_962000.json}"
NCH="${HAZYNC_CHUNKS:-16}"
# ⛔ A card that takes a REASSIGNED chunk used to run here too, writing the same fixed filenames
# (prove.log, result.json, chunk_N.bin) into the same directory as its first chunk -- clobbering its
# own telemetry and letting the stager pick up a half-written file. Reassigned work gets its own dir.
W=${HAZYNC_WORKDIR:-/workspace}; mkdir -p $W; cd $W || exit 1
BIN=/workspace/hazync-host-cuda

# ---- fetch (datacenter link, not the operator's uplink) ----------------------------------------
# ⛔ RESUME, AND SAY SO WHEN IT FAILS. This was a plain `curl -fsSL -o`, which TRUNCATES on every
#    attempt and is SILENT about failure (-s). A card on a slow link therefore spends the run
#    downloading 407 MB while its prove.log does not grow -- so the tick planner calls it stalled and
#    restarts it, and the restart begins the download again from zero. Measured 2026-09-20: one pod
#    pulled at ~1 MB/s and never passed 60 s before being restarted, leaving an EMPTY run.log and a
#    card idle at 0% GPU while its twin proved in 15 s.
#
#    `-C -` continues a partial file, `-S` lets an error through -s, and the size is checked rather
#    than assumed -- `chmod +x` on a half-downloaded file makes it look present and ready.
#    The driver normally stages this BEFORE the clock; this path is the fallback.
# ⛔ THE URL AND THE SIZE MUST COME FROM ONE PLACE. This file used to hardcode BOTH a v0.21.0 URL and
#    EXPECT_BIN_BYTES=407133112. When the driver's pin moved to v0.21.7 the card ended up with a
#    perfectly good 410,441,528-byte binary that THIS script called short, so it tried to resume past
#    the end of a complete file and the server answered 416:
#
#        curl: (22) The requested URL returned error: 416
#        prover download failed (have 410441528 of 407133112 bytes)
#
#    prove-chunk then never ran, so there was no prove.log at all, the GPU sat at 0%, and the tick
#    planner restarted the card every ~100 s for ever. A DOWNLOAD THAT SUCCEEDED REPORTED FAILURE.
#    ⚠ The driver exports HAZYNC_HOST_URL/HAZYNC_HOST_BYTES so both ends agree by construction; the
#    default below is only for the standalone callers (mile3.sh, run_continuous.sh).
BIN_URL="${HAZYNC_HOST_URL:-https://github.com/bitcoin-ghost/hazync/releases/download/v0.21.7/hazync-host-x86_64-linux-gnu-cuda}"
# ⛔ SIZE DERIVED FROM THE URL, NOT A CONSTANT. A constant is a second place to forget.
EXPECT_BIN_BYTES="${HAZYNC_HOST_BYTES:-$(curl -fsSLI "$BIN_URL" 2>/dev/null \
  | awk 'BEGIN{IGNORECASE=1} /^content-length:/{v=$2} END{gsub(/\r/,"",v); print v}')}"
case "$EXPECT_BIN_BYTES" in
  ''|*[!0-9]*)
    # ⛔ NEVER carry on with an unknown expected size: every comparison below would be against an
    # empty string, so a truncated binary would pass and fail later as an unreadable ELF.
    echo "cannot determine the prover's size from $BIN_URL (set HAZYNC_HOST_BYTES to skip the HEAD)" >&2
    exit 1 ;;
esac
HAVE=$(stat -c%s "$BIN" 2>/dev/null || echo 0)
# ⛔ A FILE LARGER THAN EXPECTED CAN NEVER BE RESUMED. `-C -` asks for a range starting past EOF and
#    the server answers 416, which -f turns into a hard failure. Resume is only ever valid when what
#    we have is SHORTER. Anything else is a different binary: start again.
if [ "$HAVE" -gt "$EXPECT_BIN_BYTES" ]; then
  echo "prover on disk is $HAVE bytes, expected $EXPECT_BIN_BYTES — a different build; re-fetching" >&2
  rm -f "$BIN"; HAVE=0
fi
if [ ! -x "$BIN" ] || [ "$HAVE" != "$EXPECT_BIN_BYTES" ]; then
  curl -fsSL -S -C - -o "$BIN" "$BIN_URL" || {
    echo "prover download failed (have $(stat -c%s "$BIN" 2>/dev/null || echo 0) of $EXPECT_BIN_BYTES bytes from $BIN_URL)" >&2
    exit 1
  }
  GOT=$(stat -c%s "$BIN" 2>/dev/null || echo 0)
  [ "$GOT" = "$EXPECT_BIN_BYTES" ] || { echo "prover is $GOT bytes, expected $EXPECT_BIN_BYTES" >&2; exit 1; }
  chmod +x "$BIN"
fi
# ⛔ THE FIXTURE COMES FROM THE CHECKOUT, NOT A URL. This used to
#      curl -fsSLO "https://hazync.org/repro/${BLOCK}.gz"
#    and that path has been 404 since at least 2026-09-18 -- measured with a full GET, following
#    redirects: the files AND the /repro/ directory itself all return the 1358-byte error page. With
#    curl -f that is a hard failure, so on any pod without the fixture already staged this script
#    stopped before proving anything (hazync#395).
#
#    Every fixture it wants is committed: prover/block_{130000,140000,741000,962000,965500}.json. So
#    take it from a checkout, and if there is none, say exactly what to copy rather than failing on a
#    download that cannot work.
if [ ! -f "/workspace/$BLOCK" ]; then
    for _d in "${HAZYNC_REPO:-}/prover" /hazync-zkvm/prover "$HOME/hazync-zkvm/prover" ./prover .; do
        [ -n "$_d" ] && [ -f "$_d/$BLOCK" ] && { cp "$_d/$BLOCK" "/workspace/$BLOCK"; break; }
    done
fi
if [ ! -f "/workspace/$BLOCK" ]; then
    echo "no $BLOCK at /workspace and no checkout to take it from." >&2
    echo "It is committed in the repo — copy it over, e.g.:" >&2
    echo "    scp <host>:/hazync-zkvm/prover/$BLOCK /workspace/$BLOCK" >&2
    echo "or set HAZYNC_REPO to a checkout on this box." >&2
    exit 1
fi

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
