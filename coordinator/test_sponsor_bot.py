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
    sponsor_bot._CONTROL_IGNORE_LANDED = True
    print("CONTROL: pods are not terminated when the bot errors, and proofs that land before a pod is stopped"
          " are ignored -- the checks below MUST fail")

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
        self.agents = []                    # the User-Agent of every request
        self.refuse_deploy = False          # answer deploys with an API error instead of a pod
        self.max_live, self.n = 0, 0
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                agent = self.headers.get("User-Agent") or ""
                fake.agents.append(agent)
                if not agent or agent.startswith("Python-urllib"):
                    # What api.runpod.io's Cloudflare does to Python's default User-Agent (measured 2026-09-14).
                    out = b"error code: 1010\n"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
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
            if "podFindAndDeployOnDemand" in q and self.refuse_deploy:
                return {"errors": [{"message": "simulated refusal"}], "data": None}
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

    def __init__(self, per_block=0.01, boot_fail=(), stall=(), raise_on_start=False, start_fail=(), prove_then_fail=()):
        self.per_block, self.boot_fail, self.stall = per_block, set(boot_fail), set(stall)
        self.raise_on_start = raise_on_start
        self.start_fail = set(start_fail)       # pods (1st, 2nd, ...) whose work cannot be started
        self.prove_then_fail = set(prove_then_fail)     # pods whose blocks land, then starting reports a failure
        self.booted, self.started, self.threads, self.status_at_start = [], [], {}, {}
        self.work, self.unregistered_at_start = [], []
        self.lock = threading.Lock()

    def nth(self, pod):
        return self.booted.index(pod.id) + 1

    def boot(self, pod):
        with self.lock:
            self.booted.append(pod.id)
            n = self.nth(pod)
        return (False, "GPU_BAD no CUDA device") if n in self.boot_fail else (True, "SHA_OK GPU_OK MID ok")

    def start(self, pod, work):
        if self.raise_on_start:
            raise RuntimeError("simulated failure while starting work")
        if self.nth(pod) in self.start_fail:
            raise sponsor_bot.StartFailed(f"simulated: could not start work on {pod.name}")
        if self.nth(pod) in self.prove_then_fail:
            # Trial 2: the work ran and its blocks landed, but the command that started it timed out.
            c = sqlite3.connect(DB, timeout=30)
            for w in work:
                c.execute("INSERT OR IGNORE INTO vranges(id, lo, hi, pubkey, handle, ts) VALUES(?,?,?,?,?,?)",
                          (f"bot-{w['height']}", w["height"], w["height"], w["pubkey"], "SPONSOR: test", time.time()))
            c.commit()
            c.close()
            raise sponsor_bot.StartFailed(f"simulated: {pod.name} proved its blocks, then its launch timed out")
        heights = [w["height"] for w in work]
        self.started.append((pod.id, heights))
        self.work.extend((pod.id, dict(w)) for w in work)
        c = sqlite3.connect(DB, timeout=30)
        registered = {r[0] for r in c.execute("SELECT pubkey FROM sponsor_keys")}
        self.unregistered_at_start.extend(w["pubkey"] for w in work if w["pubkey"] not in registered)
        self.status_at_start.setdefault(pod.id, []).append(sorted({r[0] for r in c.execute(
            "SELECT s.status FROM sponsor_work w JOIN sponsorships s ON s.id=w.sponsorship_id"
            " WHERE w.pod_id=? AND w.outcome IS NULL", (pod.id,))}))
        c.close()
        if self.nth(pod) in self.stall:
            return

        def prove():
            for w in work:
                h = w["height"]
                time.sleep(self.per_block)
                if pod.terminated is not None:
                    return
                with open(os.path.join(w["home"], "handle")) as fh:
                    handle = fh.read().strip()      # what `hazync-worker` would submit with HAZYNC_HOME=home
                c = sqlite3.connect(DB, timeout=30)
                c.execute("INSERT OR IGNORE INTO vranges(id, lo, hi, pubkey, handle, ts) VALUES(?,?,?,?,?,?)",
                          (f"bot-{h}", h, h, w["pubkey"], handle, time.time()))
                c.commit()
                c.close()
        t = threading.Thread(target=prove, daemon=True)
        t.start()
        self.threads[pod.id] = t

    def status(self, pod):
        t = self.threads.get(pod.id)
        return "finished" if (t is not None and not t.is_alive()) else "running"


def fast_clock(speed=3600.0):
    t0 = time.time()
    return lambda: t0 + (time.time() - t0) * speed


IDHOME = tempfile.mkdtemp(prefix="sponsor_ids_")


def make_bot(api, runner, **kw):
    args = dict(identity_home=IDHOME, max_pods=2, max_usd=1000.0, max_usd_per_hour=10.0, pod_price_ceiling=1.0,
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
    for t in ("sponsorships", "vranges", "ranges", "sponsor_work", "sponsor_pods", "sponsor_keys"):
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

# ---------- RunPod: the User-Agent, and a refusal is not a capacity miss ----------
print("== RunPod: User-Agent and refusals ==")
reset()
api = FakeRunPod()
make_bot(api, FakeRunner(), trial=[9700]).run()
check(api.agents and all(a == sponsor_bot.USER_AGENT and a for a in api.agents),
      f"every RunPod call carries the bot's own User-Agent, not Python's default ({sorted(set(api.agents))[:2]})")
check(len(api.deploys) == 1, f"so a pod is deployed through the Cloudflare-like guard ({len(api.deploys)} deploys)")
saved_agent, sponsor_bot.USER_AGENT = sponsor_bot.USER_AGENT, ""
try:
    sponsor_bot.RunPod("test-key", url=FakeRunPod().url, timeout=10).deploy("hz-sponsor-x", FakeRunner.ssh_pubkey)
    raised = ""
except sponsor_bot.RunPodError as e:
    raised = str(e)
sponsor_bot.USER_AGENT = saved_agent
check("403" in raised and "1010" in raised,
      f"without it RunPod refuses (403, 1010) and the client says so instead of returning 'no capacity' ({raised!r})")
reset()
api = FakeRunPod()
api.refuse_deploy = True
logs = []
reason = make_bot(api, FakeRunner(), trial=[9701], capacity_backoff_s=0, log=logs.append).run()
refusals = [m for m in logs if "RunPod refused the deploy request" in m]
check(reason == "runpod" and len(refusals) == sponsor_bot.RUNPOD_REFUSALS_MAX,
      f"{sponsor_bot.RUNPOD_REFUSALS_MAX} refused deploys in a row stop the run as 'runpod' (reason {reason}, {len(refusals)} refusals)")
check(not any("no GPU capacity" in m for m in logs), "a refusal is never logged as a capacity miss")
check(not api.live(), "and nothing is left running")
check(sponsor_bot.main(["trial", "--blocks", "9702"]) == 2, "a dry run exits with code 2, as the docs say")

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

# ---------- a pod whose work cannot be started ----------
print("== a pod whose work cannot be started ==")
reset()
s1 = sponsor(9800, 9803)
api = FakeRunPod()
runner = FakeRunner(per_block=0.01, start_fail={1})
logs = []
reason = make_bot(api, runner, max_pods=1, blocks_per_pod=5, log=logs.append).run()
check(reason == "done" and status_of(s1) == "proven",
      f"the run carries on and the sponsorship is proven by another pod (reason {reason}, {status_of(s1)})")
check(len(api.deploys) >= 2 and not api.live(), f"the pod that could not start was terminated and replaced ({len(api.deploys)} deploys, live {api.live()})")
check(q("SELECT COUNT(*) n FROM sponsor_work WHERE outcome='failed'")[0]["n"] == 4,
      "its four blocks are logged as failed on that pod, then proven on the next")
check(q("SELECT note FROM sponsor_pods WHERE note LIKE 'could not start work%'"), "and the pod's note says why")

print("== starting work returns at once, and a timeout is a failed step ==")
wd = tempfile.mkdtemp(prefix="launch_")
with open(os.path.join(wd, "sponsor-run.sh"), "w") as f:
    f.write("#!/bin/bash\nsleep 30\necho ALLDONE\n")
t0 = time.time()
try:
    r = sponsor_bot.subprocess.run(["bash", "-c", sponsor_bot.launch_command(wd)], capture_output=True, text=True, timeout=5)
    took, timed_out = time.time() - t0, False
except sponsor_bot.subprocess.TimeoutExpired:
    took, timed_out = time.time() - t0, True
sponsor_bot.subprocess.run(["pkill", "-f", os.path.join(wd, "sponsor-run.sh")], capture_output=True)
check(not timed_out and took < 2, f"the launch command returns with its output pipes free while the work runs on ({took:.2f}s)")
real_run = sponsor_bot.subprocess.run


def hang(argv, **kw):
    raise sponsor_bot.subprocess.TimeoutExpired(argv, kw.get("timeout"))


class _Pod:
    id, name, ssh = "podT", "hz-sponsor-t", ("127.0.0.1", 22)


sponsor_bot.subprocess.run = hang
try:
    sr = sponsor_bot.SshRunner.__new__(sponsor_bot.SshRunner)
    sr.key, sr.known_hosts, sr.dir = "/nonexistent/key", "/dev/null", wd
    res = sr._ssh(_Pod(), "true")
    try:
        _Pod.identities = {"trial"}
        sr.start(_Pod(), [{"height": 1, "tag": "trial", "home": wd, "pubkey": "p", "handle": "h"}])
        raised = None
    except Exception as e:
        raised = e
finally:
    sponsor_bot.subprocess.run = real_run
check(res.returncode == 124, f"a timed-out ssh comes back as a failed result, not an exception (rc {res.returncode})")
check(isinstance(raised, sponsor_bot.StartFailed), f"so starting work on an unreachable pod raises StartFailed ({type(raised).__name__})")

# ---------- proofs that land before a pod is stopped ----------
print("== a block proven just before its pod is stopped is logged proven ==")
reset()
s1 = sponsor(9900, 9901)
api = FakeRunPod()
runner = FakeRunner(per_block=0.01, prove_then_fail={1})
reason = make_bot(api, runner, max_pods=1, blocks_per_pod=5).run()
first = runner.booted[0]
rows = q("SELECT height, outcome, seconds FROM sponsor_work WHERE pod_id=? ORDER BY height", (first,))
check([(r["height"], r["outcome"]) for r in rows] == [(9900, "proven"), (9901, "proven")]
      and all(r["seconds"] is not None for r in rows),
      f"blocks that landed before the pod was stopped are logged proven, not failed ({[(r['height'], r['outcome']) for r in rows]})")
check(reason == "done" and status_of(s1) == "proven" and len(api.deploys) == 1 and not api.live(),
      f"no second pod is rented for them and the sponsorship is proven ({len(api.deploys)} deploys, {status_of(s1)})")

print("== work rows logged as stopped whose block was proven are corrected ==")
import contextlib  # noqa: E402
import io  # noqa: E402
reset()
c = sponsor_bot.connect(DB)
sponsor_bot.ensure_tables(c)
K, OTHER = "k" * 64, "o" * 64
c.execute("INSERT INTO sponsor_pods(pod_id, name, cost_per_hr, created_at, terminated_at, usd_estimate)"
          " VALUES('old', 'hz-sponsor-x-2', 0.74, 1000, 1100, 0.02)")
for h, outcome in ((90000, "cancelled"), (90001, "failed"), (90002, "cancelled"), (90003, "stalled"),
                   (90004, "cancelled"), (90005, "proven")):
    c.execute("INSERT INTO sponsor_work(height, pod_id, cost_per_hr, assigned_at, outcome, pubkey, seconds)"
              " VALUES(?,?,?,?,?,?,?)", (h, "old", 0.74, 1010, outcome, K, 10 if outcome == "proven" else None))
for vid, h, key, ts in (("e", 90005, K, 1020), ("a", 90000, K, 1040), ("b", 90001, K, 1058),
                        ("c", 90003, OTHER, 1050), ("d", 90004, K, 1200)):
    c.execute("INSERT INTO vranges(id, lo, hi, pubkey, handle, ts) VALUES(?,?,?,?,?,?)", (vid, h, h, key, "h", ts))
c.commit()
c.close()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = sponsor_bot.main(["report"])
out = buf.getvalue()
got = {r["height"]: r for r in q("SELECT * FROM sponsor_work")}
check(code == 0 and "corrected 2 work row(s)" in out, f"report corrects the log before counting ({out.splitlines()[:1]})")
check(got[90000]["outcome"] == got[90001]["outcome"] == "proven"
      and abs((got[90000]["seconds"] or 0) - 20) < 1e-9 and abs((got[90001]["seconds"] or 0) - 18) < 1e-9
      and got[90001]["proven_at"] == 1058 and abs((got[90001]["usd_estimate"] or 0) - 18 * 0.74 / 3600) < 1e-12,
      "blocks proven under the row's key while its pod was alive become proven, timed from that pod's previous proof")
check((got[90002]["outcome"], got[90003]["outcome"], got[90004]["outcome"]) == ("cancelled", "stalled", "cancelled"),
      "a block never proven, one proven by another key and one proven after the pod was stopped are left as they were")
check("outcomes: cancelled 2, proven 3, stalled 1" in out, f"so the report counts them ({out.splitlines()[-1:]})")
c = sponsor_bot.connect(DB)
check(sponsor_bot.reconcile_work(c) == 0, "a second pass changes nothing")
c.close()

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

# ---------- identities: one key per sponsorship, registered before use ----------
print("== identities ==")
import stat  # noqa: E402
reset()
api = FakeRunPod()
runner = FakeRunner(per_block=0.01)
sa = sponsor(9000, 9001, paid_at=100, name="Alice")
sb = sponsor(9100, 9100, paid_at=200, name="O'Brien <b>&co")
long_name = "L" * 40
sc = sponsor(9200, 9200, paid_at=300, name=long_name)
bot = make_bot(api, runner, max_pods=1, blocks_per_pod=10)
check(bot.run() == "done", "a run over three sponsorships finishes")
ids = {sa: sponsor_bot.identity(IDHOME, sa, "Alice"), sb: sponsor_bot.identity(IDHOME, sb, "O'Brien <b>&co"),
       sc: sponsor_bot.identity(IDHOME, sc, long_name)}
check(sponsor_bot.identity(IDHOME, sa, None)["handle"] == "SPONSOR: Alice",
      "looking a key up without a name keeps the handle it was made with")
check(len({i["pubkey"] for i in ids.values()}) == 3, "each sponsorship has its own key")
check(ids[sa]["handle"] == "SPONSOR: Alice", f"the handle is 'SPONSOR: <name>' ({ids[sa]['handle']!r})")
check(ids[sb]["handle"] == "SPONSOR: OBrien bco" == server.clean_handle("SPONSOR: O'Brien <b>&co", 49),
      f"the bot cleans a name exactly as the coordinator does ({ids[sb]['handle']!r})")
check(ids[sc]["handle"] == "SPONSOR: " + long_name and len(ids[sc]["handle"]) == 49,
      "a 40-character name is kept whole")
kp = os.path.join(IDHOME, "identities", str(sa), "key.hex")
check(stat.S_IMODE(os.stat(kp).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(kp)).st_mode) == 0o700,
      "key.hex is mode 600 in a 700 directory")
check(runner.work and not runner.unregistered_at_start, "every key was in sponsor_keys before a pod was given it")
keys = {r["pubkey"]: r for r in q("SELECT * FROM sponsor_keys")}
check(all(keys.get(i["pubkey"]) and keys[i["pubkey"]]["sponsorship_id"] == sid and keys[i["pubkey"]]["handle"] == i["handle"]
          for sid, i in ids.items()), "sponsor_keys holds each key with its sponsorship and handle")
per_block = {w["height"]: w for _, w in runner.work}
check(all(per_block[h]["home"] == ids[sid]["home"] and per_block[h]["pubkey"] == ids[sid]["pubkey"]
          for sid, hs in ((sa, (9000, 9001)), (sb, (9100,)), (sc, (9200,))) for h in hs)
      and len({pid for pid, _ in runner.work}) == 1,
      "one pod working three sponsorships runs each block under that sponsorship's identity")
rows = q("SELECT sponsorship_id, height, pubkey FROM sponsor_work")
check(len(rows) == 4 and all(r["pubkey"] == ids[r["sponsorship_id"]]["pubkey"] for r in rows),
      "sponsor_work records the key each block was proved under")
check({r["handle"] for r in q("SELECT handle FROM vranges WHERE id LIKE 'bot-%'")}
      == {"SPONSOR: Alice", "SPONSOR: OBrien bco", "SPONSOR: " + long_name}, "the proofs carry the sponsor handles")
check(not server.handle_refused(ids[sa]["handle"], ids[sa]["pubkey"]) and server.handle_refused(ids[sa]["handle"], "ab" * 32),
      "the coordinator accepts the bot's handle from its registered key, and refuses it from any other")
before = open(kp).read()
q("UPDATE sponsorships SET status='paid', proven_at=NULL WHERE id=?", (sa,))
q("INSERT INTO sponsorships(lo,hi,name,status,created_at,min_usd,min_sats,paid_sats,paid_at) VALUES(0,0,'x','cancelled',1,1,1,1,1)")
q("DELETE FROM vranges WHERE id='bot-9001'")
runner2 = FakeRunner(per_block=0.01)
make_bot(FakeRunPod(), runner2, max_pods=1).run()
check(open(kp).read() == before and [w["pubkey"] for _, w in runner2.work] == [ids[sa]["pubkey"]],
      "a later run reuses the sponsorship's key, not a new one")
reset()
runner = FakeRunner(per_block=0.01)
make_bot(FakeRunPod(), runner, trial=[9300], max_pods=1).run()
t1 = sponsor_bot.identity(IDHOME, None, None)
reset()
runner_b = FakeRunner(per_block=0.01)
make_bot(FakeRunPod(), runner_b, trial=[9301], max_pods=1).run()
tk = q("SELECT * FROM sponsor_keys")
check(t1["handle"] == "SPONSOR: Hazync trial" and [w["pubkey"] for _, w in runner.work + runner_b.work] == [t1["pubkey"]] * 2
      and len(tk) == 1 and tk[0]["sponsorship_id"] is None,
      "every trial uses one key, 'SPONSOR: Hazync trial', registered with no sponsorship")
c = sponsor_bot.connect(DB)
c.execute("DROP TABLE sponsor_keys")
c.commit()
c.close()
try:
    make_bot(FakeRunPod(), FakeRunner(), trial=[9400]).run()
    refused = False
except sponsor_bot.BotRefused as e:
    refused = "sponsor_keys" in str(e)
check(refused, "the bot refuses a coordinator database without sponsor_keys")
server.init_db()
try:
    make_bot(FakeRunPod(), FakeRunner(), trial=[9400], identity_home=None).run()
    refused = False
except sponsor_bot.BotRefused:
    refused = True
check(refused, "the bot refuses to run without an identity home")

# ---------- the pod runner uses nothing from a home directory ----------
print("== no home directory ==")
import subprocess as _subprocess  # noqa: E402
BOTHOME = tempfile.mkdtemp(prefix="sponsor_bot_home_")
os.makedirs(os.path.join(BOTHOME, "ssh"))
saved_env = {k: os.environ.get(k) for k in ("HOME", "SPONSOR_BOT_HOME", "SPONSOR_BOT_SSH_KEY", "RUNPOD_API_KEY_FILE", "GNUPGHOME")}
calls = []


class _Done:
    returncode, stdout, stderr = 0, "SHA_OK GPU_OK MID=" + "a" * 64 + "\nGood signature\nALLDONE", "Good signature"


real_run = sponsor_bot.subprocess.run
try:
    for k in saved_env:
        os.environ.pop(k, None)
    os.environ["SPONSOR_BOT_HOME"] = BOTHOME
    sponsor_bot.subprocess.run = lambda argv, **kw: (calls.append((list(argv), kw)), _Done())[1]
    check(sponsor_bot.RunPod.key_path() == os.path.join(BOTHOME, "runpod.key"),
          "with HOME unset the RunPod key file defaults to runpod.key in SPONSOR_BOT_HOME")
    runner = sponsor_bot.SshRunner.from_env()
    key = os.path.join(BOTHOME, "ssh", "id_ed25519")
    check(runner.key == key and runner.known_hosts == os.path.join(BOTHOME, "known_hosts")
          and runner.dir.startswith(os.path.join(BOTHOME, "work") + os.sep),
          "the SSH key, known_hosts and scratch files default to SPONSOR_BOT_HOME")
    runner.method_id = "a" * 64
    for f in ("boot.sh", "want.txt"):
        open(os.path.join(runner.dir, f), "w").close()
    ident = sponsor_bot.identity(os.path.join(BOTHOME), 42, "Home Test")
    pod = sponsor_bot.Pod({"id": "p1", "name": "hz-sponsor-t-1"}, time.time())
    pod.ssh = ("203.0.113.9", 22022)
    runner.boot(pod)
    runner.start(pod, [{"height": 7, **ident}])
    runner.status(pod)
    runner.verify_manifest()
    remote = [(a, kw) for a, kw in calls if a[0] in ("ssh", "scp")]
    ok = bool(remote)
    for a, _ in remote:
        joined = " ".join(a)
        ok = ok and ("-F /dev/null" in joined and f"-i {key}" in joined
                     and f"UserKnownHostsFile={os.path.join(BOTHOME, 'known_hosts')}" in joined
                     and "IdentitiesOnly=yes" in joined and "~" not in joined)
    check(ok, f"every ssh and scp call names its key, its known_hosts file and no config file ({len(remote)} calls)")
    check(not any("/.ssh" in " ".join(a) for a, _ in calls), "nothing refers to a ~/.ssh directory")
    gpg = [kw for a, kw in calls if a[0] == "gpg"]
    check(gpg and gpg[0].get("env", {}).get("GNUPGHOME") == os.path.join(BOTHOME, "gnupg"),
          "gpg verifies with the keyring in SPONSOR_BOT_HOME")
    script = open(os.path.join(runner.dir, "run-p1.sh")).read()
    check("HAZYNC_HOME=/root/.hazync-ids/42 " in script and "./hazync-worker run 7" in script,
          "the pod runs the block with HAZYNC_HOME set to its identity")
finally:
    sponsor_bot.subprocess.run = real_run
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

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
