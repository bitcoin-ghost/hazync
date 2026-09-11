#!/usr/bin/env python3
"""A box with no usable CUDA device must stop, not become a claim black hole (#261).

On 2026-09-11 a RunPod 4090 with no usable device passed every pre-flight (method-id never touches
CUDA). Each worker then claimed a block, failed on all four segment sizes in seconds, exited, and the
loop claimed the next: 14 blocks claimed and abandoned in 15 minutes, board frontier frozen.

  1. the worker: a device-class CUDA error exits EX_CONFIG (78) on the FIRST attempt -- no ladder --
     while an ordinary error still walks the ladder (so the fatal path is specific);
  2. run-workers.sh: on a GPU box, a host that cannot prove block 170 is refused BEFORE any loop starts,
     and a working one passes the smoke.

  python3 test_worker_device.py            # must PASS
  python3 test_worker_device.py --control  # device errors treated as ordinary; MUST FAIL
"""
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HAZYNC_HOME"] = tempfile.mkdtemp(prefix="hz_dev_")
CONTROL = "--control" in sys.argv
CANON = open(os.path.join(HERE, "..", "reproduce", "METHOD_ID")).read()
CANON = next(t for t in CANON.split() if len(t) == 64 and all(c in "0123456789abcdef" for c in t))

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)
if CONTROL:
    hz._DEVICE_FATAL = ()
    print("CONTROL: device errors treated as ordinary -- the device checks below MUST fail")

T = tempfile.mkdtemp(prefix="hz_fake_")
NODEV = ("terminate called after throwing an instance of 'sppark_error'\n"
         "  what():  cudaErrorNoDevice@sppark-0.1.15/sppark/util/all_gpus.cpp:43 failed: "
         "\"no CUDA-capable device is detected\"")


def fake_host(name, stderr_text, exit_code, prove_block_ok=False):
    """A stand-in `host`: counts invocations, answers method-id/seg-po2, fails every prove."""
    p = os.path.join(T, name)
    count = p + ".count"
    with open(p, "w") as f:
        f.write(f"""#!/bin/bash
case "$1" in
  method-id) echo "METHOD_ID {CANON}"; exit 0 ;;
  seg-po2) echo 21; exit 0 ;;
  prove-block) {"echo 'PROVED in 1.0s — receipt VERIFIED against METHOD_ID.'; exit 0" if prove_block_ok else ""} ;;
esac
echo x >> {count}
cat >&2 <<'EOF'
{stderr_text}
EOF
exit {exit_code}
""")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p, count


fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


def attempts(count):
    return sum(1 for _ in open(count)) if os.path.exists(count) else 0


hz._seg_ladder = lambda env: (None, 20, 19, 18)
work = tempfile.mkdtemp(prefix="hz_work_")

# 1a. no usable device: exit 78 after ONE attempt
hz.HOSTBIN, cnt = fake_host("host_nodev", NODEV, 134)
try:
    hz._run_with_seg_retry(["prove-range-bridge", "5"], dict(os.environ), work, "block 5")
    code = None
except SystemExit as e:
    code = e.code
check(code == 78, f"no usable device -> exits EX_CONFIG 78 (got {code!r})")
check(attempts(cnt) == 1, f"no usable device -> ONE attempt, no ladder (got {attempts(cnt)})")

# 1b. an ordinary failure still walks the whole ladder (the fatal path is specific)
hz.HOSTBIN, cnt = fake_host("host_oom", "memory allocation of 268435456 bytes failed", 101)
try:
    hz._run_with_seg_retry(["prove-range-bridge", "5"], dict(os.environ), work, "block 5")
    code = None
except SystemExit as e:
    code = e.code
check(attempts(cnt) == 4 and code != 78, f"ordinary error -> walks all 4 rungs, not 78 (got {attempts(cnt)}, {code!r})")

# 2. run-workers.sh pre-flight, on a box that claims to have a GPU (fake nvidia-smi on PATH)
shim = os.path.join(T, "bin")
os.makedirs(shim)
with open(os.path.join(shim, "nvidia-smi"), "w") as f:
    f.write("#!/bin/bash\n[ \"$1\" = -L ] && echo 'GPU 0: Fake GPU (UUID: GPU-0)'\nexit 0\n")
os.chmod(os.path.join(shim, "nvidia-smi"), 0o755)
rw = os.path.join(T, "rw")
os.makedirs(rw)
shutil.copy(os.path.join(HERE, "run-workers.sh"), rw)
shutil.copy(os.path.join(HERE, "hazync"), rw)
env = dict(os.environ, PATH=shim + ":" + os.environ["PATH"], COORD_URL="http://127.0.0.1:9",
           LOG_DIR=os.path.join(T, "logs"))

bad, _ = fake_host("host_rw_bad", NODEV, 134)
r = subprocess.run(["bash", os.path.join(rw, "run-workers.sh"), "1"], env=dict(env, HAZYNC_HOST=bad),
                   capture_output=True, text=True, timeout=120)
check(r.returncode == 1 and "cannot prove on it" in r.stderr and "nothing was claimed" in r.stderr,
      f"run-workers: GPU box that cannot prove is refused (rc={r.returncode})")
check(not os.path.exists(os.path.join(T, "logs", "worker_1.log")), "run-workers: ...and no worker loop was started")

good, _ = fake_host("host_rw_good", "unused", 0, prove_block_ok=True)
r = subprocess.run(["bash", os.path.join(rw, "run-workers.sh"), "1"], env=dict(env, HAZYNC_HOST=good),
                   capture_output=True, text=True, timeout=120)
check("gpu smoke       : ok" in r.stdout, "run-workers: a host that proves block 170 passes the smoke")
subprocess.run(["bash", os.path.join(rw, "run-workers.sh"), "1", "--stop"], env=dict(env, HAZYNC_HOST=good),
               capture_output=True, text=True, timeout=60)

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
