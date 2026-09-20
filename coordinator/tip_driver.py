#!/usr/bin/env python3
"""The tip rig's SSH layer: turn `tip_fleet` decisions into actions on real cards (Phase 5 ⑧).

`tip_fleet.py` decides; this executes. The split is deliberate — every guard worth having is in the pure
half where it can be tested without renting a fleet, and this half is kept thin enough to read in one
sitting.

⛔ NO LOCAL SHELL, ANYWHERE. Every command is built as an argv list and handed to `subprocess` without
`shell=True`. This is not a style preference: the single most destructive bug in the 966,256 runs was a
remote probe written in double quotes, so `$(stat …)` and `$(pgrep …)` expanded on the ORCHESTRATOR
before ssh ran. The remote command became a constant, the log size never changed, and every healthy card
was declared stalled at the same instant — it killed a chunk five minutes into run 3 and was working
down the fleet when the driver was stopped. With an argv list there is no local shell to do it.

⛔ EVERY REMOTE CALL IS PARALLEL AND UNDER A TIMEOUT. A sequential poll of 27 cards at ~0.7 s each made a
tick cost ~20 s, which is why the last chunk of run 4 sat FINISHED and unnoticed for 24.9 s — the single
largest recoverable waste in that run. And one unreachable card must never hold up the other 26: a
sequential probe once wedged a run for 12 minutes.

⛔ KILLS GO VIA A SCRIPT FILE, NEVER A `pkill -f` PATTERN. The pattern matches the invoking shell's own
command line, so a remote `pkill -f hazync` kills the ssh session that issued it and reports nothing.
"""

import base64
import concurrent.futures
import os
import subprocess

import tip_fleet


SSH_OPTS = [
    "-n",                                   # never read stdin: a prompt in a parallel fan-out hangs the lot
    "-o", "BatchMode=yes",                  # fail rather than ask for a password
    "-o", "ConnectTimeout=10",
    # ⛔ accept-new, NOT yes. This said `yes`, and a fleet of freshly rented pods CANNOT be reached
    # that way: a pod created thirty seconds ago has a host key nobody has ever seen, so with
    # BatchMode every single connection fails and the run dies waiting for cards that are up and
    # answering. Measured 2026-09-20 on two live RTX 4090s -- `ssh -o StrictHostKeyChecking=no`
    # by hand returned READY instantly while the driver saw nothing for six minutes.
    #
    # `accept-new` keeps the protection that mattered: it trusts a key it has never seen (which is
    # every new pod, unavoidably) but still REFUSES a key that has CHANGED for a host already known,
    # which is the substitution `yes` was there to stop. `no` would accept a changed key too, and is
    # the wrong fix.
    "-o", os.environ.get("HAZYNC_TIP_HOSTKEY_OPT", "StrictHostKeyChecking=accept-new"),
]

PROBE_TIMEOUT_S = float(os.environ.get("HAZYNC_TIP_PROBE_TIMEOUT_S", "25"))
COPY_TIMEOUT_S = float(os.environ.get("HAZYNC_TIP_COPY_TIMEOUT_S", "120"))


class Card:
    """One rented card: where it is and how to reach it."""

    __slots__ = ("cid", "ip", "port", "loc", "workdir")

    def __init__(self, cid, ip, port, loc="?", workdir="/workspace"):
        self.cid, self.ip, self.port, self.loc, self.workdir = cid, ip, int(port), loc, workdir

    def __repr__(self):
        return f"Card({self.cid} {self.loc} {self.ip}:{self.port})"

    # ⛔ HASHED AND COMPARED BY cid, BECAUSE THE PROBE MAP IS KEYED BY CARD. `plan_tick` looks a card up
    # with `probes.get(card)`, where `card` is whatever the assignment map holds. Without these, two
    # Card objects for the same pod are different keys, every lookup returns None, every card reads
    # UNREACHABLE and the run does nothing at all -- silently, because UNREACHABLE is the state that
    # deliberately takes no action.
    def __hash__(self):
        return hash(self.cid)

    def __eq__(self, other):
        return isinstance(other, Card) and self.cid == other.cid


class SSHRunner:
    """The real remote-execution layer. Tests substitute their own object with the same three methods."""

    def __init__(self, key, user="root"):
        self.key, self.user = key, user

    def _base(self, card, port_flag):
        return ["ssh", *SSH_OPTS, "-i", self.key, port_flag, str(card.port),
                f"{self.user}@{card.ip}"]

    def run(self, card, body, env=None, timeout=PROBE_TIMEOUT_S):
        """Run `body` on the card under `sh -c`, with `env` exported. Returns stdout, or None.

        ⛔ `body` IS PASSED THROUGH UNTOUCHED AND UNQUOTED BY US. It reaches the remote `sh` as a single
        argv element, so nothing here can expand it and nothing needs escaping. Returning None for any
        failure is deliberate: `tip_fleet.card_state` treats that as UNREACHABLE, which is the only safe
        reading of "we could not ask".
        """
        assigns = [f"{k}={v}" for k, v in sorted((env or {}).items())]
        cmd = [*self._base(card, "-p"), " ".join(assigns + ["sh", "-c", _sq(body)])]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None
        return p.stdout if p.returncode == 0 else None

    def fetch(self, card, remote, local, timeout=COPY_TIMEOUT_S):
        """Copy a file off the card. Returns True only if the local file exists and is non-empty.

        ⛔ A SILENT scp DROP IS THE FAILURE THIS CATCHES. One run staged 21 of 22 chunks and `seg-serve`
        panicked with no useful message. `scp` exiting 0 is not evidence; the file being there is.
        """
        cmd = ["scp", *[o for o in SSH_OPTS if o != "-n"], "-i", self.key,
               "-P", str(card.port), f"{self.user}@{card.ip}:{remote}", local]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return os.path.isfile(local) and os.path.getsize(local) > 0

    def push(self, card, local, remote, timeout=COPY_TIMEOUT_S):
        """Copy a file ONTO the card. Returns True only if the far end confirms it.

        ⛔ THE CONFIRMATION IS THE POINT, and it is the caller's job: `scp` exiting 0 says the transfer
        was attempted, not that a file of the right size is sitting there. `FleetRunner.stage_receipt`
        checks `test -s` on the far side before believing this, because a silent drop once staged 21 of
        22 chunks and `seg-serve` panicked with nothing useful in any log.
        """
        if not (os.path.isfile(local) and os.path.getsize(local) > 0):
            return False                      # never push something we have not got
        cmd = ["scp", *[o for o in SSH_OPTS if o != "-n"], "-i", self.key,
               "-P", str(card.port), local, f"{self.user}@{card.ip}:{remote}"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return p.returncode == 0

    def kill_provers(self, card, timeout=PROBE_TIMEOUT_S):
        """Stop everything proving on this card.

        ⛔ THE PATTERN MUST NEVER APPEAR IN THE COMMAND LINE, AND A HEREDOC IS NOT ENOUGH. `pgrep -f`
        matches the FULL command line of every process, including the remote shell that is carrying the
        script — so embedding the script inline, even inside `cat <<EOS`, still puts `hazync-host-cuda`
        in that shell's argv. The remote `pgrep` then finds the ssh session itself, kills it, and the
        call returns a transport error while the real provers keep running. Measured on a live pod
        2026-09-20: ssh exit 255 on both cards, having killed nothing.

        So the script is carried as BASE64 and decoded on the far side. The argv holds only the encoded
        blob, which matches nothing, and the pattern exists solely inside a file.
        """
        script = (
            "for p in $(pgrep -f pod-prove); do kill -9 $p 2>/dev/null; done\n"
            "for p in $(pgrep -f hazync-host-cuda); do kill -9 $p 2>/dev/null; done\n"
            "echo KILLED\n"
        )
        blob = base64.b64encode(script.encode()).decode()
        body = f"echo {blob} | base64 -d > /tmp/hzkill.sh && sh /tmp/hzkill.sh; rm -f /tmp/hzkill.sh"
        out = self.run(card, body, timeout=timeout)
        return out is not None and "KILLED" in out


def _sq(s):
    """Wrap in single quotes for the REMOTE shell, escaping any single quote already present."""
    return "'" + s.replace("'", "'\\''") + "'"


def probe_all(runner, assignments, reassigned=(), workers=32):
    """Probe every card at once and return {card_id: reply-or-None}.

    ⛔ THE REASSIGNED DIRECTORY IS RESOLVED HERE, LOCALLY. It was once referenced inside the remote
    command as a path on the orchestrator's disk — absent on the pod — so it always fell back to the
    card's first, finished, static chunk log and declared a stall. One chunk bounced across three cards
    in a reassignment loop before that was found.
    """
    body = tip_fleet.probe_body()
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for chunk, card in assignments.items():
            rdir = f"{card.workdir}/re{chunk}" if chunk in reassigned else card.workdir
            futures[pool.submit(runner.run, card, body,
                                {"RDIR": rdir, "CHUNK": str(chunk)})] = card
        for fut in concurrent.futures.as_completed(futures):
            # ⛔ KEYED BY THE CARD OBJECT, not by its id: this map is consumed by `plan_tick`, which
            # looks up with the value held in the assignment map. Keying by anything else makes every
            # lookup miss and every card read UNREACHABLE -- and UNREACHABLE takes no action, so the
            # run would simply sit there.
            card = futures[fut]
            try:
                reply = fut.result()
            except Exception:
                reply = None
            out[card] = (reply or "").strip().splitlines()[-1] if reply else None
    return out
