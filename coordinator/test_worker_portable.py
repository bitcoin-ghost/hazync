#!/usr/bin/env python3
"""The worker imports and locks correctly without `fcntl` — the one import that barred Windows (#616).

📏 WHY. There are 11 provers on the board, and the contribute page says *"You need Linux on x86-64
and an NVIDIA GPU"*. The bottleneck on participation is not awareness — most people who would help
cannot run it. `coordinator/hazync` did `import contextlib, fcntl` at module scope, and **fcntl does
not exist on Windows**, so the worker was unimportable there before it did anything at all.

⛔ WHAT THIS TEST CAN AND CANNOT SHOW. It runs on POSIX, so it verifies:

  * the module imports with `fcntl` HIDDEN — the Windows condition, simulated by blocking the import
  * the POSIX path still takes a real lock, and two holders genuinely serialise
  * `fcntl`/`os.killpg` appear only inside platform-guarded helpers, never at module scope

⚠ It does NOT exercise `msvcrt.locking` or `taskkill`. There is no Windows machine in this project's
CI, and pretending otherwise would be worse than saying so: those branches are written to be correct
and are **not claimed to be verified**.

    python3 test_worker_portable.py             # imports without fcntl; POSIX locking still serialises
    python3 test_worker_portable.py --control   # fcntl imported at module scope — Windows must break
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "hazync")
CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


SRC = open(WORKER, encoding="utf8").read()

# ── 1. no bare platform-only imports or calls at module scope ───────────────────────────────────
# ⚠ Asserting on the SOURCE, because the failure is an ImportError at module load: by the time a
# Windows user sees it, nothing of this file has run and there is nothing to introspect.
check("import contextlib, fcntl" not in SRC,
      "⛔ `fcntl` is not imported unconditionally at module scope — that alone barred Windows")
check("try:\n    import fcntl" in SRC, "it is imported inside a try, with a Windows fallback")
check(SRC.count("os.killpg") == 1,
      f"os.killpg appears once, inside the guarded helper ({SRC.count('os.killpg')})")
check('if os.name != "nt":' in SRC, "and the helper branches on the platform explicitly")

# ── 2. ⛔ THE ONE THAT MATTERS: it imports with fcntl unavailable ────────────────────────────────
# A sitecustomize that makes `import fcntl` raise ImportError reproduces the Windows condition
# exactly, without needing Windows.
# ⛔ `find_module`/`load_module` WERE REMOVED IN PYTHON 3.12. The first version of this shim used
# them: it worked on 3.10 here and did NOTHING on the 3.12 in CI, where `import fcntl` therefore
# succeeded and the test failed for a reason that had nothing to do with the worker. `find_spec` is
# the API that exists on both, and raising from it propagates to the importer.
shim = tempfile.mkdtemp(prefix="nofcntl-")
with open(os.path.join(shim, "sitecustomize.py"), "w") as fh:
    fh.write(
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'fcntl':\n"
        "            raise ImportError('no module named fcntl (simulated Windows)')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "sys.modules.pop('fcntl', None)\n"
    )

# ⛔⛔ VERIFY THE SHIM BEFORE TRUSTING WHAT IT SHOWS. A shim that silently stops working turns this
# whole file into a test that cannot fail -- which is exactly what happened on 3.12. So prove that
# `import fcntl` DOES fail under it, on this interpreter, before asking anything about the worker.
_env = dict(os.environ, PYTHONPATH=shim + os.pathsep + os.environ.get("PYTHONPATH", ""))
_probe = subprocess.run([sys.executable, "-c", "import fcntl; print('IMPORTED')"],
                        capture_output=True, text=True, timeout=60, env=_env)
if _probe.returncode == 0:
    print(f"  FAIL the fcntl-blocking shim does NOT work on {sys.version.split()[0]} — every result "
          f"below would be vacuous ({(_probe.stdout or '').strip()})")
    sys.exit(1)
print(f"  ok   the shim blocks `import fcntl` on this interpreter ({sys.version.split()[0]}), so the "
      f"Windows condition is real and not assumed")

probe = (
    "import importlib.util, sys\n"
    "import fcntl\n" if CONTROL else
    "import importlib.util, sys\n"
)
# ⚠ `coordinator/hazync` has NO .py extension, so spec_from_file_location cannot infer a loader and
# returns None. An explicit SourceFileLoader is the supported way to import an extension-less script
# -- the same reason nothing else in this repo imports the worker.
probe += (
    "import importlib.machinery\n"
    f"ldr = importlib.machinery.SourceFileLoader('w', {WORKER!r})\n"
    "spec = importlib.util.spec_from_loader('w', ldr)\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "ldr.exec_module(m)\n"
    "print('IMPORTED', 'fcntl' if m.fcntl is not None else 'msvcrt-path')\n"
)
r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, env=_env)

if CONTROL:
    check(r.returncode != 0 and "fcntl" in (r.stderr or ""),
          f"⛔ control: a module-scope `import fcntl` fails outright where fcntl is missing "
          f"(rc={r.returncode}) — this is what every Windows user hit")
else:
    check(r.returncode == 0,
          f"the worker imports with fcntl unavailable (rc={r.returncode}) "
          f"{(r.stderr or '').strip()[-120:]}")
    # ⚠ On this POSIX box neither fcntl NOR msvcrt is importable once fcntl is blocked, which is a
    # platform that exists nowhere real — but it is the strictest version of the question, and the
    # first implementation took the whole worker down with ModuleNotFoundError rather than losing a
    # lock it could not take.
    check("IMPORTED msvcrt-path" in (r.stdout or ""),
          f"and takes the non-POSIX branch, degrading to no lock rather than dying "
          f"({(r.stdout or '').strip()})")

# ── 3. the POSIX path still actually locks ──────────────────────────────────────────────────────
# ⚠ Behavioural, not structural: two processes take the lock and the second must WAIT. A lock that
# imports cleanly and serialises nothing would pass every check above.
if not CONTROL:
    lockfile = os.path.join(tempfile.mkdtemp(prefix="gpulock-"), "gpu.lock")
    holder = (
        "import importlib.util, importlib.machinery, os, time\n"
        f"os.environ['HAZYNC_GPU_LOCK'] = {lockfile!r}\n"
        f"ldr = importlib.machinery.SourceFileLoader('w', {WORKER!r})\n"
        "spec = importlib.util.spec_from_loader('w', ldr)\n"
        "m = importlib.util.module_from_spec(spec); ldr.exec_module(m)\n"
        "with m.gpu_lock('test'):\n"
        "    print('HELD', flush=True); time.sleep(3)\n"
    )
    p1 = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    first = p1.stdout.readline().strip()          # wait until it genuinely holds the lock
    t0 = time.time()
    r2 = subprocess.run([sys.executable, "-c", holder], capture_output=True, text=True, timeout=120)
    waited = time.time() - t0
    p1.wait(timeout=30)
    check(first == "HELD", f"the first holder acquires the lock ({first!r})")
    check(r2.returncode == 0, f"the second acquires it too, once released (rc={r2.returncode})")
    check(waited > 1.5,
          f"⛔ and it WAITED {waited:.1f}s for the first to finish — the lock serialises rather "
          f"than merely existing")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — the control must fail to import without fcntl")
        sys.exit(1)
    print("PASS (control): a module-scope fcntl import bars every Windows machine")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")
