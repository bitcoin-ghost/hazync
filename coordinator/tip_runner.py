#!/usr/bin/env python3
"""The concrete fleet runner: what `tip_run` calls to act on real cards (Phase 5 ⑧).

`tip_run` defines the sequence, `tip_fleet` the decisions, `tip_driver` the ssh primitives. This is the
object that joins them — every method here is a phase of `tools/milestone/run_continuous.sh`, kept in
the same order and with the same refusals.

⛔ WHAT THIS DELIBERATELY DOES NOT DO: decide anything. If a method here grows an `if` about whether a
card is healthy, that logic belongs in `tip_fleet` where it can be tested without a fleet. This layer
does, reports, and refuses — it never judges.
"""

import os
import posixpath

import tip_driver


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
        """Wipe every pod and report what each one says it has left. None means we could not ask."""
        out = {}
        for _chunk, card in cards.items():
            out[card] = _last_line(self.ssh.run(card, REMOTE_CLEAR))
        return out

    # ── phase 1 ────────────────────────────────────────────────────────────────────────────────────
    def launch_all(self, cards, *, block, chunks):
        for chunk, card in cards.items():
            self._launch(card, chunk, block=block, chunks=chunks, workdir=card.workdir)

    def _launch(self, card, chunk, *, block, chunks, workdir):
        env = dict(self.prove_env, HAZYNC_BLOCK_NAME=f"block_{block}.json",
                   HAZYNC_CHUNKS=str(chunks), HAZYNC_WORKDIR=workdir)
        assigns = " ".join(f"{k}={v}" for k, v in sorted(env.items()))
        # ⛔ setsid + nohup + `< /dev/null` + disown. Without all four the prove dies with the ssh
        # session that started it, which looks exactly like a card that failed instantly.
        body = (f"mkdir -p {workdir} && cd /workspace && {assigns} "
                f"nohup setsid ./pod-prove.sh {chunk} > {workdir}/run.log 2>&1 < /dev/null & disown; exit 0")
        return self.ssh.run(card, body) is not None

    def arm_auto_attach(self, cards):
        """Pre-position the attach script so joining the aggregate costs ~0 once its listener opens.

        Run 4 lost 40 s between staging and the listener being ready; arming in advance is what keeps a
        card from having to be told twice.
        """
        script = (
            f"cat > /workspace/autoattach.sh <<'EOS'\n"
            f"AGG=$1; WID=$2\n"
            f"cd /workspace || exit 1\n"
            f"for i in $(seq 1 600); do\n"
            f"  if timeout 3 sh -c \"</dev/tcp/${{AGG%%:*}}/${{AGG##*:}}\" 2>/dev/null; then\n"
            f"    HAZYNC_WORKER_ID=$WID exec ./hazync-host-cuda seg-connect \"$AGG\" > aggw.log 2>&1\n"
            f"  fi\n"
            f"  sleep 1\n"
            f"done\n"
            f"EOS\nchmod +x /workspace/autoattach.sh; echo ARMED"
        )
        target = f"{self.agg.ip}:{self.agg_dial}"   # what a worker dials, not what seg-serve binds
        for chunk, card in cards.items():
            if card == self.agg:
                continue                       # the aggregator serves; it does not dial itself
            self.ssh.run(card, script)
            self.ssh.run(card, f"cd /workspace && nohup setsid ./autoattach.sh {target} w{chunk} "
                               f"> aa.log 2>&1 < /dev/null & disown; exit 0")

    # ── phase 3 ────────────────────────────────────────────────────────────────────────────────────
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
                   HAZYNC_PORT=str(self.agg_port))
        assigns = " ".join(f"{k}={v}" for k, v in sorted(env.items()))
        body = (f"cd {self.workdir} && rm -f agg.log agg.err && {assigns} "
                f"nohup setsid ./hazync-host-cuda seg-serve > agg.log 2> agg.err < /dev/null & "
                f"disown; exit 0")
        return self.ssh.run(self.agg, body) is not None

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


def _last_line(text):
    if text is None:
        return None
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return lines[-1].strip() if lines else None
