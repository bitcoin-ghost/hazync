#!/usr/bin/env python3
"""A downloaded proof arrives named, with its own extension, and the bytes are untouched (#278).

Drives the REAL HTTP handler on a loopback port: what is tested is the wiring in do_GET, not a helper
nobody calls. `curl -O` names a file after the URL path, so /api/proof/1 used to land as an
extensionless "1" -- the Content-Disposition is the whole of the fix, and it is one header that any
refactor of the download path can drop without a single test noticing.

Also pins the host<->worker FILENAME CONTRACT. The worker does not pass HAZYNC_OUT for per-block
proves, so it has to know what the host called the file. That contract is invisible in both files and
breaks AFTER the expensive step is paid for: the prove succeeds and the fold cannot find its input.

  python3 test_proof_names.py            # must PASS
  python3 test_proof_names.py --control  # drops the Content-Disposition; the checks MUST FAIL
"""
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
server.init_db()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if CONTROL:
    # The pre-#278 behaviour: serve the bytes with no name. Everything else is identical.
    _real_send = server.H._send

    def _no_disposition(self, code, obj=None, ctype="application/json", raw=None, headers=None):
        headers = {k: v for k, v in (headers or {}).items() if k != "Content-Disposition"}
        return _real_send(self, code, obj, ctype, raw, headers or None)

    server.H._send = _no_disposition
    print("CONTROL: Content-Disposition stripped -- the checks below MUST fail")

# Two stored proofs (a single block and a folded range) and a spine head. Bytes are arbitrary: this
# endpoint serves a file, it does not verify one.
SINGLE = b"\x01\x02\x03 not-a-real-receipt"
RANGE = b"\x04\x05\x06 folded"
SPINE = b"\x07\x08\x09 spine"
with open(os.path.join(server.PROOFS_DIR, "proof_1.bin"), "wb") as f:
    f.write(SINGLE)
with open(os.path.join(server.PROOFS_DIR, "proof_1-2.bin"), "wb") as f:
    f.write(RANGE)
with open(os.path.join(server.SPINE_DIR, "spine.bin"), "wb") as f:
    f.write(SPINE)
with open(os.path.join(server.SPINE_DIR, "spine.json"), "w") as f:
    json.dump({"lo": 1, "hi": 4242, "handle": "G H O S T"}, f)

httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{httpd.server_address[1]}"

fails = 0


def check(cond, what):
    global fails
    sys.stdout.write(("  ok   " if cond else "  FAIL ") + what + "\n")
    fails += 0 if cond else 1


def get(path):
    try:
        with urllib.request.urlopen(URL + path, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def filename(headers):
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r'filename="([^"]*)"', cd)
    return m.group(1) if m else None


try:
    code, h, body = get("/api/proof/1")
    check(code == 200 and body == SINGLE, "a single-block proof downloads, byte for byte")
    check(filename(h) == "hazync-1.hzk", "  ...named hazync-1.hzk, not the URL's bare '1'")

    code, h, body = get("/api/proof/1-2")
    check(code == 200 and body == RANGE, "a folded range downloads, byte for byte")
    check(filename(h) == "hazync-1-2.hzk", "  ...named hazync-1-2.hzk")

    code, h, body = get("/api/spine/proof")
    check(code == 200 and body == SPINE, "the spine downloads, byte for byte")
    check(filename(h) == "hazync-spine-1-4242.hzk", "  ...named for the range it actually covers")

    # The name is built from the PARSED range, never the raw path segment. A header is not a path, and
    # parse_any_range's shape check was never written as a header-injection check -- it just happens to
    # be one. If the name is ever built from the raw segment instead, this is what notices.
    code, h, _ = get("/api/proof/1%0d%0aX-Injected:%20yes")
    check(code == 404, "a header-injection shaped id is refused outright")
    check(h.get("X-Injected") is None, "  ...and injects no header")
    code, h, _ = get("/api/proof/1")
    check("\r" not in (h.get("Content-Disposition") or "") and "\n" not in (h.get("Content-Disposition") or ""),
          "  ...the served name carries no CR/LF")

    # Stored names are deliberately NOT renamed: check-retention.py parses proof_<rid>.bin to prove no
    # proven height lacks a receipt, and a rename would silently punch a hole in that gate.
    check(os.path.exists(os.path.join(server.PROOFS_DIR, "proof_1.bin")),
          "the file AT REST keeps its name (check-retention.py parses it)")

    # --- the host <-> worker filename contract -------------------------------------------------
    host_src = open(os.path.join(ROOT, "prover", "host", "src", "main.rs")).read()
    worker_src = open(os.path.join(ROOT, "coordinator", "hazync")).read()
    host_defaults = set(re.findall(r'unwrap_or_else\(\|_\| format!\("(range|chunk)_\{\w+\}\.(\w+)"\)\)', host_src))
    exts = {ext for _, ext in host_defaults}
    check(host_defaults and exts == {"hzk"},
          f"every host default output is .hzk (found {sorted(exts) or 'none'})")
    check("_prover_out(work" in worker_src,
          "the worker RESOLVES the prover's output name rather than assuming it")
    check(not re.search(r'parts\.append\(f"range_\{h\}\.(bin|hzk)"\)', worker_src),
          "  ...and no longer hard-codes one extension for the fold inputs")
    check('.hzk"' in worker_src and '.bin"' in worker_src,
          "  ...accepting BOTH, so a worker and host from different releases still fold")
finally:
    httpd.shutdown()

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)      # the control passes only if the checks caught the missing header
sys.exit(1 if fails else 0)
