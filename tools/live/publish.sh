#!/bin/bash
# Publish the live frame to the public page. Runs WHERE THE RENDERER RUNS (the tip box), pushing one
# file one way to the web box.
#
#   HAZYNC_PUBLISH_DEST=hazync-web:/var/www/hazync/live ./publish.sh /path/to/frame.png
#   ./publish.sh --loop /path/to/frame.png        # publish whenever the frame changes
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

DEST=${HAZYNC_PUBLISH_DEST:?set HAZYNC_PUBLISH_DEST, e.g. hazync-web:/var/www/hazync/live}
KEY=${HAZYNC_PUBLISH_KEY:-}
LOOP=0
[ "${1:-}" = "--loop" ] && { LOOP=1; shift; }
FRAME=${1:?usage: publish.sh [--loop] <frame.png>}

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
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

if [ "$LOOP" = 0 ]; then
  publish_once
  exit $?
fi

# ⛔ PUBLISH ONLY WHAT CHANGED. The renderer writes a frame a second; re-uploading an identical one
#    burns the tip box's uplink for nothing. mtime is the cheap test and it is the right one.
last=""
while true; do
  cur=$(stat -c %Y "$FRAME" 2>/dev/null || echo "")
  if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
    publish_once && last="$cur"
  fi
  sleep 2
done
