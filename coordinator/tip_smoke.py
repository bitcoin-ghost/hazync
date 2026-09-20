#!/usr/bin/env python3
"""A live two-card smoke run: rent, prove one block, feed the dashboard, release. (Phase 5 ③)

Everything below is already unit-tested. What this exercises is the part no test can: real ssh, real
pods, real RunPod port mapping, and the dashboard feed against a stream that a real card is writing.
Four of the bugs found on 2026-09-20 were of exactly that kind and none of them was visible to 254
passing cases.

    python3 tip_smoke.py --cards 2 --block block_130000.json

⛔ IT ALWAYS RELEASES WHAT IT RENTED. The teardown runs from a finally: a smoke run that leaves two
cards billing because it raised somewhere in the middle is a worse outcome than one that fails.
⛔ IT NEVER TOUCHES A POD IT DID NOT CREATE — `tip_session.terminable` decides, and the protected
names are refused there by name as well.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sponsor_bot                      # noqa: E402  (the RunPod client only)
import tip_dashboard                    # noqa: E402
import tip_driver                       # noqa: E402
import tip_lifecycle                   # noqa: E402
import tip_run                          # noqa: E402
import tip_runner                       # noqa: E402
import tip_session                      # noqa: E402

PREFIX = "hz-smoke-"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class SmokeRunPod(sponsor_bot.RunPod):
    """The stock client, plus the one thing the aggregate needs: 9110 published.

    ⛔ sponsor_bot.deploy asks for `ports: "22/tcp"` only, which is right for a sponsor worker — it
    dials out and never listens. The tip aggregate is the opposite: seg-serve LISTENS on 9110 and the
    other cards dial it. With only 22 published there is no external port for 9110 at all, so every
    worker sits in its 600-second retry loop looking armed and contributing nothing.
    """

    def deploy_listening(self, name, ssh_pubkey, gpu_types=sponsor_bot.GPU_TYPES):
        refused, answered = None, False
        for gt in gpu_types:
            q = ("mutation { podFindAndDeployOnDemand(input: { cloudType: ALL, gpuCount: 1, "
                 "volumeInGb: 0, containerDiskInGb: 40, "
                 f"gpuTypeId: {self._s(gt)}, name: {self._s(name)}, "
                 f"imageName: {self._s(sponsor_bot.IMAGE)}, ports: \"22/tcp,9110/tcp\", "
                 f"env: [{{key: \"PUBLIC_KEY\", value: {self._s(ssh_pubkey)}}}] }}) "
                 "{ id costPerHr } }")
            try:
                p = self._gql(q).get("podFindAndDeployOnDemand")
                answered = True
            except sponsor_bot.RunPodError as e:
                refused, p = e, None
            if p and p.get("id"):
                return {"id": p["id"], "name": name, "gpu_type": gt,
                        "price": float(p.get("costPerHr") or 0)}
        if refused is not None and not answered:
            raise refused
        return None

    def ports_of(self, pod_id):
        """{privatePort: (ip, publicPort)} for one pod, or {} while it is still starting."""
        q = ("query { myself { pods { id name runtime { ports "
             "{ ip isIpPublic privatePort publicPort } } } } }")
        for p in ((self._gql(q).get("myself") or {}).get("pods") or []):
            if p["id"] != pod_id:
                continue
            out = {}
            for x in (((p.get("runtime") or {}).get("ports")) or []):
                if x.get("isIpPublic"):
                    out[int(x["privatePort"])] = (x["ip"], int(x["publicPort"]))
            return out
        return {}


def wait_for_ssh(api, pods, ssh, timeout_s=420, need=None):
    """Poll RunPod for the mapped ports, then prove the card answers. Returns {name: Card}."""
    t0, ready, portmap = time.time(), {}, {}
    target = need or len(pods)
    while time.time() - t0 < timeout_s and len(ready) < target:
        for p in pods:
            if p["name"] in ready:
                continue
            ports = api.ports_of(p["id"])
            if 22 not in ports:
                continue
            ip, port = ports[22]
            card = tip_driver.Card(p["name"], ip, port)
            # ⛔ A MAPPED PORT IS NOT A LIVE CARD. RunPod publishes the mapping before sshd is up, so
            # taking the mapping as readiness means the first real command fails on a card the run
            # believes it has. Judge by a command that came back.
            if (ssh.run(card, "echo READY") or "").strip().endswith("READY"):
                # ⛔ Card defines __slots__ ON PURPOSE (cid/ip/port/loc/workdir) so that two Card
                # objects for one pod stay equal and hashable as dict keys. Hanging an extra
                # attribute on it raises AttributeError -- which is what ended the first live run,
                # after the cards were up and answering. Keep the port map BESIDE the cards.
                ready[p["name"]] = card
                portmap[p["name"]] = ports
                log(f"  {p['name']} up at {ip}:{port}"
                    + (f"  (9110 -> {ports[9110][1]})" if 9110 in ports else "  ⛔ NO 9110 MAPPING"))
        if len(ready) < target:
            time.sleep(10)
    # ⚠ "only 1/2 came up" is not actionable. Say how far each one got: no mapping at all is a pod
    # RunPod never started, a mapping with no ssh is a pod that is booting or broken.
    # ⚠ REPORT THE TIME ACTUALLY WAITED, NOT THE BUDGET. Once `need` cards are up the loop exits
    # early, and printing the configured timeout there claims a card was given 420 s when it was
    # given 37 -- which would send the next reader hunting a dead pod that was merely slower than
    # its twins.
    waited = time.time() - t0
    for p in pods:
        if p["name"] not in ready:
            got = api.ports_of(p["id"])
            why = ("RunPod never published a port for it (the pod did not start)" if not got
                   else f"ports {sorted(got)} were published but ssh never answered")
            enough = "" if waited >= timeout_s - 1 else " (the run had enough cards and stopped waiting)"
            log(f"  ⚠ {p['name']} had not answered after {waited:.0f}s{enough} — {why}")
    return ready, portmap


HOST_URL = ("https://github.com/bitcoin-ghost/hazync/releases/download/v0.21.0/"
            "hazync-host-x86_64-linux-gnu-cuda")


def binary_size():
    """The prover binary's real size, from the release. Never guessed."""
    import urllib.request
    req = urllib.request.Request(HOST_URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def fetch_binary(ssh, card, want, *, tries=40, wait_s=15):
    """Pull the 407 MB prover onto the card BEFORE the clock starts, resuming if interrupted.

    ⛔ A 407 MB DOWNLOAD HAS NO BUSINESS INSIDE THE TIMED RUN. pod-prove.sh fetches it on first use,
    which means a card on a slow link spends the run downloading -- and the tick planner, which
    judges a card by whether its prove.log is growing, sees no growth and restarts it. `curl -o`
    truncates, so every restart began the download AGAIN from zero.

    Measured 2026-09-20: one pod pulled at ~1 MB/s (4.8 MB -> 18.6 MB in 14 s) and never got past
    60 seconds before being restarted, while its twin had the whole binary and proved in 15 s. The
    run could never finish, and the only visible symptom was one card sitting at 0% GPU.

    ⚠ `-C -` RESUMES. Without it this has the same failure as pod-prove.sh, just earlier.
    ⚠ The card fetches from the CDN itself; pushing it from here would put 407 MB per card through
      the driver's uplink for no benefit.
    """
    got = -1
    for _ in range(tries):
        out = ssh.run(card, "stat -c%s /workspace/hazync-host-cuda 2>/dev/null || echo 0",
                      timeout=60) or "0"
        got = int((out.strip().splitlines() or ["0"])[-1] or 0)
        if got == want:
            ssh.run(card, "chmod +x /workspace/hazync-host-cuda", timeout=60)
            return True, got
        # nohup so the fetch survives this ssh session, -C - so a partial file continues.
        ssh.run(card,
                "pgrep -f 'curl.*hazync-host' >/dev/null 2>&1 || "
                f"nohup curl -fsSL -C - -o /workspace/hazync-host-cuda {HOST_URL} "
                "> /workspace/fetch.log 2>&1 < /dev/null & disown; exit 0", timeout=60)
        time.sleep(wait_s)
    return False, got


def prepare(ssh, card, *, block_path, block_name, repo_hint):
    """Stage the binary, the fixture and pod-prove.sh, and clear the CUDA compat trap."""
    # ⛔ CUDA ERROR 804 ON A CONSUMER CARD IS AN UNPREPARED CARD, NOT A BAD ONE. The driver's compat
    # libraries shadow the real ones; bootstrap2.sh moves them aside for exactly this reason.
    ssh.run(card, "for d in /usr/local/cuda*/compat; do [ -d \"$d\" ] && "
                  "mv \"$d\" \"${d}.disabled\"; done; ldconfig 2>/dev/null; true", timeout=120)
    ok_bin = ssh.push(card, os.path.join(repo_hint, "pod-prove.sh"), "/workspace/pod-prove.sh")
    ok_blk = ssh.push(card, block_path, f"/workspace/{block_name}")
    # ⛔ scp DOES NOT PRESERVE THE EXECUTABLE BIT. 0644 here killed all 23 cards on 2026-09-20 with
    # `setsid: failed to execute ./pod-prove.sh: Permission denied` and a zero-byte prove.log.
    ssh.run(card, "chmod +x /workspace/pod-prove.sh", timeout=60)
    size = (ssh.run(card, f"stat -c%s /workspace/{block_name} 2>/dev/null || echo 0") or "0").strip()
    size = (size.splitlines() or ["0"])[-1]
    log(f"  {card.cid}: pod-prove.sh={'ok' if ok_bin else 'FAILED'} "
        f"fixture={'ok' if ok_blk else 'FAILED'} ({size} bytes on the card)")
    return ok_bin and ok_blk and size.isdigit() and int(size) > 0


def assignment_preview(order):
    """chunk -> card, as run_block will hold it. Used before the clock so the probe sees the fleet."""
    return {i: c for i, c in enumerate(order)}

def dash_chain(a):
    """The three processes that turn telemetry into a published frame.

    ⚠ `collect.py` reaches the coordinator for chain facts and carries on without it, so a run is
    never blocked by the board being unreachable — the frame simply omits the chain figures.
    """
    py = sys.executable
    chain = [("collector", [py, os.path.join(a.live_rig, "collect.py"),
                            "--rundir", a.rundir, "--out", os.path.join(a.live_rig, "snapshot.json"),
                            "--loop", "--interval", "1"]),
             ("renderer", [py, os.path.join(a.live_rig, "tip24live.py"),
                           "--snap", os.path.join(a.live_rig, "snapshot.json"),
                           "--out", os.path.join(a.live_rig, "frame.png"), "--loop"])]
    if a.publish_dest:
        chain.append(("publisher", ["/bin/bash", os.path.join(a.live_rig, "publish.sh"),
                                    "--loop", os.path.join(a.live_rig, "frame.png")]))
    return chain


def cleanup(api, rented_path):
    """Release whatever a dead driver left behind. Safe to run at any time."""
    try:
        with open(rented_path) as fh:
            created = json.load(fh)
    except (OSError, ValueError):
        log(f"no rental record at {rented_path} — nothing to clean up")
        return 0
    ids = [p["id"] for p in created]
    v = tip_session.terminable(ids, ids)
    log(f"cleanup: releasing {v['terminate']} (protected={v['protected']} not-ours={v['not_ours']})")
    for pid in v["terminate"]:
        log(f"  {pid}: {'gone' if sponsor_bot.terminate_confirmed(api, pid) else '⛔ STILL LISTED'}")
    live = {p["id"] for p in api.pods()}
    left = [p["name"] for p in created if p["id"] in live]
    log(f"account check: {'clean' if not left else '⛔ STILL PRESENT: ' + str(left)}")
    if not left:
        try:
            os.unlink(rented_path)
        except OSError:
            pass
    return 0 if not left else 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=2)
    ap.add_argument("--spares", type=int, default=1,
                    help="extra pods to rent; the first --cards to answer run, the rest are released")
    ap.add_argument("--block", default="130000",
                    help="block HEIGHT, not a filename — FleetRunner builds block_<h>.json")
    ap.add_argument("--block-path", default="/repo/prover/block_130000.json")
    ap.add_argument("--rundir", default="/root/tiprun")
    ap.add_argument("--repo", default="/root/tipsmoke/milestone")
    ap.add_argument("--key", default="/root/.ssh/hz_smoke")
    ap.add_argument("--live-rig", default="/root/hazync-live-rig")
    ap.add_argument("--publish-dest", default="")
    ap.add_argument("--publish-key", default="/root/.ssh/hazync_publish")
    ap.add_argument("--keep", action="store_true", help="do NOT terminate (debugging only)")
    ap.add_argument("--cleanup", action="store_true",
                    help="release whatever rented.json records, and exit — for a driver that died hard")
    a = ap.parse_args()

    # publish.sh reads these from the environment; os.spawnv hands the child ours.
    if a.publish_dest:
        os.environ["HAZYNC_PUBLISH_DEST"] = a.publish_dest
        os.environ["HAZYNC_PUBLISH_KEY"] = a.publish_key

    # ⛔ A HEIGHT, NOT A FILENAME. FleetRunner._launch sets HAZYNC_BLOCK_NAME=f"block_{block}.json",
    # so passing "block_130000.json" asks every card for `block_block_130000.json.json`. pod-prove.sh
    # says so clearly in its own run.log -- but the run itself only sees cards that produce no
    # receipt, restarts them, and burns the fleet doing it. Measured: two RTX 4090s idle at 0% GPU
    # for five minutes while the tick planner dutifully relaunched them.
    if not str(a.block).isdigit():
        raise SystemExit(f"--block takes a HEIGHT, not a filename: got {a.block!r}. "
                         f"Try --block {''.join(c for c in str(a.block) if c.isdigit()) or '130000'}")
    block_name = f"block_{a.block}.json"

    key_file = os.environ.get("RUNPOD_API_KEY_FILE", "/root/.hazync/runpod.key")
    with open(key_file) as fh:
        api = SmokeRunPod(fh.read().strip())
    pub = open(a.key + ".pub").read().strip()

    ssh = tip_driver.SSHRunner(a.key)
    created, helpers, t_start = [], [], time.time()
    rented_path = os.path.join(a.rundir, "rented.json")
    os.makedirs(a.rundir, exist_ok=True)

    if a.cleanup:
        return cleanup(api, rented_path)

    # ⛔ SIGTERM MUST REACH THE finally. Python does not run finally blocks on a default SIGTERM --
    # the process simply dies, and the cards go on billing. Turning it into an exception is what
    # makes the teardown reachable at all from an outside `kill`.
    import signal
    def _bail(sig, _frm):
        raise KeyboardInterrupt(f"signal {sig}")
    signal.signal(signal.SIGTERM, _bail)

    try:
        # ── rent ──────────────────────────────────────────────────────────────────────────────────
        existing = {p["name"] for p in api.pods()}
        log(f"pods already on the account (untouched): {sorted(existing) or 'none'}")
        # ⛔ RENT SPARES. RunPod does not always start what it sells: on 2026-09-20 one of two pods
        # never published a port in 420 s while its twin answered in 30 s. Renting exactly N means one
        # bad pod ends the run after the full wait, having paid for both the whole time.
        want = a.cards + a.spares
        for i in range(want):
            name = f"{PREFIX}{i+1}"
            if name in existing:
                raise SystemExit(f"refusing: {name} already exists")
            p = api.deploy_listening(name, pub)
            if not p:
                raise SystemExit(f"no capacity for {name}")
            created.append(p)
            # ⛔ WRITE IT DOWN THE INSTANT IT EXISTS. There is NO BUDGET CAP by decision, so a driver
            # that dies without releasing leaves cards billing until someone notices. A SIGINT during
            # a probe fan-out did exactly that: ThreadPoolExecutor.__exit__ waits for every in-flight
            # ssh, so the teardown did not run for minutes and the pods had to be killed by hand.
            # `--cleanup` reads this file and releases whatever is in it, whatever happened.
            with open(rented_path, "w") as fh:
                json.dump(created, fh, indent=1)
            log(f"rented {name}  {p['gpu_type']}  ${p['price']:.3f}/hr  id={p['id']}"
                f"  (recorded in {rented_path})")

        cards, portmap = wait_for_ssh(api, created, ssh, need=a.cards)
        if len(cards) < a.cards:
            raise SystemExit(f"only {len(cards)} of {want} rented cards came up; needed {a.cards}")

        # The first `cards` that answered are the run; the rest are released straight away rather
        # than billed for a run they are not in.
        chosen = sorted(cards)[:a.cards]
        order = [cards[n] for n in chosen]
        spare = [p for p in created if p["name"] not in chosen]
        if spare:
            log(f"releasing {len(spare)} unused spare(s): {[p['name'] for p in spare]}")
            for p in spare:
                sponsor_bot.terminate_confirmed(api, p["id"])
            created = [p for p in created if p["name"] in chosen]
            with open(rented_path, "w") as fh:
                json.dump(created, fh, indent=1)
        agg = order[0]
        agg_ports = portmap.get(agg.cid, {})
        if 9110 not in agg_ports:
            raise SystemExit("the aggregator has no published 9110 — workers could never attach")
        agg_dial = agg_ports[9110][1]

        # ── card-to-card reachability, BEFORE the clock ───────────────────────────────────────────
        # ⛔ REACHING THE PORT FROM HERE PROVES NOTHING ABOUT THE WORKERS. Measured 2026-09-20: the
        # aggregate's published port answered from this box while the other pod got `No route to
        # host`. Pod-to-pod connectivity is not guaranteed and varies between rentals -- the first
        # pair that day could reach each other and the second could not. Untested, the run looks
        # armed and the aggregate sits at 0/N until the tick budget is spent.
        probe_runner = tip_runner.FleetRunner(
            ssh, agg, stage_dir=os.path.join(a.rundir, "stage"),
            agg_port=9110, agg_dial=agg_dial)
        reach = probe_runner.check_reachability(assignment_preview(order), secs=25)
        if reach is None:
            log("⚠ could not stand up a listener on the aggregator, so reachability is UNTESTED — "
                "continuing, because 'we could not test' is not 'they failed'")
        else:
            ok, why, unreachable = tip_lifecycle.reachability_verdict(
                {c.cid: v for c, v in reach.items()})
            log(f"reachability: {why}")
            if not ok:
                raise SystemExit(
                    f"{why}. These cards would sit in their retry loop contributing nothing while "
                    f"the fleet size everyone reasons about silently includes them. Rent "
                    f"replacements rather than running a fleet that does not exist.")

        # ── prepare ───────────────────────────────────────────────────────────────────────────────
        for c in order:
            if not prepare(ssh, c, block_path=a.block_path, block_name=block_name, repo_hint=a.repo):
                raise SystemExit(f"{c.cid} could not be prepared")

        # ⛔ THE BINARY, BEFORE THE CLOCK, AND VERIFIED BY SIZE. In parallel: on a slow card this is
        # minutes, and doing it one at a time would double that for no reason.
        want = binary_size()
        log(f"fetching the prover ({want/1e6:.0f} MB) onto {len(order)} cards before T0")
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=len(order)) as pool:
            got = list(pool.map(lambda c: (c, *fetch_binary(ssh, c, want)), order))
        for c, ok, n in got:
            log(f"  {c.cid}: {'ok' if ok else 'INCOMPLETE'} {n}/{want} bytes")
        bad = [c.cid for c, ok, _ in got if not ok]
        if bad:
            raise SystemExit(f"the prover never finished downloading on {bad} — "
                             f"a card that starts the run without it spends the run fetching, and "
                             f"the stall detector restarts it from zero every time")

        # ── the dashboard feed, BEFORE the clock ──────────────────────────────────────────────────
        os.makedirs(a.rundir, exist_ok=True)
        fleet = [{"id": p["name"], "price": p["price"], "gpu": p["gpu_type"], "pod_id": p["id"]}
                 for p in created]
        joined = tip_dashboard.feed_records(order, fleet)
        feed = tip_dashboard.DashboardFeed(
            a.rundir, script=os.path.join(a.live_rig, "tip-stream.sh"),
            run=lambda argv, env: os.spawnve(os.P_NOWAIT, argv[0], argv, {**os.environ, **env}),
            key=a.key)
        log(f"feed: {feed.start(joined['records'])} cards in pods.txt, "
            f"unassigned={joined['unassigned']}")

        # ⛔ THE FEED IS NOT THE DASHBOARD. tip-stream.sh only writes stream/<card>.csv; without
        # these three the telemetry lands on disk and the public page keeps showing whatever frame
        # was last published -- which on the first live run was a DEMO frame, while a real fleet was
        # proving. Each is started here and torn down in the finally.
        # ⛔ EACH HELPER GETS ITS OWN LOG. They inherit this process's stdout otherwise, and the
        # collector alone writes a line per second -- which buried the run's own progress in its log
        # and made the failure that mattered (the renderer dying on a missing PIL) one line in a
        # flood. A run's log should be the RUN.
        for name, argv in dash_chain(a):
            fd = os.open(os.path.join(a.live_rig, f"{name}.log"),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            pid = os.fork()
            if pid == 0:                                   # child
                os.dup2(fd, 1); os.dup2(fd, 2); os.close(fd)
                os.execv(argv[0], argv)
                os._exit(127)
            os.close(fd)
            helpers.append((name, pid))
            log(f"  started {name} -> {name}.log")
        time.sleep(8)                      # let the streamer produce a sample before T0

        # ── prove ─────────────────────────────────────────────────────────────────────────────────
        runner = tip_runner.FleetRunner(
            ssh, agg, stage_dir=os.path.join(a.rundir, "stage"),
            agg_port=9110, agg_dial=agg_dial,
            prove_env={"HAZYNC_LIFTX_HINT": "1", "HAZYNC_FIELD_BIGINT2": "1",
                       "HAZYNC_ECMULT_WINDOW": "21"})
        os.makedirs(runner.stage_dir, exist_ok=True)
        assignment = {i: order[i] for i in range(a.cards)}
        log(f"proving {block_name} on {a.cards} cards, aggregate on {agg.cid} "
            f"(binds 9110, dialled on {agg_dial})")

        result = tip_run.run_block(block=a.block, cards=assignment, runner=runner,
                                   now=time.time, sleep=time.sleep, feed=feed,
                                   max_ticks=1200, tick_s=6.0)
        log("RESULT " + json.dumps(result, indent=1))
        return 0 if result.get("ok") else 1

    finally:
        # ── release, whatever happened ────────────────────────────────────────────────────────────
        elapsed = time.time() - t_start
        spend = sum(p["price"] for p in created) * elapsed / 3600.0
        log(f"elapsed {elapsed/60:.1f} min, ~${spend:.3f} on {len(created)} cards")
        for name, pid in helpers:
            # ⚠ A HELPER THAT ALREADY DIED IS WORTH SAYING. The renderer died on a missing PIL during
            # the first run with the chain wired, and the only sign was a traceback in a flood of
            # collector output -- meanwhile the public page kept serving the previous frame.
            try:
                os.kill(pid, 15)
                log(f"  stopped {name}")
            except OSError:
                log(f"  ⚠ {name} was ALREADY DEAD before teardown — check {name}.log")
        try:
            feed.stop()
        except Exception:
            pass
        if a.keep:
            log("--keep: pods LEFT RUNNING and still billing: "
                + ", ".join(p["name"] for p in created))
        else:
            mine = [p["id"] for p in created]
            verdict = tip_session.terminable(mine, mine)
            log(f"terminating {verdict['terminate']}  "
                f"(protected={verdict['protected']} not-ours={verdict['not_ours']})")
            for pid in verdict["terminate"]:
                ok = sponsor_bot.terminate_confirmed(api, pid)
                log(f"  {pid}: {'gone' if ok else '⛔ STILL LISTED — TERMINATE BY HAND'}")
            left = {p["name"] for p in api.pods()} & {p["name"] for p in created}
            log(f"account check: {'clean' if not left else '⛔ STILL PRESENT: ' + str(left)}")
            if not left:
                try:
                    os.unlink(rented_path)     # nothing outstanding; the record would only mislead
                except OSError:
                    pass


if __name__ == "__main__":
    sys.exit(main())
