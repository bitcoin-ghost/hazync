#!/usr/bin/env bash
# Build the reproduce container, and tell a STALL apart from a slow build (hazync#146).
#
# Three times in two days a "failed build" turned out to be the dependency-FETCH phase, with no
# compiler ever running. It is not diagnosable from the log: rustup prints no progress on a
# non-TTY, so a healthy download and a dead socket look identical. Log growth is the WRONG
# liveness signal for this step.
#
# CPU time is the right signal for a hang; network RX is the right signal for a download. Both must
# be measured INSIDE the build, not on the machine. A first version of this script sampled
# host-wide counters and could not fire at all: the coordinator also runs an archive node, so
# machine-wide RX never goes quiet, and the "flat CPU AND flat network" test never held. Verified
# against a `RUN sleep 600` positive control, which it sat through for 200 seconds without a word.
#
# So both counters are read from the step's OWN processes:
#   CPU  — utime+stime of the build-step pids
#   RX   — /proc/<pid>/net/dev, which is the step container's network namespace, not the host's
# Build-step pids are found by diffing the set of docker cgroups against a baseline taken before
# the build starts, so other containers on the same box are not mistaken for this build.
#
# Also: never pipe the build through `tail`/`head`. Both buffer, so the log stays empty until the
# command exits, which is how the first stall stayed invisible for 40 minutes.
set -uo pipefail

IMAGE="${IMAGE:-hazync-repro:latest}"
LOG="${LOG:-/tmp/hazync-repro-build.log}"
STALL_MINUTES="${STALL_MINUTES:-8}"
POLL_SECONDS="${POLL_SECONDS:-30}"

cd "$(dirname "$0")/.." || exit 90
[ -f reproduce/Dockerfile ] || { echo "no reproduce/Dockerfile here"; exit 90; }

TICKS=$(getconf CLK_TCK 2>/dev/null || echo 100)

# Every docker cgroup id currently on the box. Anything in here is NOT ours.
docker_cgroups() {
  awk '/:docker:/{ sub(/.*:docker:/, ""); print }' /proc/[0-9]*/cgroup 2>/dev/null | sort -u
}

# Pids whose docker cgroup appeared after we started: the build's own step processes.
step_pids() {
  local p cg
  for p in /proc/[0-9]*; do
    cg=$(grep -o ':docker:.*' "$p/cgroup" 2>/dev/null) || continue
    cg=${cg#:docker:}
    grep -qxF "$cg" "$BASELINE" && continue
    echo "${p#/proc/}"
  done
}

# CPU seconds consumed by the step processes.
step_cpu() {
  local pid total=0 u s
  for pid in $(step_pids); do
    read -r u s < <(awk '{print $14, $15}' "/proc/$pid/stat" 2>/dev/null) || continue
    total=$(( total + u + s ))
  done
  echo $(( total / TICKS ))
}

# Bytes received inside the step containers' network namespaces.
step_rx() {
  local pid total=0 ns seen=""
  for pid in $(step_pids); do
    ns=$(readlink "/proc/$pid/ns/net" 2>/dev/null) || continue
    case "$seen" in *"$ns"*) continue ;; esac
    seen="$seen $ns"
    total=$(( total + $(awk '/:/ && $1 !~ /^lo:/ {gsub(/.*:/,""); s+=$1} END{print s+0}' "/proc/$pid/net/dev" 2>/dev/null || echo 0) ))
  done
  echo "$total"
}

BASELINE=$(mktemp); trap 'rm -f "$BASELINE"' EXIT
docker_cgroups > "$BASELINE"

: > "$LOG"
echo "building $IMAGE  ->  $LOG"
echo "stall = the build's own processes burn no CPU and receive nothing for ${STALL_MINUTES}m"

docker build --progress=plain -f reproduce/Dockerfile -t "$IMAGE" . >> "$LOG" 2>&1 &
BUILD_PID=$!

last_cpu=$(step_cpu); last_rx=$(step_rx); last_npids=$(step_pids | wc -l); flat=0
limit=$(( STALL_MINUTES * 60 / POLL_SECONDS ))
[ "$limit" -lt 1 ] && limit=1

while kill -0 "$BUILD_PID" 2>/dev/null; do
  sleep "$POLL_SECONDS"
  kill -0 "$BUILD_PID" 2>/dev/null || break

  npids=$(step_pids | wc -l)
  cpu=$(step_cpu); rx=$(step_rx)
  d_cpu=$(( cpu - last_cpu )); d_rx=$(( rx - last_rx ))
  step=$(grep -oE '^#[0-9]+ \[[^]]+\]' "$LOG" | tail -1)

  # A NEGATIVE cpu delta means processes EXITED between polls -- their utime+stime left the sum. That
  # is evidence of progress, not of a stall, and the earlier `-le 0` test counted it as flat. Observed
  # live: "FLAT (cpu +-99s, rx +0B, 37 pids)" on a build that was compiling hard. Enough staggered
  # exits in a row could have killed a healthy build, which is the exact failure this script exists to
  # prevent. Only an EXACTLY zero delta over an UNCHANGED process set counts as flat now.
  if [ "$npids" -eq 0 ]; then
    # Between steps: buildkit is exporting layers or resolving cache inside dockerd, where there is
    # no step process to measure. Not a stall, and counting it as one would kill healthy builds.
    echo "  [$(date +%H:%M:%S)] ${step:-starting} — between steps"
    flat=0
  elif [ "$d_cpu" -eq 0 ] && [ "$npids" -eq "$last_npids" ] && [ "$d_rx" -lt 65536 ]; then
    flat=$(( flat + 1 ))
    echo "  [$(date +%H:%M:%S)] ${step:-?} — FLAT (cpu +${d_cpu}s, rx +${d_rx}B, ${npids} pids) ${flat}/${limit}"
    if [ "$flat" -ge "$limit" ]; then
      echo
      echo "STALLED: the build's own processes did nothing for ${STALL_MINUTES} minutes."
      echo "  last step: ${step:-<none>}"
      echo "  This is the hazync#146 fetch hang, not a slow build. Killing it."
      kill "$BUILD_PID" 2>/dev/null; wait "$BUILD_PID" 2>/dev/null
      echo "REAL_EXIT=99" >> "$LOG"
      exit 99
    fi
  else
    [ "$flat" -gt 0 ] && echo "  [$(date +%H:%M:%S)] moving again (cpu +${d_cpu}s, rx +${d_rx}B)"
    flat=0
  fi
  last_cpu=$cpu; last_rx=$rx; last_npids=$npids
done

wait "$BUILD_PID"; rc=$?
# Written INTO the log: a backgrounded wrapper's own exit status is not trustworthy, and a log that
# cannot say how it ended is a check that cannot fail.
echo "REAL_EXIT=$rc" >> "$LOG"

if [ "$rc" -ne 0 ]; then
  echo "build FAILED (exit $rc). Last 30 lines:"; tail -30 "$LOG"; exit "$rc"
fi

echo "build OK. Confirming the guest image id is canonical:"
got=$(docker run --rm "$IMAGE" /hazync-zkvm/prover/target/release/host method-id 2>&1 | awk '/^METHOD_ID/{print $2}')
want=$(grep -oE '^[0-9a-f]{64}$' reproduce/METHOD_ID | head -1)
echo "  built:  ${got:-<none>}"
echo "  pinned: ${want:-<none>}"
if [ -z "$got" ] || [ "$got" != "$want" ]; then
  echo "MISMATCH — this image is NOT canonical. Do not wrap fixtures or cut a release with it."
  exit 98
fi
echo "canonical ✓"
