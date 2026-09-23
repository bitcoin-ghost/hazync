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
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import concurrent.futures as _cf        # noqa: E402
import sponsor_bot                      # noqa: E402  (the RunPod client only)
import tip_board                        # noqa: E402
import tip_controller                   # noqa: E402
import tip_dashboard                    # noqa: E402
import tip_driver                       # noqa: E402
import tip_economics                    # noqa: E402
import tip_harvest                      # noqa: E402
import tip_lifecycle                   # noqa: E402
import tip_run                          # noqa: E402
import tip_runner                       # noqa: E402
import tip_session                      # noqa: E402

PREFIX = "hz-smoke-"

# Heights at or above this come from the TIP bridge; below it, from the coordinator's bundle set.
# Mirrors HAZYNC_BRIDGE_EMIT_FROM on the bridge host — if that moves, this moves with it.
TIP_FROM = int(os.environ.get("HAZYNC_TIP_FROM", "967500"))

# Before any block has been measured, assume the slowest one seen on 2026-09-21 (226.7 s). Being
# wrong high costs idle at the tail; being wrong low orphans a claim for an hour.
DEFAULT_BLOCK_EST_S = float(os.environ.get("HAZYNC_BLOCK_EST_S", "230"))


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
            # ⛔ SECURE, NOT ALL. `cloudType: ALL` includes community hosts, and those mostly never
            # start at all -- RunPod never publishes a port for them, so the run waits out its full
            # SSH timeout and then gives up having paid for pods that never existed.
            #
            # Measured 2026-09-20 across two runs, and the split is by PRICE, which is the tell:
            #     $0.34/hr   1 of 6 started   (17%)
            #     $0.49/hr   1 of 1 started
            #     $0.74/hr  14 of 14 started  (100%)
            # A 4-card run died outright on it: three of five never started, so only two came up and
            # the run could not reach its minimum. The cheap listing is not cheaper, it is absent.
            q = ("mutation { podFindAndDeployOnDemand(input: { cloudType: SECURE, gpuCount: 1, "
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
                # ⛔ RECORD WHERE THE CARD IS. Geography is the one term in a fleet's throughput that
                # has never been controlled for here, and it has already caused a published error
                # once: 142 s of spread was attributed to geography when it was CARD TYPE
                # (hazync#448), and controlling for the card collapsed it to 9.0 s. The lesson taken
                # was "state the card mix" -- but the site was then left unrecorded entirely, so the
                # rival explanation can still never be tested. The milestone tooling recorded a
                # `site` column per chunk; tip_smoke never did.
                #
                # In mode 6 the coordinator PUSHES segments over the network to every worker, so RTT
                # plausibly reaches per-card throughput. Whether it does is unmeasured -- which is
                # exactly why it must be written down before anyone compares two fleets again.
                dc = None
                try:
                    q2 = ('query { pod(input:{podId:%s}) { machine { dataCenterId } } }'
                          % self._s(p["id"]))
                    dc = ((self._gql(q2).get("pod") or {}).get("machine") or {}).get("dataCenterId")
                except Exception:
                    pass          # never fail a rental over a label
                return {"id": p["id"], "name": name, "gpu_type": gt,
                        "price": float(p.get("costPerHr") or 0), "dc": dc}
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


# ⛔ THIS PIN IS LOAD-BEARING, AND IT WAS SEVEN RELEASES STALE. Every tip run proved with v0.21.0 —
# the release BEFORE the instrumentation the fleet exists to produce. Measured on a real run
# 2026-09-21 (block 741,000, 3 cards, VERIFIED) whose harvest could answer nothing:
#
#   #253  execution 12.3 s, 35 segments, 15.8 MB, marker `, depth 4`   <- pre-#236 spelling
#         ⚠ NOT a post-#236 binary — this log cannot speak to #253
#   #252  [rtt]: NOT MEASURED (no [rtt] lines in agg.log)
#
# What v0.21.0 is missing, and what each one costs us:
#   #236  v0.21.1  stream segments as they are produced   -> no `(streamed)` marker, #253 unanswerable
#   #254  v0.21.2  every seg-connect task line timestamped -> no epochs, so no overlap can be computed
#   #402  v0.21.7  seg-connect RECONNECTS instead of exiting on a dropped link
#
# ⚠ That last one is why a worker "not attaching" and a stale binary look identical from here: on
# v0.21.0 a worker that loses its link is simply gone, and the run finishes on the coordinator alone.
# ⇒ Track the CURRENT release. A tip run on an old binary still proves the block correctly — it just
# produces none of the evidence, which is the expensive way to learn this.
# ⭐ THE FLEET'S CARD TYPE, AND IT IS 4090-ONLY BY DEFAULT ON MEASUREMENT (hazync#448).
#
# sponsor_bot.GPU_TYPES is ("NVIDIA GeForce RTX 4090", "NVIDIA A40") and deploy_listening walks it in
# order, so 4090 was already PREFERRED -- but when 4090 capacity was short it silently fell back to an
# A40 and produced a MIXED fleet. That fallback is not a cheaper run. Measured 2026-09-21, 9 runs on
# block 741000:
#
#     all 4090          267.9 / 271.7 / 276.9 s   mean 272.2 s   spread  9.0 s
#     contains an A40   357.9 / 380.2 / 410.4 s   mean 382.8 s   spread 52.5 s
#
# ⛔ AND THE SLOWER FLEET COST MORE: the all-4090 run billed $0.243, the 4090+2xA40 run $0.262. The
# A40 is $0.49/hr against the 4090's $0.74/hr and is slow enough that the cheap card costs more PER
# PROOF. Selecting on price per hour is the wrong objective outright.
#
# ⚠ It also explains away a "142 s of run-to-run variance" I published as a geography effect: control
# for card type and the spread is 9.0 s. There was no variance mystery, only an unrecorded confound.
#
# `--gpu-type "NVIDIA GeForce RTX 4090,NVIDIA A40"` restores the old fallback for when a run matters
# more than its wall-clock.
DEFAULT_GPU_TYPES = ("NVIDIA GeForce RTX 4090",)

# ⭐ AUTO: RANK THE WHOLE LIVE CATALOGUE, PREFER ONE TYPE, LEARN FROM EVERY RUN (hazync#493).
#
# The lesson of #448 was never "only ever rent a 4090". It was two narrower things: a MIXED fleet is
# slower and dearer than a uniform one, and PRICE PER HOUR IS THE WRONG OBJECTIVE. Hard-coding one
# card type satisfies both by accident and fails the moment that card is out of stock -- which is
# exactly what happened on 2026-09-23: five attempts, 4090 secure stock "Low", the run never started.
# Restricting the catalogue did not buy comparability, it bought an outage.
#
# So `--gpu-type auto` ranks every type RunPod ACTUALLY has, in two tiers:
#
#   MEASURED   a type with a clean uniform measurement in docs/history/fleet-economics.jsonl,
#              ordered by usd_per_proof -- the objective that matters. Today that is the 4090 alone
#              ($0.243-$0.262 over three runs on block 741000).
#   UNMEASURED everything else with SECURE stock and enough VRAM, ordered by price per hour.
#
# ⛔ THE SECOND ORDERING IS A GUESS AND IS LABELLED AS ONE. Price per hour is the objective #448
# proved wrong, so it is used ONLY to break ties between cards nobody has measured, never to rank a
# measured card. A cheap card that proves slowly sorts high here and is still the wrong buy -- the
# run finds that out and writes it down, which is the point of the tier existing at all.
#
# ⛔ NO INVENTED FACTORS. It is tempting to score an L40S from docs/history/BENCH_8xL40S_2026-09-08.md,
# but that bench is a different block set on a different guest version; dividing its card-seconds by
# the 741000 runs would manufacture a number that was never measured. An unmeasured card stays
# unmeasured until a run measures it.
#
# ⚠ VRAM FLOOR IS EVIDENCE, NOT A SPEC. The 4090 has 24 GB and proves, so 24 GB is known-sufficient.
# It is not known to be the minimum; it is the smallest card we have actually seen work.
VRAM_FLOOR_GB = 24
ECONOMICS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "docs", "history", "fleet-economics.jsonl")


def measured_cost_per_proof(path=ECONOMICS):
    """({display name: best usd_per_proof}, block) — from UNIFORM fleets, ON ONE BLOCK.

    ⛔ UNIFORM ONLY. A '2x A40 + 1x RTX 4090' row measures a MIXTURE, and #448 is precisely the
    finding that a mixture's cost cannot be attributed to either card in it. Charging that row's
    $0.262 to the A40 would be inventing the very number this refuses to invent.

    ⛔ AND ONE OPERATING POINT ONLY (hazync#497). Cost per proof is not a property of the card. It
    is a property of the card AND the conditions it ran under, and there are at least three:

      block     968,243 is 10,666 segments and cost $7.43 on 16x A40; the 741,000 rows cost $0.243
                on 3x RTX 4090. Rank those together and the A40 looks 30x worse, when nearly all of
                that gap is BLOCK SIZE.
      po2       HAZYNC_SEG_PO2 sets cycles-per-segment. po2 22 halves the segment count, but peak
                VRAM at po2 21 was already 22,478 MiB on a 24GB card (milestone 966,256), so only
                48GB+ cards can use it. A card that CAN is being credited for the setting, not for
                the silicon.
      pods      8 GPUs in one pod share a NIC and PCIe. That is a deployment shape, not a card
                property, and recording `32x A40` for 4x8 hides it completely.

    Each of those, left unrecorded, attributes a condition to the card -- the same error as
    attributing a MIXTURE to one card, wearing a different hat three times over. So the comparison
    is confined to rows sharing all three, and the point is named wherever the ranking is shown.

    ⚠ Rows written before these fields existed carry None, which forms its own group. That is
    deliberate: an unknown operating point is not evidence of a shared one, and guessing would be
    the very thing this refuses to do.
    """
    per_block = {}
    try:
        with open(path, encoding="utf8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                fleet, usd = r.get("fleet", ""), r.get("usd_per_proof")
                blk = str(r.get("block") or "")
                if usd is None or "+" in fleet or not blk:
                    continue
                m = re.match(r"\s*\d+x\s+(.+?)\s*$", fleet)
                if not m:
                    continue
                name = m.group(1)
                point = (blk, r.get("po2"), r.get("gpus_per_pod"))
                b = per_block.setdefault(point, {"best": {}, "rows": 0})
                b["rows"] += 1
                if name not in b["best"] or usd < b["best"][name]:
                    b["best"][name] = float(usd)
    except FileNotFoundError:
        pass
    if not per_block:
        return {}, None
    point = max(per_block, key=lambda k: (len(per_block[k]["best"]), per_block[k]["rows"]))
    blk, po2, gpp = point
    label = f"block {blk}"
    if po2 is not None:
        label += f", po2 {po2}"
    if gpp is not None:
        label += f", {gpp} GPU/pod"
    return per_block[point]["best"], label


def rank_card_types(api, vram_floor=VRAM_FLOOR_GB, economics=ECONOMICS):
    """Every type with SECURE stock and enough VRAM, best-known-value first.

    Returns [{id, display, vram, price, stock, usd_per_proof|None}], measured tier first.
    """
    ids = [g["id"] for g in api._gql("query { gpuTypes { id } }")["gpuTypes"]]
    measured, measured_block = measured_cost_per_proof(economics)
    out = []
    for gt in ids:
        # ⛔ CUDA ONLY. The prover is a risc0 CUDA build; an AMD card cannot run it at all, so
        # offering one would rent a pod that is guaranteed to fail the GPU gate.
        if not gt.startswith("NVIDIA"):
            continue
        # ⛔ NO MIG SLICES. `MIG 1g.24gb` / `2g.48gb` are Multi-Instance GPU PARTITIONS of one
        # physical card, not cards. RunPod lists them beside whole GPUs with their own price, and
        # this catalogue ranks unmeasured types by price per hour -- so a slice presents as a cheap
        # 24GB card and sorts near the top while delivering a fraction of a GPU and sharing memory
        # bandwidth with its neighbours. That is hazync#448's error in a new costume: buying the
        # cheap-looking thing that costs more per proof.
        #
        # It nearly happened: on 2026-09-23 `PRO 6000 MIG 24GB` at $0.59 ranked FOURTH, above the
        # RTX PRO 4500 that actually won the fleet. The tell is in the id, and `maxGpuCount` agrees
        # (16 for the 48GB slice against 9 for the whole RTX PRO 6000).
        #
        # ⚠ Not banned outright -- `--gpu-type` still takes one by name if it is ever wanted. This
        # only keeps them out of the AUTOMATIC ranking, where nobody chose them.
        if " MIG " in gt:
            continue
        q = ('query { gpuTypes(input:{id:%s}) { id displayName memoryInGb '
             'lowestPrice(input:{gpuCount:1, secureCloud:true}) '
             '{ uninterruptablePrice stockStatus } } }' % json.dumps(gt))
        try:
            g = api._gql(q)["gpuTypes"][0]
        except Exception:
            continue
        lp = g.get("lowestPrice") or {}
        price, stock = lp.get("uninterruptablePrice"), lp.get("stockStatus")
        vram = g.get("memoryInGb") or 0
        # No secure price or no stock means RunPod cannot sell it right now, whatever it lists.
        if not price or not stock or vram < vram_floor:
            continue
        out.append({"id": gt, "display": g.get("displayName") or gt, "vram": vram,
                    "price": float(price), "stock": stock,
                    "usd_per_proof": measured.get(g.get("displayName") or gt),
                    "measured_on": measured_block})
    # Measured tier (by the objective that matters) ahead of the unmeasured tier (by the objective
    # that does not, used only because nothing better exists for a card nobody has run).
    out.sort(key=lambda c: (c["usd_per_proof"] is None,
                            c["usd_per_proof"] if c["usd_per_proof"] is not None else c["price"]))
    return out

HOST_RELEASE = "v0.21.7"
HOST_URL = (f"https://github.com/bitcoin-ghost/hazync/releases/download/{HOST_RELEASE}/"
            "hazync-host-x86_64-linux-gnu-cuda")


def binary_size():
    """The prover binary's real size, from the release. Never guessed."""
    import urllib.request
    req = urllib.request.Request(HOST_URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def _drop_cards(order, created, bad, why, *, release, record):
    """Release the named cards and return the (order, created) that are left (hazync#479).

    ⛔ ONE PLACE, because it was three. Reachability and the GPU smoke each open-coded this, the
    staging and prover-fetch gates raised instead, and the difference was not a decision -- it was
    which gate somebody happened to be looking at. A card that fails any pre-clock gate is released
    and the run carries on with what is left; whether that is ENOUGH is a separate question, asked
    once, after every gate has run.
    """
    bad = set(bad)
    if not bad:
        return order, created
    for p in [x for x in created if x["name"] in bad]:
        release(p)
    kept = [x for x in created if x["name"] not in bad]
    record(kept)
    return [c for c in order if c.cid not in bad], kept


def fetch_binary(ssh, card, want, *, wait_s=15, stall_polls=8, ceiling_s=2400):
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

    ⛔ IT GIVES UP ON A STALL, NOT ON A CLOCK (hazync#479). This used to allow `tries=40` x 15 s =
    600 s flat. Measured 2026-09-22: hz-smoke-1 pulled at 0.57 MB/s, so 410 MB needed 724 s; the
    deadline expired with the card at 348 of 410 MB -- **116 seconds short** -- and killed a run that
    had already spent 13 minutes and $0.48. The docstring above cites ~1 MB/s as the rate this guard
    exists for, and at exactly 1 MB/s 410 MB fits in 600 s: the budget never covered its own worst
    case, it only looked like it did.
    ⚠ A BIGGER NUMBER IS NOT THE FIX. Raising it would make a genuinely dead card hold the fleet for
    longer, which is the failure this guard was written to prevent. What separates "slow" from "dead"
    is whether the byte count is still MOVING, so that is what is measured: keep waiting while it
    climbs, abandon after `stall_polls` consecutive polls with no progress at all. `ceiling_s` is a
    backstop for a card that trickles forever, not the normal exit.
    """
    got, last, stuck, t0 = -1, -1, 0, time.time()
    while time.time() - t0 < ceiling_s:
        out = ssh.run(card, "stat -c%s /workspace/hazync-host-cuda 2>/dev/null || echo 0",
                      timeout=60) or "0"
        got = int((out.strip().splitlines() or ["0"])[-1] or 0)
        if got == want:
            ssh.run(card, "chmod +x /workspace/hazync-host-cuda", timeout=60)
            return True, got
        # ⚠ Progress resets the patience; no progress spends it. An ssh hiccup reads as `got == last`
        # for one poll and costs one of the eight, which is the right price for not being able to see.
        if got > last:
            last, stuck = got, 0
        else:
            stuck += 1
            if stuck >= stall_polls:
                return False, got
        # ⛔ NOT pgrep. `pgrep -f 'curl.*hazync-host'` MATCHES THE SSH SESSION CARRYING IT: the
        # pattern is in our own command line on the remote, so the check always succeeded, the `||`
        # always short-circuited, and curl NEVER RAN. Measured twice -- 0 bytes on every card after
        # five minutes of "fetching", with no fetch.log and no lock file to show for it.
        #
        # ⛔ AND THIS FIX WAS LOST ONCE. It was applied live on the box and never committed, so a
        # later deploy from the branch silently restored the pgrep version and a 12-card run stalled
        # on it again. A fix that exists only on a machine is a fix that gets clobbered.
        #
        # `flock -n` needs no pattern at all: it either takes the lock or exits, so exactly one
        # fetch runs per card and a retry here simply resumes it.
        ssh.run(card,
                "nohup flock -n /workspace/fetch.lock "
                f"curl -fsSL -S -C - -o /workspace/hazync-host-cuda {HOST_URL} "
                "> /workspace/fetch.log 2>&1 < /dev/null & disown; exit 0", timeout=60)
        time.sleep(wait_s)
    return False, got


def gpu_smoke(ssh, card, timeout_s=300):
    """Make the card actually PROVE something on its GPU, and verify it. (ok, detail)

    ⛔ A BINARY THAT IS THE RIGHT SIZE IS NOT A CARD THAT CAN USE IT. Two of twelve cards in the
    2026-09-20 run were duds, and neither was caught before the clock because nothing had ever asked
    them to prove anything:

      * `hz-smoke-13` had no usable CUDA device at all. It died one second into its chunk with
        `cudaErrorNoDevice`, and every check before that passed -- the fixture landed, the 407 MB
        prover was byte-exact, ssh answered, and it could reach the aggregator. `method-id` never
        touches CUDA, so it proves nothing about the GPU.
      * `hz-smoke-15` was worse, because it LOOKED busy: 100% reported utilisation while holding
        723 MiB and drawing 87 W. A 4090 genuinely proving holds ~22 GB and pulls 300-400 W. It ran
        fifteen minutes and produced no segments.

    This is the boot check `fleet.sh` has always had and this path never did, and it is exactly what
    my own notes demanded after a dud card claimed and abandoned 14 blocks: the boot check must
    include a real GPU prove. `prove-block` is self-contained -- no fixture, no arguments -- and its
    output must say VERIFIED. Nothing weaker distinguishes these two cards from a working one.
    """
    out = ssh.run(card,
                  f"cd /workspace && timeout {int(timeout_s)} ./hazync-host-cuda prove-block "
                  f"> gpu_smoke.log 2>&1; "
                  f"if grep -q VERIFIED gpu_smoke.log; then echo GPU_OK; "
                  f"else echo \"GPU_BAD $(tail -1 gpu_smoke.log 2>/dev/null | cut -c1-120)\"; fi",
                  timeout=timeout_s + 60)
    last = (out or "").strip().splitlines()
    if not last:
        # ⚠ An ssh we could not complete is not a bad card. Say so rather than discarding a good one.
        return None, "unreachable during the GPU smoke"
    line = last[-1].strip()
    return (line == "GPU_OK"), line


def prepare(ssh, card, *, block_path, block_name, repo_hint):
    """Stage pod-prove.sh and (for the chunk path) the fixture; clear the CUDA compat trap.

    ⚠ `block_path=None` MEANS MODE 6 AND IS NOT A FAILURE. A claimed block is proved from its BUNDLE,
    which `start_range_aggregate` stages onto the aggregate later -- there is no fixture to push and
    no per-card chunk phase to push it for. Staging one anyway is what failed the first live session:
    `--block-path` still held its default container path, so every card reported
    `fixture=FAILED (0 bytes)` and the run refused before claiming anything.
    """
    # ⛔ CUDA ERROR 804 ON A CONSUMER CARD IS AN UNPREPARED CARD, NOT A BAD ONE. The driver's compat
    # libraries shadow the real ones; bootstrap2.sh moves them aside for exactly this reason.
    ssh.run(card, "for d in /usr/local/cuda*/compat; do [ -d \"$d\" ] && "
                  "mv \"$d\" \"${d}.disabled\"; done; ldconfig 2>/dev/null; true", timeout=120)
    ok_bin = ssh.push(card, os.path.join(repo_hint, "pod-prove.sh"), "/workspace/pod-prove.sh")
    if block_path is None:
        # ⛔ Say so out loud. A silently skipped stage is indistinguishable from one that worked.
        log(f"  {card.cid}: pod-prove.sh={'ok' if ok_bin else 'FAILED'} "
            f"fixture=n/a (mode 6 — the bundle is staged onto the aggregate)")
        ssh.run(card, "chmod +x /workspace/pod-prove.sh", timeout=60)
        return ok_bin
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
    ap.add_argument("--gpu-type", default="auto",
                    help="'auto' (default) ranks every type RunPod has in SECURE stock by MEASURED "
                         "cost per proof, then by price for cards nobody has measured, and fills the "
                         "fleet from ONE type wherever capacity allows (hazync#493). Or pass a "
                         "comma-separated preference list to pin the choice.")
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
    ap.add_argument("--claim", action="store_true",
                    help="claim a block from the board and prove it from its BUNDLE (mode 6, #367) "
                         "instead of proving a named fixture")
    ap.add_argument("--claim-source", choices=("api", "ssh"), default="api",
                    help="api: /api/witness (board heights, <=418,268). ssh: straight off the bridge "
                         "host (TIP heights, once the walk passes EMIT_FROM)")
    ap.add_argument("--bridge-host", default="hazync-coord",
                    help="ssh host holding tip_bundles, for --claim-source=ssh")
    ap.add_argument("--session", type=float, default=0.0, metavar="HOURS",
                    help="keep the fleet and prove continuously for HOURS: the tip block when one is "
                         "waiting, board work otherwise (#367). Implies --claim.")
    ap.add_argument("--budget-usd", type=float, default=0.0, metavar="USD",
                    help="stop the session once this much GPU time has been spent (checked BEFORE "
                         "starting each block, so the one that crosses the line is never started)")
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
    # ── claim FIRST, before spending anything (#367) ──────────────────────────────────────────────
    # ⛔ THE ORDER MATTERS. Renting takes ~2 minutes and costs money; a claim costs a POST. If the
    # board has nothing free -- or this key is at its 4-unfinished cap -- the right answer is to exit
    # having spent nothing, not to discover it with four cards already billing.
    # ⚠ A claim's TTL starts here, and renting + staging + fetching the prover runs ~4 min against an
    # hour, so the margin is wide.
    claimed = None
    ident = None
    if a.session:
        a.claim = True          # a session is claim-driven by definition
    # ⛔ ident IS LOADED FOR BOTH PATHS. It used to be bound only in the single-block branch, so a
    # --session run hit a NameError the first time the loop tried to claim -- after renting. The
    # undefined-name check cannot see this: `ident` IS bound in the module, just not on every path.
    if a.claim:
        ident = tip_board.identity()
        log(f"claiming as {ident[2]!r} ({ident[1][:10]}…)")
    if a.claim and not a.session:
        res = tip_board.claim(ident=ident)
        if res["state"] == "idle":
            log(f"nothing to claim right now: {res['why']} — nothing rented, nothing spent")
            return 0
        if res["state"] != "claimed":
            raise SystemExit(f"claim refused: {res['why']}")
        claimed = res["range"]
        a.block = claimed
        log(f"claimed block {claimed} (yours for {res['ttl'] // 60} min)")

    if not str(a.block).isdigit():
        raise SystemExit(f"--block takes a HEIGHT, not a filename: got {a.block!r}. "
                         f"Try --block {''.join(c for c in str(a.block) if c.isdigit()) or '130000'}")
    block_name = f"block_{a.block}.json"

    # The BUNDLE, not the fixture. `looks_like_bundle` refuses the fixture shape by name, and a
    # rejected fetch writes no file, so nothing downstream can pick one up by accident.
    # ⚠ SINGLE-BLOCK ONLY. A session claims inside its loop, so fetching a bundle here would pull
    # one for --block's DEFAULT height, which was never claimed and will never be proved.
    bundle_path = None
    if a.claim and not a.session:
        os.makedirs(a.rundir, exist_ok=True)
        bundle_path = os.path.join(a.rundir, f"bundle_{a.block}.json")
        if a.claim_source == "ssh":
            ok, why = tip_board.fetch_bundle_ssh(int(a.block), bundle_path, a.bridge_host)
        else:
            ok, why = tip_board.fetch_bundle(int(a.block), bundle_path)
        if not ok:
            raise SystemExit(f"no bundle for claimed block {a.block}: {why}\n"
                             f"The claim reopens by itself; nothing was rented.")
        log(f"bundle for {a.block}: {os.path.getsize(bundle_path)} bytes -> {bundle_path}")

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

    # ⚠ Bound BEFORE the try so the `finally` can always read them. The harvest runs on whatever the
    # run got as far as — a fleet that died during staging still has a `run.log` worth keeping — and
    # a NameError in teardown would leave the cards billing.
    assignment, runner, agg = {}, None, None

    # ⛔ #429 ADDED SEVEN `phase(...)` CALLS AND NEVER DEFINED IT. Every run since died on the FIRST
    # one — `NameError: name 'phase' is not defined` at "PREPARING · renting …", before a single pod
    # was rented. The teardown ran and reported "0 cards, account check: clean", so it cost nothing
    # but a run; it just could not work at all.
    # ⚠ It logs FIRST and writes the tile second: the phase is information the operator needs whether
    # or not a dashboard is attached, and a run must never die for the sake of its status tile —
    # `write_phase` touches the filesystem and the rundir may not exist yet.
    def phase(text):
        log(text)
        try:
            tip_dashboard.write_phase(a.rundir, text)
        except Exception:
            pass

    try:
        # ── rent ──────────────────────────────────────────────────────────────────────────────────
        phase(f"PREPARING · renting {a.cards} cards (+{a.spares} spare)")
        existing = {p["name"] for p in api.pods()}
        log(f"pods already on the account (untouched): {sorted(existing) or 'none'}")
        # ⛔ RENT SPARES. RunPod does not always start what it sells: on 2026-09-20 one of two pods
        # never published a port in 420 s while its twin answered in 30 s. Renting exactly N means one
        # bad pod ends the run after the full wait, having paid for both the whole time.
        # ⛔ VALIDATE BEFORE RENTING. An unrecognised name is accepted by the GraphQL call and simply
        # matches nothing, so the run would report "no capacity" for every pod and look like a RunPod
        # outage rather than a typo. Fail here, having spent nothing.
        if a.gpu_type.strip() == "auto":
            catalogue = rank_card_types(api)
            if not catalogue:
                raise SystemExit("no NVIDIA type has SECURE stock and "
                                 f"{VRAM_FLOOR_GB}GB+ right now — nothing to rent")
            gpu_types = tuple(c["id"] for c in catalogue)
            log(f"card catalogue ({len(catalogue)} types with SECURE stock and {VRAM_FLOOR_GB}GB+), "
                "best known value first:")
            for c in catalogue:
                val = (f"${c['usd_per_proof']:.3f}/proof on {c['measured_on']} MEASURED"
                       if c["usd_per_proof"] is not None
                       else "unmeasured — ordered by $/hr, which is NOT the objective (#448)")
                log(f"    {c['display']:34} {c['vram']:>3}GB  ${c['price']:>5.2f}/hr  "
                    f"stock={c['stock']:<7} {val}")
        else:
            # ⛔ VALIDATE AGAINST WHAT RUNPOD ACTUALLY OFFERS, not against a hard-coded pair. The old
            # check refused any type outside sponsor_bot.GPU_TYPES, so naming a real, in-stock,
            # perfectly capable card (an L40S, say) was rejected as "unknown" — a typo guard that had
            # quietly become a policy. Typos still fail here; real cards no longer do.
            gpu_types = tuple(t.strip() for t in a.gpu_type.split(",") if t.strip())
            if not gpu_types:
                raise SystemExit("--gpu-type is empty")
            offered = {g["id"] for g in api._gql("query { gpuTypes { id } }")["gpuTypes"]}
            unknown = [t for t in gpu_types if t not in offered]
            if unknown:
                raise SystemExit(f"unknown --gpu-type {unknown} — RunPod offers no such type")
            log(f"card type preference: {' > '.join(gpu_types)}"
                + ("" if len(gpu_types) > 1 else "  (no fallback — hazync#448)"))

        want = a.cards + a.spares
        for i in range(want):
            name = f"{PREFIX}{i+1}"
            if name in existing:
                raise SystemExit(f"refusing: {name} already exists")

        # ⭐ ONE TYPE FOR THE WHOLE FLEET IF ANY TYPE CAN SUPPLY IT (hazync#493).
        #
        # deploy_listening walks the type list PER POD, so pod 1 took a 4090, pod 2 found none left
        # and took an A40, and the fleet was mixed before anyone chose to mix it. That is the exact
        # mechanism behind #448's 142 s of unexplained "variance".
        #
        # So try each type for the ENTIRE fleet, best-value first, and keep the first one that can
        # field at least --cards. A type that comes up short is RELEASED, not topped up from the next
        # type down: a partial fleet held while we try the next type costs seconds of billing, and a
        # mixed fleet costs 40% of the run. Only when NO single type can field the minimum do we mix
        # — deliberately, and labelled.
        def rent_uniform(gt, upto):
            got = []
            for i in range(upto):
                p = api.deploy_listening(f"{PREFIX}{i+1}", pub, gpu_types=(gt,))
                if not p:
                    break
                got.append(p)
                with open(rented_path, "w") as fh:
                    json.dump(got, fh, indent=1)
            return got

        for gt in gpu_types:
            got = rent_uniform(gt, want)
            if len(got) >= a.cards:
                created = got
                log(f"UNIFORM fleet from {gt}: {len(created)} of {want} rented")
                break
            if got:
                log(f"  {gt} could field only {len(got)} of the {a.cards} needed — releasing and "
                    f"trying the next type (a mixed fleet costs more than these seconds do)")
                for p in got:
                    # ⛔ CONFIRMED, NOT FIRE-AND-FORGET. A terminate that silently failed here would
                    # leave a pod billing for the whole run with nothing in `created` to release it.
                    try:
                        sponsor_bot.terminate_confirmed(api, p["id"])
                    except Exception as e:
                        log(f"  ⚠ could not release {p['name']}: {e}")
                with open(rented_path, "w") as fh:
                    json.dump([], fh, indent=1)
            else:
                log(f"  {gt}: no capacity")

        # ⛔ MIXING IS THE LAST RESORT, AND IT IS STATED. Reaching here means no single type could
        # field --cards, so the choice is a mixed fleet or no run at all. #448 says a mix is slower
        # and dearer; it does not say a mix is wrong when the alternative is not proving the block.
        #
        # ⛔ AND ONLY WHEN THE FLEET IS EMPTY. Topping a uniform fleet up to its spare count from the
        # next type down would mix it after the fact — a spare is promoted to a run card the moment
        # one of the originals fails a gate, so a "spare" of another type is a mixed fleet on a delay.
        mixed_fallback = not created
        if mixed_fallback:
            log(f"⚠ NO SINGLE TYPE can field {a.cards} cards — falling back to a MIXED fleet across "
                f"{len(gpu_types)} types. Its wall-clock is NOT comparable with a uniform run.")
        for i in range(len(created), want if mixed_fallback else len(created)):
            name = f"{PREFIX}{i+1}"
            p = api.deploy_listening(name, pub, gpu_types=gpu_types)
            if not p:
                # ⛔ SPARES ARE OPTIONAL BY DEFINITION — THAT IS WHAT MAKES THEM SPARES. This used to
                # raise on the first pod RunPod could not sell, which threw away every pod already
                # rented. Measured 2026-09-21: five came up, the SIXTH (a spare) had no capacity, and
                # the run released all five and failed. Renting spares to survive a bad pod, then
                # failing because a spare was unavailable, is the opposite of the intent.
                if len(created) >= a.cards:
                    log(f"  no capacity for {name} — continuing with {len(created)} pod(s), "
                        f"{a.cards} needed")
                    break
                raise SystemExit(f"no capacity for {name} — only {len(created)} pod(s) rented and "
                                 f"{a.cards} are needed")
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

        # ⛔ NAME THE FLEET'S COMPOSITION IN THE EVIDENCE. `pods.txt` carried it all along but nothing
        # summarised it, so a mixed fleet looked identical to a uniform one in every log and summary.
        # I published "all 3x A40, same card type" off runs that were actually mixed, and built an
        # issue on the 142 s of "variance" that mix produced (hazync#448). One line would have stopped
        # it. A run whose card types are not stated is a run whose results cannot be compared.
        mix = {}
        for c in created:
            mix[c["gpu_type"]] = mix.get(c["gpu_type"], 0) + 1
        sites = {}
        for c in created:
            sites[c.get("dc") or "?"] = sites.get(c.get("dc") or "?", 0) + 1
        log("FLEET: " + ", ".join(f"{n}x {g}" for g, n in sorted(mix.items()))
            + (["", "   ⚠ MIXED CARD TYPES — timings are NOT comparable with a uniform fleet"][len(mix) > 1]))
        # ⚠ SITES, for the same reason the card mix is stated: a run whose geography is not recorded
        # is a run whose throughput cannot be compared with any other.
        log("SITES: " + ", ".join(f"{n}x {d}" for d, n in sorted(sites.items()))
            + (["", "   ⚠ SPREAD ACROSS SITES — per-card rates mix card and network"][len(sites) > 1]))

        phase(f"PREPARING · waiting for {a.cards} of {want} cards to answer")
        cards, portmap = wait_for_ssh(api, created, ssh, need=a.cards)
        if len(cards) < a.cards:
            raise SystemExit(f"only {len(cards)} of {want} rented cards came up; needed {a.cards}")

        # ⛔ THE SPARES LIVE UNTIL THE GATES HAVE RUN (hazync#479). They used to be released here,
        # immediately after the SSH gate -- 23 seconds before the three gates that actually find a bad
        # card (staging, the 410 MB prover fetch, and the GPU smoke). So a card that answered ssh and
        # then failed one of those killed the whole run with nothing left to swap in, which is the
        # exact opposite of what --spares says it is for: "extra pods rented so one bad pod does not
        # end the run".
        #
        # Measured 2026-09-22: three spares released at 20:31:30, hz-smoke-1 failed the prover fetch
        # at 20:43:41, run over, 13.0 min and $0.483 spent, zero blocks proved. Three healthy pods
        # were sitting right there when it happened and had already been terminated.
        #
        # ⚠ THE GATES NOW SELECT, RATHER THAN PASS OR FAIL A SET CHOSEN IN ADVANCE. Every card that
        # answered goes through them -- they are already run in parallel, so this costs no extra wall
        # clock, only the spares' own billing for the length of the gates (3 spares x $0.74 x ~0.2 h
        # = ~$0.45, against a $2.20 run thrown away).
        order = [cards[n] for n in sorted(cards)]
        log(f"{len(order)} card(s) answered; ALL go through the gates and the survivors are the run")

        # ⛔ A POD THAT NEVER ANSWERED SSH IS NOT A SPARE, IT IS DEAD WEIGHT — release it NOW.
        # Keeping the spares through the gates (above) accidentally kept these too, because the old
        # release swept up "everything not chosen" and that set happened to include them. Measured
        # 2026-09-22 on the very next run: hz-smoke-1 (ports published, ssh never answered) and
        # hz-smoke-2 (never started) sat rented while four cards went through the gates — 2 x $0.74
        # of pure waste, which on a one-hour $3.00 budget is half of it.
        # ⚠ These are NOT candidates. The gates need a card that can be reached; one that never
        # answered cannot be gated, cannot be promoted, and will not start answering later.
        never_up = [p["name"] for p in created if p["name"] not in cards]
        if never_up:
            log(f"releasing {len(never_up)} pod(s) that never answered ssh: {sorted(never_up)}")
            order, created = _drop_cards(
                order, created, never_up, "it never answered ssh",
                release=lambda p: (sponsor_bot.terminate_confirmed(api, p["id"]),
                                   log(f"  released {p['name']} — it never answered ssh")),
                record=lambda kept: json.dump(kept, open(rented_path, "w"), indent=1))

        def _drop(order_, created_, bad, why):
            def release(p):
                sponsor_bot.terminate_confirmed(api, p["id"])
                log(f"  released {p['name']} — {why}")
            def record(kept):
                with open(rented_path, "w") as fh:
                    json.dump(kept, fh, indent=1)
            return _drop_cards(order_, created_, bad, why, release=release, record=record)

        agg = order[0]
        agg_ports = portmap.get(agg.cid, {})
        if 9110 not in agg_ports:
            raise SystemExit("the aggregator has no published 9110 — workers could never attach")
        agg_dial = agg_ports[9110][1]

        # ── the dashboard feed: started NOW, so the page is not dark through setup ───────────────
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
        time.sleep(4)                      # let the streamer produce a sample

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
                # ⛔ DROP THEM AND RE-PLAN, rather than run a fleet that does not exist. A card that
                # cannot attach is not a slow card: it sits in its retry loop contributing nothing
                # while the straggler, the projection and the cost are all computed against a size
                # that includes it. reachability_verdict's own docstring asks for exactly this --
                # "drop the card deliberately, and re-plan with the size you actually have".
                bad = set(unreachable)
                order = [c for c in order if c.cid not in bad]
                log(f"dropping {sorted(bad)} and re-planning with {len(order)} card(s)")
                for p in [x for x in created if x["name"] in bad]:
                    sponsor_bot.terminate_confirmed(api, p["id"])
                    log(f"  released {p['name']} — it could not reach the aggregate")
                created = [x for x in created if x["name"] not in bad]
                with open(rented_path, "w") as fh:
                    json.dump(created, fh, indent=1)
                if len(order) < 2:
                    raise SystemExit(
                        f"only {len(order)} card(s) can reach the aggregate — a distributed run "
                        f"needs at least 2, and an aggregate with no workers is a single-card prove")
                # ⚠ The feed was written for the fleet we rented; rewrite it for the one we have, or
                # the dashboard shows cards that are no longer in the run at $0.00/hr.
                joined = tip_dashboard.feed_records(order, fleet)
                feed.start(joined["records"])
                log(f"feed rewritten for {len(order)} card(s)")

        # ── prepare ───────────────────────────────────────────────────────────────────────────────
        # ⛔ IN PARALLEL. This was a serial loop and it was the largest avoidable cost in a run.
        # Measured 2026-09-20 on a 14-card fleet: the cards prepared ~40 s apart --
        #     22:59:51  hz-smoke-10
        #     23:00:32  hz-smoke-11   (+41s)
        #     23:01:11  hz-smoke-12   (+39s)
        # -- about NINE MINUTES of setup with the whole fleet up, idle and billing at $9.71/hr, and
        # nothing on the dashboard but flat traces. The work is an scp and two short ssh calls per
        # card: independent, I/O-bound, and exactly what the binary fetch and the GPU smoke already
        # fan out. There was no reason for this one to be serial except that nobody had looked.
        phase(f"PREPARING · staging the block onto {len(order)} cards")
        with _cf.ThreadPoolExecutor(max_workers=len(order)) as pool:
            prepped = list(pool.map(
                lambda c: (c, prepare(ssh, c, block_path=(None if a.claim else a.block_path),
                                      block_name=block_name, repo_hint=a.repo)), order))
        unprepared = [c.cid for c, ok in prepped if not ok]
        if unprepared:
            # ⛔ DROP IT, DO NOT END THE RUN (hazync#479). This raised, so one card that could not be
            # staged took the whole fleet down with it -- while its spares stood by. Reachability and
            # the GPU smoke already drop-and-re-plan; this gate now does the same, and the surplus
            # check below is what decides whether enough survived.
            order, created = _drop(order, created, unprepared,
                                   "it could not be staged (script or fixture)")

        # ⛔ THE BINARY, BEFORE THE CLOCK, AND VERIFIED BY SIZE. In parallel: on a slow card this is
        # minutes, and doing it one at a time would double that for no reason.
        want = binary_size()
        phase(f"PREPARING · fetching the prover ({want/1e6:.0f} MB) onto {len(order)} cards")
        with _cf.ThreadPoolExecutor(max_workers=len(order)) as pool:
            got = list(pool.map(lambda c: (c, *fetch_binary(ssh, c, want)), order))
        for c, ok, n in got:
            log(f"  {c.cid}: {'ok' if ok else 'INCOMPLETE'} {n}/{want} bytes")
        bad = [c.cid for c, ok, _ in got if not ok]
        if bad:
            # ⛔ THE GATE THAT KILLED THE 2026-09-22 RUN. Its reasoning is right -- a card without the
            # prover spends the run fetching and the stall detector restarts it from zero -- but the
            # remedy was to end the run rather than to drop the card. With the spares still alive
            # there is now something to carry on with.
            order, created = _drop(order, created, bad,
                                   "the prover never finished downloading")

        # ── the GPU smoke, BEFORE the clock ───────────────────────────────────────────────────────
        phase(f"PREPARING · proving one block on each of {len(order)} GPUs")
        with _cf.ThreadPoolExecutor(max_workers=len(order)) as pool:
            smoke = list(pool.map(lambda c: (c, *gpu_smoke(ssh, c)), order))
        duds = []
        for c, ok, detail in smoke:
            if ok:
                log(f"  {c.cid}: GPU ok")
            elif ok is None:
                log(f"  ⚠ {c.cid}: {detail} — not judged a dud on an ssh failure")
            else:
                log(f"  ⛔ {c.cid}: {detail}")
                duds.append(c.cid)
        if duds:
            # Same treatment as an unreachable card: drop it, release it, re-plan with what is left.
            order = [c for c in order if c.cid not in set(duds)]
            for p in [x for x in created if x["name"] in set(duds)]:
                sponsor_bot.terminate_confirmed(api, p["id"])
                log(f"  released {p['name']} — its GPU cannot prove")
            created = [x for x in created if x["name"] not in set(duds)]
            with open(rented_path, "w") as fh:
                json.dump(created, fh, indent=1)
            if len(order) < 2:
                raise SystemExit(f"only {len(order)} card(s) have a working GPU")
            joined = tip_dashboard.feed_records(order, fleet)
            feed.start(joined["records"])
            log(f"re-planned with {len(order)} card(s) after dropping {sorted(duds)}")

        # ── the gates are done: now choose the run and release the surplus (hazync#479) ───────────
        # ⛔ THIS IS THE QUESTION THAT USED TO BE ASKED FIRST. Every gate above now drops a bad card
        # and carries on; whether enough survived is decided once, here, with all the evidence in.
        if agg.cid not in {c.cid for c in order}:
            # ⚠ Fatal on purpose. The reachability gate proved the workers could reach THIS
            # aggregate; promoting a different card would silently throw that result away, and the
            # honest answer is to say so rather than run on an untested topology.
            raise SystemExit(f"the aggregate {agg.cid} failed a pre-clock gate — every worker was "
                             f"checked against it, so the run cannot simply promote another card")
        if len(order) < a.cards:
            raise SystemExit(f"only {len(order)} of {want} rented cards passed every pre-clock gate; "
                             f"needed {a.cards}. Rent more spares, or read the per-card lines above")
        surplus = [c.cid for c in order[a.cards:]]
        if surplus:
            log(f"the run has its {a.cards} card(s); releasing {len(surplus)} that passed the gates "
                f"but are not needed: {sorted(surplus)}")
            order, created = _drop(order, created, surplus, "surplus to the run")
            # ⚠ The feed was written for the fleet that went through the gates; rewrite it for the
            # one actually running, or the dashboard shows cards nobody is paying for.
            joined = tip_dashboard.feed_records(order, fleet)
            feed.start(joined["records"])
            log(f"feed rewritten for {len(order)} card(s)")


        # ── prove ─────────────────────────────────────────────────────────────────────────────────
        runner = tip_runner.FleetRunner(
            ssh, agg, stage_dir=os.path.join(a.rundir, "stage"),
            agg_port=9110, agg_dial=agg_dial,
            # ⛔ TELL THE CARD WHICH BINARY WE MEAN. pod-prove.sh has its own fallback URL and used to
            # have its own hardcoded size; when this driver's pin moved and the script's did not, the
            # card fetched a correct 410,441,528-byte prover, the script called it short against
            # 407,133,112, tried to resume past EOF and got HTTP 416. prove-chunk never ran, there was
            # no prove.log at all, and the planner restarted the card every ~100 s for ever.
            # ⇒ Both ends now read ONE value. `want` is the size this driver measured from the release
            # with a HEAD, so the card never has to guess and never re-derives it.
            # ⛔ THE OPERATOR'S LEVERS, OR NO LEVER CAN EVER BE TESTED. This was a hardcoded dict and
            # nothing else reached the cards, so HAZYNC_RESOLVE_LOCAL / WORKER_LIFTS / JOIN_LOCAL_MAX
            # set in the driver's shell arrived NOWHERE -- both arms of an A/B would run identically
            # and report "no difference", which is indistinguishable from a lever that does nothing.
            # ⚠ The three tuning keys below are read ONLY in methods/build.rs and
            # methods/guest/build.rs. They are BUILD-time guest flags baked into the released binary;
            # passing them at runtime has never done anything and is kept only for continuity.
            prove_env={"HAZYNC_LIFTX_HINT": "1", "HAZYNC_FIELD_BIGINT2": "1",
                       "HAZYNC_ECMULT_WINDOW": "21",
                       **tip_lifecycle.lever_env(),
                       "HAZYNC_HOST_URL": HOST_URL, "HAZYNC_HOST_BYTES": str(want)})
        os.makedirs(runner.stage_dir, exist_ok=True)
        # ⛔ len(order), NOT a.cards. Cards can be dropped by the reachability gate, and indexing by
        # the requested count would either raise or silently prove a chunk count the fleet cannot
        # cover -- the chunk count IS the fleet size.
        assignment = {i: c for i, c in enumerate(order)}
        # ⛔ DO NOT NAME A BLOCK THE RUN IS NOT PROVING (hazync#496). `a.block` is only assigned from
        # a claim on the `a.claim and not a.session` path, so a SESSION run never updates it and this
        # line printed the argparse default. Captured on the 968,243 run, one second apart:
        #
        #     [09:46:04] PROVING block 130000 on 16 cards
        #     [09:46:05]   proving 968243 (attempt 1)
        #
        # 130,000 is a real fixture height, so the line reads as a true statement about the wrong
        # block rather than as an obvious placeholder. A session claims its height per block inside
        # the loop below, and at this point there is no height yet -- so say that instead.
        phase(f"PROVING block {a.block} on {len(order)} cards" if not a.session else
              f"READY · {len(order)} cards, claiming each block as it arrives")
        if not a.claim:
            log(f"proving {block_name} on {len(order)} cards, aggregate on {agg.cid} "
                f"(binds 9110, dialled on {agg_dial})")

        if a.claim:
            # ── mode 6: one claimed block, proved from its bundle, then submitted ─────────────────
            def prove_and_submit(rng, *, from_tip=False):
                """Fetch the bundle, prove it as a range, collect the receipt, submit. Returns the
                run dict. Shared by the single-block path and the session loop so there is exactly
                ONE definition of what proving a claimed block means."""
                bp = os.path.join(a.rundir, f"bundle_{rng}.json")
                if not os.path.exists(bp):
                    src = "ssh" if from_tip else a.claim_source
                    if src == "ssh":
                        bok, bwhy = tip_board.fetch_bundle_ssh(int(rng), bp, a.bridge_host)
                    else:
                        bok, bwhy = tip_board.fetch_bundle(int(rng), bp)
                    if not bok:
                        raise RuntimeError(f"no bundle for {rng}: {bwhy}")
                hi_l = {"n": 0}

                def _b(progress):
                    hi_l["n"] = tip_board.beat(rng, progress, hi_l["n"], ident=ident)
                    return hi_l["n"]

                res = tip_run.run_range(height=int(rng), cards=assignment, runner=runner,
                                        bundle_path=bp, now=time.time, sleep=time.sleep, feed=feed,
                                        on_event=lambda m: log(f"  {m}"), beat=_b,
                                        max_ticks=1200, tick_s=6.0)
                rc = os.path.join(a.rundir, f"receipt_{rng}.bin")
                gotr, which = runner.fetch_receipt(int(rng), rc)
                if not gotr:
                    raise RuntimeError(f"{rng} PROVED but the receipt could not be collected: {which}")
                with open(rc, "rb") as fh:
                    sok2, serr2 = tip_board.submit(rng, fh.read(), ident=ident)
                res["submitted"] = sok2
                if not sok2:
                    # ⚠ A rejected submit is not a failed proof: the receipt is on disk and can be
                    # resubmitted without re-proving. Say where, rather than losing it with the pods.
                    log(f"  ⛔ submit rejected for {rng}: {serr2}  (receipt kept at {rc})")
                return res

        if a.session:
            # ── keep the fleet and prove continuously (#367) ──────────────────────────────────────
            # ⛔ THE FLEET IS RENTED ONCE AND HELD. Releasing between blocks pays rent + a 410 MB
            # prover fetch + the GPU gate every time -- ~3 minutes of a ~10 minute tip window, for a
            # fleet that is about to be rebuilt identically.
            # ⚠ NO PREEMPTION, BY DECISION: a tip block that appears mid-proof waits for the current
            # board block to finish. Splitting would need two concurrent aggregates, and a board block
            # at the frontier is small (h=113,537 is a 595 KB bundle against 16 MB at h=418,268).
            spath = os.path.join(a.rundir, "session.json")
            state = tip_session.new_state(started_at=time.time(), duration_s=a.session * 3600.0,
                                          fleet_ids=[p["id"] for p in created])
            tip_session.save(spath, state)
            proved_tip = {"h": 0}

            def claim_fn():
                """The board's next block, or None when the board is genuinely busy.

                ⛔ AN ERROR IS NOT AN IDLE BOARD, AND THIS IS WHERE THAT GETS LOST. `tip_board.claim`
                goes to some trouble to separate "nothing available / already holds / rate limit"
                (benign, wait) from a refusal that retrying cannot fix (a signature the coordinator
                will not verify, a clock it rejects, a reserved handle). Collapsing both to
                `.get("range")` -- which is None either way -- made the first live session report
                `idle: the board has nothing free right now` for 15 minutes while G H O S T held
                0 of its 4 claims and the frontier sat at 113,536 with ~854k blocks unproven.
                A session that spins on a fixable fault is worse than one that stops.
                """
                res = tip_board.claim(ident=ident) or {}
                st = res.get("state")
                if st == "claimed":
                    log(f"  claimed {res['range']} (yours for {res.get('ttl', 3600) // 60} min)")
                    return res["range"]
                if st == "idle":
                    log(f"  board busy: {res.get('why')}")
                    return None
                raise RuntimeError(f"claim refused and retrying cannot fix it: {res.get('why')}")

            def work_fn():
                # ⛔ A TIP BLOCK IS "WAITING" ONLY IF ITS BUNDLE EXISTS. Asking the node for its height
                # would report a tip the fleet cannot prove: nothing is emitted below EMIT_FROM.
                t = tip_board.highest_tip_bundle(a.bridge_host)
                pending = t if (t and t > proved_tip["h"]) else None
                return tip_controller.next_work(pending, claim_fn)

            def prove_one(rng):
                # ⚠ A tip height is simply one at or above EMIT_FROM: the bridge emits nothing below
                # it, so a bundle can only come off the bridge host up there. Board work comes from
                # the frontier (~113,537) and is fetched from /api/witness. No cleverness needed.
                from_tip = str(rng).isdigit() and int(rng) >= TIP_FROM
                out = prove_and_submit(rng, from_tip=from_tip)
                if from_tip:
                    proved_tip["h"] = int(rng)
                return out

            # The fleet's hourly rate, from what RunPod actually charged for the cards we KEPT.
            live = {c.cid for c in order}
            rate_hr = sum(p["price"] for p in created if p["name"] in live)

            # ⛔ ELAPSED TIME, NOT BLOCK TIME. A pod bills from the moment it exists; claiming,
            # fetching a bundle, submitting and waiting on a busy board are all billed and none of
            # them is inside a block's wall_s. Charging block time undercounted by 17% over 42
            # blocks. This returns what has accrued since the last call, so the rate in force at
            # the time is the rate applied — which is what makes it survive a fleet resize.
            billed_to = {"t": time.time()}

            def spend_fn():
                now_t = time.time()
                delta, billed_to["t"] = now_t - billed_to["t"], now_t
                return rate_hr * (delta / 3600.0)

            # ⛔ MAX, NOT MEDIAN. Measured over 18 blocks: median 30.0 s, max 226.7 s. A
            # median-based guard let a claim be taken with 30 s left that then ran for nearly four
            # minutes; the block finished but the claim for the NEXT one was taken and orphaned.
            # Max never overruns; it costs at most one slow block's worth of idle at the tail.
            def estimate_s():
                seen = [b.get("wall_s") for b in state["blocks"].values()
                        if b.get("ok") and b.get("wall_s")]
                return max(seen) if seen else DEFAULT_BLOCK_EST_S

            phase(f"SESSION · {a.session:.1f} h on {len(order)} cards"
                  + (f", budget ${a.budget_usd:.2f}" if a.budget_usd else ""))
            log(f"  fleet rate ${rate_hr:.3f}/hr across {len(live)} card(s)")
            summ = tip_session.run_session(state=state, path=spath, prove=prove_one,
                                           work_fn=work_fn, now=time.time, sleep=time.sleep,
                                           feed=feed, on_event=lambda m: log(f"  {m}"),
                                           idle_s=30.0, block_estimate_s=estimate_s,
                                           budget_usd=(a.budget_usd or None), spend_fn=spend_fn)
            log("SESSION " + json.dumps(summ, indent=1))
            return 0

        if a.claim:
            result = prove_and_submit(a.block)
            phase(f"VERIFIED claimed block {a.block} in {result.get('wall_s')}s")
            log("RESULT " + json.dumps(result, indent=1))

            # ⛔ THE RECEIPT IS THE PRODUCT, AND IT LIVES ON A POD THAT IS ABOUT TO BE TERMINATED.
            # Collect it BEFORE the teardown, or the block is proved and unclaimable — an hour of TTL
            # burned on work nobody can see.
            rcpt = os.path.join(a.rundir, f"receipt_{a.block}.bin")
            got, which = runner.fetch_receipt(int(a.block), rcpt)
            if not got:
                raise SystemExit(f"block {a.block} PROVED but the receipt could not be collected: "
                                 f"{which}. The claim will reopen by itself.")
            log(f"receipt {os.path.getsize(rcpt)} bytes (from {which})")

            with open(rcpt, "rb") as fh:
                sok, serr = tip_board.submit(a.block, fh.read(), ident=ident)
            if sok:
                phase(f"SUBMITTED block {a.block} as {ident[2]}")
                log(f"✓ block {a.block} submitted and accepted for {ident[2]!r}")
                return 0
            # ⚠ A rejected submit is NOT a failed proof. The receipt is on disk and can be resubmitted
            # by hand; say where it is rather than losing it with the pods.
            log(f"⛔ submit rejected: {serr}")
            log(f"   the receipt is kept at {rcpt} — resubmit without re-proving")
            return 1

        # ⚠ The fold caption is set from the run's own event stream, so it appears when the
        # aggregate actually starts rather than when we guess it might.
        result = tip_run.run_block(block=a.block, cards=assignment, runner=runner,
                                   now=time.time, sleep=time.sleep, feed=feed,
                                   on_event=lambda m: log(f"  {m}"),
                                   max_ticks=1200, tick_s=6.0)
        phase(f"VERIFIED block {a.block} in {result.get('wall_s')}s on {len(order)} cards"
              if result.get("ok") else f"FAILED on block {a.block}")
        log("RESULT " + json.dumps(result, indent=1))

        # ⛔ RECORD WHAT THIS FLEET COST PER PROOF, OR THE 4090 DEFAULT STAYS AN ANECDOTE. #449's
        # default rests on nine runs on one block on one night, and nothing kept that result in a
        # form outliving the run -- so re-checking it meant re-reading a session transcript, which
        # is how "142 s of geography variance" got published when it was card type (hazync#448).
        # ⚠ Only a run that actually produced a proof is recorded; a failed run says nothing about
        # cost per proof and would drag every mean toward whatever went wrong.
        if result.get("ok"):
            try:
                row = tip_economics.record(
                    block=a.block,
                    gpu_types=[c.gpu_type for c in order],
                    seconds=result.get("wall_s"),
                    usd=tip_lifecycle.spend_so_far(
                        [{"price": c.price} for c in order], result.get("wall_s") or 0))
                if row:
                    log(f"economics: {row['fleet']}  {row['seconds']}s  ${row['usd']:.3f} "
                        f"(appended to the fleet ledger)")
            # ⚠ Bookkeeping NEVER fails a finished run. The proof is made and verified by this
            # point; losing a ledger row is a nuisance, losing the run's exit code is not.
            except Exception as e:                       # noqa: BLE001
                log(f"economics: not recorded ({e})")

        return 0 if result.get("ok") else 1

    finally:
        # ⛔ END THE WORKERS' ATTACH LOOP FIRST (hazync#463). `stop_auto_attach` documents itself as
        # "Called when the run is done with them" and had NO CALLER — every worker kept polling
        # /dev/tcp once a second for up to 86,400 iterations. Harmless when the pod is terminated
        # seconds later, which is why nobody noticed; not harmless under --keep, or when a card is
        # released without being terminated.
        # ⚠ Before the harvest, so the loop is not competing for the ssh channel we are about to use,
        # and best-effort by its own design: a card we cannot reach is one about to go away.
        try:
            if assignment and runner is not None:
                runner.stop_auto_attach(assignment)
        except Exception as e:                                     # noqa: BLE001
            log(f"stop_auto_attach: {e}")

        # ── harvest BEFORE release: this is the only chance ───────────────────────────────────────
        # ⛔ EVERY PREVIOUS RUN THREW ITS EVIDENCE AWAY. The pods were terminated below with no logs
        # fetched, so `agg.log`, every `aggw.log` and every `[rtt]` line died with them — which is the
        # whole reason hazync#252 and hazync#253 still say "the instrumentation exists, nobody has run
        # it". The instrumentation ran on every run; nothing kept the output.
        # ⚠ Wrapped whole: a harvest that fails must never leave cards billing. Release is below.
        try:
            if assignment:
                man = tip_harvest.harvest(ssh, assignment, agg, a.rundir)
                log(f"harvested {man['files']} log file(s) from {man['cards']} card(s) "
                    f"-> {os.path.join(a.rundir, 'logs')}")
                if man["missing"]:
                    log(f"  ⚠ not fetched (a dying pod refuses connections): {man['missing']}")
                agg_text, worker_texts = tip_harvest.read_logs(a.rundir)
                txt, blob = tip_harvest.report(agg_text, worker_texts,
                                               getattr(runner, "agg_started_ms", None))
                log("\n" + txt)
                with open(os.path.join(a.rundir, "measurements.json"), "w", encoding="utf8") as fh:
                    json.dump(blob, fh, indent=1)
        except Exception as e:                       # noqa: BLE001 — teardown must not be blocked
            log(f"  ⚠ harvest failed ({type(e).__name__}: {e}) — releasing the fleet regardless")

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
