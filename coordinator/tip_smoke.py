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
import threading
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
import tip_chain                        # noqa: E402
import tip_harvest                      # noqa: E402
import tip_lifecycle                   # noqa: E402
import tip_recruit                      # noqa: E402
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


# ⛔⛔ EXECUTION IS 54% OF A TIP BLOCK AND IT IS SERIAL CPU WORK ON THE AGGREGATE (hazync#567).
#
# 📏 Measured, tip hour 4: of a tip block's wall, the segment phase is 86% and the EXECUTOR's own
# window is 54% — and the segment phase can never fall below it, because a segment cannot be proved
# before it is produced. 969,118 carried a hard 157.7 s floor whatever the fleet size.
#
# ⛔ AND IT CANNOT BE PARALLELISED WITHOUT MOVING METHOD_ID. Chunk mode (guest mode 4) does distribute
# execution, but yields KIND_CHUNK and the coordinator requires KIND_RANGE; `fold_range` (mode 7)
# composes ranges ACROSS HEIGHTS, not within one block; and the guest asserts the domain tag
# (`assert!(l.kind == KIND_RANGE && rr.kind == KIND_RANGE)`, H8). Splitting one block's execution is a
# GUEST change, so it is out.
#
# ⇒ What is left is WHICH CARD executes. The aggregate is elected on reachability then network
# throughput and its CPU is never measured — while hour 3 recorded TWELVE distinct host CPUs on one
# fleet, from EPYC 7452 (~3.35 GHz) to i5-13600K (~5.1 GHz boost). Picking the aggregate blind to that
# may routinely put the executor on the slowest core in the fleet, and fixing it costs no rent.
#
# ⚠ THIS IS A HYPOTHESIS, NOT A MEASUREMENT. Nothing here has shown execution scales with single-core
# speed; it is strongly implied by single-threaded witness generation and unverified. So the probe
# REPORTS by default and only ranks on CPU under --agg-prefer-cpu. That is the same discipline lever 1
# and lever 2 shipped under, and lever 2's confident prediction measured NEGATIVE.
def probe_cpu(ssh, card):
    """(model, single_core_score, cores) for a card, or (None, None, None).

    The score is deliberately crude and self-relative: a fixed integer loop timed on ONE core. It is
    not a benchmark anyone should quote — it exists to order candidates against each other on the same
    evening, which is the only comparison that matters here.
    """
    body = (
        "MODEL=$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//'); "
        "CORES=$(nproc 2>/dev/null); "
        # ⚠ `time` on a shell builtin loop, not python: the pods are not guaranteed a python, and a
        # process launch would dominate a short measurement.
        "S=$( { TIMEFORMAT=%R; time (i=0; while [ $i -lt 300000 ]; do i=$((i+1)); done) ; } 2>&1 ); "
        "echo \"CPUPROBE|$MODEL|$CORES|$S\"")
    out = ssh.run(card, body)
    if not out:
        return None, None, None
    for ln in out.splitlines():
        if ln.startswith("CPUPROBE|"):
            _, model, cores, secs = (ln.split("|") + ["", "", ""])[:4]
            try:
                secs = float(secs)
            except ValueError:
                return (model or None), None, (int(cores) if cores.isdigit() else None)
            # Higher is better, so invert: a shorter loop time is a faster core.
            score = (1.0 / secs) if secs > 0 else None
            return (model or None), score, (int(cores) if cores.isdigit() else None)
    return None, None, None


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

# ⚠ BUMP THIS WITH EVERY RELEASE WHOSE INSTRUMENTATION THE FLEET NEEDS. `test_host_pin.py`
# enforces it against docs/history/releases/, because this pin sat on v0.21.0 for SEVEN releases
# and every run proved correctly while producing none of the evidence the fleet exists for.
# v0.22.1 is what puts `card=` on the [rtt] line (#570) and the corrected phase summary (#567)
# on the cards — without it a run still cannot say WHICH card has the slow tail.
HOST_RELEASE = "v0.22.1"
HOST_URL = (f"https://github.com/hazync/hazync/releases/download/{HOST_RELEASE}/"
            "hazync-host-x86_64-linux-gnu-cuda")


def binary_size():
    """The prover binary's real size, from the release. Never guessed."""
    import urllib.request
    req = urllib.request.Request(HOST_URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def slow_worker_cut(order, per, *, need, floor=0.0):
    """Which workers to release so the aggregate is not feeding a tail (hazync#527).

    Returns (drop_cids, ranked) where `ranked` is [(cid, mbit_or_None)] worst-first among droppable
    cards, for the log. Pure: it releases nothing and asks nothing of the network.

    ⛔ WHY THE TAIL AND NOT THE MEDIAN. The fold waits for the SLOWEST peer at every level, so one
    bad link sets the wall clock for the whole block. Measured on block 968,340, 7,986 join samples:

        min 0.4 s    p50 17.1 s    p90 55.5 s    max 107.4 s      a 268x spread

    That block took 31.8 minutes and the chain moved three blocks past us. Adding cards does not fix
    a tail -- it adds join levels for the tail to appear at.

    ⛔ AND IT GATES ON MEASUREMENT, NOT ON THE DATACENTRE LABEL. `dc` has been recorded per card all
    along and it is tempting to just cluster on it, but the run that produced those numbers says
    plainly that "nothing here separates geography from card-to-card routing". Dropping a card for
    the datacentre it happens to sit in would be acting on a hypothesis nobody has tested; dropping
    it for a link we just measured is acting on evidence.

    ⚠ AN UNMEASURED CARD RANKS AT THE MEDIAN OF THE MEASURED ONES. "Untested is not failed" is the
    rule the aggregate choice and the reachability gate already follow, so an unmeasured card is not
    condemned -- but it cannot be favoured over a card measured to be fast either, or the gate would
    prefer ignorance. The median is where a typical card sits, which is the honest default.

    ⚠ THE AGGREGATE IS NEVER DROPPED. It is order[0] by construction, it is not in `per` (it does not
    stream to itself), and hazync#509 is what happens when a gate forgets that a role is not
    fungible: a fleet-relative check dropped the aggregate and killed a 30-card run 34 seconds in.
    """
    if not order:
        return set(), []
    agg, workers = order[0], order[1:]
    if len(order) <= need:
        return set(), []

    measured = [v for v in (per.get(c.cid) for c in workers) if v is not None]
    median = sorted(measured)[len(measured) // 2] if measured else None

    def rank(c):
        v = per.get(c.cid)
        return (v if v is not None else median) if (v is not None or median is not None) else 0.0

    # ⚠ Sort by rank, then by cid, so the same fleet always yields the same decision. Without the
    # tiebreak two equal cards would be dropped in whatever order the dict happened to give.
    ranked = sorted(workers, key=lambda c: (rank(c), c.cid))

    drop, surplus = set(), len(order) - need
    # ⛔ Evidence first: a card MEASURED below the floor goes regardless of surplus, because keeping
    # it is choosing to wait for it. An unmeasured card is never cut by the floor -- there is nothing
    # to compare it against.
    if floor:
        # ⚠ WORST FIRST, not iteration order. When the floor can only take one card -- because
        # dropping a second would go below `need` -- it must take the WORST one. Iterating the fleet
        # in its own order cut a 12 Mbit/s link and left a 3 Mbit/s one in place, which is the exact
        # tail this gate exists to remove. Caught by test_worker_gate, not by reading it.
        below = sorted(((per[c.cid], c.cid) for c in workers
                        if per.get(c.cid) is not None and per[c.cid] < floor))
        for _v, cid in below:
            if len(order) - len(drop) > need:
                drop.add(cid)
    for c in ranked:
        if len(drop) >= surplus:
            break
        drop.add(c.cid)
    drop.discard(agg.cid)
    return drop, [(c.cid, per.get(c.cid)) for c in ranked]


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


class FetchFleet:
    """What the OTHER cards are doing, so a laggard can be judged against the fleet (hazync#503).

    ⛔ A PER-CARD GUARD CANNOT SEE A SLOW CARD. Both of `fetch_binary`'s guards ask only about the
    card in front of them: the stall check asks "did the byte count move" and the ceiling asks "have
    40 minutes passed". Measured 2026-09-23 -- 17 of 18 cards had the whole 410 MB within seconds,
    and hz-smoke-13 pulled at 93 KB/s:

        hz-smoke-13   69,414,912 of 410,441,528   +4,210,688 bytes in 45 s   ~60 min remaining

    It was growing the whole time, so it never stalled; it was nowhere near 40 minutes, so the
    ceiling never fired. The fleet sat in the gate for 18 minutes at $13.32/hr with every GPU idle
    at 16 W, and the only way out was to terminate the pod by hand -- killing the curl did not work,
    because `-C -` resumes, exactly as designed.

    ⚠ THE SPARES ARE WHAT BUY THE IMPATIENCE. Cutting a card short is only safe while enough others
    remain to run, so that is the condition, and it is asked here rather than per card: `abandon`
    refuses once dropping one more would leave fewer than `keep`, and that card gets the old patient
    treatment. ⛔ A run with no spares therefore behaves EXACTLY as it did before -- which matters,
    because the previous fix in this area (hazync#479) exists to stop a merely-slow card killing a
    run that has nothing to swap in.
    """

    def __init__(self, total, keep, *, grace_s=120, never_abandon=()):
        self.total, self.keep, self.grace_s = total, keep, grace_s
        self.done, self.dropped, self.enough_at = 0, 0, None
        self.reasons = {}
        # ⛔ CARDS THAT ARE NOT FUNGIBLE. A worker is replaced by a spare; the AGGREGATE is not --
        # every worker's reachability was tested against it, so dropping it ends the run. This gate
        # did exactly that on its first live outing (2026-09-24), cutting the aggregate loose for
        # needing two more minutes and killing a healthy 30-card fleet 34 seconds in.
        self.never_abandon = set(never_abandon)
        self._lk = threading.Lock()

    def completed(self, cid):
        with self._lk:
            self.done += 1
            # ⚠ The clock starts when the fleet could RUN, not when the first card lands. With a
            # floor of 15 and 18 rented, three cards may finish long before the run has enough.
            if self.enough_at is None and self.done >= self.keep:
                self.enough_at = time.time()

    def grace_left(self):
        """Seconds a laggard still has, or None while the fleet does not yet have enough cards."""
        with self._lk:
            if self.enough_at is None:
                return None
            return self.grace_s - (time.time() - self.enough_at)

    def abandon(self, cid, why):
        """Take one card out of the fleet, if the fleet can still afford to lose it."""
        with self._lk:
            # ⛔ NO SURPLUS BUYS THE AGGREGATE. This is an identity, not a threshold: there is no
            # fleet size at which losing the one card the run cannot promote is the cheap option.
            if cid in self.never_abandon:
                return False
            # ⛔ COUNT THE SURVIVORS, NOT THE CASUALTIES. `total - dropped - 1` is what would be
            # left if this card went; comparing `dropped` against the spare count instead would be
            # wrong the moment the run rented fewer pods than it asked for, which is the normal
            # case when capacity is thin (18 of a requested 60 on 2026-09-23).
            if self.total - self.dropped - 1 < self.keep:
                return False
            self.dropped += 1
            self.reasons[cid] = why
            return True


def fetch_binary(ssh, card, want, *, wait_s=15, stall_polls=8, ceiling_s=2400, fleet=None):
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

    ⛔ AND "MOVING" IS NOT ENOUGH EITHER (hazync#503). A card can climb steadily at 93 KB/s and hold
    a whole fleet in the gate for the full 40 minutes without ever tripping either guard. Pass a
    `FetchFleet` and the third question gets asked -- not "is this card moving" but "is it going to
    arrive before the fleet that is already waiting for it has been paid for twice" -- and the
    answer is acted on only while there are enough other cards to run without it.
    """
    got, last, stuck, t0, polls = -1, -1, 0, time.time(), 0
    while time.time() - t0 < ceiling_s:
        out = ssh.run(card, "stat -c%s /workspace/hazync-host-cuda 2>/dev/null || echo 0",
                      timeout=60) or "0"
        got = int((out.strip().splitlines() or ["0"])[-1] or 0)
        polls += 1
        if got == want:
            ssh.run(card, "chmod +x /workspace/hazync-host-cuda", timeout=60)
            if fleet is not None:
                fleet.completed(card.cid)
            return True, got
        # ── is this card worth waiting for, given what the rest of the fleet has already done? ──
        # ⚠ ASKED ONLY ONCE THE FLEET HAS ENOUGH. Before that there is no surplus to spend and
        # every card is needed, so `grace_left()` returns None and this whole branch is skipped.
        if fleet is not None:
            left = fleet.grace_left()
            # ⚠ NEVER ON THE FIRST POLL. The loop polls before it starts the curl, so every card
            # reads 0 bytes at t=0, and a card whose ssh hiccups reads 0 once too. Judging on one
            # reading would condemn a healthy card for the crime of not having started yet; a
            # second poll costs one `wait_s` and is what makes the number a RATE.
            if left is not None and polls >= 2:
                # ⚠ Rate since the start, not since the last poll. A single interval is noisy
                # enough to condemn a healthy card on one slow ssh round-trip; the average over
                # the whole fetch is what was 93 KB/s in the incident, and it is stable.
                elapsed = time.time() - t0
                rate = got / elapsed if elapsed > 0 and got > 0 else 0.0
                # ⛔ NO RATE MEANS NO ARRIVAL. A card still at zero bytes while the rest of the
                # fleet is ready is not "unmeasured", it is not coming -- treat it as infinite ETA
                # rather than letting an undefined number read as fast.
                eta = (want - got) / rate if rate > 0 else float("inf")
                if eta > max(left, 0.0):
                    why = (f"it needs ~{eta / 60:.0f} more min at {rate / 1e3:.0f} KB/s "
                           f"({got:,}/{want:,}) and the fleet is ready now")
                    if fleet.abandon(card.cid, why):
                        return False, got
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


# ── segment size is a property of the WEAKEST card in the fleet ──────────────────────────────────
PO2_22_PEAK_GB = 40.6          # measured: a stock chunk peaked here, so 24 GB cards cannot run it
PO2_22_MIN_VRAM_GB = 48


def resolve_seg_po2(setting, fleet, catalogue=None, api=None):
    """The po2 to run, and why. Returns (po2_str, reason).

    ⛔ ONE po2 FOR THE WHOLE BLOCK. `segment_limit_po2()` is set on the ExecutorEnv, so the aggregate
    segments the block once and every worker proves what it is handed -- there is no per-card po2.
    The fleet is therefore capped by its SMALLEST VRAM, not its average.

    ⭐ po2 22 measured ~11-12% faster on block 962,000 (~7,200 inputs) and peaks ~40.6 GB. On a
    9,000-segment block that is the difference between 674 s and 597 s against a 600 s gate, so it is
    not a micro-optimisation at tip scale.

    ⚠ A card whose VRAM we do not know is treated as TOO SMALL. Guessing upward here costs the whole
    block: every worker OOMs on a segment it cannot hold, and the retry ladder reads as a slow fleet.
    """
    if str(setting).lower() != "auto":
        return str(int(setting)), f"pinned by --seg-po2 {setting}"
    # \u26d4 THE CATALOGUE IS ONLY BUILT FOR --gpu-type auto, AND A PINNED LIST IS THE NORMAL CASE.
    # Without this the VRAM lookup is empty on every pinned run, every type reads as unknown, and the
    # answer silently degrades to 21 -- the safe direction, but wrong, and invisible.
    if not catalogue and api is not None:
        try:
            catalogue = rank_card_types(api)
        except Exception:                 # noqa: BLE001 -- a failed lookup means 21, not a dead run
            catalogue = None
    vram = {}
    for c in (catalogue or []):
        if c.get("id") and c.get("vram"):
            vram[c["id"]] = float(c["vram"])
    types = sorted({p.get("gpu_type") for p in fleet if p.get("gpu_type")})
    if not types:
        return "21", "no fleet card types known — falling back to the safe 21"
    unknown = [t for t in types if t not in vram]
    if unknown:
        return "21", f"VRAM unknown for {unknown[:2]} — 21, because guessing upward OOMs the block"
    smallest = min(vram[t] for t in types)
    if smallest >= PO2_22_MIN_VRAM_GB:
        return "22", (f"every card has >={PO2_22_MIN_VRAM_GB}GB (smallest {smallest:.0f}GB) and po2 22 "
                      f"peaks ~{PO2_22_PEAK_GB}GB")
    return "21", (f"the smallest card has {smallest:.0f}GB and po2 22 peaks ~{PO2_22_PEAK_GB}GB — "
                  f"one card that cannot hold a segment stalls the whole block")


def dash_chain(a):
    """The processes that turn telemetry into a published frame, and keep the frames.

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
    # \u2b50 AND THE ARCHIVER, SO THE RUN LEAVES A TIMELAPSE INSTEAD OF ONE PNG. `publish.sh`
    # rewrites a single frame.png; the previous frame is gone. That is right for a live page and
    # useless afterwards -- the most legible artifact a tip run can produce is the hour compressed
    # into a few seconds, and after the run there is nothing left to compress.
    #
    # \u26d4 IT WAS NEVER IN THIS CHAIN. Measured 2026-09-28: nothing was archiving, and the archiver
    # was started by hand 49 minutes in, so the run's frames from 12:45 to 13:34 do not exist. The
    # run that needs it least is the one someone is watching; started here, no one has to remember.
    #
    # \u26a0 Deduped by CONTENT by the script itself, so an idle fleet does not outweigh the minutes
    # that matter, and it writes into the RUNDIR -- the frames belong to the run, not to the rig.
    chain.append(("archiver", ["/bin/bash", os.path.join(a.live_rig, "frame-archive.sh"),
                               os.path.join(a.live_rig, "frame.png"),
                               os.path.join(a.rundir, "frames"), "3"]))
    return chain


def adopt_fleet(api, record_path, expect_names=None):
    """Pods from a previous run's rented.json that are STILL ALIVE on the account.

    ⛔ A RECORD IS NOT A POD. rented.json says what was rented, not what still exists: the previous
    driver may have released them, RunPod may have reclaimed one, or the file may be from a run three
    days ago. Adopting the file blindly makes the run sit in its ssh wait for pods that are not there
    and then fail at the gate with nothing to show for the time. So every id is checked against a
    LIVE LISTING and the dead ones are dropped here, loudly, before anything depends on them.

    Returns (adopted, missing). Raises SystemExit only when the record itself cannot be read.
    """
    try:
        with open(record_path) as fh:
            records = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"--adopt {record_path}: cannot read the rental record ({e})")
    if not isinstance(records, list) or not records:
        raise SystemExit(f"--adopt {record_path}: the rental record is empty — nothing to adopt")

    live = {}
    for pod in api.pods():
        if pod.get("id"):
            live[str(pod["id"])] = pod

    adopted, missing = [], []
    for r in records:
        pid = str(r.get("id") or "")
        if pid and pid in live:
            # Keep the RECORD (it carries gpu_type, price and dc, which the listing may not), but
            # take the name from the live pod so a rename on RunPod's side cannot desync the feed.
            r = dict(r)
            r["name"] = live[pid].get("name") or r.get("name")
            adopted.append(r)
        else:
            missing.append(r.get("name") or pid or "?")
    return adopted, missing


def cleanup(api, rented_path):
    """Release whatever a dead driver left behind. Safe to run at any time.

    ⛔ READ BOTH RECORDS, NEVER JUST ONE (hazync#557). `rented.json` is written by the rent phase and
    `recruited.json` by the recruiter thread, and a run with `--grow-to` puts cards in the second
    that the first has never heard of. Measured 2026-09-28: rented.json held 17 while the fleet had
    grown to 26. Releasing the union is the difference between this command doing its job and
    reporting success over nine pods still billing at ~$18.81/hr.

    ⚠ This is the RECOVERY path -- it runs precisely when the driver is not around to know better,
    so it must assume its own bookkeeping is incomplete.
    """
    rec_path = os.path.join(os.path.dirname(rented_path), "recruited.json")
    pods, sources = {}, {}
    for path, what in ((rented_path, "rented.json"), (rec_path, "recruited.json")):
        try:
            with open(path) as fh:
                for p in json.load(fh):
                    if p.get("id"):
                        pods.setdefault(p["id"], p)
                        sources.setdefault(p["id"], what)
        except (OSError, ValueError):
            log(f"  (no usable {what})")
    if not pods:
        log(f"no rental record at {rented_path} or {rec_path} — nothing to clean up")
        return 0
    only_rec = [i for i, s in sources.items() if s == "recruited.json"]
    log(f"cleanup: {len(pods)} pod(s) across both records"
        + (f", {len(only_rec)} of them known ONLY to recruited.json" if only_rec else ""))

    ids = list(pods)
    v = tip_session.terminable(ids, ids)
    log(f"cleanup: releasing {v['terminate']} (protected={v['protected']} not-ours={v['not_ours']})")
    for pid in v["terminate"]:
        log(f"  {pid}: {'gone' if sponsor_bot.terminate_confirmed(api, pid) else '⛔ STILL LISTED'}")

    # ⛔ CHECK THE ACCOUNT, NOT OUR OWN LIST. A pod this command never heard of is exactly the
    # failure it exists to catch, and comparing only against `pods` can never see one.
    live = {p["id"]: p.get("name") for p in api.pods()}
    left = [pods[i].get("name") or i for i in pods if i in live]
    unknown = [n for i, n in live.items() if i not in pods and not tip_session.is_protected(i)]
    log(f"account check: {'clean' if not left else '⛔ STILL PRESENT: ' + str(left)}")
    if unknown:
        log(f"⚠ {len(unknown)} pod(s) on the account that neither record knows about: {unknown} — "
            f"NOT touched (this command only releases what it can show it rented)")
    if not left:
        for path in (rented_path, rec_path):
            try:
                os.unlink(path)
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
    ap.add_argument("--fresh-tip", action="store_true",
                    help="prove only blocks mined AFTER the fleet is ready: the claim floor starts "
                         "at the current tip, so boot is paid while idle and each block is timed "
                         "from a height that did not exist when the run began")
    # ⛔ THE HOUR MUST NOT BE SPENT WAITING FOR THE THING IT MEASURES (hazync#553). The 2026-09-28 run
    # started its window the moment 17 cards were ready and then sat 14+ minutes waiting for the chain
    # to mine a block above the --fresh-tip floor, filling the time with board work. A quarter of the
    # hour was gone before the first tip block existed, so "blocks proved in one hour" understated the
    # fleet by whatever the chain happened to be doing. Board fill does NOT start the clock.
    # ⛔ THE TIP IS PROVED IN SEQUENCE (hazync#556). The selector asked `highest_tip_bundle` what to
    # prove next, so when 968,984 and 968,985 were mined 5 seconds apart on 2026-09-28 their bundles
    # landed together, the run took 985, and 984 has no proof and never will. The product is a CHAIN
    # of proofs: a gap is a missing link, not a slower result.
    # ⚠ EACH GATE HOLDS AN SSH CHANNEL AND PULLS 411 MB. Unbounded parallelism would saturate the
    # box's uplink and slow the very gates it is overlapping.
    # ⭐ SEGMENT SIZE, CHOSEN BY THE WEAKEST CARD. po2 22 measured ~11-12% faster on a
    # NEAR-TIP block (962,000, ~7,200 inputs) but peaks ~40.6 GB, so a 24 GB card cannot run
    # it at all and the fleet is capped by its smallest VRAM. `auto` reads the fleet.
    #
    # ⛔ IT CANNOT BE MIXED. segment_limit_po2() is set on the ExecutorEnv, so the aggregate
    # segments the block ONCE and every worker proves whatever it is handed.
    #
    # ⚠ The note in prover/host/src/main.rs calling 21 and 22 'flat' was measured on block
    # 130,000 -- a 2011 block with almost no transactions, which says nothing about fold
    # overhead at 9,000 segments. Judge this at the tip or not at all.
    ap.add_argument("--seg-po2", default="auto",
                    help="zkVM segment size: 'auto' (22 when every card has >=48GB, "
                         "else 21), or a number to pin it")
    ap.add_argument("--gate-parallel", type=int, default=4,
                    help="how many recruits to gate at once (default 4). Gating was serial, which "
                         "capped growth at roughly one card every four minutes")
    ap.add_argument("--tip-max-behind", type=int, default=3,
                    help="how many unproved tip bundles may queue before the session gives up on "
                         "sequence and jumps to the newest, recording the skipped heights "
                         "(0 = never jump, always stay sequential). Default 3")
    # ⚠ A SEQUENTIAL RUN THAT CAN NEVER CATCH UP STOPS FOLLOWING THE TIP AT ALL, which is the thing
    # the rig exists to demonstrate. The bound above is what keeps "gapless" from quietly becoming
    # "permanently behind"; the skip is then explicit, in the ledger and in the summary.
    ap.add_argument("--tip-from", type=int, default=0,
                    help="start the tip floor just below this height instead of at the current "
                         "tip, to close a known gap (e.g. --tip-from 968984). Overrides "
                         "--fresh-tip's floor")
    ap.add_argument("--allow-tip-gaps", action="store_true",
                    help="prove a tip block even when it is not the child of the last one proved. "
                         "⛔ Produces a proof chain with a hole in it; the refusal exists for a "
                         "reason and this is for recovery only")
    ap.add_argument("--clock-from-tip", action="store_true",
                    help="start the --session window at the FIRST TIP BLOCK rather than when the "
                         "fleet is ready. Waiting and board fill are then billed but not counted "
                         "against the hour; the summary reports both totals separately")
    # ⚠ THE BOUND ON WAITING, AND IT IS NOT OPTIONAL. A deferred clock has no deadline until it starts,
    # so a bridge that stops serving tip bundles would hold a rented fleet indefinitely with every
    # other guard satisfied. --budget-usd is the other backstop and it CAN be omitted; this cannot.
    ap.add_argument("--clock-wait-max", type=float, default=2.0,
                    help="hours to wait for the first tip block before giving up, with "
                         "--clock-from-tip (default 2.0)")
    # ⛔ AN ESCAPE HATCH, NOT THE DEFAULT. Filling the gaps between tip blocks with board work is
    # what #367 asked for and what the flagship hour measured the cost of not doing -- 31.4 idle
    # minutes, $5.81. This flag exists for the case where a run must be a clean measurement of tip
    # latency alone, with nothing else touching the fleet.
    # ⛔ THE ONE ROLE WHERE THE LINK MATTERS MORE THAN THE GPU (hazync#517). 1 disables the probe and
    # restores the old "first card that answered ssh" behaviour, for a run that must not spend the
    # ~10 s per candidate.
    ap.add_argument("--agg-candidates", type=int, default=3,
                    help="how many cards to measure before choosing the aggregate (1 = do not probe)")
    ap.add_argument("--agg-probe-secs", type=int, default=8,
                    help="seconds each candidate streams to every worker at once")
    # ⚠ Default 0 = REPORT, do not refuse. A block needs roughly segments x ~1.3 MB x 8 / target_s
    # (1.1-1.3 MB/segment measured over two runs), so 9,600 segments under 600 s wants ~166 Mbit/s --
    # but the right floor depends on the block and the target, so the operator sets it.
    ap.add_argument("--agg-prefer-cpu", action="store_true",
                    help="rank aggregate candidates on single-core CPU speed (after "
                         "reachability, before link speed). The executor runs on the "
                         "aggregate alone and is ~54%% of a tip block's wall (hazync#567), "
                         "and hour 3 saw TWELVE distinct host CPUs on one fleet. ⚠ OFF by "
                         "default: the CPU is always measured and reported, but nothing has "
                         "yet shown execution scales with this score — lever 2 was predicted "
                         "to save 36s and measured NEGATIVE. Turn it on to test it.")
    ap.add_argument("--agg-min-mbit", type=float, default=0.0,
                    help="refuse to start if the best aggregate candidate is below this Mbit/s")
    ap.add_argument("--no-board-fill", action="store_true",
                    help="with --fresh-tip, sit idle between tip blocks instead of proving board "
                         "work; use when measuring tip latency with nothing else on the fleet")
    ap.add_argument("--worker-min-mbit", type=float, default=0.0,
                    help="release any WORKER the chosen aggregate cannot push to at this rate "
                         "(Mbit/s), and, when there are spares, the slowest ones down to --cards. "
                         "0 (default) = measure and report, release nothing. The fold waits for the "
                         "slowest peer at every level, so the tail sets the wall clock: block "
                         "968,340 saw join RTTs from 0.4 s to 107.4 s and took 31.8 minutes")
    ap.add_argument("--grow-to", type=int, default=0, metavar="N",
                    help="during a SESSION, keep trying to grow the fleet toward N as capacity "
                         "appears. Stock is erratic -- the same card went 26 available -> 0 -> 18 in "
                         "one evening (hazync#504) -- so a run that rents once takes whatever "
                         "existed in the second it started. Recruits are rented and gated on a "
                         "background thread, off the clock, and join at a BLOCK BOUNDARY; one that "
                         "is not ready simply misses that block. 0 = do not grow")
    ap.add_argument("--min-cards", type=int, default=0, metavar="N",
                    help="the FLOOR: run with whatever survives the gates, as long as it is at least "
                         "this many. Default 0 = use --cards, which is the old all-or-nothing "
                         "behaviour. ⛔ --cards is a TARGET; treating it as a floor threw away four "
                         "healthy fleets on 2026-09-27 — 29 cards when 30 were asked for, then 28 of "
                         "29, then 25 of 26 twice — each after paying for every pre-clock gate")
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
    ap.add_argument("--adopt", metavar="RENTED_JSON", default="",
                    help="reuse the pods in a previous run's rented.json instead of renting new "
                         "ones. Capacity is the binding constraint on a big fleet -- 30 RTX PRO "
                         "6000 could be had on 2026-09-27 and the 31st could not -- so a fleet that "
                         "dies with the run that rented it is a fleet that may not be reassembled. "
                         "Adopted pods are NOT released at the end unless --release-adopted")
    ap.add_argument("--release-adopted", action="store_true",
                    help="with --adopt, DO release the adopted pods at the end. Off by default: "
                         "surviving the run is the whole point of adopting them")
    ap.add_argument("--claim", action="store_true",
                    help="claim a block from the board and prove it from its BUNDLE (mode 6, #367) "
                         "instead of proving a named fixture")
    ap.add_argument("--claim-as", default=None, metavar="HANDLE",
                    help="refuse to start unless this box's claim identity is HANDLE. Board work is "
                         "credited to whoever signs the claim, permanently and publicly, and "
                         "tip_board.identity() returns whatever $HAZYNC_HOME holds — so a run on the "
                         "wrong box claims as the wrong name and says so in one line nobody reads. "
                         "Checked before anything is rented")
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
    # ⛔ THE FLOOR IS NOT THE TARGET. Every pre-clock gate can legitimately drop a card -- a pod that
    # never starts, a GPU that cannot prove, a link the aggregate cannot push to -- and the gates are
    # right to drop them. What was wrong was the response: the run asked for N, got N-1, and exited
    # having paid for all of it. 2026-09-27 lost four fleets that way in under an hour.
    # Defaulting min_cards to cards keeps the old behaviour exactly for anyone who wants it.
    a.min_cards = a.min_cards or a.cards
    if a.min_cards > a.cards:
        raise SystemExit(f"--min-cards {a.min_cards} is above --cards {a.cards}: the floor cannot "
                         f"exceed the target")

    # ⚠ A ZERO OR NEGATIVE WAIT WOULD ARM THE CLOCK BY EXPIRING IT -- the deadline is checked before
    # any tip block can arrive, so the session would stop instead of waiting. Refuse it here rather
    # than let it read as "the chain stalled".
    if a.clock_from_tip and a.clock_wait_max <= 0:
        raise SystemExit("--clock-wait-max must be above 0: a zero wait stops the session before the "
                         "first tip block can arrive to start its clock")

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
        # ⛔ WHOSE WORK IS THIS? tip_board.identity() returns whatever $HAZYNC_HOME happens to hold,
        # and every box has SOME identity, so a run always claims as someone. Measured 2026-09-26:
        # a tip session launched on the rig claimed board blocks as 'hazync-coordinator'
        # (9be361b031…) when hazync#367 decided they are claimed as 'G H O S T' (c4c7d99b6b…) --
        # whose key deliberately lives on a box the operator owns and NOT on any server. The log line
        # above said so plainly and nobody was reading it; the operator spotted it, not the tooling.
        #
        # ⚠ The credit is the whole point of claiming. Board work proved under the wrong identity is
        # not a cosmetic slip: it lands on a leaderboard, under a name, permanently.
        #
        # ⚠ Checked HERE, before anything is rented, so a wrong identity costs nothing. Default None
        # keeps the old behaviour -- this refuses only when an operator has said who they expect.
        if a.claim_as and a.claim_as != ident[2]:
            raise SystemExit(
                f"--claim-as {a.claim_as!r} but this box's identity is {ident[2]!r} "
                f"({ident[1][:10]}…). Nothing has been rented. Point HAZYNC_HOME at the identity "
                f"whose name should go on this work, or drop --claim-as if the box's own is right.")
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
    # Always bound, so the teardown can ask about them whichever path the run took.
    recruiter, recruited = None, []

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

        # ⭐ ADOPT AN EXISTING FLEET INSTEAD OF RENTING ONE (the supply half of hazync#506).
        #
        # ⛔ CAPACITY, NOT MONEY, IS WHAT CAPS A BIG FLEET. Measured 2026-09-27 at 45 requested:
        # RTX 4090 granted 1, RTX PRO 4500 SE granted 4, RTX PRO 6000 granted 38 — and the live run
        # then got exactly 30 before the 31st was refused. A fleet that size may not be reassembled
        # on demand, so releasing it at the end of a one-hour session can cost more than the pods do.
        if a.adopt:
            adopted, missing = adopt_fleet(api, a.adopt)
            if missing:
                log(f"⚠ {len(missing)} pod(s) in {a.adopt} are NO LONGER on the account and were "
                    f"dropped: {sorted(missing)}")
            if len(adopted) < a.min_cards:
                raise SystemExit(f"--adopt {a.adopt}: only {len(adopted)} of the recorded pods are "
                                 f"still alive; at least {a.min_cards} are needed. Lower "
                                 f"--min-cards or rent fresh")
            created = adopted
            # ⛔ `rented` and `want` are read AFTER this branch (the ssh wait, the prover-fetch
            # gate and their messages), and both were only ever assigned on the renting path. An
            # adopted fleet therefore reached `waiting for N of {want}` with `want` unbound and died
            # with UnboundLocalError — after the pods were already adopted, so the run had a fleet
            # and no driver. There is no spare to wait for when the fleet is adopted: both are the
            # number of pods actually in hand.
            rented = want = len(adopted)
            log(f"ADOPTED {len(created)} live pod(s) from {a.adopt} — nothing rented")
            # ⚠ The record is now THIS run's to maintain: a card dropped at a gate must leave the
            # file, or a later --cleanup would try to release a pod that is already gone.
            with open(rented_path, "w") as fh:
                json.dump(created, fh, indent=1)
        else:
            # ⚠ `rented` NOT `want` (hazync#492). `want` is reused at the prover-fetch gate for the
            # BINARY SIZE, so a message down there that reads `want` prints 410441528 where a card count
            # belongs — which is what a real run reported: "only 2 of 410441528 rented cards".
            rented = a.cards + a.spares
            want = rented
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
                # ⛔ TARGET, NOT FLOOR: a type that can field only the floor should not stop us
                # trying the next one, which might field the target. The final gate check is what
                # accepts a short fleet, after the gates have had their say.
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
                    if len(created) >= a.min_cards:
                        log(f"  no capacity for {name} — continuing with {len(created)} pod(s), "
                            f"{a.min_cards} needed at minimum")
                        break
                    raise SystemExit(f"no capacity for {name} — only {len(created)} pod(s) rented and "
                                     f"at least {a.min_cards} are needed")
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

        # ⛔ WAIT FOR THE TARGET, REFUSE ON THE FLOOR. wait_for_ssh stops the moment it holds
        # `need` cards, so passing the FLOOR turned it into an impatience setting: on 2026-09-27
        # a 21-card fleet reached 10 after 44 seconds and the other five were released as "never
        # answered ssh" — with 420 s of its timeout left, and every one of them still booting.
        # The floor decides whether the run PROCEEDS (the check below); never how long to wait.
        phase(f"PREPARING · waiting for {a.cards} of {want} cards to answer "
              f"(will proceed on {a.min_cards})")
        cards, portmap = wait_for_ssh(api, created, ssh, need=a.cards)
        if len(cards) < a.min_cards:
            raise SystemExit(f"only {len(cards)} of {want} rented cards came up; "
                             f"needed at least {a.min_cards}")

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
        def _release_pod(p, why):
            """Release a pod a PRE-CLOCK GATE dropped — unless the fleet was adopted.

            ⛔ A DROPPED CARD IS DROPPED FROM THIS RUN, NOT FROM EXISTENCE. --adopt promises the
            fleet outlives the run, and four separate paths broke that promise: the teardown, the
            surplus cut (#547), and every gate drop (#551). On 2026-09-28 a wrong --repo failed the
            staging gate and destroyed four hand-picked RTX PRO 6000 that had taken two rental rounds
            to assemble — a recoverable operator error cost an unreplaceable fleet, because a
            specific siting cannot be re-requested from RunPod.

            ⚠ A RENTED pod is still terminated here, and must be: #479 exists because a card failing
            a gate used to end the whole run instead of being dropped from it.
            """
            if a.adopt and not a.release_adopted:
                log(f"  dropped {p['name']} from the run — {why} (LEFT RUNNING: adopted)")
                return
            sponsor_bot.terminate_confirmed(api, p["id"])
            log(f"  released {p['name']} — {why}")

        if never_up:
            log(f"releasing {len(never_up)} pod(s) that never answered ssh: {sorted(never_up)}")
            order, created = _drop_cards(
                order, created, never_up, "it never answered ssh",
                release=lambda p: _release_pod(p, "it never answered ssh"),
                record=lambda kept: json.dump(kept, open(rented_path, "w"), indent=1))

        def _drop(order_, created_, bad, why):
            def release(p):
                _release_pod(p, why)
            def record(kept):
                with open(rented_path, "w") as fh:
                    json.dump(kept, fh, indent=1)
            return _drop_cards(order_, created_, bad, why, release=release, record=record)

        # ⛔ THE AGGREGATE IS CHOSEN ON ITS LINK, NOT ON WHO ANSWERED SSH FIRST (hazync#517).
        # This was `order[0]`. Every segment is pushed FROM the aggregate and every join round-trips
        # THROUGH it -- 12.48 GB through one card on block 968,340 -- so it is the one role where the
        # pod's NETWORK matters more than its GPU, and it was being filled at random.
        #
        # ⚠ WHY A PROBE AND NOT THE RUN LOGS. Two past runs sustained 36 and 52 Mbit/s, but the fleet
        # was GPU-busy 92% of the block: the aggregate only ever pushed as fast as the workers
        # consumed, so those figures are DEMAND, not capacity. A healthy run never saturates the link,
        # which is exactly why it has to be asked directly, before the clock.
        cand = [c for c in order if 9110 in portmap.get(c.cid, {})][:a.agg_candidates]
        if not cand:
            raise SystemExit("no card has a published 9110 — workers could never attach")
        best = (None, -1.0, {}, 0.0)
        cpu_note = {}
        if len(cand) > 1 and a.agg_candidates > 1:
            phase(f"PREPARING · measuring the link on {len(cand)} aggregate candidate(s)")
            for c in cand:
                probe = tip_runner.FleetRunner(
                    ssh, c, stage_dir=os.path.join(a.rundir, "stage"),
                    agg_port=9110, agg_dial=portmap[c.cid][9110][1])
                try:
                    mbit, per = probe.measure_egress(order, secs=a.agg_probe_secs)
                except Exception as exc:                       # noqa: BLE001
                    log(f"  {c.cid}: egress probe failed ({type(exc).__name__}) — not judged on it")
                    continue
                if mbit is None:
                    # ⚠ "could not test" is not "it failed" — the same rule the reachability gate uses.
                    log(f"  {c.cid}: egress UNTESTED (no streamer, or no worker answered)")
                    continue
                # #567: measure the CPU too. The executor runs HERE, serially, for 54% of a tip
                # block's wall — and until now the election never looked at it.
                cmodel, cscore, ccores = probe_cpu(ssh, c)
                cpu_note[c.cid] = (cmodel, cscore, ccores)
                _cpu = (f", cpu {cscore:.1f} ({(cmodel or '?')[:26]}, {ccores or '?'} cores)"
                        if cscore else ", cpu UNMEASURED")
                log(f"  {c.cid}: {mbit:.0f} Mbit/s to {len(per)} worker(s){_cpu}")
                # ⛔⛔ REACHABILITY FIRST, THROUGHPUT SECOND (hazync#573). Ranking on Mbit/s alone
                # elected a card that 9 of its own /24 could not reach, and the fleet went 30 -> 21
                # before a single block was proved. Measured 2026-09-28:
                #
                #     hz-smoke-1 : 33039 Mbit/s to 23 worker(s)   <- chosen, on speed alone
                #     hz-smoke-11: 13411 Mbit/s to 33 worker(s)   <- ten more cards
                #
                # A card that cannot reach the aggregate is worth NOTHING, and surplus bandwidth
                # above what the block needs is worth nothing either: a 9,600-segment block under
                # 600 s wants ~166 Mbit/s and every candidate that night was 50-200x that. So
                # throughput was never the binding constraint and reachability always was.
                #
                # ⚠ `per` is the probe's PER-WORKER result, so len(per) is how many workers this
                # candidate actually reached. It was measured and logged all along and simply
                # never entered the ranking.
                #
                # ⚠ --agg-min-mbit still rejects a genuinely slow candidate; this only decides
                # WHICH of the acceptable ones wins.
                # ⚠ CPU RANKS ONLY WHEN ASKED (--agg-prefer-cpu). Reachability stays first in both
                # cases: a fast core that half the fleet cannot reach is worth less than nothing, which
                # is what #573 cost us. With the flag, CPU comes second and throughput third, because
                # surplus bandwidth is measurably not the binding constraint (every candidate in hour 4
                # was 50-200x what a block needs) while the executor demonstrably is.
                _sc = cpu_note.get(c.cid, (None, None, None))[1] or 0.0
                key = (len(per), _sc, mbit) if a.agg_prefer_cpu else (len(per), mbit)
                bkey = ((len(best[2]), best[3], best[1]) if a.agg_prefer_cpu
                        else (len(best[2]), best[1]))
                if key > bkey:
                    best = (c, mbit, per, _sc)
        agg = best[0] or cand[0]
        if best[0] is not None:
            _m, _sc, _co = cpu_note.get(agg.cid, (None, None, None))
            _how = ("most REACHABLE first, then CPU, then fastest link" if a.agg_prefer_cpu
                    else "most REACHABLE first, then fastest link")
            log(f"aggregate: {agg.cid} reaches {len(best[2])} worker(s) at {best[1]:.0f} Mbit/s "
                f"(best of {len(cand)} candidate(s) — {_how})")
            # ⭐ SAY WHAT THE EXECUTOR IS ABOUT TO RUN ON. 54% of a tip block's wall happens on this
            # one CPU, and before #567 the frame and the log never named it, so execution could not be
            # attributed to anything afterwards.
            if _sc:
                _spread = [v[1] for v in cpu_note.values() if v[1]]
                _rng = (f"; candidates spanned {min(_spread):.1f}-{max(_spread):.1f}"
                        if len(_spread) > 1 else "")
                log(f"  executor will run on {(_m or 'an unnamed CPU')} — cpu score {_sc:.1f}, "
                    f"{_co or '?'} cores{_rng}")
                if not a.agg_prefer_cpu and len(_spread) > 1 and _sc < max(_spread):
                    log(f"  ⚠ a candidate had a FASTER core ({max(_spread):.1f} vs {_sc:.1f}) and was "
                        f"not preferred — execution is ~54% of a tip block (#567). "
                        f"--agg-prefer-cpu ranks on it.")
            else:
                log("  ⚠ the executor's CPU was not measured, so execution cannot be attributed to it")
            # ⛔ A FLOOR, BECAUSE THE BEST OF A BAD SET IS STILL BAD. The rate a block needs is
            # roughly segments x ~1.3 MB x 8 / target_seconds (1.1-1.3 MB/segment measured over two
            # runs), so a 9,600-segment block under 600 s wants ~166 Mbit/s. Default 0 = report only,
            # because the right floor depends on the block and the operator's target.
            if a.agg_min_mbit and best[1] < a.agg_min_mbit:
                raise SystemExit(
                    f"the best aggregate candidate sustains {best[1]:.0f} Mbit/s, below the "
                    f"--agg-min-mbit floor of {a.agg_min_mbit:.0f}. Every segment is pushed from this "
                    f"one card, so the fleet cannot outrun it. Rent more spares and try again")
        else:
            log(f"aggregate: {agg.cid} (link UNMEASURED — falling back to the first candidate)")
        agg_ports = portmap.get(agg.cid, {})
        if 9110 not in agg_ports:
            raise SystemExit("the aggregator has no published 9110 — workers could never attach")
        agg_dial = agg_ports[9110][1]
        # ⚠ The aggregate must lead `order`: assignment_preview and the surplus check both take the
        # head of the list as the aggregate, and a mismatch would arm the wrong card.
        order = [agg] + [c for c in order if c.cid != agg.cid]

        # ── ⛔ and cut the tail the aggregate has to feed (hazync#527) ───────────────────────────
        # The same probe that chose the aggregate already measured EVERY worker individually; only
        # the total was being used. `best[2]` is {cid: Mbit/s} from the winning candidate, so this
        # costs nothing extra and happens before the clock starts.
        w_drop, w_ranked = slow_worker_cut(
            # ⛔ THIS TRIMS THE FLEET DOWN TO `need` (`if len(order) <= need: return`), so the
            # FLOOR here means "cut everything above the floor". On 2026-09-27 that took a
            # 16-card fleet to 10 in seven seconds, each one reported as a card the aggregate
            # could not push to — a configuration fault wearing a network fault's clothes.
            order, best[2], need=a.cards, floor=a.worker_min_mbit)
        if w_ranked:
            shown = ", ".join(f"{cid} {'untested' if v is None else format(v, '.0f')}"
                              for cid, v in w_ranked[:6])
            log(f"worker links, slowest first: {shown}"
                + ("" if len(w_ranked) <= 6 else f", +{len(w_ranked) - 6} more"))
        if w_drop:
            order, created = _drop(order, created, w_drop,
                                   "the aggregate could not push to it fast enough")
        elif a.worker_min_mbit:
            log(f"worker gate: every worker clears {a.worker_min_mbit:.0f} Mbit/s")

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
            # ⛔ SAY IT GROUPED, BEFORE THE CLOCK (hazync#508). A bare count says how bad it is and
            # nothing about what to do. On 2026-09-24 the same numbers grouped by network block were
            # a different fact: reachability was predicted PERFECTLY by block, with the aggregate's
            # own block failing completely. That took a log-grep after the run to find; it is
            # knowable here, for free, from a result already in hand.
            if not ok:
                # ⚠ Card.ip, not portmap. portmap is keyed BY PORT NUMBER (ports[22] = (ip, port)),
                # so a .get("host") on it returns "" for every card and the grouping would put the
                # whole fleet in one bucket called "?" -- a report that cannot say anything.
                hosts = {c.cid: getattr(c, "ip", "") for c in order}
                by_block = tip_lifecycle.reachability_by_block(
                    {c.cid: v for c, v in reach.items()}, hosts)
                for blk, row in sorted(by_block.items()):
                    mark = "⛔" if row["ok"] == 0 else ("⚠" if row["bad"] else "  ")
                    log(f"  {mark} {blk:<16} reached {row['ok']:>2}, failed {row['bad']:>2}"
                        + ("   <- the aggregate's own block"
                           if blk == ".".join(str(getattr(agg, "ip", "")).split(".")[:3])
                           else ""))
                if tip_lifecycle.reachability_is_structural(by_block):
                    log("  ⛔ every block is all-reached or all-failed — this is the NETWORK, not "
                        "the pods. ⚠ Do NOT assume renting within one site fixes it: on the run "
                        "that produced this shape, the block that FAILED was the aggregate's own "
                        "(hazync#508).")
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
                    _release_pod(p, "it could not reach the aggregate")
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
        # ⛔ THE GATE IS FLEET-RELATIVE NOW (hazync#503). One card at 93 KB/s held 17 ready cards in
        # this gate for 18 minutes and cost ~$5 of a one-hour run. `a.min_cards` is the floor, so a
        # laggard is cut loose only while the survivors would still be enough to run.
        # ⛔⛔ AND THE AGGREGATE IS NEVER CUT LOOSE FOR BEING SLOW. Measured 2026-09-24 02:03, on this
        # gate's FIRST live outing: it dropped hz-smoke-1 for needing "~2 more min at 2424 KB/s" --
        # and hz-smoke-1 was the aggregate. Every worker's reachability had been tested against it,
        # so the surplus check below could not promote another card and killed the run 34 seconds
        # into a 30-card fleet.
        #
        # ⚠ A worker is fungible and a spare replaces it; the aggregate is the one card the run
        # cannot swap. Waiting two minutes for it is obviously cheaper than losing the fleet, and no
        # amount of surplus changes that -- which is why this is an identity, not a threshold.
        fleet_fetch = FetchFleet(len(order), a.min_cards, never_abandon={agg.cid})
        with _cf.ThreadPoolExecutor(max_workers=len(order)) as pool:
            got = list(pool.map(lambda c: (c, *fetch_binary(ssh, c, want, fleet=fleet_fetch)), order))
        for c, ok, n in got:
            # ⚠ Say WHY, not just "INCOMPLETE". A card cut loose for being slow and a card that
            # never started look identical in a byte count, and the difference is the whole point.
            why = fleet_fetch.reasons.get(c.cid)
            log(f"  {c.cid}: {'ok' if ok else 'INCOMPLETE'} {n}/{want} bytes"
                + (f" — dropped: {why}" if why else ""))
        bad = [c.cid for c, ok, _ in got if not ok]
        if bad:
            # ⛔ THE GATE THAT KILLED THE 2026-09-22 RUN. Its reasoning is right -- a card without the
            # prover spends the run fetching and the stall detector restarts it from zero -- but the
            # remedy was to end the run rather than to drop the card. With the spares still alive
            # there is now something to carry on with.
            order, created = _drop(order, created, bad,
                                   "the prover never finished downloading")

        # ── the GPU smoke, BEFORE the clock ───────────────────────────────────────────────────────
        # ⚠ THE PHASE LINE IS PUBLISHED, so it has to read correctly to someone who is not holding
        # the code. "proving one block on each of 29 GPUs" reads as though 29 blocks of real work are
        # under way — on a page whose headline is a block count sitting at 0, which makes the run look
        # like it is losing proofs. Nothing is being proved for the chain here: each card proves a
        # throwaway block so a GPU that cannot prove is found BEFORE the clock rather than during it.
        phase(f"PREPARING · checking all {len(order)} GPUs can prove (a throwaway block each)")
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
                _release_pod(p, "its GPU cannot prove")
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
        # ⛔ THE CHECK THAT KILLED FOUR FLEETS ON 2026-09-27. It compared against the TARGET, so a
        # run that asked for 30 and had 29 healthy gated cards exited and released them — four times
        # in an hour, each after paying for every gate above. The floor is what matters; the surplus
        # below trims to the target only when there IS a surplus.
        if len(order) < a.min_cards:
            # ⚠ len(created), not `rented`. `rented` is what we ASKED for (cards + spares); the
            # run routinely gets fewer when capacity is thin -- 18 of a requested 60 on 2026-09-23.
            # Reporting the request makes a healthy fleet look like a catastrophic shortfall.
            raise SystemExit(f"only {len(order)} of {len(created)} cards "
                             f"passed every pre-clock gate; "
                             f"need at least {a.min_cards} (target {a.cards}). Lower --min-cards, "
                             f"rent more spares, or read the per-card lines above")
        surplus = [c.cid for c in order[a.cards:]]
        # ⛔ SURPLUS IS NOT THE TEARDOWN'S BUSINESS, AND THE TEARDOWN'S GUARD DOES NOT REACH IT. An
        # adopted fleet exists precisely so it survives the run, but this path terminates through
        # _drop long before the finally ever sees it -- so `--adopt` without this check would quietly
        # destroy every card beyond --cards, which is the opposite of what adopting is for. Found by
        # reading the adopt path against this one, not by a failure: on the night it shipped the run
        # was launched with --cards set exactly to the fleet, so `surplus` was empty and the bug
        # could not fire.
        if surplus and a.adopt and not a.release_adopted:
            log(f"{len(surplus)} adopted card(s) are surplus to --cards {a.cards} and are being "
                f"LEFT RUNNING rather than released (adopted fleets outlive the run): "
                f"{sorted(surplus)}")
            order = order[:a.cards]
            surplus = []
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
        # ⭐ One po2 for the whole block, set by the weakest card (see resolve_seg_po2).
        seg_po2, _po2_why = resolve_seg_po2(
            a.seg_po2, created, locals().get("catalogue"), api=api)
        log(f"segment size: po2 {seg_po2} \u2014 {_po2_why}")
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
                       "HAZYNC_SEG_PO2": seg_po2,
                       **tip_lifecycle.lever_env(),
                       "HAZYNC_HOST_URL": HOST_URL, "HAZYNC_HOST_BYTES": str(want)})
        os.makedirs(runner.stage_dir, exist_ok=True)
        # ⛔ len(order), NOT a.cards. Cards can be dropped by the reachability gate, and indexing by
        # the requested count would either raise or silently prove a chunk count the fleet cannot
        # cover -- the chunk count IS the fleet size.
        # ⛔ MUTATE, DO NOT REBIND. `assignment` is created before the try block and is what the
        # teardown, the harvest and stop_auto_attach reach for. Rebinding is fine while the fleet is
        # fixed; once it can GROW, the teardown must see the cards that were added, so the same dict
        # has to be the one everybody holds.
        assignment.update(assignment_preview(order))

        def current_assignment():
            """The fleet as it stands NOW, rebuilt per block.

            ⛔ THE CHUNK COUNT IS THE FLEET SIZE, so this map cannot change while a block is in
            flight -- run_range is handed a SNAPSHOT and keeps it for the whole proof. Rebuilding
            between blocks is what lets a card recruited mid-session ever reach a proof: before
            this, `assignment` was built once before the loop and every block was proved with the
            fleet as it stood at that instant, so appending to `order` reached nothing at all.
            """
            assignment.clear()
            assignment.update(assignment_preview(order))
            return dict(assignment)
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
            def prove_and_submit(rng, *, from_tip=False, abort=None):
                """Fetch the bundle, prove it as a range, collect the receipt, submit. Returns the
                run dict. Shared by the single-block path and the session loop so there is exactly
                ONE definition of what proving a claimed block means."""
                bp = os.path.join(a.rundir, f"bundle_{rng}.json")
                if not os.path.exists(bp):
                    # ⛔ BOARD WORK ALWAYS COMES FROM THE API, NEVER FROM THE BRIDGE. This read
                    # `a.claim_source` for board heights, so a tip run started with
                    # --claim-source ssh asked the BRIDGE for a board block -- and the bridge emits
                    # nothing below EMIT_FROM, so it can never have one:
                    #
                    #   no bundle for 123538: .../tip_bundles/bundle_123538.json: No such file
                    #
                    # Three of those in a row tripped the fleet-fault guard and released a 36-card
                    # fleet 17 seconds into a session (2026-09-23). It also silently disabled the
                    # whole point of #367 -- filling the gaps between tip blocks with board work --
                    # because every board claim was unfetchable. A 15-card fleet then sat idle for
                    # 31.4 minutes of a 60-minute session, $5.81 of rented GPU doing nothing.
                    #
                    # --claim-source governs where a TIP bundle comes from. The board has exactly
                    # one source, /api/witness, and it does not depend on that flag.
                    src = "ssh" if from_tip else "api"
                    if src == "ssh":
                        bok, bwhy = tip_board.fetch_bundle_ssh(int(rng), bp, a.bridge_host)
                    else:
                        bok, bwhy = tip_board.fetch_bundle(int(rng), bp)
                    if not bok:
                        raise RuntimeError(f"no bundle for {rng}: {bwhy}")

                # ⭐ IS THIS THE NEXT LINK? (hazync#556) Checked AFTER the bundle is on disk and
                # BEFORE a single GPU touches it, so a gap costs one file read rather than a block
                # of fleet time. Only tip work: board blocks are deliberately out of order.
                #
                # ⛔ A DECLARED JUMP IS NOT A GAP TO REFUSE. If --tip-max-behind made the selector
                # skip ahead, the parent is not the block we proved and the check would refuse work
                # the operator asked for. The link is RESET instead, and the skip is already in the
                # ledger from pick_tip -- so the hole is recorded once, in one place, either way.
                if from_tip:
                    try:
                        with open(bp) as _fh:
                            _doc = json.load(_fh)
                    except (OSError, ValueError) as _exc:
                        raise RuntimeError(f"bundle for {rng} is unreadable: {_exc}")
                    _prev = None if int(rng) in jumped else proved_tip.get("header")
                    v = tip_chain.verdict(_doc, expect_height=int(rng), prev_header=_prev)
                    tip_note("link", height=int(rng), ok=bool(v["ok"]), linked=bool(v["linked"]),
                             parent=v["parent"], why=v["why"])
                    if not v["ok"]:
                        if a.allow_tip_gaps:
                            log(f"  ⚠ CHAIN CHECK FAILED and --allow-tip-gaps is set, proving "
                                f"anyway: {v['why']}")
                        else:
                            raise RuntimeError(f"refusing to prove {rng}: {v['why']}")
                    log("  chain: " + ("LINKED — " if v["linked"] else "unlinked — ") + v["why"])
                    # Held until the proof succeeds; a block we failed to prove must not become the
                    # parent the next one is checked against.
                    staged_header[int(rng)] = tip_chain.bundle_header(_doc)
                hi_l = {"n": 0}

                def _b(progress):
                    hi_l["n"] = tip_board.beat(rng, progress, hi_l["n"], ident=ident)
                    return hi_l["n"]

                res = tip_run.run_range(height=int(rng), cards=current_assignment(), runner=runner,
                                        bundle_path=bp, now=time.time, sleep=time.sleep, feed=feed,
                                        on_event=lambda m: log(f"  {m}"), beat=_b,
                                        max_ticks=1200, tick_s=6.0, abort=abort)
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
                                          fleet_ids=[p["id"] for p in created],
                                          armed=not a.clock_from_tip,
                                          arm_deadline_s=(a.clock_wait_max * 3600.0
                                                          if a.clock_from_tip else None))
            tip_session.save(spath, state)
            # ⭐ --fresh-tip: prove only blocks mined AFTER the fleet was ready.
            #
            # `proved_tip["h"]` is the floor for claiming a tip block, and starting it at 0 means the
            # session grabs whatever bundle the bridge already holds -- a block that may have been
            # mined minutes before we booted. Proving that measures our speed against a head start,
            # not against the chain, and it is the wrong number to publish.
            #
            # With --fresh-tip the floor starts at the CURRENT tip, so the session sits idle until
            # the chain produces a block it has never seen, then proves that. The measurement then
            # runs from "this block did not exist" to "its proof is accepted", which is the only
            # figure that supports a claim about following the tip. It also prices boot separately:
            # renting and gating happen while waiting, so they cannot hide inside a block's time.
            # `header` is the 80 bytes of the last tip block we proved — the thing the NEXT bundle
            # must name as its parent. None until we have proved one, which is why the first tip
            # block of a session is reported as unlinked rather than as verified (hazync#556).
            proved_tip = {"h": 0, "header": None}
            # ⛔ AN EXPLICIT FLOOR BEATS THE FRESH ONE. --fresh-tip sets the floor at the current tip,
            # so a height that was skipped earlier is below it for ever and no later run can reach
            # back for it. 968,984 and 968,986 are in exactly that position after 2026-09-28.
            if a.tip_from:
                proved_tip["h"] = int(a.tip_from) - 1
                log(f"TIP FROM: floor set at {a.tip_from - 1} — the next tip block proved will be "
                    f"{a.tip_from}, to close a known gap"
                    + (" (overriding --fresh-tip)" if a.fresh_tip else ""))
            elif a.fresh_tip:
                t0h = tip_board.highest_tip_bundle(a.bridge_host)
                if t0h:
                    proved_tip["h"] = int(t0h)
                    log(f"FRESH TIP: floor set at {t0h} — waiting for the chain to mine a block "
                        f"this fleet has never seen (boot cost is paid while waiting)")
                else:
                    log("⚠ --fresh-tip: no tip bundle on the bridge yet, so the floor stays at 0")

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

            # ⭐ THE TIP LEDGER — what turns "we followed the tip" into a measurement.
            #
            # session.log records when WE started and finished a block. It has never recorded what
            # the CHAIN was doing at those moments, so the central claim of a tip run could only ever
            # be asserted. One line per event, appended as it happens, so it survives a crash and can
            # be read by anyone: when a height first became provable, when we claimed it, when its
            # proof was accepted, and where the chain had got to by then.
            #
            # `appeared` is when the BUNDLE first existed, which is the earliest moment this fleet
            # could have begun -- not the block's header timestamp, which a miner sets and which can
            # run backwards. The lag that matters is measured from the former.
            tipledger = os.path.join(a.rundir, "tip_ledger.jsonl")
            seen_at = {}
            # ⚠ Noted ONCE each. work_fn runs every loop, so an unguarded skip line would fill the
            # ledger with the same gap and make it unreadable as evidence.
            gap_noted = set()
            # Heights the selector reached by JUMPING. The chain check refuses an UNEXPECTED gap;
            # a jump the operator's own --tip-max-behind asked for is declared, so it resets the
            # link rather than failing it. Without this the two features would contradict.
            jumped = set()
            staged_header = {}

            def tip_note(event, **kv):
                rec = {"event": event, "t": round(time.time(), 3),
                       "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kv}
                try:
                    with open(tipledger, "a") as fh:
                        fh.write(json.dumps(rec) + "\n")
                except OSError:
                    pass          # a ledger that cannot be written must never stop a run

            def pick_tip():
                """The next tip height to prove, IN SEQUENCE, and what taking it skips (hazync#556).

                ⛔ THE LOWEST UNPROVED, NOT THE HIGHEST. `highest_tip_bundle` was the selector, so two
                blocks mined seconds apart meant the lower one was skipped for good: 968,984 and
                968,985 both appeared at 13:24:59 on 2026-09-28 and only 985 was ever proved.

                ⛔ AND `appeared` IS WRITTEN FOR EVERY BUNDLE, NOT JUST THE MAXIMUM. The old loop
                noted only the height it selected, so 968,984 has no `appeared` line in the tip
                ledger at all -- the file that exists to be the audit trail read as complete and
                gapless while a block went by unproven. A fix that leaves the evidence blind is not
                a fix.

                Returns (chosen, skipped, newest).
                """
                hs = tip_board.tip_bundles_above(a.bridge_host, proved_tip["h"])
                now_t = time.time()
                for h in hs:
                    if h not in seen_at:
                        seen_at[h] = now_t
                        tip_note("appeared", height=int(h))
                if not hs:
                    return None, [], None
                newest = hs[-1]
                # ⚠ The policy is a pure function in tip_board so it can be tested without a bridge,
                # an ssh hop or a fleet. This closure only does the bookkeeping around it.
                chosen, skipped = tip_board.choose_tip(hs, a.tip_max_behind)
                for h in skipped:
                    if h not in gap_noted:
                        gap_noted.add(h)
                        tip_note("skipped", height=int(h), chose=int(chosen),
                                 why=f"{len(hs)} tip bundles were waiting, above "
                                     f"--tip-max-behind {a.tip_max_behind}")
                        log(f"  ⛔ SKIPPING tip block {h} — {len(hs)} bundles waiting, jumping "
                            f"to {chosen}. This leaves a hole in the proof chain.")
                return chosen, skipped, newest

            def work_fn():
                # ⛔ A TIP BLOCK IS "WAITING" ONLY IF ITS BUNDLE EXISTS. Asking the node for its height
                # would report a tip the fleet cannot prove: nothing is emitted below EMIT_FROM.
                pending, skipped, t = pick_tip()
                if skipped:
                    jumped.update(int(x) for x in [pending])
                # ⛔ --fresh-tip MEANS TIP ONLY. Without this, next_work() falls through to BOARD
                # work whenever no fresh tip is waiting -- and a driver configured with
                # --claim-source ssh fetches from the bridge's tip_bundles, which holds nothing
                # below EMIT_FROM. So it claimed board heights 123538/123541/123544, each failed
                # instantly with "no bundle", and three in a row tripped the fleet-fault guard:
                #
                #   stopping: 3 blocks failed in a row — this is the FLEET, not the blocks.
                #
                # It released a 36-card fleet 17 seconds into a session, and the guard was right to
                # fire on what it could see -- three consecutive failures DO usually mean the fleet.
                # The fault was asking it to prove blocks this driver can never fetch.
                #
                # ⛔ THE REFUSAL ABOVE IS GONE, AND IT WAS ALWAYS A WORKAROUND (hazync#506). It read:
                #
                #     if a.fresh_tip and pending is None: return idle
                #
                # which kept a --fresh-tip run from ever touching the board. That was the right
                # emergency measure when every board claim was unfetchable, and it is the wrong
                # steady state: a 15-card fleet sat idle for 31.4 minutes of a 60-minute session,
                # $5.81 of rented GPU doing nothing, while the board had work waiting. With the
                # fetch fixed (board work now always comes from /api/witness) the gaps between tip
                # blocks are the board's, which is what #367 intended all along.
                #
                # ⚠ --fresh-tip still means what it says about the TIP: `pending` is only ever a
                # height that did not exist at boot. Board work does not weaken that, because the
                # tip always wins -- see the `abort` in prove_one, which abandons a board block the
                # moment a tip bundle lands.
                # ⚠ SAY WHAT IS ACTUALLY BEING WAITED FOR (hazync#505). Both idles used to print
                # "the board has nothing free right now" -- a claim about a request this path never
                # makes. During the 2026-09-23 flagship a reader watching live took twelve of those
                # in a row to mean the run had proved one block and given up; it was waiting for the
                # chain to mine 968,316, which it then proved. Naming the tip and the floor makes
                # the wait self-explanatory and roughly self-timing.
                if a.fresh_tip and pending is None and a.no_board_fill:
                    return {"source": "idle", "range": None,
                            "why": f"waiting for the chain — bridge tip {t or '?'}, "
                                   f"floor {proved_tip['h']} (--no-board-fill: the gap is not "
                                   f"filled with board work)"}
                # ⛔ #585: hand next_work a way to ASK AGAIN just before it claims board work. The
                # `pending` above was read at the top of this tick; a bundle landing in between used
                # to lose the fleet for a whole board block (191 s, measured 2026-09-29).
                # ⚠ Re-reads the bridge, which costs one ssh — but only on the path where board work
                # is about to be claimed, and only when no tip was already pending. `pick_tip`'s
                # `seen_at` guard makes a second call idempotent: a height already noted is not
                # re-noted, so the tip ledger does not gain duplicate `appeared` lines.
                out = tip_controller.next_work(pending, claim_fn,
                                               recheck_tip_fn=lambda: pick_tip()[0])
                # ⭐ WHEN THE BUNDLE APPEARED, so the session's window can start there rather than
                # at the moment this loop reached it. `seen_at` is stamped by pick_tip the first time
                # a height is seen on the bridge, which is the earliest instant this fleet could have
                # begun proving it. Only meaningful for tip work; board work never arms the clock.
                if out.get("source") == "tip" and out.get("range") is not None:
                    try:
                        out["appeared_at"] = seen_at.get(int(out["range"]))
                    except (TypeError, ValueError):
                        out["appeared_at"] = None
                if out.get("source") == "idle" and a.fresh_tip and pending is None:
                    # Both are true here and only one of them is the interesting one.
                    out["why"] = (f"waiting for the chain — bridge tip {t or '?'}, "
                                  f"floor {proved_tip['h']}; the board has nothing free either")
                return out

            def tip_waiting():
                """A tip bundle above what we have proved, or None. The board block's abort signal.

                ⛔ COMPLETE BUNDLES ONLY, which is why highest_tip_bundle had to be fixed first: its
                filter counted `bundle_<h>.json.tmp` -- the file the bridge is still writing -- as an
                available tip. Abandoning a board block for a bundle that does not exist yet would
                throw away real work and then fail to fetch the thing it was thrown away for.

                ⚠ THE SAME SELECTOR AS work_fn, ON PURPOSE (hazync#556). If this said "a tip is
                waiting" about the HIGHEST bundle while work_fn then proved the LOWEST, the board
                block would be abandoned for one height and the fleet would start another -- and the
                log would name the wrong block as the reason. One picker, one answer.
                """
                try:
                    chosen, _skipped, _newest = pick_tip()
                except Exception:
                    return None                      # ⚠ never abort a healthy block on an ssh blip
                if chosen:
                    return f"tip block {chosen} is waiting and the tip comes first"
                return None

            def prove_one(rng):
                # ⚠ A tip height is simply one at or above EMIT_FROM: the bridge emits nothing below
                # it, so a bundle can only come off the bridge host up there. Board work comes from
                # the frontier (~113,537) and is fetched from /api/witness. No cleverness needed.
                from_tip = str(rng).isdigit() and int(rng) >= TIP_FROM
                # ⛔ ONLY BOARD WORK IS PREEMPTIBLE. A tip block is the thing everything else gives
                # way to; making it interruptible would mean a later tip could abandon an earlier
                # one mid-proof, and the fleet would chase the chain without ever finishing a block.
                try:
                    out = prove_and_submit(rng, from_tip=from_tip,
                                           abort=(None if from_tip else tip_waiting))
                except tip_run.RunAborted as exc:
                    # ⚠ Returned, not raised: the session distinguishes "aborted" from "failed", and
                    # a raise here would be caught by its generic handler and counted as a failure.
                    return {"ok": False, "aborted": True, "block": str(rng),
                            "why": exc.why, "wall_s": exc.elapsed_s}
                if from_tip:
                    proved_tip["h"] = int(rng)
                    # ⛔ ONLY NOW. The parent for the next check is the last block we actually
                    # PROVED — promoting it at fetch time would let a failed block become the link
                    # the next one is measured against, and the chain would verify against
                    # something that was never proved.
                    proved_tip["header"] = staged_header.pop(int(rng), None)
                    staged_header.clear()
                    # chain tip NOW, so the lag is against the real chain rather than our own clock
                    try:
                        tip_now = tip_board.highest_tip_bundle(a.bridge_host)
                    except Exception:
                        tip_now = None
                    app = seen_at.get(int(rng))
                    tip_note("accepted", height=int(rng),
                             appeared_at=round(app, 3) if app else None,
                             lag_s=round(time.time() - app, 1) if app else None,
                             chain_tip_now=int(tip_now) if tip_now else None,
                             blocks_behind=(int(tip_now) - int(rng)) if tip_now else None)
                return out

            # The fleet's hourly rate, from what RunPod actually charged for the cards we KEPT.
            # ⛔ THE RATE IS NOT A CONSTANT ONCE THE FLEET CAN GROW. spend_fn closed over a number
            # computed here, so every card recruited mid-session billed invisibly: the budget cap
            # would be compared against a figure that stopped counting the moment the fleet changed.
            def rate_now():
                names = {c.cid for c in order}
                return sum(p["price"] for p in created if p["name"] in names)

            live = {c.cid for c in order}
            rate_hr = rate_now()

            # ⛔ ELAPSED TIME, NOT BLOCK TIME. A pod bills from the moment it exists; claiming,
            # fetching a bundle, submitting and waiting on a busy board are all billed and none of
            # them is inside a block's wall_s. Charging block time undercounted by 17% over 42
            # blocks. This returns what has accrued since the last call, so the rate in force at
            # the time is the rate applied — which is what makes it survive a fleet resize.
            billed_to = {"t": time.time()}

            def spend_fn():
                now_t = time.time()
                delta, billed_to["t"] = now_t - billed_to["t"], now_t
                # ⚠ Charged at the rate the fleet is on NOW, over the interval since the last
                # charge. The loop charges every pass, so a fleet that grew part-way through an
                # interval is out by at most that interval.
                return rate_now() * (delta / 3600.0)

            # ⛔ MAX, NOT MEDIAN. Measured over 18 blocks: median 30.0 s, max 226.7 s. A
            # median-based guard let a claim be taken with 30 s left that then ran for nearly four
            # minutes; the block finished but the claim for the NEXT one was taken and orphaned.
            # Max never overruns; it costs at most one slow block's worth of idle at the tail.
            def estimate_s():
                seen = [b.get("wall_s") for b in state["blocks"].values()
                        if b.get("ok") and b.get("wall_s")]
                return max(seen) if seen else DEFAULT_BLOCK_EST_S

            # ── growing the fleet while it runs (the supply half of hazync#504/#506) ─────────────
            recruited_path = os.path.join(a.rundir, "recruited.json")

            def _recruit_rent():
                """⛔ RECORD IT THE INSTANT IT EXISTS, in a file of its own. rented.json is written
                by the main thread; a second writer would race it and the loser is a pod nobody can
                release. There is no budget cap by decision, so an unrecorded pod bills until an
                invoice says so."""
                pod = api.deploy_listening(f"hz-grow-{len(recruited) + 1}", pub, gpu_types=gpu_types)
                if pod:
                    recruited.append(pod)
                    with open(recruited_path, "w") as fh:
                        json.dump(recruited, fh, indent=1)
                return pod

            def _recruit_gate(pod):
                """THE SAME GATES THE RUN APPLIES TO ITSELF, on one card, off the clock.

                ⛔ NOT A REDUCED SET. Every one of these caught a real card on 2026-09-27:
                hz-smoke-8 never answered ssh, hz-smoke-21's GPU could not prove, three more failed
                reachability. Admitting a card that skipped them would put exactly those failures
                inside a block instead of before it, where they cost the whole fleet's wall clock.
                """
                # ⛔ wait_for_ssh RETURNS A DICT KEYED BY POD NAME, not a list. `cs[0]` raised
                # KeyError: 0 on the first recruit this feature ever gated on real hardware
                # (2026-09-28), so every recruit was rented, failed instantly and released — the
                # feature could never have added a card. The main path has always read it as a dict
                # (`order = [cards[n] for n in sorted(cards)]`); this was the one caller that did not.
                cs, _pm = wait_for_ssh(api, [pod], ssh, need=1)
                if not cs:
                    return False, None, "it never answered ssh"
                c = next(iter(cs.values()))
                if not prepare(ssh, c, block_path=(None if a.claim else a.block_path),
                               block_name=block_name, repo_hint=a.repo):
                    return False, None, "it could not be staged"
                fok, n = fetch_binary(ssh, c, want, fleet=None)
                if not fok:
                    return False, None, f"the prover never finished downloading ({n} bytes)"
                gok, detail = gpu_smoke(ssh, c)
                if gok is None:
                    return False, None, f"its GPU could not be judged ({detail})"
                if not gok:
                    return False, None, f"its GPU cannot prove ({detail})"
                reach = probe_runner.check_reachability({0: c}, secs=15)
                if reach is not None and not all(reach.values()):
                    return False, None, "it cannot reach the aggregate"
                return True, c, ""

            if a.grow_to and a.grow_to > len(order):
                # ⭐ THE TARGET LIVES IN A FILE, SO IT CAN BE CHANGED MID-RUN (hazync#548). During
                # tip hour 3 the fleet sat at 18 cards while missing a 600 s gate and the target
                # could not be raised without restarting -- which would have cost the clock already
                # spent and landed on the same capacity. A file needs no port, no signal and no
                # protocol, survives a driver restart, and leaves an audit trail of what was asked.
                grow_path = os.path.join(a.rundir, "grow_to")
                try:
                    with open(grow_path, "w") as fh:
                        fh.write(f"{a.grow_to}\n")
                except OSError as exc:
                    log(f"  ⚠ could not write {grow_path} ({exc}); the target is fixed at "
                        f"{a.grow_to} for this run")

                def _read_grow_to():
                    """The live target. ⚠ A missing or unreadable file keeps the current one."""
                    try:
                        with open(grow_path) as fh:
                            txt = fh.read().strip()
                    except OSError:
                        return None
                    if not txt.isdigit():
                        return None
                    return int(txt)

                recruiter = tip_recruit.Recruiter(
                    target=a.grow_to, have_fn=lambda: len(order), rent_fn=_recruit_rent,
                    gate_fn=_recruit_gate,
                    release_fn=lambda pod: sponsor_bot.terminate_confirmed(api, pod["id"]),
                    log=lambda m: log(f"  {m}"), poll_s=45.0,
                    max_parallel=a.gate_parallel, target_fn=_read_grow_to).start()
                log(f"  growing toward {a.grow_to} cards as capacity appears, gating up to "
                    f"{a.gate_parallel} at once (recruits join at a block boundary)")
                log(f"  ⭐ raise or lower it WITHOUT restarting:  echo 32 > {grow_path}")

            def grow_fn():
                """Admit finished recruits. Called BETWEEN blocks only; never rents or waits."""
                if recruiter is None:
                    return None
                joined = recruiter.drain()
                if not joined:
                    return None
                for item in joined:
                    order.append(item["card"])
                    created.append(item["pod"])
                    # ⛔ `fleet` IS DERIVED FROM `created`, ONCE, BEFORE THE LOOP — the same shape of
                    # bug as `assignment`. Appending to `created` alone left feed_records with a card
                    # it had no price or GPU name for, and its guard refused (rightly: defaulting the
                    # price to 0 under-reports a run with no budget cap, and dropping the card hides
                    # a pod that is proving and being billed). Measured 2026-09-28: the fleet grew to
                    # 3 and proved a block on 3 cards while the dashboard still showed 2.
                    # EXTEND, do not rebind — the list is closed over by this function.
                    pod = item["pod"]
                    fleet.append({"id": pod["name"], "price": pod["price"],
                                  "gpu": pod["gpu_type"], "pod_id": pod["id"]})
                    # ⛔ AND THE SESSION'S OWN RECORD OF ITS FLEET. resume_verdict compares
                    # state["fleet"] against the pods that are actually alive: a recruit missing from
                    # it shows up as `extra` on every resume, and if the ORIGINAL cards die while the
                    # recruits live, `recorded & live` is empty and the session refuses to resume —
                    # "this is a new fleet, not a resume" — about the fleet it grew itself.
                    # tip_economics also sizes a run by len(state["fleet"]).
                    state["fleet"] = sorted(set(state.get("fleet") or ()) | {str(pod["id"])})
                # ⛔ AND THE RENTAL RECORD ON DISK (hazync#557). This is the FOURTH structure derived
                # from the fleet and written once before the session loop -- `assignment` (#540),
                # `fleet` and `state["fleet"]` (#547), and now this. A clean teardown hid it, because
                # the release loop walks `created` and then sweeps `recruited`.
                #
                # It bites on the RECOVERY path. `--cleanup --rundir` is what releases the fleet when
                # a driver is SIGKILLed or the box reboots, and it reads ONLY this file. Measured
                # 2026-09-28: rented.json held 17 pods while the fleet had grown to 26, so cleanup
                # would have released 17, reported success, and left nine RTX PRO 6000 billing at
                # ~$18.81/hr. There is no budget cap by decision, so nothing else would have stopped
                # them.
                try:
                    with open(rented_path, "w") as fh:
                        json.dump(created, fh, indent=1)
                except OSError as exc:
                    log(f"  ⛔ COULD NOT UPDATE {rented_path} ({exc}) — if this driver dies, "
                        f"`--cleanup` will not know about the {len(joined)} card(s) just added. "
                        f"Release them by hand from recruited.json.")
                try:
                    feed.start(tip_dashboard.feed_records(order, fleet)["records"])
                except Exception as exc:                       # noqa: BLE001
                    log(f"  ⚠ could not rewrite the feed after growing: {exc}")
                tip_session.save(spath, state)   # the record must survive the driver, as ever
                names = ", ".join(i["pod"].get("name", "?") for i in joined)
                return (f"fleet grew by {len(joined)} to {len(order)} cards ({names}) — "
                        f"they join from the next block")

            phase(f"SESSION · {a.session:.1f} h on {len(order)} cards"
                  + (f", budget ${a.budget_usd:.2f}" if a.budget_usd else ""))
            if a.clock_from_tip:
                log(f"  ⏱ the clock has NOT started: the {a.session:.1f}-hour window begins at the "
                    f"first tip block. Board fill until then is billed but not counted against it "
                    f"(giving up after {a.clock_wait_max:.1f} h of waiting)")
            log(f"  fleet rate ${rate_hr:.3f}/hr across {len(live)} card(s)")
            summ = tip_session.run_session(state=state, path=spath, prove=prove_one,
                                           work_fn=work_fn, now=time.time, sleep=time.sleep,
                                           feed=feed, on_event=lambda m: log(f"  {m}"),
                                           idle_s=30.0, block_estimate_s=estimate_s,
                                           budget_usd=(a.budget_usd or None), spend_fn=spend_fn,
                                           grow_fn=grow_fn)
            # ⛔ A GAP MUST BE IN THE SUMMARY, NOT ONLY IN THE LEDGER (hazync#556). The 2026-09-28
            # skip was absent from the session summary, the dashboard grid, the run log AND the tip
            # ledger — four artifacts, no trace. Whatever else this run reports, it reports its holes.
            summ["tip_blocks_skipped"] = sorted(gap_noted)
            summ["tip_chain_gapless"] = not gap_noted
            log("SESSION " + json.dumps(summ, indent=1))
            if gap_noted:
                log(f"⛔ THE PROOF CHAIN HAS {len(gap_noted)} HOLE(S): {sorted(gap_noted)} — these "
                    f"heights had bundles and were never proved. Close them with "
                    f"--tip-from <height>.")
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
        # ⛔⛔ STOP THE THING THAT SPENDS MONEY FIRST. recruiter.stop() used to sit AFTER the harvest,
        # which pulls logs from every card and takes minutes. Measured 2026-09-28: the teardown began
        # at 11:21:58 and the recruiter rented a NEW pod at 11:23:25 — 87 seconds into shutdown — and
        # gated another in between. The fleet GREW while it was being torn down, and SIGTERM did not
        # help because the recruiter is a separate thread; the run had to be SIGKILLed and the pods
        # released by hand through the API.
        try:
            if recruiter is not None:
                recruiter.stop()
                _rc = recruiter.counts()
                log(f"recruiter stopped first: rented {_rc['rented']}, rejected {_rc['rejected']}")
        except Exception as e:                                     # noqa: BLE001
            log(f"recruiter.stop: {e}")

        try:
            if assignment and runner is not None:
                runner.stop_auto_attach(assignment)
        except Exception as e:                                     # noqa: BLE001
            log(f"stop_auto_attach: {e}")

        # ⭐ BILLED TIME, NOT rate x wall. Every cost figure this run reports is otherwise
        # COMPUTED: the fleet rate multiplied by our own wall clock. That is close, but it is not
        # what RunPod charges, and a published cost should be the billed one. Ask the API for each
        # pod's real lifetime BEFORE it is released -- afterwards the pod is gone and so is the
        # answer. Same discipline as the harvest below, for the same reason.
        try:
            billed = []
            for p_ in created:
                q = ('query { pod(input:{podId:%s}) { id name costPerHr '
                     'runtime { uptimeInSeconds } } }' % api._s(p_["id"]))
                try:
                    pd = (api._gql(q) or {}).get("pod") or {}
                    up = (pd.get("runtime") or {}).get("uptimeInSeconds")
                    billed.append({"name": p_["name"], "id": p_["id"], "dc": p_.get("dc"),
                                   "gpu": p_["gpu_type"], "price_hr": p_["price"],
                                   "uptime_s": up,
                                   "usd": round((up or 0) / 3600.0 * p_["price"], 4)})
                except Exception as e:
                    billed.append({"name": p_["name"], "id": p_["id"], "error": str(e)[:80]})
            with open(os.path.join(a.rundir, "billing.json"), "w") as fh:
                json.dump(billed, fh, indent=1)
            tot = sum(b.get("usd") or 0 for b in billed)
            known = sum(1 for b in billed if b.get("uptime_s"))
            log(f"BILLED: ${tot:.2f} across {known}/{len(billed)} pod(s) with a known uptime "
                f"-> {a.rundir}/billing.json")
        except Exception as e:
            log(f"⚠ billing snapshot failed ({str(e)[:70]}) — cost falls back to rate x wall")

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

        # ⛔ A RECRUIT CAUGHT MID-GATE IS IN NEITHER LIST. `created` holds only cards the session
        # ADMITTED; the recruiter's own queue holds only ones that FINISHED gating. A pod rented
        # thirty seconds before the session ended, still downloading the prover, is in neither — and
        # there is no budget cap by decision, so it would bill until an invoice said so. Every pod
        # the recruiter ever rented is in recruited.json for exactly this moment.
        try:
            # (the recruiter was already stopped at the top of the teardown)
            admitted_ids = {p["id"] for p in created}
            orphans = [p for p in recruited if p["id"] not in admitted_ids]
            if orphans:
                log(f"releasing {len(orphans)} recruit(s) that never joined the fleet")
                for p_ in orphans:
                    ok_ = sponsor_bot.terminate_confirmed(api, p_["id"])
                    log(f"  {p_.get('name')}: {'gone' if ok_ else '⛔ STILL LISTED — RELEASE BY HAND'}")
        except Exception as exc:       # noqa: BLE001 -- cleanup bookkeeping must not mask a failure
            log(f"⛔ could not finish releasing recruits: {type(exc).__name__}: {exc}")

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
        # ⛔ AN ADOPTED FLEET IS NOT THIS RUN'S TO DESTROY, BY DEFAULT. The point of adopting is to
        # outlive the session that rented it, so releasing here would defeat the feature entirely.
        # But "left running" must never be quiet: the cards bill whether or not anyone is watching,
        # so the cost and the exact release command go in the log where the run ends.
        if a.keep or (a.adopt and not a.release_adopted):
            why = "--keep" if a.keep else "--adopt (use --release-adopted to hand them back)"
            rate = sum(float(p.get("price") or 0) for p in created)
            log(f"{why}: {len(created)} pod(s) LEFT RUNNING and STILL BILLING at "
                f"${rate:.2f}/hr (${rate * 24:.2f}/day)")
            log("  release them with:  python3 tip_smoke.py --cleanup --rundir " + a.rundir)
            for p in created:
                log(f"    {p['name']}  {p['id']}  ${float(p.get('price') or 0):.3f}/hr")
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
