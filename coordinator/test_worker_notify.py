#!/usr/bin/env python3
"""A worker that stops or keeps failing tells its prover, through ntfy (`hazync notify`).

Before this, a box with no usable GPU, a prover left on an old guest id, and a clock the coordinator refuses all
stopped or failed in a log file, and from outside looked like an idle machine.

  1. `hazync notify <url>` saves the address privately and sends a test push; alerts are off by default; a bare
     topic means ntfy.sh; one problem is pushed once per HAZYNC_NTFY_REPEAT, even from a new process; a push that
     cannot be sent returns quickly and raises nothing;
  2. the worker pushes when it stops for good (no usable GPU, guest id mismatch), when a proof is rejected, and
     when a claim is refused for a reason the box must fix -- not for "already proven" or "nothing free";
  3. run-workers.sh pushes when a worker fails NOTIFY_FAIL_STREAK times in a row (a busy board does not count),
     when it recovers, when a loop stops, and when it refuses to start.

  python3 test_worker_notify.py            # must PASS
  python3 test_worker_notify.py --control  # alerts never sent; MUST FAIL
"""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOMEDIR = tempfile.mkdtemp(prefix="hz_notify_home_")
os.environ["HAZYNC_HOME"] = HOMEDIR
for _v in ("HAZYNC_NTFY", "HAZYNC_NTFY_REPEAT", "NOTIFY_FAIL_STREAK"):
    os.environ.pop(_v, None)
CONTROL = "--control" in sys.argv
CANON = open(os.path.join(HERE, "..", "reproduce", "METHOD_ID")).read()
CANON = next(t for t in CANON.split() if len(t) == 64 and all(c in "0123456789abcdef" for c in t))

received = []


class Ntfy(http.server.BaseHTTPRequestHandler):
    """Stands in for ntfy (POST <topic>) and, for the launcher's pre-flight, a coordinator's /api/meta."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        received.append({"path": self.path, "title": self.headers.get("Title", ""),
                         "priority": self.headers.get("Priority", ""), "tags": self.headers.get("Tags", ""),
                         "body": self.rfile.read(n).decode("utf-8", "replace")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"id": "x"}')

    def do_GET(self):
        out = json.dumps({"method_id": "c" * 64}).encode()      # never this worker's id
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ntfy)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
URL = BASE + "/test-topic"


def load_cli():
    name = f"hazync_cli_{time.time_ns()}"
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(HERE, "hazync"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    if CONTROL:
        mod.notify = lambda *a, **k: False
    return mod


if CONTROL:
    print("CONTROL: alerts are never sent -- the checks below MUST fail")
hz = load_cli()
fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


def wait_for(n, timeout=5.0):
    t = time.time()
    while time.time() - t < timeout:
        if len(received) >= n:
            return True
        time.sleep(0.05)
    return len(received) >= n


def exit_code(fn, *a):
    try:
        fn(*a)
        return None
    except SystemExit as e:
        return e.code


T = tempfile.mkdtemp(prefix="hz_notify_fake_")


def script(name, text):
    p = os.path.join(T, name)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


print("== setting up ==")
check(hz._ntfy_url() is None and hz.notify("x", "y") is False and not received, "alerts are off by default: nothing is sent")
os.environ["HAZYNC_NTFY"] = "some-topic"
check(hz._ntfy_url() == "https://ntfy.sh/some-topic", "a bare topic means ntfy.sh")
del os.environ["HAZYNC_NTFY"]
code = exit_code(hz.cmd_notify, [URL])
f = os.path.join(HOMEDIR, "ntfy")
check(code is None and os.path.exists(f) and stat.S_IMODE(os.stat(f).st_mode) == 0o600 and open(f).read().strip() == URL,
      "`hazync notify <url>` saves the address, mode 600")
check(wait_for(1) and "alerts are on" in received[-1]["title"], f"and sends a test push ({[r['title'] for r in received]})")
check(exit_code(hz.cmd_notify, ["not a topic!"]) not in (None, 0), "a malformed topic is refused")

print("== one problem, one push an hour ==")
received.clear()
hz.notify("disk full", "details", key="k1")
check(wait_for(1) and received[0]["priority"] == "high" and "disk full" in received[0]["title"] and received[0]["body"] == "details",
      "a push carries its title, priority and body")
hz.notify("disk full", "details", key="k1")
check(len(received) == 1, "the same problem again within the hour is not pushed again")
load_cli().notify("disk full", "details", key="k1")
check(len(received) == 1, "not even from a new process (every `hazync run` is one)")
os.environ["HAZYNC_NTFY_REPEAT"] = "0"
hz.notify("disk full", "details", key="k1")
del os.environ["HAZYNC_NTFY_REPEAT"]
check(wait_for(2), "once the window has passed it is pushed again")

print("== a push that cannot be sent never gets in the way ==")
os.environ["HAZYNC_NTFY"] = "http://127.0.0.1:9/nowhere"
t0 = time.time()
try:
    ok, raised = hz.notify("x", "y", key="dead"), False
except Exception:
    ok, raised = None, True
took = time.time() - t0
del os.environ["HAZYNC_NTFY"]
check(not raised and ok is False and took < 12, f"an unreachable ntfy returns False in {took:.1f} s and raises nothing")

print("== the worker pushes when it stops for good ==")
received.clear()
nodev = script("host_nodev", "#!/bin/bash\n[ \"$1\" = method-id ] && { echo 'METHOD_ID %s'; exit 0; }\n"
               "echo 'what():  cudaErrorNoDevice: no CUDA-capable device is detected' >&2\nexit 134\n" % CANON)
hz.HOSTBIN = nodev
hz._seg_ladder = lambda env: (None,)
code = exit_code(hz._run_with_seg_retry, ["prove-range-bridge", "5"], dict(os.environ), tempfile.mkdtemp(), "block 5")
check(code == 78 and wait_for(1) and "no usable GPU" in received[-1]["title"] and received[-1]["priority"] == "urgent",
      f"no usable GPU: exits 78 and pushes an urgent alert ({code}, {[r['title'] for r in received]})")
received.clear()
other = script("host_other", "#!/bin/bash\necho 'METHOD_ID %s'\n" % ("b" * 64))
hz.HOSTBIN = other
real_get = hz.get
hz.get = lambda path: json.dumps({"method_id": "a" * 64}).encode()
code = hz._guest_mismatch_exit("guest image id mismatch")
hz.get = real_get
check(code == 78 and wait_for(1) and "guest id mismatch" in received[-1]["title"],
      f"guest id mismatch: exits 78 and pushes an alert ({code}, {[r['title'] for r in received]})")

print("== a rejected proof pushes; a race lost does not ==")
received.clear()
os.makedirs(os.path.join(HOMEDIR, "receipts"), exist_ok=True)
with open(os.path.join(HOMEDIR, "receipts", "5.bin"), "wb") as fh:
    fh.write(b"receipt")
real_post = hz.post
hz.post = lambda path, body: {"error": "proof does not verify"}
code = exit_code(hz.cmd_submit, ["5"])
check(code == 1 and wait_for(1) and "rejected" in received[-1]["title"], f"a rejected proof exits 1 and pushes ({code}, {[r['title'] for r in received]})")
n = len(received)
hz.post = lambda path, body: {"error": "range 5 already proven"}
code = exit_code(hz.cmd_submit, ["5"])
time.sleep(0.3)
check(code == 0 and len(received) == n, f"'already proven' is a race lost: exit 0, no push ({code}, {len(received) - n} pushed)")

print("== a claim refused for a reason this box must fix pushes; a busy board does not ==")
received.clear()
hz.HOSTBIN = nodev
hz.post = lambda path, body: {"error": "claim timestamp outside +/-120s — check your clock"}
code = exit_code(hz.cmd_run, [])
check(code not in (0, 75, None) and wait_for(1) and "claim refused" in received[-1]["title"],
      f"a clock the coordinator refuses: the worker exits and pushes ({code!r}, {[r['title'] for r in received]})")
n = len(received)
for err in ("nothing available to claim", "this key already holds 4 claimed blocks that are not finished (at most 4)"):
    hz.post = lambda path, body, e=err: {"error": e}
    code = exit_code(hz.cmd_run, [])
    time.sleep(0.3)
    check(code == 75 and len(received) == n, f"'{err[:30]}…' is a busy board: exit 75, no push ({code}, {len(received) - n} pushed)")
hz.post = real_post

print("== run-workers.sh ==")
rw = os.path.join(T, "rw")
os.makedirs(rw)
shutil.copy(os.path.join(HERE, "run-workers.sh"), rw)
plan = os.path.join(T, "plan")
count = os.path.join(T, "count")
# A stand-in worker CLI: `notify ...` is the real CLI; every other call follows the plan, one exit code per call.
script("rw/hazync-worker", f"""#!/bin/bash
if [ "$1" = notify ]; then exec {sys.executable} {os.path.join(HERE, "hazync")} "$@"; fi
n=$(( $(cat {count} 2>/dev/null || echo 0) + 1 )); echo $n > {count}
rc=$(sed -n "${{n}}p" {plan}); echo "attempt $n exits ${{rc:-0}}"
[ -n "$rc" ] || {{ sleep 600; exit 0; }}
exit $rc
""")
host_ok = script("host_ok", "#!/bin/bash\necho 'METHOD_ID %s'\n" % CANON)
with open(os.path.join(HOMEDIR, "handle"), "w") as fh:
    fh.write("tester\n")
try:
    os.remove(os.path.join(HOMEDIR, "ntfy-sent.json"))
except FileNotFoundError:
    pass
env = dict(os.environ, HAZYNC_HOST=host_ok, COORD_URL="http://127.0.0.1:9", SKIP_GPU_SMOKE="1",
           LOG_DIR=os.path.join(T, "logs"), NOTIFY_FAIL_STREAK="3", HAZYNC_NTFY=URL)
if CONTROL:
    env["HAZYNC_NTFY"] = "off"      # the address saved above would otherwise still reach the launcher's real CLI
# 75 (a busy board, not counted), three failures (alert on the third), a success (recovered), then 78 (stopped).
with open(plan, "w") as fh:
    fh.write("75\n1\n1\n1\n0\n78\n")
received.clear()
subprocess.run(["bash", os.path.join(rw, "run-workers.sh"), "1"], env=env, capture_output=True, text=True, timeout=60)
log = os.path.join(T, "logs", "worker_1.log")
t0 = time.time()
while time.time() - t0 < 120 and not (os.path.exists(log) and "attempt 6" in open(log).read() and len(received) >= 3):
    time.sleep(0.5)
time.sleep(2)
subprocess.run(["pkill", "-f", T], capture_output=True)
titles = [r["title"] for r in received]
failing = [r for r in received if "failed 3 times in a row" in r["title"]]
check(len(failing) == 1 and "attempt 4 exits 1" in failing[0]["body"],
      f"one push after 3 failures in a row, not counting the busy board (titles {titles})")
check(any("is working again" in t for t in titles), "a push when the worker recovers")
check(any("stopped" in r["title"] and r["priority"] == "urgent" for r in received), "an urgent push when the loop stops for good")
check(len(received) == 3, f"and nothing more: three pushes in all ({len(received)})")

received.clear()
r = subprocess.run(["bash", os.path.join(rw, "run-workers.sh"), "1"], env=dict(env, COORD_URL=BASE),
                   capture_output=True, text=True, timeout=60)
check(r.returncode == 1 and wait_for(1) and "not started: guest id mismatch" in (received[-1]["title"] if received else ""),
      f"a launcher that refuses to start says so (rc {r.returncode}, {[x['title'] for x in received]})")

srv.shutdown()
print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
