HAZYNC LIVE RIG — the mp4's renderer, driven by real pods.

RUN IT (from WSL, in this folder):
  1) pods              HAZYNC_RUNDIR=/home/defenwycke/tiprun-fleet ./tip-stream.sh start     # needs pods.txt from fleet.sh
  2) snapshot          python3 collect.py --rundir /home/defenwycke/tiprun-fleet --loop
  3) frame             python3 tip24live.py --loop
  4) open dash.html    (shows frame.png, refreshing once a second)

NO PODS? prove the pipeline first:
  python3 collect.py --demo --once && python3 tip24live.py --once && open dash.html

WHAT FEEDS WHAT
  tip-stream.sh   one persistent ssh per pod; 1 line/sec:
                  epoch,util,mem,temp,power_w,sm,mem_clk,phase,seg_n,seg_total,block
                  phase/segments parsed with the WORKER'S OWN regexes (dist/hazync-worker:636-640):
                    "executed, N segments" | "segment n/N" | "assembling N segment receipts"
                  -> proving and folding are MEASURED, not assumed.
  collect.py      streams + api.hazync.org/api/state?slim=1  ->  snapshot.json
  tip24live.py    snapshot.json -> frame.png, using tip24e's layout constants (same as the film)
  dash.html       displays frame.png

REAL vs NOT
  real: GPU power/util per card (1 Hz), segment progress, phase, block heights, chain %,
        cost (RunPod costPerHr from pods.txt x card-hours)
  not drawn at all: txs/inputs per block (not in pod telemetry) — omitted rather than invented

BEFORE THE RUN
  * gpu_samples-style telemetry cannot be reconstructed after a pod is gone. Start tip-stream.sh
    BEFORE the run, not during it.
  * a tip run needs bundles at tip heights, and workers driven at EXPLICIT heights
    (`hazync run <h>`): pick()/claim() hand out the earliest open block, never the tip.
  * one hazync identity per pod, or 30 pods appear as one worker.

PUBLISHING IT (hazync.org/live/)
  On the TIP BOX, beside the renderer:
    HAZYNC_PUBLISH_DEST=hazync-web:/var/www/hazync/live \
    HAZYNC_PUBLISH_KEY=~/.ssh/hazync_publish ./publish.sh --loop frame.png

  public.html is the page; install it as index.html in that directory.

  * DO NOT run the collector or the streamer on the web box. They need ssh to every pod and the web
    box is the most exposed machine there is. Keep the pod key on the tip box and push a PNG one way.
  * publish.sh sends frame.png and meta.json ONLY. rsync renames into place -- nginx will serve a
    half-written PNG if you "simplify" this to scp.
  * --chmod=F644 is not optional: mktemp makes meta.json 600, nginx 403s it, and the page then shows
    "no signal" for ever beside a frame.png that loads perfectly.
  * meta.json carries the frame's mtime, so the page can go visibly stale. Without it a dead renderer
    leaves the last frame on the page for ever, looking live -- the timestamp drawn into the image
    reads as "now" to anyone who does not know better.
  * The frame shows total spend and $/hr. That is public by decision (2026-09-20).
