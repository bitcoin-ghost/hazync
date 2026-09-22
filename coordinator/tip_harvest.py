#!/usr/bin/env python3
"""Pull every card's logs off before the pods die, and read the two numbers nobody has ever captured.

⛔ THE REASON THIS EXISTS. `tip_smoke` releases the fleet in its `finally`, and until now it released
it **without fetching anything**. Every `agg.log`, every `aggw.log` and every `[rtt]` line ever
produced by a tip run was destroyed seconds after the run proved the block. That is why hazync#252
and hazync#253 have both sat open for ten days saying "the instrumentation exists, nobody has run
it" — the instrumentation ran every single time, and the evidence went with the pod.

Checked 2026-09-21: `grep -rl '\\[rtt\\]'` across every surviving run directory on the ops box
(`~/tiprun-mile`, `~/hazync-235-sweep`, `~/hazync-milestone-966256-run4`,
`~/hazync-v0.21.0-release-evidence`) matches **zero files**, ten days after hazync#291 shipped the
emitter. Not one line was ever kept.

⚠ NOTHING HERE TOUCHES THE HOT PATH. An earlier sketch wrapped the aggregate's stdout in a
timestamping shell loop, the way the #235 sweep had to. That is unnecessary now and it is a bad
trade: a broken pipe in that loop SIGPIPEs the aggregate mid-run, so the measurement could kill the
thing it measures. Everything below reads logs the run already writes:

  * `execution {s} s  {n} segments, {mb} MB (streamed)` — the duration is printed outright by
    `seg_serve_cmd` (`prover/host/src/main.rs:6580`), so #253's execution term needs no clock of ours.
  * `seg-connect` leads EVERY line with epoch ms since hazync#254, so a worker's first segment has an
    absolute timestamp with no wrapper.
  * `[rtt] peer=… kind=… rtt_ms=…` is printed by the aggregate (`:6347`), so it lands in `agg.log`.

Read as a library by `tip_smoke`; runnable on a directory of already-fetched logs:

    python3 tip_harvest.py --rundir <dir>           # re-read logs fetched earlier
    python3 tip_harvest.py --selftest               # assertions; exit 0 on success
    python3 tip_harvest.py --selftest --control     # guards removed; MUST fail
"""
import argparse
import json
import os
import re
import sys

# `(streamed)` is the post-#236 spelling; `, depth N` is what a pre-#236 binary printed. Keeping both
# is how a harvested log can SAY which code produced it instead of us assuming the current release.
RE_EXEC = re.compile(
    r"execution\s+([0-9.]+)\s*s\s+(\d+)\s+segments,\s*([0-9.]+)\s*MB\s*(\(streamed\)|,\s*depth\s*\d+)?")
RE_RTT = re.compile(
    r"\[rtt\]\s+peer=(\S+)\s+kind=(\S+)\s+tag=(\S+)\s+rtt_ms=([0-9.]+)\s+bytes_out=(\d+)\s+bytes_in=(\d+)")
# ⚠ ANCHORED AT THE START. `seg-connect` prefixes epoch ms (#254); a line without one is from a
# HAZYNC_SEG_QUIET=1 worker and carries no absolute time, so it must not be read as if it did.
# ⚠ re.M IS LOAD-BEARING. Without it `^` anchors to the start of the whole file, so only a log whose
# very first line was a task could ever match — and `seg-connect`'s first line is always
# "connected to …", so EVERY worker log would have parsed as zero tasks. The self-test caught this.
RE_TASK = re.compile(
    r"^(\d{13})\s+\[(\w+)\]\s+task\s+kind=(\S+)\s+.*?\bwait_s=([0-9.]+)\s+compute_s=([0-9.]+)", re.M)
RE_CONN = re.compile(r"^(\d{13})\s+\[(\w+)\]\s+connected to\s+(\S+)", re.M)

# What to pull off each card. The aggregate and the workers write different files, and `run.log` /
# `prove.log` exist on both. A file that is absent is RECORDED as absent — see `harvest`.
# ⛔ `facts.json` IS ON BOTH LISTS AND IT IS THE ONLY RECORD OF WHERE A CARD PHYSICALLY WAS.
# pod-prove.sh writes it per card with RUNPOD_DC_ID, the GPU's name and UUID, driver, clocks and the
# host CPU. None of it can be reconstructed once the pod is released, and until now none of it was
# fetched -- so every question of the form "was that slow card in a different datacenter?" was
# answerable only by inferring from peer IPs. hazync#448 is exactly that question.
AGG_LOGS = ("agg.log", "agg.err", "run.log", "prove.log", "facts.json")
WORKER_LOGS = ("aggw.log", "aa.log", "run.log", "prove.log", "facts.json")


def parse_execution(text):
    """The `execution` line: duration, segment count, wire bytes, and WHICH binary printed it.

    Returns None when absent, which is itself a result — a run that died before execute finished has
    no execution term, and reporting 0.0 there would be a fabrication.
    """
    m = RE_EXEC.search(text or "")
    if not m:
        return None
    tail = (m.group(4) or "").strip()
    return {"exec_s": float(m.group(1)), "segments": int(m.group(2)), "mb": float(m.group(3)),
            # ⛔ The marker decides which ISSUE a log can speak to. Only a `(streamed)` log is
            # evidence about #236; a `depth N` log predates it and answers a different question.
            "streamed": tail.startswith("(streamed)"),
            "marker": tail or "(none)"}


def parse_rtt(text):
    """Every `[rtt]` line the aggregate printed, as dicts. Empty list means the run recorded none."""
    out = []
    for peer, kind, tag, ms, bo, bi in RE_RTT.findall(text or ""):
        out.append({"peer": peer, "kind": kind, "tag": tag, "rtt_ms": float(ms),
                    "bytes_out": int(bo), "bytes_in": int(bi)})
    return out


def parse_worker(text):
    """A worker's connect time and its task lines, from the epoch ms `seg-connect` already prints."""
    tasks = []
    for epoch, wid, kind, wait_s, compute_s in RE_TASK.findall(text or ""):
        tasks.append({"epoch_ms": int(epoch), "worker": wid, "kind": kind,
                      "wait_s": float(wait_s), "compute_s": float(compute_s)})
    conn = [{"epoch_ms": int(e), "worker": w, "addr": a} for e, w, a in RE_CONN.findall(text or "")]
    return {"connected": conn, "tasks": tasks}


def percentile(xs, p):
    """p in [0,100]. Nearest-rank, so it never interpolates a value that was not measured."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


def critical_path(agg_text, worker_texts, agg_started_epoch_ms=None):
    """#253's question: how much of the execution term leaves the critical path?

    The comparison that answers it is `first segment proved by a REMOTE worker` against
    `execution finished`. If a worker was already proving while execute was still producing, the
    streaming in #236 is doing what it was built to do, and the amount overlapped is the prize.

    ⛔ Returns `overlap_s: None` rather than a number whenever the inputs cannot support one. There
    are three distinct ways to not know, and collapsing them into 0.0 is how an unmeasured thing
    starts getting quoted as measured:
      * no `execution` line   — the run did not reach the end of execute
      * no worker task lines  — nothing attached, so there is nothing to overlap
      * no aggregate start    — we have worker epochs but no epoch to measure them against
    """
    ex = parse_execution(agg_text)
    firsts = []
    for t in worker_texts:
        w = parse_worker(t)
        segs = [x for x in w["tasks"] if x["kind"] == "segment"]
        if segs:
            firsts.append(min(x["epoch_ms"] for x in segs))
    out = {"execution": ex, "workers_with_segments": len(firsts),
           "first_segment_epoch_ms": min(firsts) if firsts else None,
           "agg_started_epoch_ms": agg_started_epoch_ms,
           "overlap_s": None, "basis": None}
    if ex is None:
        out["basis"] = "no execution line — the run did not finish executing"
        return out
    if not firsts:
        out["basis"] = "no worker segment tasks — nothing attached to overlap with"
        return out
    if agg_started_epoch_ms is None:
        out["basis"] = "no aggregate start epoch — worker times have nothing to be measured against"
        return out
    # Seconds from the aggregate starting to the first segment proved anywhere on the fleet.
    t_first = (min(firsts) - agg_started_epoch_ms) / 1000.0
    out["first_segment_after_start_s"] = round(t_first, 3)
    # Positive => a worker was proving while execute was still running: that much of the execution
    # term is off the critical path. Negative => execute finished before anyone attached.
    out["overlap_s"] = round(ex["exec_s"] - t_first, 3)
    out["basis"] = ("execution {:.1f}s vs first remote segment at +{:.1f}s"
                    .format(ex["exec_s"], t_first))
    return out


def harvest(ssh, cards, agg, outdir):
    """Fetch every card's logs into `outdir/logs/<cid>/`. Never raises; teardown must not be blocked.

    ⛔ THIS RUNS BEFORE THE PODS ARE RELEASED, and it is the only chance. Returns a manifest naming
    every file that came back AND every one that did not, because "we fetched nothing" and "there was
    nothing to fetch" are different failures and a silent skip makes them look identical.
    """
    got, missed = {}, {}
    agg_cid = getattr(agg, "cid", None)
    for key, card in cards.items():
        # ⚠ NAMED BY THE CARD, NOT BY THE DICT KEY. `assignment` is {chunk_index: Card}, so keying the
        # output directory on `key` would name every run's logs 0,1,2… and lose which pod they came
        # from. It would also break the aggregate test: comparing a chunk INDEX to `agg.cid` is an
        # int-vs-str compare that is False for ever, so the aggregate would be harvested as a worker
        # and `agg.log` — the only file #252 and #253 need — would never be fetched at all.
        cid = getattr(card, "cid", key)
        names = AGG_LOGS if (card is agg or (agg_cid is not None and cid == agg_cid)) else WORKER_LOGS
        d = os.path.join(outdir, "logs", str(cid))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            missed[str(cid)] = [f"mkdir: {e}"]
            continue
        for n in names:
            local = os.path.join(d, n)
            try:
                ok = ssh.fetch(card, f"{getattr(card, 'workdir', '/workspace')}/{n}", local)
            except Exception as e:                      # a dying pod refuses connections; keep going
                ok, e = False, e
            if ok:
                got.setdefault(str(cid), []).append(n)
            else:
                missed.setdefault(str(cid), []).append(n)
    return {"fetched": got, "missing": missed,
            "cards": len(cards), "files": sum(len(v) for v in got.values())}


def read_logs(rundir):
    """Read back a harvested tree. Returns (agg_text, [worker_text, …]) by which files are present."""
    root = os.path.join(rundir, "logs")
    agg_text, workers = "", []
    if not os.path.isdir(root):
        return agg_text, workers
    for cid in sorted(os.listdir(root)):
        d = os.path.join(root, cid)
        a = os.path.join(d, "agg.log")
        w = os.path.join(d, "aggw.log")
        if os.path.isfile(a):
            try:
                agg_text += open(a, encoding="utf8", errors="replace").read()
            except OSError:
                pass
        if os.path.isfile(w):
            try:
                workers.append(open(w, encoding="utf8", errors="replace").read())
            except OSError:
                pass
    return agg_text, workers


def report(agg_text, worker_texts, agg_started_epoch_ms=None):
    """The block printed at the end of a run and written to `measurements.json`.

    Deliberately says "NOT MEASURED" in words wherever a number is unavailable. These two issues have
    each been re-opened once already because an inference got quoted as a measurement.
    """
    cp = critical_path(agg_text, worker_texts, agg_started_epoch_ms)
    rtts = parse_rtt(agg_text)
    lines = ["── measurements (hazync#252, hazync#253) " + "─" * 38]
    ex = cp["execution"]
    if ex:
        lines.append(f"  #253  execution {ex['exec_s']:.1f} s, {ex['segments']} segments, "
                     f"{ex['mb']:.1f} MB, marker {ex['marker']}")
        if not ex["streamed"]:
            lines.append("        ⚠ NOT a post-#236 binary — this log cannot speak to #253")
    else:
        lines.append("  #253  execution: NOT MEASURED (no execution line in agg.log)")
    if cp["overlap_s"] is None:
        lines.append(f"  #253  overlap: NOT MEASURED — {cp['basis']}")
    else:
        v = cp["overlap_s"]
        # ⚠ Built before the f-string: a newline inside an f-string EXPRESSION is a SyntaxError
        # before Python 3.12, and the coordinator boxes are not all on 3.12.
        verdict = (f"OVERLAP {v:.1f} s off the critical path" if v > 0
                   else f"no overlap ({-v:.1f} s AFTER execute finished)")
        lines.append(f"  #253  first remote segment at +{cp['first_segment_after_start_s']:.1f} s; "
                     f"{verdict}")
        lines.append(f"        basis: {cp['basis']}  ({cp['workers_with_segments']} worker(s) proved segments)")
    if rtts:
        by = {}
        for r in rtts:
            by.setdefault(r["kind"], []).append(r["rtt_ms"])
        lines.append(f"  #252  {len(rtts)} [rtt] samples captured — FIRST TIME ANY RUN HAS KEPT THESE")
        for kind, xs in sorted(by.items()):
            lines.append(f"        {kind:8s} n={len(xs):5d}  p50={percentile(xs,50):8.1f} ms  "
                         f"p90={percentile(xs,90):8.1f} ms  max={max(xs):8.1f} ms")
        peers = sorted({r["peer"] for r in rtts})
        lines.append(f"        peers: {len(peers)}")
    else:
        lines.append("  #252  [rtt]: NOT MEASURED (no [rtt] lines in agg.log)")
    return "\n".join(lines), {"critical_path": cp, "rtt_samples": len(rtts),
                              "rtt": rtts[:2000]}


# ── self-test ────────────────────────────────────────────────────────────────────────────────────
AGG_FIXTURE = """=== segment coordinator (push) — block 741000 chunk 0 po2 21 ===
  streaming segments as they are produced (hazync#235), depth 4
  listening on 0.0.0.0:9110
  execution 40.1 s   585 segments, 299.4 MB (streamed)
  [rtt] peer=10.0.0.2:5 kind=join tag=0x10001 rtt_ms=46.7 bytes_out=1200 bytes_in=900
  [rtt] peer=10.0.0.3:7 kind=join tag=0x10002 rtt_ms=1460.2 bytes_out=1200 bytes_in=900
  [rtt] peer=10.0.0.2:5 kind=resolve tag=0x20001 rtt_ms=812.0 bytes_out=50 bytes_in=60
  joins 529/529
"""
OLD_AGG_FIXTURE = "  execution 42.8 s   585 segments, 299.4 MB, depth 4\n"
# A worker that attached and proved a segment 12 s after the aggregate started, while execute
# (40.1 s) was still running => 28.1 s of the execution term is off the critical path.
W1 = ("1700000000000 [w1] connected to 10.0.0.1:9110\n"
      "1700000012000 [w1] task kind=segment idx=7 recv_ms=1 send_ms=2 wait_s=0.100 compute_s=2.450 "
      "bytes_in=10 bytes_out=20 done=1\n")
W2 = ("1700000000000 [w2] connected to 10.0.0.1:9110\n"
      "1700000030000 [w2] task kind=segment idx=3 recv_ms=1 send_ms=2 wait_s=0.200 compute_s=2.430 "
      "bytes_in=10 bytes_out=20 done=1\n")
QUIET_W = "[w3] task kind=segment idx=1 recv_ms=1 send_ms=2 wait_s=0.0 compute_s=2.3 bytes_in=1 bytes_out=1 done=1\n"


def selftest(control=False):
    fails = []
    if control:
        # ⛔ THE CONTROL REMOVES THE REAL GUARD, it does not merely assert about it. The guard is
        # "a measurement we do not have is None, never 0.0" — so here `critical_path` collapses all
        # three don't-know cases to 0.0 and `report` stops saying NOT MEASURED, exactly the way a
        # careless refactor would. If the assertions below still pass, they were never checking.
        global critical_path, report
        _real_cp, _real_report = critical_path, report

        def critical_path(agg_text, worker_texts, agg_started_epoch_ms=None):   # noqa: F811
            cp = _real_cp(agg_text, worker_texts, agg_started_epoch_ms)
            if cp["overlap_s"] is None:
                cp["overlap_s"] = 0.0
                cp["first_segment_after_start_s"] = 0.0
            return cp

        def report(agg_text, worker_texts, agg_started_epoch_ms=None):          # noqa: F811
            txt, blob = _real_report(agg_text, worker_texts, agg_started_epoch_ms)
            return txt.replace("NOT MEASURED", "0.0"), blob


    def check(ok, what):
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails.append(what)

    ex = parse_execution(AGG_FIXTURE)
    check(ex and abs(ex["exec_s"] - 40.1) < 1e-9 and ex["segments"] == 585, f"execution parsed {ex}")
    check(ex and ex["streamed"] is True, "a `(streamed)` log is recognised as post-#236")
    old = parse_execution(OLD_AGG_FIXTURE)
    check(old is not None and old["streamed"] is False,
          "⛔ a `, depth 4` log parses but is NOT marked streamed — it predates #236 and cannot "
          "speak to #253")
    check(parse_execution("nothing here") is None,
          "an agg.log with no execution line returns None, not a fabricated 0.0")

    r = parse_rtt(AGG_FIXTURE)
    check(len(r) == 3, f"3 [rtt] samples parsed ({len(r)})")
    check(percentile([x["rtt_ms"] for x in r], 50) == 812.0,
          "p50 is a value that was actually measured, not an interpolation")

    w = parse_worker(W1)
    check(len(w["tasks"]) == 1 and w["tasks"][0]["epoch_ms"] == 1700000012000,
          "a worker task line yields an absolute epoch")
    check(parse_worker(QUIET_W)["tasks"] == [],
          "⚠ a HAZYNC_SEG_QUIET worker line carries no epoch and is NOT read as if it did")

    cp = critical_path(AGG_FIXTURE, [W1, W2], 1700000000000)
    check(cp["overlap_s"] is not None and abs(cp["overlap_s"] - 28.1) < 0.01,
          f"⛔ OVERLAP MEASURED: execution 40.1 s, first remote segment +12.0 s => 28.1 s "
          f"off the critical path (got {cp['overlap_s']})")
    check(cp["workers_with_segments"] == 2, "both workers counted")

    # ⛔ The three ways to not know must stay distinct and must NOT become 0.0.
    check(critical_path("", [W1], 1700000000000)["overlap_s"] is None,
          "no execution line => overlap is None, not 0.0")
    check(critical_path(AGG_FIXTURE, [], 1700000000000)["overlap_s"] is None,
          "no worker tasks => overlap is None, not 0.0")
    check(critical_path(AGG_FIXTURE, [W1], None)["overlap_s"] is None,
          "⛔ no aggregate start epoch => overlap is None — worker epochs alone cannot say")

    # ── harvest picks the right file list per card ───────────────────────────────────────────────
    # ⛔ THE AGGREGATE MUST BE RECOGNISED. `assignment` is {chunk_index: Card}, and an earlier draft
    # compared that INTEGER key to `agg.cid` — false for ever, so the aggregate would have been
    # harvested as a worker and `agg.log`, the only file these two issues need, never fetched.
    import tempfile

    class _Card:
        __slots__ = ("cid", "workdir")

        def __init__(self, cid):
            self.cid, self.workdir = cid, "/workspace"

    class _SSH:
        def __init__(self):
            self.asked = []

        def fetch(self, card, remote, local):
            self.asked.append((card.cid, os.path.basename(remote)))
            if os.path.basename(remote) in ("agg.err", "prove.log"):
                return False                       # a file that does not exist on this card
            with open(local, "w", encoding="utf8") as fh:
                fh.write("x")
            return True

    tmp = tempfile.mkdtemp(prefix="harv_")
    A, B = _Card("hz-agg"), _Card("hz-w1")
    sshm = _SSH()
    man = harvest(sshm, {0: A, 1: B}, A, tmp)
    asked_agg = {n for c, n in sshm.asked if c == "hz-agg"}
    asked_w = {n for c, n in sshm.asked if c == "hz-w1"}
    check("agg.log" in asked_agg,
          "⛔ the AGGREGATE is asked for agg.log — keyed on the card, not the chunk index")
    check("aggw.log" in asked_w and "agg.log" not in asked_w,
          "a WORKER is asked for aggw.log and not agg.log")
    check(man["missing"].get("hz-agg") and "agg.err" in man["missing"]["hz-agg"],
          "⚠ a file that did not come back is RECORDED as missing, not silently skipped")
    check(os.path.isdir(os.path.join(tmp, "logs", "hz-agg")),
          "logs are named by pod, not by chunk index")

    txt, blob = report(AGG_FIXTURE, [W1, W2], 1700000000000)
    check("[rtt] samples captured" in txt and blob["rtt_samples"] == 3, "report names the rtt capture")
    txt2, _ = report("", [], None)
    check("NOT MEASURED" in txt2,
          "⛔ a run that captured nothing SAYS 'NOT MEASURED' rather than printing zeros")

    print()
    expected = {"no aggregate start epoch", "NOT MEASURED"}
    if control:
        hit = {e for e in expected if any(e in f for f in fails)}
        if hit:
            print("CONTROL OK — the guards were removed and the assertions that detect it failed:")
            for e in sorted(hit):
                print(f"  - {e}")
            return 0
        print("CONTROL FAILED — a missing measurement went undetected and would read as 0.0.")
        return 1
    if fails:
        print(f"FAILED {len(fails)}: " + "; ".join(fails))
        return 1
    print("all good")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rundir")
    ap.add_argument("--agg-start-ms", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--control", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest(control=a.control)
    if not a.rundir:
        ap.error("--rundir or --selftest")
    agg_text, workers = read_logs(a.rundir)
    txt, blob = report(agg_text, workers, a.agg_start_ms)
    print(txt)
    out = os.path.join(a.rundir, "measurements.json")
    with open(out, "w", encoding="utf8") as fh:
        json.dump(blob, fh, indent=1)
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
