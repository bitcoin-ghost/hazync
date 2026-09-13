#!/usr/bin/env python3
"""
The sponsor proving bot (sponsor_bot.py): money safety first, then the queue and the cost log.

WHY THIS EXISTS. The bot rents GPUs. A bug that leaves a pod running, launches past a cap or proves an
unpaid sponsorship spends real money and nothing on the board would show it. So the tests put the bot's
REAL RunPod client against a fake GraphQL server over HTTP, and a fake pod runner that "proves" blocks by
writing verified proofs into a real coordinator database, and check what was deployed and terminated.

WHAT THIS DOES NOT COVER: the real RunPod API, SSH, the pod boot script and real proving (SshRunner is
not exercised). Time runs 3,600 times faster than the wall clock, so a real second is an hour of pod time.

Usage:
  python3 test_sponsor_bot.py            # assertions; exit 0 on success
  python3 test_sponsor_bot.py --control  # pods are NOT terminated when the bot errors; these tests MUST fail
"""
import http.server
import json
import os
import re
import signal
import sqlite3
import sys
import tempfile
import threading
import time

CONTROL = "--control" in sys.argv

os.environ["COORD_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import sponsor_bot  # noqa: E402

server.init_db()
DB = os.environ["COORD_DB"]
if CONTROL:
    sponsor_bot._CONTROL_SKIP_CLEANUP_ON_ERROR = True
    print("CONTROL: pods are not terminated when the bot errors -- the checks below MUST fail")

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


# ---------- fakes ----------

class FakeRunPod:
    """api.runpod.io/graphql over real HTTP, so the bot's own RunPod client is what gets tested."""

    def __init__(self):
        self.pods = {}
        self.prices = {"NVIDIA GeForce RTX 4090": 0.34, "NVIDIA A40": 0.49}
        self.capacity = {"NVIDIA GeForce RTX 4090": True, "NVIDIA A40": True}
        self.deploys, self.terminations = [], []
        self.max_live, self.n = 0, 0
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = json.dumps(fake.handle(body["query"], self.headers.get("Authorization"))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/graphql"

    def live(self):
        return [i for i, p in self.pods.items() if p["alive"]]

    def deployed(self):
        return {d[0] for d in self.deploys}

    def handle(self, q, auth):
        with self.lock:
            if auth != "Bearer test-key":
                return {"errors": [{"message": "unauthorized"}], "data": None}
            if "podFindAndDeployOnDemand" in q:
                gt = re.search(r'gpuTypeId: "([^"]+)"', q).group(1)
                name = re.search(r'name: "([^"]+)"', q).group(1)
                if not self.capacity.get(gt):
                    return {"data": {"podFindAndDeployOnDemand": None}}
                self.n += 1
                pid = f"pod{self.n}"
                self.pods[pid] = {"name": name, "gpu": gt, "cost": self.prices[gt], "alive": True}
                self.deploys.append((pid, name, gt))
                self.max_live = max(self.max_live, len(self.live()))
                return {"data": {"podFindAndDeployOnDemand": {"id": pid, "costPerHr": self.prices[gt]}}}
            if "podTerminate" in q:
                pid = re.search(r'podId: "([^"]+)"', q).group(1)
                self.terminations.append(pid)
                if pid in self.pods:
                    self.pods[pid]["alive"] = False
                return {"data": {"podTerminate": None}}
            if "myself" in q:
                return {"data": {"myself": {"pods": [
                    {"id": i, "name": p["name"], "costPerHr": p["cost"], "machine": {"gpuDisplayName": p["gpu"]},
                     "runtime": {"ports": [{"ip": "127.0.0.1", "isIpPublic": True, "privatePort": 22, "publicPort": 22022}]}}
                    for i, p in self.pods.items() if p["alive"]]}}}
            return {"errors": [{"message": "unknown query"}], "data": None}


class FakeRunner:
    """A pod that proves its assigned blocks by writing verified proofs into the coordinator database."""
    ssh_pubkey = "ssh-ed25519 AAAAFAKE sponsor-bot@test"

    def __init__(self, per_block=0.01, boot_fail=(), stall=(), raise_on_start=False):
        self.per_block, self.boot_fail, self.stall = per_block, set(boot_fail), set(stall)
        self.raise_on_start = raise_on_start
        self.booted, self.started, self.threads, self.status_at_start = [], [], {}, {}
        self.lock = threading.Lock()

    def nth(self, pod):
        return self.booted.index(pod.id) + 1

    def boot(self, pod):
        with self.lock:
            self.booted.append(pod.id)
            n = self.nth(pod)
        return (False, "GPU_BAD no CUDA device") if n in self.boot_fail else (True, "SHA_OK GPU_OK MID ok")

    def start(self, pod, heights):
        if self.raise_on_start:
            raise RuntimeError("simulated failure while starting work")
        self.started.append((pod.id, list(heights)))
        c = sqlite3.connect(DB, timeout=30)
        self.status_at_start.setdefault(pod.id, []).append(sorted({r[0] for r in c.execute(
            "SELECT s.status FROM sponsor_work w JOIN sponsorships s ON s.id=w.sponsorship_id"
            " WHERE w.pod_id=? AND w.outcome IS NULL", (pod.id,))}))
        c.close()
        if self.nth(pod) in self.stall:
            return

        def work():
            for h in heights:
                time.sleep(self.per_block)
                if pod.terminated is not None:
                    return
                c = sqlite3.connect(DB, timeout=30)
                c.execute("INSERT OR IGNORE INTO vranges(id, lo, hi, pubkey, handle, ts) VALUES(?,?,?,?,?,?)",
                          (f"bot-{h}", h, h, "b0t", "hazync sponsor", time.time()))
                c.commit()
                c.close()
        t = threading.Thread(target=work, daemon=True)
        t.start()
        self.threads[pod.id] = t

    def status(self, pod):
        t = self.threads.get(pod.id)
        return "finished" if (t is not None and not t.is_alive()) else "running"


def fast_clock(speed=3600.0):
    t0 = time.time()
    return lambda: t0 + (time.time() - t0) * speed


def make_bot(api, runner, **kw):
    args = dict(max_pods=2, max_usd=1000.0, max_usd_per_hour=10.0, pod_price_ceiling=1.0,
                stall_s=3 * 3600, ssh_timeout_s=10 * 3600, boot_timeout_s=10 * 3600, fail_grace_s=2 * 3600,
                blocks_per_pod=5, sleep_s=0.01, budget_lead_s=0, capacity_backoff_s=60, confirm_wait_s=0.01,
                clock=fast_clock(), log=lambda m: None)
    args.update(kw)
    return sponsor_bot.Bot(DB, sponsor_bot.RunPod("test-key", url=api.url, timeout=10), runner, **args)


def q(sql, args=()):
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def reset():
    c = sqlite3.connect(DB, timeout=30)
    for t in ("sponsorships", "vranges", "ranges", "sponsor_work", "sponsor_pods"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    c.close()


def sponsor(lo, hi, status="paid", paid_at=100.0, min_sats=1000, paid_sats=1000, name="Sponsor"):
    c = sqlite3.connect(DB, timeout=30)
    cur = c.execute("INSERT INTO sponsorships(lo,hi,name,status,created_at,min_usd,min_sats,paid_sats,paid_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", (lo, hi, name, status, 1.0, 1, min_sats, paid_sats, paid_at))
    c.commit()
    sid = cur.lastrowid
    c.close()
    return sid


def proof(lo, hi, who="someone"):
    q("INSERT INTO vranges(id, lo, hi, pubkey, handle, ts) VALUES(?,?,?,?,?,?)",
      (f"{who}-{lo}-{hi}", lo, hi, who, who, time.time()))


def status_of(sid):
    return q("SELECT status FROM sponsorships WHERE id=?", (sid,))[0]["status"]


# ---------- live mode refuses without caps; plan touches nothing ----------
print("== live mode needs every cap; plan is a dry run ==")
check(not q("SELECT name FROM sqlite_master WHERE name='sponsor_work'"), "a fresh database has no sponsor_work table")
check(sponsor_bot.main([]) == 0, "plan (the default) runs")
check(not q("SELECT name FROM sqlite_master WHERE name='sponsor_work'"), "plan wrote nothing to the database")
for argv, why in ((["run", "--live"], "no caps"),
                  (["run", "--live", "--max-pods", "1", "--max-usd", "5"], "no hourly cap"),
                  (["trial", "--blocks", "5", "--live", "--max-pods", "1", "--max-usd-per-hour", "1"], "a trial with no total cap"),
                  (["--live"], "the old skeleton's flag")):
    try:
        sponsor_bot.main(argv)
        code, msg = 0, ""
    except SystemExit as e:
        code, msg = e.code, str(e.code)
    check(code not in (0, None) and "--max" in msg, f"live mode refuses to start: {why}")
for kw, why in ((dict(max_pods=0), "zero pods"), (dict(max_usd=0), "a zero total cap"),
                (dict(max_usd_per_hour=0.5, pod_price_ceiling=1.0), "a pod ceiling above the hourly cap")):
    args = dict(max_pods=1, max_usd=5.0, max_usd_per_hour=2.0)
    args.update(kw)
    try:
        sponsor_bot.Bot(DB, None, None, **args)
        refused = False
    except sponsor_bot.BotRefused:
        refused = True
    check(refused, f"the bot refuses {why}")

# ---------- the queue: only holds, oldest payment first ----------
print("== the queue ==")
reset()
a = sponsor(10, 11, paid_at=200)
b = sponsor(20, 21, paid_at=100)
f = sponsor(30, 30, status="proving", paid_at=150)
sponsor(40, 41, status="requested", paid_sats=None, paid_at=None)
sponsor(50, 51, status="underpaid", paid_sats=999, paid_at=50)
sponsor(60, 61, status="paid", paid_sats=999, paid_at=10)
c = sponsor_bot.connect(DB)
got = sponsor_bot.pending_work(c)
check(got == [(b, 20), (b, 21), (f, 30), (a, 10), (a, 11)],
      f"held sponsorships only, oldest payment first; requested, underpaid and below-minimum are never worked ({got})")
proof(21, 21)
got = sponsor_bot.pending_work(c, busy={10})
check(got == [(b, 20), (f, 30), (a, 11)], f"proven and busy heights are skipped ({got})")
check([r["id"] for r in sponsor_bot.queue(DB)] == [b, f, a], "queue() lists the same holds, for test_block_api's plan")
c.close()

# ---------- reconcile ----------
print("== reconcile ==")
reset()
s = sponsor(70, 72)
r_ = sponsor(80, 80, status="requested", paid_sats=None, paid_at=None)
u = sponsor(90, 90, status="underpaid", paid_sats=999)
part = sponsor(100, 102)
proof(70, 71, "alice")
proof(72, 72, "bob")
proof(80, 80)
proof(90, 90)
proof(100, 101)
c = sponsor_bot.connect(DB)
done = sponsor_bot.reconcile(c)
c.close()
check(done == [s] and status_of(s) == "proven" and q("SELECT proven_at FROM sponsorships WHERE id=?", (s,))[0]["proven_at"],
      "a held span covered by proofs from anyone is marked proven")
check(status_of(r_) == "requested" and status_of(u) == "underpaid", "requested and underpaid rows are never marked proven")
check(status_of(part) == "paid", "a span only partly covered stays held")

# ---------- trial ----------
print("== trial ==")
reset()
proof(500, 500)
now = time.time()
q("INSERT INTO ranges(id, lo, hi, status, assignee, claimed_at) VALUES('501',501,501,'claimed','p',?)", (now,))
q("INSERT INTO ranges(id, lo, hi, status, assignee, claimed_at) VALUES('504',504,504,'claimed','p',?)", (now - 7200,))
sponsor(502, 503)
c = sponsor_bot.connect(DB)
bad = sponsor_bot.trial_refusals(c, [500, 501, 502, 503, 504, 505])
c.close()
check(len(bad) == 4 and all(any(str(h) in m for m in bad) for h in (500, 501, 502, 503)),
      f"a trial refuses proven, live-claimed and held blocks, and allows a stale claim and an open block ({bad})")
check(sponsor_bot.parse_blocks("100000,150000-150004") == [100000, 150000, 150001, 150002, 150003, 150004],
      "trial blocks parse as heights and ranges")
for spec in ("5-3", "abc", "0", f"1-{sponsor_bot.TRIAL_MAX + 1}"):
    try:
        sponsor_bot.parse_blocks(spec)
        ok = False
    except ValueError:
        ok = True
    check(ok, f"a bad trial spec is refused: {spec!r}")
api = FakeRunPod()
try:
    make_bot(api, FakeRunner(), trial=[500, 505]).run()
    refused = False
except sponsor_bot.BotRefused:
    refused = True
check(refused and not api.deploys, "a trial with a refused block rents nothing")
reset()
api = FakeRunPod()
tbot = make_bot(api, FakeRunner(), trial=[505, 506], max_pods=1)
check(tbot.run() == "done", "a trial proves its blocks")
rows = q("SELECT sponsorship_id, height, outcome FROM sponsor_work ORDER BY height")
check([(r["sponsorship_id"], r["height"], r["outcome"]) for r in rows] == [(None, 505, "proven"), (None, 506, "proven")],
      "trial blocks are logged with no sponsorship")

# ---------- a run: max pods, proving on start, the cost log, every pod terminated ----------
print("== a run ==")
reset()
api = FakeRunPod()
runner = FakeRunner(per_block=0.01)
s1 = sponsor(1000, 1011, paid_at=100)
s2 = sponsor(2000, 2003, paid_at=200)
bot = make_bot(api, runner, max_pods=2, blocks_per_pod=4)
check(bot.run() == "done", "the run ends when every held block is proven")
check(api.max_live <= 2 and len(api.deploys) >= 2, f"never more than --max-pods live at once (max {api.max_live})")
check(api.deployed() <= set(api.terminations) and not api.live(), "every pod is terminated at the end")
check(status_of(s1) == "proven" and status_of(s2) == "proven", "both sponsorships end proven")
check(runner.started and runner.started[0][1] == [1000, 1001, 1002, 1003], "the oldest payment's blocks go first")
starts = [st for sts in runner.status_at_start.values() for st in sts if st]
check(starts and all(st == ["proving"] for st in starts), f"a sponsorship is `proving` before its pod starts ({starts})")
work = q("SELECT * FROM sponsor_work")
check(len(work) == 16 and all(w["outcome"] == "proven" and w["seconds"] > 0 for w in work),
      f"one proven work row per block, with its time ({len(work)} rows)")
check(all(abs(w["usd_estimate"] - w["seconds"] * w["cost_per_hr"] / 3600) < 1e-9 for w in work),
      "each block's cost is its time at its pod's price")
pods = q("SELECT * FROM sponsor_pods")
check(pods and all(p["terminated_at"] and p["usd_estimate"] > 0 for p in pods), "every pod is logged with its spend")

# ---------- the hourly cap, a GPU fallback, and the price ceiling ----------
print("== the hourly cap ==")
reset()
api = FakeRunPod()
api.capacity["NVIDIA GeForce RTX 4090"] = False
sponsor(3000, 3029)
bot = make_bot(api, FakeRunner(per_block=0.01), max_pods=5, max_usd_per_hour=1.0, pod_price_ceiling=0.5)
bot.run()
check(api.max_live == 2, f"the hourly cap allows two $0.49/h pods under $1.00/h with a $0.50 ceiling, not five ({api.max_live})")
check(api.deploys and all(d[2] == "NVIDIA A40" for d in api.deploys), "no 4090 capacity falls back to an A40")
reset()
api = FakeRunPod()
api.prices["NVIDIA GeForce RTX 4090"] = 0.90
sponsor(3100, 3101)
bot = make_bot(api, FakeRunner(), max_pods=3, max_usd_per_hour=3.0, pod_price_ceiling=0.5)
c = sponsor_bot.connect(DB)
sponsor_bot.ensure_tables(c)
bot._launch(c, bot.clock())
c.close()
note = (q("SELECT note FROM sponsor_pods") or [{"note": ""}])[0]["note"] or ""
check(len(api.deploys) == 1 and not api.live() and "over the" in note,
      f"a pod dearer than the ceiling is terminated at once and no more are launched this tick ({note})")

# ---------- the total cap ----------
print("== the total cap ==")
reset()
api = FakeRunPod()
held = sponsor(4000, 4099)
bot = make_bot(api, FakeRunner(per_block=0.3), max_pods=2, max_usd=0.5, max_usd_per_hour=2.0, blocks_per_pod=10)
check(bot.run() == "budget", "the run stops on the total cap")
spent = bot.spend()
check(api.deployed() <= set(api.terminations) and not api.live(), "reaching the total cap terminates every pod")
check(0 < spent <= 0.55, f"spend stops at the cap (${spent:.3f} of $0.50)")
check(status_of(held) in ("paid", "proving"), "an unfinished sponsorship stays held")
check(q("SELECT COUNT(*) AS n FROM sponsor_work WHERE outcome IS NULL")[0]["n"] == 0, "no work row is left in flight")

# ---------- a stalled pod ----------
print("== a stalled pod ==")
reset()
api = FakeRunPod()
runner = FakeRunner(per_block=0.01, stall={1})
s = sponsor(5000, 5003)
bot = make_bot(api, runner, max_pods=1, stall_s=1800, blocks_per_pod=4)
check(bot.run() == "done" and status_of(s) == "proven", "the sponsorship is still proven after a pod stalls")
first = runner.booted[0]
stalled = q("SELECT height FROM sponsor_work WHERE pod_id=? AND outcome='stalled' ORDER BY height", (first,))
check(first in api.terminations and [r["height"] for r in stalled] == [5000, 5001, 5002, 5003],
      "the stalled pod is terminated and its blocks are logged as stalled")
check(any(pid != first and hs == [5000, 5001, 5002, 5003] for pid, hs in runner.started),
      "its blocks go back to the queue and another pod proves them")

# ---------- a failed boot ----------
print("== a failed boot ==")
reset()
api = FakeRunPod()
runner = FakeRunner(per_block=0.01, boot_fail={1})
s = sponsor(6000, 6001)
make_bot(api, runner, max_pods=1, blocks_per_pod=2).run()
first = runner.booted[0]
note = q("SELECT note FROM sponsor_pods WHERE pod_id=?", (first,))[0]["note"] or ""
check(first in api.terminations and first not in [p for p, _ in runner.started] and note.startswith("boot failed"),
      f"a pod whose boot fails is terminated before it is given work ({note})")
check(status_of(s) == "proven", "a second pod proves the blocks")

# ---------- an exception mid-run ----------
print("== an exception mid-run ==")
reset()
api = FakeRunPod()
sponsor(7000, 7009)
bot = make_bot(api, FakeRunner(raise_on_start=True), max_pods=2, blocks_per_pod=5)
try:
    bot.run()
    raised = False
except RuntimeError:
    raised = True
check(raised and api.deploys and api.deployed() <= set(api.terminations) and not api.live(),
      f"an exception mid-run terminates every pod it rented (deployed {sorted(api.deployed())}, live {api.live()})")

# ---------- SIGTERM ----------
print("== SIGTERM ==")
reset()
api = FakeRunPod()
sponsor(8000, 8099)
bot = make_bot(api, FakeRunner(per_block=0.2), max_pods=2, blocks_per_pod=10)
saved = {sg: signal.getsignal(sg) for sg in (signal.SIGINT, signal.SIGTERM)}
sponsor_bot.install_signal_handlers()
threading.Timer(0.6, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
code = None
try:
    bot.run()
except SystemExit as e:
    code = e.code
finally:
    for sg, h in saved.items():
        signal.signal(sg, h)
check(code == 128 + signal.SIGTERM and api.deploys and api.deployed() <= set(api.terminations) and not api.live(),
      f"SIGTERM stops the bot and terminates every pod (exit {code}, live {api.live()})")

# ---------- stop-all ----------
print("== stop-all ==")
api = FakeRunPod()
api.pods = {"x1": {"name": "hz-sponsor-123-1", "gpu": "NVIDIA A40", "cost": 0.49, "alive": True},
            "x2": {"name": "hz-board-3", "gpu": "NVIDIA A40", "cost": 0.49, "alive": True},
            "x3": {"name": "hz-sponsor-9-2", "gpu": "NVIDIA A40", "cost": 0.49, "alive": True}}
done, unconfirmed = sponsor_bot.stop_all(sponsor_bot.RunPod("test-key", url=api.url), wait_s=0.01)
check(sorted(done) == ["x1", "x3"] and not unconfirmed and api.pods["x2"]["alive"],
      f"stop-all terminates only hz-sponsor-* pods ({done})")

# ---------- report ----------
print("== report ==")
reset()
c = sponsor_bot.connect(DB)
sponsor_bot.ensure_tables(c)
for h, secs, usd, outcome in ((1000, 10, 0.001, "proven"), (2000, 30, 0.003, "proven"), (3000, 20, 0.002, "proven"),
                              (120000, 100, 0.02, "proven"), (4000, None, None, "stalled")):
    c.execute("INSERT INTO sponsor_work(height, seconds, usd_estimate, outcome) VALUES(?,?,?,?)", (h, secs, usd, outcome))
c.execute("INSERT INTO sponsor_pods(pod_id, terminated_at, usd_estimate) VALUES('a', 1, 0.5)")
c.execute("INSERT INTO sponsor_pods(pod_id, terminated_at, usd_estimate) VALUES('b', 1, 0.25)")
c.commit()
rep = sponsor_bot.report(c)
c.close()
b1, b2 = rep["bands"][0], rep["bands"][1]
check((b1["lo"], b1["blocks"], b1["median_seconds"], b1["max_seconds"], b1["median_usd"], b1["max_usd"])
      == (1, 3, 20, 30, 0.002, 0.003), f"band 1 to 100,000: 3 blocks, median 20 s, max 30 s ({b1})")
check((b2["lo"], b2["blocks"], b2["median_seconds"]) == (100001, 1, 100), f"band 100,001 to 150,000 ({b2})")
check(rep["blocks_proven"] == 4 and abs(rep["pod_usd"] - 0.75) < 1e-9 and abs(rep["all_in_usd_per_block"] - 0.1875) < 1e-9,
      "all in: $0.75 of pods over 4 proven blocks is $0.1875 a block")
check(rep["outcomes"] == {"proven": 4, "stalled": 1}, f"outcomes are counted ({rep['outcomes']})")
check("MEASURED" in sponsor_bot.format_report(rep), "the report says its figures are measured")

print(f"{'CONTROL: ' if CONTROL else ''}{len(fails)} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
