#!/bin/bash
# Publish the live frame to the public page. Runs WHERE THE RENDERER RUNS (the tip box), pushing one
# file one way to the web box.
#
#   HAZYNC_PUBLISH_DEST=root@152.53.86.216: HAZYNC_PUBLISH_KEY=~/.ssh/hazync_publish \
#     ./publish.sh --loop frame.png
#
# ⛔ THE DESTINATION PATH IS RELATIVE TO THE RESTRICTED ROOT, SO IT IS EMPTY. The publish key is
#    pinned to `rrsync -wo -no-del /var/www/hazync/live`, and rrsync resolves every path INSIDE that
#    directory -- so a dest of `host:/var/www/hazync/live` is asked for
#    /var/www/hazync/live/var/www/hazync/live and fails with a "No such file or directory" that reads
#    like a missing target rather than a path that was doubled. Verified against the live host.
#
# ⛔ THE COLLECTOR MUST NOT RUN ON THE WEB BOX. It needs ssh to every pod, and the web box is the
#    most exposed machine there is. Keep the pod key on the tip box; push a PNG the other way.
#
# ⛔ PUBLISH ATOMICALLY. nginx will happily serve a half-written PNG -- a torn frame that looks like
#    a rendering bug rather than a transfer in progress. rsync renames into place; scp does not, so
#    do not "simplify" this to scp.
#
# ⛔ A STALE FRAME LOOKS EXACTLY LIKE A LIVE ONE. If the renderer dies, the last frame sits on the
#    page indefinitely showing a fleet that stopped hours ago. meta.json carries the render time so
#    the page can say so; publishing the PNG without it would be publishing a lie with a timestamp
#    baked into the image.
set -u

DEST=${HAZYNC_PUBLISH_DEST:?set HAZYNC_PUBLISH_DEST, e.g. root@152.53.86.216: (path relative to the restricted root, so usually empty)}

# ⚠ A dest carrying the restricted directory's own path is the mistake this catches -- it doubles
# under rrsync and the error names a directory nobody typed.
case "$DEST" in
  *:/var/www/*) echo "refusing: HAZYNC_PUBLISH_DEST must be anchored at the restricted root." >&2
                echo "  the key is pinned to /var/www/hazync/live, so use 'user@host:' with no path." >&2
                exit 2 ;;
esac
KEY=${HAZYNC_PUBLISH_KEY:-}
LOOP=0
[ "${1:-}" = "--loop" ] && { LOOP=1; shift; }
FRAME=${1:?usage: publish.sh [--loop] <frame.png>}

# ⛔ WITHOUT CONNECTION REUSE THIS LOOP CANNOT KEEP UP WITH ITSELF.
# Each tick makes TWO rsync calls, and each one opened a fresh ssh. Measured to the live web box
# 2026-09-22:
#
#     cold connection   0.48 - 0.71 s
#     reused (control)  0.07 s
#
# Two cold handshakes is ~1.0 s of setup inside a `sleep 1` loop -- before a single byte of the
# 108 KB frame moves. The public page would fall to ~2 s per update and the run would make ~172,800
# handshakes over 24 hours, for no benefit.
#
# ControlMaster=auto opens one connection and reuses it; ControlPersist keeps it warm across ticks.
# ⚠ The socket lives beside this script's own runtime, not in /tmp shared with anything else, and
# the path is per-destination so two publishers cannot collide on one socket.
CTL_DIR=${HAZYNC_PUBLISH_CTL_DIR:-${TMPDIR:-/tmp}/hazync-publish-$(id -u)}
mkdir -p "$CTL_DIR" && chmod 700 "$CTL_DIR"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o ControlMaster=auto -o "ControlPath=$CTL_DIR/%r@%h:%p" -o ControlPersist=60)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")

publish_once() {
  [ -s "$FRAME" ] || { echo "no frame at $FRAME"; return 1; }

  # The render time is the FILE's mtime, not `date` -- publishing is not rendering, and a publisher
  # that stamps its own clock would mark a three-hour-old frame as fresh every time it retried.
  local mtime size meta
  mtime=$(stat -c %Y "$FRAME") || return 1
  size=$(stat -c %s "$FRAME") || return 1
  meta=$(mktemp); trap 'rm -f "$meta"' RETURN
  printf '{"rendered_at": %s, "bytes": %s, "published_at": %s}\n' \
         "$mtime" "$size" "$(date +%s)" > "$meta"

  # Renamed into place on arrival, and MODE 644 EXPLICITLY.
  # ⛔ mktemp creates 600. Without --chmod the meta lands unreadable by nginx, which 403s it while
  #    frame.png (644) serves perfectly -- so the page shows "no signal" for ever next to a frame
  #    that is loading fine. Measured locally: meta.json arrived 600, frame.png 644.
  rsync -q --chmod=F644 -e "ssh ${SSH_OPTS[*]}" "$FRAME" "$DEST/frame.png" || return 1
  rsync -q --chmod=F644 -e "ssh ${SSH_OPTS[*]}" "$meta" "$DEST/meta.json"  || return 1
  echo "$(date -u +%H:%M:%S) published ${size} bytes (rendered $(date -u -d @"$mtime" +%H:%M:%S) UTC)"
}

# ⚠ Tear the shared connection down on the way out. A ControlPersist socket outliving the script
# is a live authenticated channel to the web box with nothing watching it.
cleanup_ctl() { ssh "${SSH_OPTS[@]}" -O exit "${DEST%%:*}" >/dev/null 2>&1 || true; }
trap cleanup_ctl EXIT INT TERM

if [ "$LOOP" = 0 ]; then
  publish_once
  exit $?
fi

# ⛔ PUBLISH ONLY WHAT CHANGED. The renderer writes a frame a second; re-uploading an identical one
#    burns the tip box's uplink for nothing. mtime is the cheap test and it is the right one.
# ⛔ SLEEP THE REMAINDER, NOT A FLAT SECOND. `sleep 1` ran AFTER the work, so the real period was
# publish_time + 1 s -- measured 1.65 s per update with connection reuse, 2.46 s without. The
# renderer writes a frame every second, so roughly every other frame was skipped and the public page
# ran up to 1.65 s behind what had already been drawn.
#
# ⚠ A tick that overruns its interval sleeps ZERO and goes again immediately, which is correct: it
# is already behind. It cannot spin, because a failed publish returns fast and the remainder is then
# nearly the whole interval.
INTERVAL=${HAZYNC_PUBLISH_INTERVAL:-1}
last=""
while true; do
  t0=$(date +%s.%N)
  cur=$(stat -c %Y "$FRAME" 2>/dev/null || echo "")
  if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
    publish_once && last="$cur"
  fi
  rest=$(awk -v a="$t0" -v b="$(date +%s.%N)" -v i="$INTERVAL" \
             'BEGIN{d=i-(b-a); printf "%.3f", (d>0?d:0)}')
  sleep "$rest"
done
