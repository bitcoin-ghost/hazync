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


def wait_for_ssh(api, pods, ssh, timeout_s=420):
    """Poll RunPod for the mapped ports, then prove the card answers. Returns {name: Card}."""
    t0, ready, portmap = time.time(), {}, {}
    while time.time() - t0 < timeout_s and len(ready) < len(pods):
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
        if len(ready) < len(pods):
            time.sleep(10)
    return ready, portmap


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
    log(f"  {card.cid}: pod-prove.sh={'ok' if ok_bin else 'FAILED'} "
        f"fixture={'ok' if ok_blk else 'FAILED'} ({size} bytes on the card)")
    return ok_bin and ok_blk and size.isdigit() and int(size) > 0


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=2)
    ap.add_argument("--block", default="block_130000.json")
    ap.add_argument("--block-path", default="/repo/prover/block_130000.json")
    ap.add_argument("--rundir", default="/root/tiprun")
    ap.add_argument("--repo", default="/root/tipsmoke/milestone")
    ap.add_argument("--key", default="/root/.ssh/hz_smoke")
    ap.add_argument("--live-rig", default="/root/hazync-live-rig")
    ap.add_argument("--publish-dest", default="")
    ap.add_argument("--publish-key", default="/root/.ssh/hazync_publish")
    ap.add_argument("--keep", action="store_true", help="do NOT terminate (debugging only)")
    a = ap.parse_args()

    # publish.sh reads these from the environment; os.spawnv hands the child ours.
    if a.publish_dest:
        os.environ["HAZYNC_PUBLISH_DEST"] = a.publish_dest
        os.environ["HAZYNC_PUBLISH_KEY"] = a.publish_key

    key_file = os.environ.get("RUNPOD_API_KEY_FILE", "/root/.hazync/runpod.key")
    with open(key_file) as fh:
        api = SmokeRunPod(fh.read().strip())
    pub = open(a.key + ".pub").read().strip()

    ssh = tip_driver.SSHRunner(a.key)
    created, helpers, t_start = [], [], time.time()

    try:
        # ── rent ──────────────────────────────────────────────────────────────────────────────────
        existing = {p["name"] for p in api.pods()}
        log(f"pods already on the account (untouched): {sorted(existing) or 'none'}")
        for i in range(a.cards):
            name = f"{PREFIX}{i+1}"
            if name in existing:
                raise SystemExit(f"refusing: {name} already exists")
            p = api.deploy_listening(name, pub)
            if not p:
                raise SystemExit(f"no capacity for {name}")
            created.append(p)
            log(f"rented {name}  {p['gpu_type']}  ${p['price']:.3f}/hr  id={p['id']}")

        cards, portmap = wait_for_ssh(api, created, ssh)
        if len(cards) != a.cards:
            raise SystemExit(f"only {len(cards)}/{a.cards} cards came up")

        order = [cards[f"{PREFIX}{i+1}"] for i in range(a.cards)]
        agg = order[0]
        agg_ports = portmap.get(agg.cid, {})
        if 9110 not in agg_ports:
            raise SystemExit("the aggregator has no published 9110 — workers could never attach")
        agg_dial = agg_ports[9110][1]

        # ── prepare ───────────────────────────────────────────────────────────────────────────────
        for c in order:
            if not prepare(ssh, c, block_path=a.block_path, block_name=a.block, repo_hint=a.repo):
                raise SystemExit(f"{c.cid} could not be prepared")

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
        for name, argv in dash_chain(a):
            helpers.append((name, os.spawnv(os.P_NOWAIT, argv[0], argv)))
            log(f"  started {name}")
        time.sleep(8)                      # let the streamer produce a sample before T0

        # ── prove ─────────────────────────────────────────────────────────────────────────────────
        runner = tip_runner.FleetRunner(
            ssh, agg, stage_dir=os.path.join(a.rundir, "stage"),
            agg_port=9110, agg_dial=agg_dial,
            prove_env={"HAZYNC_LIFTX_HINT": "1", "HAZYNC_FIELD_BIGINT2": "1",
                       "HAZYNC_ECMULT_WINDOW": "21", "HAZYNC_BLOCK_NAME": a.block})
        os.makedirs(runner.stage_dir, exist_ok=True)
        assignment = {i: order[i] for i in range(a.cards)}
        log(f"proving {a.block} on {a.cards} cards, aggregate on {agg.cid} "
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
            try:
                os.kill(pid, 15)
                log(f"  stopped {name}")
            except OSError:
                pass
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


if __name__ == "__main__":
    sys.exit(main())
