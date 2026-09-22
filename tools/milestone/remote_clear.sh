#!/bin/bash
# ⛔ pipefail ONLY — see tools/milestone/README.md § Shell posture (hazync#462). Not -e: these report
# per card and must not abort the fleet over one bad one. Not -u YET: they carry single-quoted ssh
# payloads whose variables the REMOTE shell expands, a static scan cannot tell those from local
# reads, and -u added blind fails the first real run. pipefail is unconditional — it catches the
# `cmd | grep -c` class, where $? reports the LAST command and a failed producer reads as success.
set -o pipefail

cd /workspace || exit 1
# Reassignment dirs MUST go: a stale /workspace/reN/prove.log still contains a "segments at po2"
# line, so the watchdog thinks proving began, applies the tight stall limit to a file that will
# never grow again, and bounces healthy cards forever. This is what broke run 3 twice.
rm -rf /workspace/re[0-9]*
rm -f chunk_*.bin chunk_*.hzk result.json prove.log gpu_samples.csv run*.log agg*.log c4.log
echo "LEFT:$(ls chunk_*.bin chunk_*.hzk 2>/dev/null | wc -l) REDIRS:$(ls -d /workspace/re[0-9]* 2>/dev/null | wc -l)"
for p in $(pgrep -f pod-prove); do kill -9 "$p" 2>/dev/null; done
for p in $(pgrep -f hazync-host); do kill -9 "$p" 2>/dev/null; done
