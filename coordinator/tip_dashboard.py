#!/usr/bin/env python3
"""The feed between a live fleet and the dashboard in `tools/live/` (Phase 5 ⑨).

The dashboard already exists — it is the renderer behind the 24-hour films, and it is driven entirely
by files:

    $RUNDIR/pods.txt            id name ip port cost gpu      (one line per card)
    $RUNDIR/stream/<name>.csv   1 Hz telemetry, written by tools/live/tip-stream.sh
    $RUNDIR/t0                  the epoch the run declares as its start

So "wiring the dashboard to the runner" is: write `pods.txt`, write `t0` at the moment the clock
starts, and start `tip-stream.sh`. That is all this module does. It computes the file CONTENTS as pure
functions so every trap below is testable without a card, and leaves the writing and the subprocess to
the caller.

⛔ EVERY FAILURE IN THIS FILE IS SILENT. Not one of them raises anywhere: the dashboard renders, the
numbers look plausible, and they are wrong. That is the whole reason this is a module with tests rather
than an f-string at a call site.

  * `collect.py:read_pods` keeps a line only `if len(f) >= 6`. A card with no GPU string emits five
    fields and is **dropped without a word** — it never appears on the dashboard, its cost is never
    counted, and the fleet silently reads as smaller than it is.
  * `pods.txt` is WHITESPACE-SPLIT and the GPU name is the LAST field. `NVIDIA RTX 4090` is three
    fields, so the line has seven, `len(f) >= 6` passes, and `gpu` is read as `NVIDIA` while the real
    name is discarded. `read_pods` un-escapes `_` to a space, so the GPU must be written escaped.
  * `float(f[4])` is UNGUARDED. One card with a missing or non-numeric price raises inside
    `read_pods`, which is called from the collector's main loop — the whole dashboard stops updating,
    for every card, because of one bad field.
  * The card name is field 1 AND the stream filename (`stream/<name>.csv`). A name with whitespace in
    it splits into two fields and shifts every field after it; a name with a `/` writes outside the
    stream directory.
  * `t0` is what the dashboard's elapsed clock counts from. Writing it during the fleet clear
    back-dates the run by the whole of phase 0 and flatters every per-block figure — the same mistake
    the runner refuses to make in its own timing.
"""

import os
import re

# The dashboard keys a card by `name`, and that name is also a path component.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# A complete telemetry line, per tip-stream.sh:
#   epoch,util,mem,temp,power_w,sm,mem_clk,phase,seg_n,seg_total,block
# collect.py drops anything shorter, and so must we -- see last_epochs().
STREAM_FIELDS = 11


class FeedRefused(ValueError):
    """A card cannot be represented in pods.txt without corrupting it. Fix the input, not this."""


def escape_gpu(gpu):
    """`NVIDIA RTX 4090` -> `NVIDIA_RTX_4090`, which `read_pods` un-escapes back to spaces.

    Collapses runs of whitespace so a double space cannot produce an empty field.
    """
    return "_".join(str(gpu).split())


def pods_line(cid, ip, port, *, price, gpu, pod_id=None):
    """One `pods.txt` line: `id name ip port cost gpu`.

    Every field is validated, because the reader validates none of them.
    """
    name = str(cid)
    if not _SAFE_NAME.match(name):
        raise FeedRefused(
            f"card name {name!r} is not safe as a pods.txt field and a filename — it is field 1 of a "
            f"whitespace-split line AND becomes stream/{name}.csv")

    gpu_s = escape_gpu(gpu or "")
    if not gpu_s:
        # ⛔ THE SILENT DROP. Five fields fails `len(f) >= 6` and read_pods skips the line entirely.
        raise FeedRefused(
            f"card {name} has no GPU string — a 5-field line is dropped by collect.py without any "
            f"error, so the card would vanish from the dashboard and from the cost")

    try:
        price_f = float(price)
    except (TypeError, ValueError):
        raise FeedRefused(
            f"card {name} has a non-numeric price {price!r} — collect.py calls float() on this field "
            f"unguarded, so one bad card stops the dashboard updating for ALL of them") from None
    if price_f < 0:
        raise FeedRefused(f"card {name} has a negative price {price_f}")

    ip_s, port_s = str(ip).strip(), str(int(port))
    if not ip_s or len(ip_s.split()) != 1:
        raise FeedRefused(f"card {name} has an unusable address {ip!r}")

    # The id is only ever echoed back; a missing one must still occupy its field.
    pid = escape_gpu(pod_id) if pod_id else "-"
    return f"{pid} {name} {ip_s} {port_s} {price_f:.4f} {gpu_s}"


def pods_txt(cards):
    """Render the whole file. `cards` is an iterable of dicts with cid/ip/port/price/gpu[/pod_id].

    ⛔ ALL OR NOTHING. A partial pods.txt is worse than none: the run proceeds, the dashboard shows a
    fleet smaller than the one being paid for, and the straggler and cost are computed against it.
    Raising here stops the run before it spends anything.
    """
    seen, lines = set(), []
    for c in cards:
        cid = c["cid"]
        if cid in seen:
            raise FeedRefused(
                f"duplicate card name {cid!r} — the second would overwrite the first's stream file and "
                f"two cards would render as one")
        seen.add(cid)
        lines.append(pods_line(cid, c["ip"], c["port"],
                               price=c.get("price"), gpu=c.get("gpu"), pod_id=c.get("pod_id")))
    return "".join(l + "\n" for l in lines)


def parse_pods_txt(text):
    """Exactly what `collect.py:read_pods` does — kept here so a test can prove the round trip.

    ⚠ This deliberately reproduces the reader's leniency, INCLUDING the silent skip of a short line.
    It is here to demonstrate the trap, not to be lenient on our behalf.
    """
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 6:
            # ⚠ `role` MIRRORS collect.py: the FIRST line is the coordinator, because tip_smoke writes
            # pods.txt from `order` and `agg = order[0]`. This mirror exists to prove we match the real
            # reader field for field, so a field added there must be added here.
            out[f[1]] = {"pod": f[0], "ip": f[2], "port": f[3],
                         "cost_hr": float(f[4]), "gpu": f[5].replace("_", " "),
                         "role": "coordinator" if not out else "worker"}
    return out


def write_feed(rundir, cards):
    """Write `pods.txt` atomically and make sure `stream/` exists. Returns the number of cards."""
    text = pods_txt(cards)                      # raises BEFORE anything is written
    os.makedirs(os.path.join(rundir, "stream"), exist_ok=True)
    path = os.path.join(rundir, "pods.txt")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)                        # never a half-written pods.txt under a live collector
    return len(text.splitlines())


def write_t0(rundir, t, once=True):
    """Declare the SESSION's start. Returns the epoch now in force.

    ⛔ t0 IS THE SESSION, NOT THE BLOCK, AND MOVING IT DESTROYS THE HISTORY. `collect.py`'s
    `blocks_from_cards` drops every height that began before t0:

        if since and e["t0"] < since: continue

    so advancing t0 at the start of each block deletes every block already proved in that session.
    Measured against the real collector on a two-block capture: t0=1000 yields blocks [100, 101],
    t0=1020 yields [101]. A 24-hour session would show "1 block today" for its whole duration, the
    time-per-block chart would hold a single bar, and the cost would only ever cover the current
    block — all of it looking perfectly healthy.

    So this is WRITE-ONCE by default: the first block of a session sets it, every later block leaves
    it alone. `DashboardFeed.start()` is the session boundary and clears it.

    ⚠ The filter is also why t0 must not be earlier than the real start: a run inherits the previous
    occupant's log lines, and a t0 before them counts their blocks as this session's.
    """
    path = os.path.join(rundir, "t0")
    if once and os.path.exists(path):
        try:
            with open(path) as fh:
                return float(fh.read().strip())
        except (OSError, ValueError):
            pass                      # unreadable: fall through and rewrite it
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(f"{float(t):.3f}\n")
    os.replace(tmp, path)
    return float(t)


def write_phase(rundir, text):
    """Say what the run is doing, for the frame's status tile.

    ⛔ A LIVE FLEET DOING NOTHING LOOKS EXACTLY LIKE A DEAD FEED. Preparing a card means staging a
    fixture and pulling a 407 MB prover: minutes of flat traces and an empty dial, which on the frame
    is indistinguishable from an idle fleet or a broken collector. The question "why is the dashboard
    empty" was asked three times in one evening and the answer was "it is preparing" every time.

    Written atomically, because the collector reads it once a second and a half-written line would
    render as a half-written line.
    """
    path = os.path.join(rundir, "phase")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(str(text).strip()[:80] + "\n")
    os.replace(tmp, path)
    return path


def stream_cmd(rundir, action, *, script, key=None, log_dir=None):
    """argv for `tip-stream.sh {start,stop,status}`.

    ⛔ argv, NEVER a shell string — the same rule as tip_driver. A rundir with a space in it would
    otherwise become two arguments and the streamer would write somewhere else entirely.
    """
    if action not in ("start", "stop", "status"):
        raise FeedRefused(f"unknown tip-stream action {action!r}")
    env = {"HAZYNC_RUNDIR": rundir}
    if key:
        env["HAZYNC_SSH_KEY"] = key
    if log_dir:
        env["LOG_DIR"] = log_dir
    return [script, action], env


def last_epochs(rundir, names):
    """`{name: last epoch seen}` from the stream files, None where a card has produced nothing.

    ⚠ Reads only the tail. These files grow at 1 line/sec/card for the length of a 24-hour run — a
    30-card fleet is ~2.6 million lines — and reading them whole on every tick would make the check
    cost more than the thing it is checking.
    """
    out = {}
    for name in names:
        path = os.path.join(rundir, "stream", f"{name}.csv")
        ts = None
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                back = min(4096, fh.tell())
                fh.seek(-back, os.SEEK_END)
                tail = fh.read().decode("utf8", "replace").splitlines()
            for line in reversed(tail):
                # ⛔ `float()` ALONE DOES NOT DETECT A TORN LINE. tip-stream.sh appends over a
                # reconnecting ssh, so the tail can end mid-write — and a truncated epoch like
                # `17899187` is a perfectly valid float. It parses, it is accepted, and the card
                # reads as decades stale while streaming perfectly. Judge the line by its FIELD
                # COUNT, which is what collect.py itself does (`if len(f) < 11: continue`).
                f = line.split(",")
                if len(f) < STREAM_FIELDS:
                    continue
                try:
                    ts = float(f[0].strip())
                    break
                except ValueError:
                    continue          # a torn line whose commas survived
        except OSError:
            ts = None                 # no file at all: the card was never streamed
        out[name] = ts
    return out


def staleness(rows, now, limit_s=15.0):
    """Which cards have stopped producing telemetry, from `{name: last_epoch}`.

    ⛔ A CARD THAT STOPPED STREAMING STILL RENDERS. `tip-stream.sh` reconnects on its own and the gap
    shows in the data as missing seconds — which is honest, but on the frame a frozen trace looks like
    a quiet card, not a broken feed. The run should be told the difference.

    A card with NO rows at all is `never`, not `stale`: it has not been observed once, which usually
    means it is missing from pods.txt rather than that its stream died.
    """
    stale, live, never = [], [], []
    for name, last in sorted(rows.items()):
        if last is None:
            never.append(name)
        elif now - float(last) > limit_s:
            stale.append(name)
        else:
            live.append(name)
    return {"live": live, "stale": stale, "never": never,
            "ok": not stale and not never}


class DashboardFeed:
    """The live half: writes the files, runs the streamer, and reports whether telemetry is arriving.

    ⛔ THE STREAMER MUST BE RUNNING BEFORE THE CLOCK STARTS. Per-card telemetry cannot be
    reconstructed once a pod is gone (`tools/milestone/README.md`), so a run that starts streaming at
    T0 has already lost the launch, and one that starts it later has a dashboard that begins mid-run
    with no way to fill the gap. `start()` is called before phase 0; `mark_t0()` at phase 1.

    ⚠ A DEAD FEED NEVER FAILS A RUN. The receipt is the product; the dashboard is a view of it. When
    telemetry is missing this records an event and carries on, rather than throwing away a fleet that
    is proving perfectly well because a screen is blank.
    """

    def __init__(self, rundir, *, script, run, key=None, log_dir=None):
        self.rundir, self.script, self.run = rundir, script, run
        self.key, self.log_dir = key, log_dir
        self.names = []

    def start(self, cards):
        """Begin a SESSION: write pods.txt, clear any previous t0, and start the streamer.

        ⛔ Clearing t0 here is what makes `mark_t0` write-once safe. Without it a new session would
        inherit the last one's t0 and count its blocks as this session's.
        """
        n = write_feed(self.rundir, cards)
        # ⚠ AFTER write_feed, NEVER BEFORE. write_feed validates every card and RAISES on a bad
        # one (pods_txt is all-or-nothing), so stopping first meant a REFUSED feed killed the
        # streamer that was running perfectly well -- caught by test_tip_dashboard's "a refused
        # feed never starts the streamer".
        # ⛔ STOP WHATEVER IS ALREADY STREAMING FIRST (hazync#500). `start` never did, and a run
        # calls it more
        # than once -- once when the fleet is rented, again after the gates pick the final cards
        # ("feed rewritten for N cards"). Both streamer sets then ran against the SAME csv files for
        # the rest of the run, each appending its own 1 Hz line.
        #
        # Measured on the 968,243 run: the log shows `streaming 18 pods` then `streaming 16 pods`,
        # and the coordinator's capture held 5,356 rows across 2,860 s -- 1.87 rows/sec, not 1.
        # Nothing on the frame said so. It also silently doubles capture growth, which on a 24 h run
        # is the thing StreamCursor exists to keep ahead of.
        #
        # ⚠ Anything that counts rows as seconds is wrong by that factor, which is exactly how the
        # behind-the-chain figure came to report 4,845s for a block that had run 2,560s.
        try:
            self.stop()
        except Exception:
            pass                      # nothing was streaming yet, which is the normal first call

        try:
            os.remove(os.path.join(self.rundir, "t0"))
        except OSError:
            pass                      # no previous session

        # ⛔ A STREAM FILE FOR A CARD THAT IS NOT IN THIS RUN IS A PHANTOM CARD. collect.py walks
        # `stream/*.csv`, not pods.txt, so any leftover .csv becomes a card on the frame -- with no
        # pods.txt entry it reads gpu "?" and **cost_hr 0.0**, so the fleet count is too high and the
        # spend is too low, both silently.
        #
        # It is not hypothetical: `tip-stream.sh stop` kills only the PIDs recorded in the rundir, so
        # a streamer orphaned by a lost pid file (or a rundir deleted between runs) keeps looping
        # against a terminated pod and RECREATES its file. Measured 2026-09-20: two orphaned
        # subshells from an earlier run put an empty `hz-smoke-2.csv` back into a fresh rundir and the
        # renderer drew "3 cards" for a two-card fleet.
        keep = {str(c["cid"]) + ".csv" for c in cards}
        sdir = os.path.join(self.rundir, "stream")
        try:
            stale = [f for f in os.listdir(sdir) if f.endswith(".csv") and f not in keep]
        except OSError:
            stale = []
        for f in stale:
            try:
                os.remove(os.path.join(sdir, f))
            except OSError:
                pass
        self.stale_removed = sorted(stale)
        self.names = [c["cid"] for c in cards]
        argv, env = stream_cmd(self.rundir, "start", script=self.script,
                               key=self.key, log_dir=self.log_dir)
        self.run(argv, env)
        return n

    def mark_t0(self, t):
        """Declare the session clock at the first block's T0. Write-once — see write_t0()."""
        return write_t0(self.rundir, t, once=True)

    def staleness(self, now, limit_s=15.0):
        return staleness(last_epochs(self.rundir, self.names), now, limit_s=limit_s)

    def stop(self):
        argv, env = stream_cmd(self.rundir, "stop", script=self.script,
                               key=self.key, log_dir=self.log_dir)
        return self.run(argv, env)


def feed_records(cards, fleet):
    """Join the two halves of a card into the dicts `pods_txt` consumes.

    A card exists twice in this codebase and neither half is sufficient alone:

        tip_driver.Card      cid, ip, port        -- how to REACH it
        the lifecycle dict   id, price, gpu       -- what it COSTS and what it is

    `cards` is an iterable of anything with `.cid`, `.ip` and `.port`; `fleet` is the lifecycle dicts.
    Returns `{"records": [...], "unassigned": [...]}`.

    ⛔ A CARD WITH NO FLEET ENTRY IS REFUSED, NOT DEFAULTED. The tempting fallbacks are both silent
    and both wrong: `price=0.0` under-reports the spend on a run that has NO budget cap by decision,
    and dropping the card removes it from the dashboard while it goes on proving and go on being
    billed. Either way the money on the frame is not the money being spent.

    ⚠ `unassigned` is a fleet entry with no card in this run — a spare, or one dropped by
    `keep_and_release`. It is REPORTED rather than refused, because holding spares is legitimate, but
    it is never silent: those pods are being paid for and will not appear in the dashboard's cost.
    """
    by_id = {}
    for f in fleet or ():
        fid = f.get("id")
        if fid is not None:
            by_id[str(fid)] = f

    records, matched = [], set()
    for c in cards:
        cid = str(getattr(c, "cid", c))
        f = by_id.get(cid)
        if f is None:
            raise FeedRefused(
                f"card {cid} is in the run but not in the fleet list, so it has no price and no GPU "
                f"name. Defaulting the price to 0 would under-report a run that has no budget cap, "
                f"and dropping the card would hide a pod that is proving and being billed")
        matched.add(cid)
        records.append({"cid": cid, "ip": getattr(c, "ip", None), "port": getattr(c, "port", None),
                        "price": f.get("price"), "gpu": f.get("gpu"), "pod_id": f.get("pod_id") or cid})

    return {"records": records,
            "unassigned": sorted(set(by_id) - matched)}
