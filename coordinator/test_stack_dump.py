#!/usr/bin/env python3
"""SIGUSR1 dumps every thread's stack and the coordinator keeps serving.

The only way to learn why a wedged coordinator is wedged is a thread dump taken BEFORE the restart
(#265 recurred on 2026-09-11 and the restart erased the evidence). Starts the REAL server as a
subprocess on a loopback port, parks a request thread inside a handler, sends SIGUSR1, and checks that
stderr carries a stack for more than one thread -- including the parked handler -- and that the
server still answers afterwards.

  python3 test_stack_dump.py            # must PASS
  python3 test_stack_dump.py --control  # the hook is not installed; MUST FAIL
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL = "--control" in sys.argv

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
tmp = tempfile.mkdtemp(prefix="hz_dump_")
env = dict(os.environ, COORD_DB=os.path.join(tmp, "c.db"), COORD_SPINE=tmp, COORD_PROOFS=tmp,
           VERIFY_MODE="mock", COORD_ALLOW_MOCK="1", COORD_ALLOW_UNSIGNED="1", COORD_BIND="127.0.0.1",
           COORD_PORT=str(port), COORD_WEB=HERE, TIP_CACHE_TTL="0", PYTHONUNBUFFERED="1")
if CONTROL:   # the same server with the hook call removed, and nothing else changed
    boot = ("import os; p=os.path.abspath('server.py'); src=open(p).read();"
            "assert src.count('    install_stack_dump()\\n') == 1;"
            "src=src.replace('    install_stack_dump()\\n', '    pass\\n');"
            "exec(compile(src, p, 'exec'), {'__name__': '__main__', '__file__': p})")
else:
    boot = "import runpy; runpy.run_path('server.py', run_name='__main__')"
err = open(os.path.join(tmp, "stderr"), "w+")
p = subprocess.Popen([sys.executable, "-c", boot], cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=err)
URL = f"http://127.0.0.1:{port}"

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


def up():
    try:
        return urllib.request.urlopen(URL + "/api/state", timeout=10).status == 200
    except Exception:
        return False


try:
    for _ in range(100):
        if up():
            break
        time.sleep(0.1)
    check(up(), "coordinator serves before the signal")
    # a request thread parked mid-handler: an open connection that never finishes its body
    parked = socket.create_connection(("127.0.0.1", port))
    parked.sendall(b"POST /api/claim HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
    time.sleep(0.5)
    p.send_signal(signal.SIGUSR1)
    time.sleep(1.0)
    alive = p.poll() is None
    check(alive, "the process survives SIGUSR1 (default action would kill it)")
    err.flush(); err.seek(0); dump = err.read()
    threads = dump.count("Thread 0x") + dump.count("Current thread 0x")
    check(threads >= 2, f"stderr has a stack for every thread ({threads} found)")
    check("_body" in dump or "rfile" in dump or "readinto" in dump,
          "the parked request thread's frame is in the dump")
    check(alive and up(), "and it still serves afterwards")
    parked.close()
finally:
    if p.poll() is None:
        p.kill()
    p.wait()

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)
