#!/usr/bin/env python3
"""The concrete fleet runner: what `tip_run` calls to act on real cards (Phase 5 ⑧).

`tip_run` defines the sequence, `tip_fleet` the decisions, `tip_driver` the ssh primitives. This is the
object that joins them — every method here is a phase of `tools/milestone/run_continuous.sh`, kept in
the same order and with the same refusals.

⛔ WHAT THIS DELIBERATELY DOES NOT DO: decide anything. If a method here grows an `if` about whether a
card is healthy, that logic belongs in `tip_fleet` where it can be tested without a fleet. This layer
does, reports, and refuses — it never judges.
"""

import base64
import concurrent.futures
import os
import posixpath
import shlex
import time

import tip_driver
import tip_lifecycle


# How many cards to touch at once. 23 sequential ssh calls made phase 0 the longest part of a run.
FANOUT = int(os.environ.get("HAZYNC_TIP_FANOUT", "32"))


REMOTE_CLEAR = """\
# Everything a previous run could have left that would make this one lie. A stale chunk_N.bin makes a
# pod report DONE in seconds and its receipt is collected as though it were this block's.
rm -rf /workspace/re* 2>/dev/null
rm -f /workspace/chunk_*.bin /workspace/chunk_*.hzk /workspace/prove.log /workspace/run.log 2>/dev/null
rm -f /workspace/aggw.log /workspace/aa.log /workspace/screen.bin 2>/dev/null
LEFT=$(ls /workspace/chunk_*.bin /workspace/chunk_*.hzk 2>/dev/null | wc -l)
REDIRS=$(ls -d /workspace/re* 2>/dev/null | wc -l)
echo "LEFT:$LEFT REDIRS:$REDIRS"
"""


class FleetRunner:
    """Drives one fleet for one block.

    `aggregator` is the card that runs `seg-serve`. In the 966,256 runs it was one of the fleet; it can
    equally be a separate box. `stage_dir` is where receipts land on the orchestrator on their way to
    the aggregator.
    """

    def __init__(self, ssh, aggregator, *, stage_dir, agg_port=9110, agg_dial=None,
                 prove_env=None, workdir="/workspace"):
        self.ssh, self.agg = ssh, aggregator
        # ⛔ THE PORT seg-serve BINDS IS NOT ALWAYS THE PORT WORKERS DIAL. On RunPod the container's
        # 9110 is published on some arbitrary external port (58231, 27427 …), so an aggregator that
        # binds 9110 is reached at publicIp:58231. Using one number for both means every worker dials a
        # closed port, sits in its 600-second retry loop looking armed, and contributes nothing — the
        # silent no-op the reachability gate exists to catch. `agg_dial` defaults to `agg_port` for the
        # simple case where they genuinely are the same.
        self.stage_dir, self.agg_port, self.workdir = stage_dir, int(agg_port), workdir
        self.agg_dial = int(agg_dial if agg_dial is not None else agg_port)
        # Set by `start_aggregate`. None means the aggregate was never launched, and `tip_harvest`
        # reports "NOT MEASURED" on that rather than measuring worker epochs against nothing.
        self.agg_started_ms = None
        # The settings every 966,256 run used. LIFTX_HINT is not optional: without it the host omits the
        # pubkey hints the CORE guest expects and the guest dies with `DeserializeUnexpectedEnd`, which
        # reads as a corrupt fixture and sends you hunting the wrong thing.
        self.prove_env = prove_env or {
            "HAZYNC_LIFTX_HINT": "1",
            "HAZYNC_FIELD_BIGINT2": "1",
            "HAZYNC_ECMULT_WINDOW": "21",
        }
        os.makedirs(stage_dir, exist_ok=True)

    # ── phase 0 ────────────────────────────────────────────────────────────────────────────────────
    def clear_and_check(self, cards):
        """Wipe every pod and report what each one says it has left. None means we could not ask.

        ⛔ IN PARALLEL. This was a serial loop, and at 23 cards that is 23 sequential ssh round-trips
        before the clock even starts — minutes of dead time, and one slow pod holds up every other.
        run_continuous.sh fans this out with `&`; measured 2026-09-20, the serial version made phase 0
        and phase 1 the longest part of a 23-card run.
        """
        out = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=FANOUT) as pool:
            futs = {pool.submit(self.ssh.run, card, REMOTE_CLEAR): card for card in cards.values()}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    out[futs[fut]] = _last_line(fut.result())
                except Exception:
                    out[futs[fut]] = None
        return out

    # ── phase 1 ────────────────────────────────────────────────────────────────────────────────────
    def launch_all(self, cards, *, block, chunks):
        """Launch every chunk at once. Serial here means the last card starts minutes after the first,
        and the whole fleet's straggler is measured from a clock that started before it existed."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=FANOUT) as pool:
            list(pool.map(lambda kv: self._launch(kv[1], kv[0], block=block, chunks=chunks,
                                                  workdir=kv[1].workdir),
                          list(cards.items())))

    def _launch(self, card, chunk, *, block, chunks, workdir):
        env = dict(self.prove_env, HAZYNC_BLOCK_NAME=f"block_{block}.json",
                   HAZYNC_CHUNKS=str(chunks), HAZYNC_WORKDIR=workdir)
        assigns = " ".join(f"{k}={v}" for k, v in sorted(env.items()))
        # ⛔ setsid + nohup + `< /dev/null` + disown. Without all four the prove dies with the ssh
        # session that started it, which looks exactly like a card that failed instantly.
        # ⛔ chmod FIRST, EVERY TIME. `scp` does NOT preserve the executable bit, so a staged
        # pod-prove.sh lands 0644 and every card dies with
        #     setsid: failed to execute ./pod-prove.sh: Permission denied
        # -- a zero-byte prove.log, an idle GPU, and a run that sits in its poll loop believing the
        # fleet is merely slow. Measured on 23 live cards 2026-09-20. It is one syscall; do it here
        # rather than trusting whoever staged the file.
        body = (f"chmod +x /workspace/pod-prove.sh 2>/dev/null; mkdir -p {workdir} && cd /workspace && {assigns} "
                f"nohup setsid ./pod-prove.sh {chunk} > {workdir}/run.log 2>&1 < /dev/null & disown; exit 0")
        return self.ssh.run(card, body) is not None

    # ── card-to-card reachability, tested BEFORE the clock ────────────────────────────────────────
    #
    # ⛔ POD-TO-POD CONNECTIVITY IS NOT GUARANTEED, AND THE DRIVER CANNOT TEST IT FOR THEM. Measured
    # 2026-09-20 on two rented pods: the aggregate's published port answered from the ORCHESTRATOR
    # and the other pod got `connect: No route to host`. Reaching it from here proves only that the
    # port is open to the internet; it says nothing about the path the workers will take. The probe
    # has to run ON each worker.
    #
    # Without this the run looks perfectly armed: seg-serve listens, autoattach.sh loops correctly,
    # `aa.log` stays 0 bytes, and the aggregate sits at `0/N segments` until the tick budget is
    # spent. That is exactly how the first 2-card milestone run failed.

    # The throwaway listener, as a SCRIPT rather than a `python3 -c` one-liner: quoting a multi-line
    # program through ssh is how escaping bugs get in, and this one only has to accept and close.
    _LISTENER = """import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", PORT))
s.listen(16)
s.settimeout(1)
end = time.time() + SECS
while time.time() < end:
    try:
        c, _ = s.accept()
        c.close()
    except Exception:
        pass
"""

    # ⛔ THE AGGREGATE'S LINK CARRIES THE WHOLE FLEET, AND IT WAS CHOSEN AT RANDOM (hazync#517).
    # Every segment is pushed FROM the aggregate and every join round-trips THROUGH it. Measured on
    # block 968,340: 12.48 GB through one card in 1,910 s. The driver took `order[0]` -- whichever pod
    # answered ssh first -- for the one role where the pod's network matters more than its GPU.
    #
    # ⚠ AND THE RUN LOGS CANNOT ANSWER THIS. Two runs sustained 36 and 52 Mbit/s, but the fleet was
    # GPU-busy 92%, so those are DEMAND, not capacity: the aggregate only ever pushed as fast as the
    # workers consumed. The link is never saturated by a healthy run, so it has to be asked directly.
    _EGRESS_SERVER = """import socket, threading, time
CHUNK = b"x" * 65536
def serve(c):
    end = time.time() + SECS
    try:
        while time.time() < end:
            c.sendall(CHUNK)
    except Exception:
        pass
    try: c.close()
    except Exception: pass
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", PORT))
s.listen(64)
s.settimeout(1)
end = time.time() + SECS + 8
while time.time() < end:
    try:
        c, _ = s.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()
    except Exception:
        pass
"""

    def measure_egress(self, cards, *, secs=8):
        """Mbit/s this aggregate can push to ALL its workers at once, or None if untestable.

        ⛔ CONCURRENTLY, NOT ONE AT A TIME. The real shape is one aggregate feeding every worker
        simultaneously; a single-stream figure would flatter a card whose link collapses under 14
        readers. Every worker pulls for `secs` and the totals are summed.

        ⛔ AND None IS NOT ZERO. If the streamer cannot be confirmed up, this returns None so the
        caller can say "untested" rather than condemn a healthy pod -- the same rule check_reachability
        already follows, and for the same reason.
        """
        script = (self._EGRESS_SERVER.replace("PORT", str(self.agg_port))
                                     .replace("SECS", str(int(secs))))
        blob = base64.b64encode(script.encode()).decode()
        self.ssh.run(self.agg,
                     f"echo {blob} | base64 -d > /tmp/egress.py && "
                     f"nohup timeout {int(secs) + 12} python3 /tmp/egress.py "
                     f"> /tmp/egress.log 2>&1 < /dev/null & disown; exit 0",
                     timeout=tip_driver.PROBE_TIMEOUT_S)
        time.sleep(2)
        up = (self.ssh.run(self.agg,
                           f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -c ':{self.agg_port} '",
                           timeout=tip_driver.PROBE_TIMEOUT_S) or "").strip().splitlines()
        if not up or not up[-1].strip().isdigit() or int(up[-1].strip()) < 1:
            return None, {}

        workers = [c for c in dict.fromkeys(cards.values() if hasattr(cards, "values") else cards)
                   if c.cid != self.agg.cid]
        if not workers:
            return None, {}
        host = self.agg.ip
        reader = (f"import socket,time\n"
                  f"s=socket.create_connection(('{host}',{self.agg_dial}),10); s.settimeout(3)\n"
                  f"n=0; t0=time.time(); end=t0+{int(secs)}\n"
                  f"while time.time()<end:\n"
                  f"    b=s.recv(262144)\n"
                  f"    if not b: break\n"
                  f"    n+=len(b)\n"
                  f"print('EGRESS',n,round(time.time()-t0,3))\n")
        blob2 = base64.b64encode(reader.encode()).decode()

        def one(card):
            out = self.ssh.run(card,
                               f"echo {blob2} | base64 -d > /tmp/pull.py && "
                               f"timeout {int(secs) + 10} python3 /tmp/pull.py 2>/dev/null; exit 0",
                               timeout=tip_driver.PROBE_TIMEOUT_S + secs)
            for ln in reversed((out or "").strip().splitlines()):
                parts = ln.strip().split()
                if len(parts) == 3 and parts[0] == "EGRESS":
                    try:
                        return int(parts[1]), float(parts[2])
                    except ValueError:
                        return None
            return None

        raw, per = {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as pool:
            for card, res in zip(workers, pool.map(one, workers)):
                if res and res[1] > 0:
                    raw[card.cid] = res                      # (bytes, elapsed_s)
                    per[card.cid] = res[0] * 8 / res[1] / 1e6
        if not raw:
            return None, {}
        # ⛔ TOTAL BYTES OVER THE LONGEST READ, not the sum of per-stream rates. Summing rates treats a
        # stream that delivered 8 MB in 1 s the same as one that delivered 64 MB in 8 s, so a fleet
        # where half the readers finish early reports a throughput the aggregate never sustained.
        # Dividing the total by the window they actually shared cannot flatter it that way.
        total_bytes = sum(b for b, _ in raw.values())
        window = max(e for _, e in raw.values())
        return total_bytes * 8 / window / 1e6, per

    def check_reachability(self, cards, *, secs=30):
        """{card: bool} — can each worker actually open a TCP connection to the aggregate's port?

        Opens a throwaway listener on the aggregator, probes from every worker in parallel, and lets
        the listener expire. The aggregator is never probed against itself.

        ⛔ A LISTENER THAT NEVER CAME UP WOULD CONDEMN A HEALTHY FLEET. Every card would read
        unreachable and the run would discard cards that are perfectly fine, so the listener is
        VERIFIED from the aggregator before a single negative result is believed. When it cannot be
        confirmed this returns None rather than a verdict — "we could not test" is not "they failed".
        """
        script = (self._LISTENER.replace("PORT", str(self.agg_port))
                                .replace("SECS", str(int(secs))))
        blob = base64.b64encode(script.encode()).decode()
        self.ssh.run(self.agg,
                     f"echo {blob} | base64 -d > /tmp/reachlisten.py && "
                     f"nohup timeout {int(secs) + 5} python3 /tmp/reachlisten.py "
                     f"> /tmp/reachlisten.log 2>&1 < /dev/null & disown; exit 0",
                     timeout=tip_driver.PROBE_TIMEOUT_S)
        time.sleep(2)                      # give it a moment to bind before asking if it is up

        # ⛔ CONFIRM THE LISTENER, from the aggregator itself, before trusting any failure below.
        up = (self.ssh.run(self.agg,
                           f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -c ':{self.agg_port} '",
                           timeout=tip_driver.PROBE_TIMEOUT_S) or "").strip().splitlines()
        if not up or not up[-1].strip().isdigit() or int(up[-1].strip()) < 1:
            return None

        workers = [c for c in dict.fromkeys(cards.values() if hasattr(cards, "values") else cards)
                   if c.cid != self.agg.cid]
        host = self.agg.ip

        def one(card):
            # ⛔ bash, NOT sh. /dev/tcp is a bash feature and /bin/sh is dash, where the test can
            # never succeed -- which once made an entire armed fleet attach to nothing.
            out = self.ssh.run(
                card,
                f"timeout 5 bash -c '</dev/tcp/{host}/{self.agg_dial}' >/dev/null 2>&1 "
                f"&& echo REACH || echo NOREACH",
                timeout=tip_driver.PROBE_TIMEOUT_S)
            # ⛔ EXACT MATCH ON THE LAST LINE. `endswith("REACH")` is TRUE for "NOREACH", so an
            # unreachable card read as reachable -- the dangerous direction, since it lets a fleet
            # that cannot attach straight through the gate this function exists to be.
            last = (out or "").strip().splitlines()
            return card, bool(last) and last[-1].strip() == "REACH"

        with concurrent.futures.ThreadPoolExecutor(max_workers=FANOUT) as pool:
            return dict(pool.map(one, workers))

    def arm_auto_attach(self, cards):
        """Pre-position the attach script so joining the aggregate costs ~0 once its listener opens.

        Run 4 lost 40 s between staging and the listener being ready; arming in advance is what keeps a
        card from having to be told twice.
        """
        # ⛔ THE ATTACH WINDOW MUST NOT EXPIRE BEFORE THE AGGREGATE OPENS. This counted to 600 and
        # gave up, and the countdown started at T0 -- so the window closed 600 s into the CHUNK phase,
        # which has nothing to do with when the listener appears.
        #
        # Measured 2026-09-20 on 14 cards: one slow card made the chunk phase 637 s, the aggregate
        # began listening at ~23:16, and every worker's attach had already expired at 23:14:27 --
        # about ninety seconds too early. The aggregate then ran all 529 segments ON ONE CARD while
        # thirteen idle cards sat beside it, which is precisely the cost seg-serve exists to remove.
        # Nothing looked broken: the cards were reachable, the script was staged and correct, and it
        # had simply stopped waiting.
        #
        # It now waits as long as the run does. `attach.stop` is how the run ends it deliberately,
        # and the day-long cap only exists so a pod kept alive by hand cannot spin for ever.
        _lv = tip_lifecycle.lever_env()
        if _lv:
            print(f"    forwarding to every worker: {' '.join(sorted(_lv))}")
        script = worker_attach_script(_lv)
        target = f"{self.agg.ip}:{self.agg_dial}"   # what a worker dials, not what seg-serve binds
        for chunk, card in cards.items():
            if card == self.agg:
                continue                       # the aggregator serves; it does not dial itself
            self.ssh.run(card, script)
            self.ssh.run(card, f"cd /workspace && nohup setsid bash ./autoattach.sh {target} w{chunk} "
                               f"> aa.log 2>&1 < /dev/null & disown; exit 0")

    # ── phase 3 ────────────────────────────────────────────────────────────────────────────────────
    def stop_auto_attach(self, cards):
        """End the attach wait on every worker. Called when the run is done with them.

        ⚠ Best effort by design: a card we cannot reach is usually one about to be terminated, and
        failing the teardown over it would be worse than leaving a loop on a pod that is going away.
        ⛔ PER CARD, not once around the whole loop (hazync#463). A dying pod refuses connections, and
        one raise partway through would skip every card AFTER it while the teardown reported nothing —
        the cards still looping would be exactly the ones nobody looked at.
        """
        for card in dict.fromkeys(cards.values() if hasattr(cards, "values") else cards):
            if card == self.agg:
                continue
            try:
                self.ssh.run(card, f"touch {ATTACH_STOP}; exit 0")
            except Exception:                              # noqa: BLE001
                continue

    def probe_all(self, cards, reassigned=()):
        return tip_driver.probe_all(self.ssh, cards, reassigned=reassigned)

    def stage_receipt(self, card, chunk, *, reassigned=()):
        """Card -> orchestrator -> aggregator, with BOTH hops verified.

        ⛔ `scp` EXITING 0 IS NOT EVIDENCE. One run staged 21 of 22 and `seg-serve` panicked with no
        useful message. Each hop is confirmed by the file existing at the far end, and the whole thing
        is retried rather than reported once and forgotten.
        """
        src_dir = posixpath.join(card.workdir, f"re{chunk}") if chunk in reassigned else card.workdir
        remote = posixpath.join(src_dir, f"chunk_{chunk}.bin")
        local = os.path.join(self.stage_dir, f"chunk_{chunk}.bin")
        for _ in range(3):
            if not self.ssh.fetch(card, remote, local):
                continue
            if not self.ssh.push(self.agg, local, f"{self.workdir}/chunk_{chunk}.bin"):
                continue
            if _last_line(self.ssh.run(
                    self.agg, f"test -s {self.workdir}/chunk_{chunk}.bin && echo Y")) == "Y":
                return True
        return False

    def staged_count(self):
        out = _last_line(self.ssh.run(
            self.agg, f"ls {self.workdir}/chunk_*.bin {self.workdir}/chunk_*.hzk 2>/dev/null | wc -l"))
        try:
            return int(out)
        except (TypeError, ValueError):
            # ⛔ UNREADABLE IS NOT ZERO AND IT IS NOT N. Returning 0 fails the gate, which is the safe
            # direction: refusing to aggregate because we could not count is recoverable; aggregating
            # on a count we invented is not.
            return 0

    def kill_provers(self, card):
        return self.ssh.kill_provers(card)

    def relaunch(self, card, chunk, *, block, chunks, workdir_suffix):
        workdir = posixpath.join(card.workdir, f"re{workdir_suffix}")
        self.ssh.run(card, f"rm -rf {workdir} && mkdir -p {workdir}; echo OK")
        return self._launch(card, chunk, block=block, chunks=chunks, workdir=workdir)

    # ── phase 6 ────────────────────────────────────────────────────────────────────────────────────
    def start_aggregate(self, *, block, chunks):
        env = dict(self.prove_env,
                   HAZYNC_BLOCK=f"{self.workdir}/block_{block}.json",
                   HAZYNC_CHUNKS=str(chunks), HAZYNC_AGG="1",
                   HAZYNC_PORT=str(self.agg_port),
                   # ⛔ WITHOUT THIS THE AGGREGATE BINDS LOOPBACK AND NO RENTED CARD CAN EVER ATTACH.
                   # The prover's rule (seg_bind_addr): HAZYNC_BIND wins, else HAZYNC_SEG_REMOTE=1
                   # means 0.0.0.0, else 127.0.0.1. A tip fleet is remote BY CONSTRUCTION — every card
                   # is a rented pod — so loopback is never right here.
                   # Measured 2026-09-21, a full run lost to it ($1.02, 27.6 min):
                   #     listening on 127.0.0.1:9110
                   #     ⚠ LOOPBACK ONLY — workers on OTHER machines cannot attach to this run.
                   #     execution 6.4 s  35 segments, 15.8 MB (streamed)
                   #       0/34 segments
                   #     ⛔ no worker has been connected for 600s and 35 piece(s) of work are stranded
                   # ⚠ The port is UNAUTHENTICATED. That is an accepted trade for a solo operator's own
                   # rented fleet on an ephemeral pod: every receipt a peer returns is verified and
                   # requeued if bad, so a stranger can waste work but cannot forge a proof.
                   HAZYNC_SEG_REMOTE="1")
        # ⛔ AND CHECK IT, rather than trusting the line above to stay. `tip_lifecycle.bind_verdict`
        # has encoded this rule all along and NOTHING CALLED IT — which is why a loopback bind cost a
        # whole run instead of being refused in the first second. accept_public is deliberate: the
        # verdict rejects 0.0.0.0 on principle, and this caller is the case that has accepted it.
        ok, why = tip_lifecycle.bind_verdict(env, cards_are_remote=True, accept_public=True)
        if not ok:
            raise RuntimeError(f"refusing to start the aggregate: {why}")
        assigns = " ".join(f"{k}={v}" for k, v in sorted(env.items()))
        body = (f"cd {self.workdir} && rm -f agg.log agg.err && {assigns} "
                f"nohup setsid ./hazync-host-cuda seg-serve > agg.log 2> agg.err < /dev/null & "
                f"disown; exit 0")
        # ⚠ RECORDED HERE, NOT BY THE CALLER, and recorded BEFORE the ssh rather than after, so a slow
        # ssh is charged to the aggregate's startup instead of silently shortening it. This is the one
        # epoch every worker's first-segment timestamp is measured against (hazync#253): `seg-connect`
        # timestamps its own lines, but nothing else knows when the listener was asked to come up.
        # It is set even if the launch fails — `tip_harvest` needs it in the teardown path too.
        self.agg_started_ms = int(time.time() * 1000)
        return self.ssh.run(self.agg, body) is not None

    # ── mode 6: prove ONE BOARD BLOCK from its bridge bundle (hazync#367) ─────────────────────────
    def start_range_aggregate(self, *, height, bundle_path):
        """Stage the bundle onto the aggregate and serve it as a range. Returns (ok, why).

        ⛔ THIS IS A DIFFERENT PROOF FROM `start_aggregate`, NOT A VARIANT OF IT. The chunk path proves
        `block_<h>.json` -- the FIXTURE -- through build_full(), and prover/host/src/main.rs says in as
        many words that that path "serves the FIXTURE shape and cannot produce a board block at all
        (#361)". The two files sit side by side with the same height in the name and prove entirely
        different things:

            fixture  bits, coinbase_hex, merkle, nonce, prev, txs, ...   no accumulator state
            bundle   in_roots, in_leaves, in_tip, witness, ...           the real UTXO transition

        Only the bundle commits to the chain's accumulator, which is what the board re-verifies. A
        receipt from the chunk path would cost a claim and an hour of TTL and be rejected.

        ⚠ NO per-card `prove-chunk` phase here. seg-serve executes the guest once and pushes segments
        to whichever workers have dialled in, so the cards do nothing until they attach.
        """
        remote = posixpath.join(self.workdir, f"bundle_{height}.json")
        if not self.ssh.push(self.agg, bundle_path, remote):
            return False, f"could not stage {bundle_path} onto {self.agg.cid}"
        # ⛔ CONFIRM IT LANDED. `scp` exiting 0 is not evidence -- the same silent-drop that once staged
        # 21 of 22 chunks and left seg-serve panicking with nothing useful in any log.
        size = (self.ssh.run(self.agg, f"stat -c%s {remote} 2>/dev/null || echo 0") or "0").strip()
        size = (size.splitlines() or ["0"])[-1]
        if not (size.isdigit() and int(size) > 0):
            return False, f"bundle staged as {size} bytes on {self.agg.cid}"

        env = dict(self.prove_env,
                   HAZYNC_RANGE=str(height),
                   HAZYNC_BRIDGE_OUT=self.workdir,
                   HAZYNC_PORT=str(self.agg_port),
                   HAZYNC_OUT=posixpath.join(self.workdir, f"range_{height}.hzk"),
                   HAZYNC_SEG_REMOTE="1")
        ok, why = tip_lifecycle.bind_verdict(env, cards_are_remote=True, accept_public=True)
        if not ok:
            return False, f"refusing to start the range aggregate: {why}"
        assigns = " ".join(f"{k}={v}" for k, v in sorted(env.items()))
        body = (f"cd {self.workdir} && rm -f agg.log agg.err && {assigns} "
                f"nohup setsid ./hazync-host-cuda seg-serve > agg.log 2> agg.err < /dev/null & "
                f"disown; exit 0")
        self.agg_started_ms = int(time.time() * 1000)
        if self.ssh.run(self.agg, body) is None:
            return False, "the launch command did not come back"
        return True, ""

    def stop_range_aggregate(self, *, tries=5, wait_s=2.0):
        """Kill the range aggregate and CONFIRM it is gone. Returns (ok, why).

        ⛔ CONFIRMED, NOT REQUESTED. `seg-serve` is launched with `nohup setsid ... &` and holds port
        9110; the next block's aggregate binds the same port. A stop that only issues a kill and
        returns leaves the caller free to start the next one against a process that has not died
        yet, and this codebase has already relaunched seg-serve on top of a healthy one -- the new
        process died on `bind()` while the original kept working with its log unlinked, which looks
        from the outside exactly like a fleet that has stopped making progress.

        ⛔ `pkill -x`, MATCHING THE TRUNCATED `comm`, NEVER `pkill -f`. `pkill -f seg-serve` matches
        the ssh command line carrying the pattern -- our own invocation -- so it kills the shell
        that is asking and reports success. `comm` is truncated to 15 characters by the kernel, so
        the name to match is `hazync-host-cud`, which is also what `aggregate_status` keys on.

        ⚠ TERM first, then KILL. The aggregate holds an open receipt file; give it the chance to
        close cleanly before taking the process out from under it.
        """
        alive_cmd = "ps -eo comm | grep -c '^hazync-host-cud'"
        for attempt in range(tries):
            sig = "-TERM" if attempt < tries - 2 else "-KILL"
            # ⚠ `exit 0`: pkill exits non-zero when nothing matched, which is the SUCCESS case here.
            self.ssh.run(self.agg, f"pkill {sig} -x hazync-host-cud; exit 0")
            time.sleep(wait_s)
            out = (self.ssh.run(self.agg, f"{alive_cmd}; exit 0") or "").strip()
            n = (out.splitlines() or ["?"])[-1].strip()
            if n == "0":
                return True, f"no hazync-host-cud left on {self.agg.cid} after {attempt + 1} signal(s)"
            if not n.isdigit():
                # ⛔ AN UNREADABLE COUNT IS NOT A ZERO. If ssh gave us nothing we do not know what is
                # running, and reporting "stopped" would be the absence-as-green failure exactly.
                return False, f"could not read the process count on {self.agg.cid} (got {n!r})"
        return False, f"{n} hazync-host-cud still running on {self.agg.cid} after {tries} signals"

    def fetch_receipt(self, height, local_path):
        """Bring the proved range receipt back. Returns (ok, why).

        ⚠ The aggregate honours HAZYNC_OUT (main.rs:1505/3961) and otherwise writes
        `aggregate_receipt.bin`; we set the former, and fall back to the latter so a run started by an
        older driver is still collectable.
        """
        for name in (f"range_{height}.hzk", "aggregate_receipt.bin"):
            if self.ssh.fetch(self.agg, posixpath.join(self.workdir, name), local_path):
                return True, name
        return False, "no receipt found on the aggregate (tried range_<h>.hzk, aggregate_receipt.bin)"

    def aggregate_status(self):
        """Verified? still alive? and the join-tree progress, which exists nowhere else.

        ⛔ LIVENESS COMES FROM `ps -eo comm`, MATCHED AGAINST THE TRUNCATED NAME. `pgrep -f seg-serve`
        self-matches the shell carrying it, so a dead aggregate reads as alive; and `comm` truncates at
        15 characters, so matching the full 16-character name reads a LIVE aggregate as dead — which
        once had `seg-serve` relaunched on top of a healthy one, killing the new process on `bind()`
        while the original kept working with its log unlinked.

        ⚠ `joins N/M` is the only record of the fold's progress that exists anywhere, and the pod is
        gone before anyone wants it. It is collected here or not at all.
        """
        body = (f"grep -E 'VERIFIED|digest|TOTAL|execution|worker wall|assembly|panicked' "
                f"{self.workdir}/agg.log 2>/dev/null | tail -8; "
                f"tail -2 {self.workdir}/agg.err 2>/dev/null; "
                f"echo \"ALIVE:$(ps -eo comm | grep -c '^hazync-host-cud')\"; "
                f"echo \"JOINS:$(grep -oE 'joins [0-9]+/[0-9]+' {self.workdir}/agg.log 2>/dev/null | tail -1)\"")
        out = self.ssh.run(self.agg, body)
        if out is None:
            # Could not ask. Not dead — saying "dead" here would abort a healthy run over one bad ssh.
            return {"alive": True, "verified": False, "digest": None, "joins": None, "unreachable": True}
        digest = None
        for line in out.splitlines():
            if "digest" in line:
                parts = [w for w in line.replace("\t", " ").split() if len(w) == 64]
                if parts:
                    digest = parts[0]
        alive = any(l.startswith("ALIVE:") and l[6:].strip() not in ("", "0") for l in out.splitlines())
        joins = next((l[6:].strip() for l in out.splitlines() if l.startswith("JOINS:")), "")
        return {"alive": alive, "verified": "VERIFIED" in out, "digest": digest,
                "joins": joins or None, "unreachable": False}


# ⛔ THE FILE THE ATTACH LOOP WATCHES, NAMED ONCE (hazync#463). `worker_attach_script` is module-level
# and knows nothing about a FleetRunner's `workdir`, so the loop's path is fixed at /workspace. If
# `stop_auto_attach` touched a workdir-relative path instead, a non-default workdir would make it
# touch a file NOBODY READS — a teardown that reports success and stops nothing.
ATTACH_STOP = "/workspace/attach.stop"


def worker_attach_script(levers):
    """The autoattach.sh a worker runs, with the operator's levers in front of the exec.

    ⛔ A MODULE-LEVEL FUNCTION ON PURPOSE. When this lived inline in arm_auto_attach the only way
    to test it was to rebuild the line in the test -- which proves the test right, not the code.
    hazync#252 lost ten days to exactly that shape.
    """
    worker_levers = "".join(f"{k}={shlex.quote(v)} " for k, v in sorted(levers.items()))
    return (
        f"cat > /workspace/autoattach.sh <<'EOS'\n"
        f"#!/bin/bash\n"
        f"AGG=$1; WID=$2\n"
        f"cd /workspace || exit 1\n"
        f"rm -f {ATTACH_STOP}\n"
        f"for i in $(seq 1 86400); do\n"
        f"  [ -f {ATTACH_STOP} ] && exit 0\n"
        # ⛔ bash, NOT sh. /dev/tcp is a BASH feature; /bin/sh is dash on the RunPod image and
        # reports "cannot open /dev/tcp/...: No such file". Under sh this test NEVER succeeds, so
        # the worker loops its full 600 s and never attaches even when the aggregate is perfectly
        # reachable — an entire fleet that looks armed and proves nothing. run_continuous.sh used
        # bash here for exactly this reason; changing it to sh silently broke attachment, and the
        # first live run is what caught it.
        f"  if timeout 3 bash -c \"</dev/tcp/${{AGG%%:*}}/${{AGG##*:}}\" 2>/dev/null; then\n"
        # ⛔ THE LEVERS GO HERE OR THEY GO NOWHERE. This line carried HAZYNC_WORKER_ID and nothing
        # else, so a worker ran with NO HAZYNC_* environment at all -- and HAZYNC_WORKER_LIFTS is
        # read in the WORKER path (seg-connect, main.rs:5260). It was UNREACHABLE BY CONSTRUCTION:
        # #148 built it, SEGDIST_TASKS.md measured it at 'undivided work 58% -> 2.1%', and no run
        # could ever have switched it on.
        f"    {worker_levers}HAZYNC_WORKER_ID=$WID exec ./hazync-host-cuda seg-connect \"$AGG\" > aggw.log 2>&1\n"
        f"  fi\n"
        f"  sleep 1\n"
        f"done\n"
        f"EOS\nchmod +x /workspace/autoattach.sh; echo ARMED"
    )


def _last_line(text):
    if text is None:
        return None
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return lines[-1].strip() if lines else None
