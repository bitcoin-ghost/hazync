#!/usr/bin/env python3
"""Generate docs/COORDINATOR_REFERENCE.md from the coordinator's own source.

    python3 scripts/gen-coordinator-reference.py            # rewrite the doc
    python3 scripts/gen-coordinator-reference.py --check    # fail if the committed doc has drifted
    python3 scripts/gen-coordinator-reference.py --control  # prove --check can fail

WHY GENERATED. `/api/rotate` shipped in #113 and was documented nowhere; `claim`, `beat` and several
environment variables were each described in a different doc with a different default. A hand-kept API
reference drifts the day a route is added. So the facts that CAN be read from the code — every route,
every environment variable, its default as written, where it is read, which handlers check a signature,
which status codes a handler returns — are read from it, with `ast` rather than regex, so a reformat does
not change the answer.

What cannot be derived is a one-line MEANING. That lives in the maps below, and they are held to the code
in both directions: a route or variable with no description fails, and a description for something the
code no longer has fails. That is what stops the maps rotting the way a hand-written page does.

The output deliberately carries no line numbers, timestamps or commit ids, so it changes only when the
code's behaviour does.
"""
import argparse
import ast
import difflib
import os
import re
import shutil
import sys
import tempfile
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = "docs/COORDINATOR_REFERENCE.md"
SOURCES = {
    "server": "coordinator/server.py",
    "cli": "coordinator/hazync",
    "launcher": "coordinator/run-workers.sh",
    "nginx": "coordinator/deploy/nginx-hazync.conf",
}

# ── hand-written meanings, each checked against the handler when written ─────────────────────────────
# Keyed "METHOD path" exactly as extracted. Prefix routes end in "/" plus a placeholder.
ROUTES = {
    "GET /api/state":
        "Board snapshot: `progress`, `blocked` (the frontier's next block and why), the board window, "
        "`leaderboard`, `recent` submissions, `claims`, `fold_claims` (#333), `frontier_proof`, `timeline`, `signatures`, "
        "`verify_mode`. `?slim=1` omits `vranges`. Coalesced for `STATE_CACHE_TTL` (`state_cached`).",
    "GET /api/vranges":
        "The full verified-range index (`lo`, `hi`, `handle`, `fold`, `proof` link), built by "
        "`build_vranges`. Single-flight cache for `VRANGES_CACHE_TTL`; weak `ETag`, `304` on "
        "`If-None-Match`.",
    "GET /api/blockstatus":
        "Every block's furthest state as runs `[lo, hi, status]` (3 proven, 4 folded, 5 anchored). "
        "`?prover=<handle>` keeps only ranges that prover proved; `404` for an unknown handle. `ETag`/`304`.",
    "GET /api/block/<height>":
        "Everything about one block: proofs covering it, who anchored it, a live claim, a public sponsor. "
        "`400` unless the segment is 1-9 digits; `404` above the chain tip. A live fold claim covering the block is `fold_claim` (#333).",
    "GET /api/sponsor":
        "Sponsorship settings: `open`, `max_blocks`, `payments` (always `false`), `priced`, `bands`, "
        "`btc_usd`, `name_max`. See `docs/SPONSORSHIP.md`.",
    "GET /api/sponsor/quote":
        "Minimum USD and sats for `?lo=&hi=`. Answers while sponsorship is closed; refuses spans that are "
        "not open (proven, anchored, claimed or already held).",
    "GET /api/sponsor/status/<token>":
        "One sponsorship, looked up by the sha256 of its private token (`[A-Za-z0-9_-]{16,128}`); "
        "`404` otherwise.",
    "GET /api/sponsors":
        "Public table of sponsorships whose status is paid, proving or proven and whose settled amount "
        "covers the minimum (`SPONSOR_PUBLIC_SQL`).",
    "GET /api/pick":
        "Advice only, claims nothing: the first block after the frontier that is not proven here or at a "
        "peer, not held, not (on the first pass) being proven at a peer, and has a witness. `404` if none.",
    "GET /api/meta":
        "Pre-flight: `method_id` (the `method-id` output of `HAZYNC_HOST`), `frontier`, `reproduce` "
        "(`reproduce/METHOD_ID`) and `source_sha256` of the running `server.py`.",
    "GET /api/foldable":
        "Sibling pairs of the canonical fold tree whose parent is not yet verified. `?limit=` clamped to "
        "1-32, default 32 (`FOLDABLE_DEFAULT`; 8 before #333). A pair under a live fold claim is left out.",
    "GET /api/spine":
        "Spine head metadata (`lo`, `hi`, `out_tip`, `out_leaves`, `range_work`, `sha256`, `bytes`, "
        "`handle`, `pubkey`, `ts`); `404` before the first spine.",
    "GET /api/spine/segments":
        "Who absorbed each block into the spine, as runs keyed on pubkey (#244). Cached for "
        "`VRANGES_CACHE_TTL`.",
    "GET /api/spine/proof":
        "The spine receipt bytes, served as `hazync-spine-1-<hi>.hzk`. Check with `hazync-verify`.",
    "GET /api/proof/<id>":
        "A retained verified receipt for range id `n` or `lo-hi`, served as `hazync-<lo>[-<hi>].hzk`. The "
        "id is shape-checked by `parse_any_range` before it becomes a path. Re-verify with "
        "`host verify-any`.",
    "GET /api/witnesses":
        "Bulk bundle sync (#69): a streamed tar of `MANIFEST.json` (`served`, `missing`) then the bundles "
        "for `?from=&count=`, `count` at most `BULK_MAX`. `400` on a malformed request.",
    "GET /api/witness/<height>":
        "One witness: the bridge bundle `bundle_<n>.json` if present, else the legacy `block_<n>.json`. "
        "Accepts a height or a range id (its `lo`). `404` if neither exists.",
    "GET /<path>":
        "Static files under `COORD_WEB`, contained to that directory; `/` serves `index.html`. Not rate "
        "limited.",
    "OPTIONS *":
        "CORS pre-flight: `204` with `Access-Control-Allow-Origin: *`.",
    "POST /api/submit":
        "Submit a receipt for range id `range`, signed over the receipt bytes. A range wider than one "
        "block must be tiled exactly by verified ranges (a fold, #281). Blocks held for a sponsorship are "
        "accepted only from that sponsorship's registered key. Verified with `host verify-any`; a bad "
        "signature or proof is `422`, not `403`. No prior claim is required.",
    "POST /api/claim":
        "Take the earliest block that is not proven or held, width 1; frontier+1 is re-offered first when "
        "a cover of it cannot seam (#281). A claim signed by its key over `claim:<nonce>:<ts>` (within "
        "`BEAT_SKEW`) speaks for that key: the per-key cap and the re-take wait count signed and unsigned claims "
        "apart, and a signature that does not verify is refused, not treated as unsigned (#310). Unsigned claims "
        "are accepted unless `CLAIM_REQUIRE_SIG=1`. A `nonce` makes a retried claim return the same block (#268). "
        "Advisory: `submit` never requires it.",
    "POST /api/spine":
        "Submit a new spine head, signed over the receipt. `host verify-range` must pass (full genesis "
        "pin), then `verify-any` supplies `lo`/`hi`; `lo` must be 1 and `hi` must exceed the current head "
        "(`409` otherwise). Recorded as a `spine:1-<hi>` submission.",
    "POST /api/foldclaim":
        "Reserve one pair offered by `/api/foldable` for `FOLD_CLAIM_TTL` seconds, so other folders are not "
        "offered it (#333). Signed by its key over `foldclaim:<result>:<nonce>:<ts>` within `BEAT_SKEW`. Only a key "
        "with a verified submission may hold one (`403` with `unproven` otherwise), at most `FOLD_CLAIM_CAP` at a "
        "time (`429`); `409` if another key holds the pair or it is already proven. The holder asking again gets its "
        "claim back, never extended. Held in memory. Advisory: `submit` accepts a valid fold from anyone, and a "
        "verified fold releases its claim.",
    "POST /api/beat":
        "Keep the caller's own claim alive. Signed over `<range>:<ts>` with `ts` as integer seconds within "
        "`BEAT_SKEW`. Only the assignee of a live claim may beat it, and not past `CLAIM_MAX`. Rejected "
        "beats are logged (`log_beat_rejection`).",
    "POST /api/rotate":
        "Key rotation (#113): both `old_pubkey` and `new_pubkey` sign `hazync-rotate-v1:<old>:<new>:<ts>` "
        "(`rotate_message`), `ts` within `ROTATE_MAX_SKEW`. Refuses keys on the moderation list, an old key "
        "that already rotated (`409`) and cycles. Records an edge in `rotations`; `vranges` and "
        "`submissions` are not rewritten, the old key keeps working, and its work resolves to the head. "
        "Used by `hazync rotate`.",
    "POST /api/sponsor":
        "Record a sponsorship request `{lo, hi, name, amount_sats}`. `503` unless `SPONSOR_OPEN=1`; "
        "`amount_sats` must reach the span's minimum. Charges nothing. Returns `202` with the private status "
        "token once; only its sha256 is stored.",
}

# Keyed "<source key>:<NAME>".
ENV = {
    # coordinator/server.py
    "server:COORD_PORT": "Listen port.",
    "server:COORD_BIND": "Listen address. Use `127.0.0.1` behind a reverse proxy; a non-loopback bind in an "
                         "insecure mode refuses to start (see `COORD_ALLOW_PUBLIC_INSECURE`).",
    "server:COORD_DB": "SQLite database path.",
    "server:COORD_WEB": "Directory served as static files.",
    "server:TIP_HEIGHT": "Floor for `chain_tip()`, which bounds acceptable range ids and the progress "
                         "denominator.",
    "server:TIP_CACHE_TTL": "Seconds `_servable_high()` caches the highest block with a bundle on disk.",
    "server:TIP_FILE": "File holding the node's height, written by `deploy/hazync-node-tip.*`.",
    "server:TIP_FILE_MAX_AGE": "Seconds after which `TIP_FILE` is ignored as stale.",
    "server:RANGE_SIZE": "Width of a board row, the legacy claim grid, and the default `BULK_MAX`.",
    "server:SEED_RANGES": "Number of `RANGE_SIZE` rows `init_db()` seeds.",
    "server:WITNESS_DIR": "Legacy per-block witnesses, `block_<n>.json`.",
    "server:HAZYNC_BRIDGE_OUT": "Archive-node bundle directory, `bundle_<n>.json`; empty means none.",
    "server:BULK_MAX": "Maximum `count` for one `GET /api/witnesses`.",
    "server:HAZYNC_HOST": "Host binary run for `verify-any`, `verify-range` and `method-id`.",
    "server:VERIFY_MODE": "`real` runs the host; `mock` accepts without verifying and still needs "
                          "`COORD_ALLOW_MOCK`.",
    "server:COORD_STATE": "Scratch directory for receipts while they are verified.",
    "server:COORD_PROOFS": "Retained receipts, `proof_<id>.bin`, served by `/api/proof/`.",
    "server:COORD_SPINE": "`spine.bin` and `spine.json`.",
    "server:GENESIS_TIP": "Genesis block hash (internal byte order) used by `is_genesis_anchored`.",
    "server:CLAIM_TTL": "Seconds a claim stays live after `COALESCE(last_beat, claimed_at)`; a claim that has "
                        "never beaten is released sooner, at `CLAIM_GRACE`.",
    "server:CLAIM_MAX": "Hard cap in seconds from `claimed_at`, whatever the beats.",
    "server:CLAIM_GRACE": "Seconds before a claim that has never beaten is released (#296).",
    "server:CLAIM_OPEN_MAX": "Live claims one key may hold at once; a further claim is refused with 429 until one is "
                             "proven or lapses. `0` means no limit.",
    "server:CLAIM_REQUIRE_SIG": "`1` refuses claims that are not signed by their key (#310). Off by default: workers "
                                "up to v0.21.4 do not sign claims.",
    "server:CLAIM_RETAKE_WAIT": "Seconds before a key may re-take a block its own claim let lapse without a heartbeat; "
                                "other keys are offered it at once. `0` means straight away.",
    "server:FOLD_CLAIM_TTL": "Seconds a fold claim (`POST /api/foldclaim`) holds its pair. Held in memory, so a "
                             "restart forgets them (#333).",
    "server:FOLD_CLAIM_CAP": "Live fold claims one key may hold at once; a further one is refused with 429 (#333).",
    "server:BEAT_SKEW": "Allowed distance in seconds between a beat's signed `ts` and server time.",
    "server:MAX_ATTEMPTS": "Failure count at which `/api/state` flags the frontier blocker as needing "
                           "attention.",
    "server:MAX_ENV_FAILURES": "Intended cap for environmental failures.",
    "server:CLAIM_WIDTH": "Second accepted claim-id grid in `parse_range`, beside `RANGE_SIZE`.",
    "server:MAX_BODY": "Maximum POST body and base64 receipt length, in bytes (`413` above it).",
    "server:MAX_HANDLE": "Handle length cap. A registered sponsor key gets room for `SPONSOR: ` plus a "
                         "40-character name if that is longer (`handle_cap`).",
    "server:RATE_MAX": "POST requests per IP per window.",
    "server:RATE_MAX_GET": "`GET /api/*` requests per IP per window.",
    "server:RATE_WINDOW": "Rate-limit window, seconds.",
    "server:RATE_MAP_MAX": "Rate map size at which aged-out keys are evicted.",
    "server:STATE_CACHE_TTL": "Seconds `/api/state` is coalesced.",
    "server:CACHE_MAX_STALE": "Seconds past its TTL that `/api/state` and `/api/blockstatus` may still be "
                              "served while they rebuild in the background; beyond it the caller rebuilds.",
    "server:TRUSTED_PROXIES": "Peers whose `X-Forwarded-For` is believed, comma-separated.",
    "server:HANDLE_DENY": "Reserved handles, compared after reducing to lowercase letters and digits.",
    "server:MOD_BLOCK_FILE": "Takedown list of pubkeys hidden from the public board; re-read on every call.",
    "server:VRANGES_CACHE_TTL": "Cache TTL for `/api/vranges` and `/api/spine/segments`.",
    "server:VERIFY_CONCURRENCY": "Maximum concurrent receipt verifications (`_verify_sem`).",
    "server:RATE_EXEMPT": "IPs exempt from rate limiting, comma-separated.",
    "server:DB_BUSY_TIMEOUT": "SQLite busy timeout, seconds.",
    "server:PEER_COORDINATORS": "Peer coordinator base URLs, comma-separated; enables peer sync.",
    "server:PEER_TTL": "Seconds peers' proven and busy heights are cached.",
    "server:PEER_BUSY_MAX_WIDTH": "Widest peer claim honoured as busy, blocks.",
    "server:PEER_BUSY_MAX_TOTAL": "Most busy heights accepted from peers.",
    "server:COORD_ALLOW_UNSIGNED": "If set and no ed25519 library is present, `verify_sig` accepts "
                                   "everything. Development only.",
    "server:ROTATE_MAX_SKEW": "Allowed distance in seconds between a rotation's `ts` and server time.",
    "server:COORD_ALLOW_MOCK": "Required for `VERIFY_MODE=mock` to accept anything.",
    "server:FRONTIER_CACHE_TTL": "Seconds the frontier chain is cached (single-flight).",
    "server:FRONTIER_SETTLE": "Seconds a verified cover of frontier+1 must predate the frontier snapshot before "
                              "`claim()` re-offers it and `/api/state` calls it unseamable (#339).",
    "server:SPONSOR_OPEN": "`1` opens `POST /api/sponsor`.",
    "server:SPONSOR_MAX_BLOCKS": "Largest span one sponsorship may cover.",
    "server:SPONSOR_PRICE_BANDS": "JSON `[[lo, hi, usd_per_block], ...]`; set but invalid means unpriced.",
    "server:SPONSOR_BTC_USD": "Dollars per bitcoin for converting minimums to sats; unset means no sats "
                              "minimum, so nothing can be sponsored.",
    "server:SPONSOR_HOLD_ALERT": "Age in seconds after which a hold on the frontier's next block is "
                                 "reported in `/api/state`.",
    "server:BEAT_LOG_WINDOW": "Seconds identical beat rejections are collapsed into one log line.",
    "server:COORD_ALLOW_PUBLIC_INSECURE": "Allows a non-loopback bind in an insecure mode. Not for "
                                          "production.",
    "server:PEER_SYNC_INTERVAL": "Seconds between peer sync passes (only when `PEER_COORDINATORS` is set).",
    # coordinator/hazync (shipped as hazync-worker)
    "cli:HAZYNC_HOME": "Identity (`key.hex`, `handle`) and receipts. A different directory is a different "
                       "contributor.",
    "cli:COORD_URL": "Coordinator base URL.",
    "cli:HAZYNC_HOST": "Prover binary. Unset: looked for beside the CLI, in `$HAZYNC_HOME/bin`, "
                       "`$HAZYNC_HOME`, the working directory, then `hazync-*` names on `PATH`.",
    "cli:WITNESS_DIR": "Legacy per-block witnesses for the replay path.",
    "cli:BUNDLE_DIR": "Bundles fetched from the coordinator for the bridge path.",
    "cli:HAZYNC_ALLOW_DEV_WRITES": "`1`, `true` or `yes` lets a source checkout (`VERSION = \"dev\"`) POST to "
                                   "the default public coordinator.",
    "cli:HAZYNC_GPU_LOCK": "Lock file serialising GPU jobs on one box; `none` disables it.",
    "cli:HAZYNC_STALL_MIN": "Minimum seconds without segment progress before a prove is killed.",
    "cli:HAZYNC_FIRST_PROGRESS": "Seconds allowed before the first segment completes.",
    "cli:HAZYNC_ASSEMBLY_MIN": "Floor, in seconds, of the silent lift-and-join budget after the last "
                               "segment.",
    "cli:HAZYNC_PROVE_TIMEOUT": "Outer bound, seconds, for one host invocation (6 h for a watched prove, "
                                "90 min otherwise).",
    "cli:HAZYNC_TICK": "Seconds between progress lines; a beat is sent on a tick only if a segment finished "
                       "since the last one.",
    "cli:HAZYNC_FOLD_CONCURRENCY": "Folds run concurrently within one tree level.",
    "cli:HAZYNC_SPINE_VRANGES_TTL": "Seconds `hazync spine` reuses its copy of `/api/vranges`.",
    "cli:HAZYNC_NTFY": "An ntfy topic or URL for alerts when this worker stops or cannot work; overrides what "
                       "`hazync notify` saved. `off` disables.",
    "cli:HAZYNC_NTFY_REPEAT": "Seconds before the same problem is pushed again.",
    # coordinator/run-workers.sh
    "launcher:COORD_URL": "Coordinator base URL, used for the `/api/meta` guest-id pre-flight.",
    "launcher:LOG_DIR": "Per-worker logs `worker_<i>.log` and bundle directories `bundles_<i>`.",
    "launcher:MODE": "What the loops run; see the modes table.",
    "launcher:HAZYNC_HOST": "Prover binary. Required, and must be executable.",
    "launcher:HAZYNC_BASE": "Core/secp source root, exported to the workers.",
    "launcher:SKIP_GPU_SMOKE": "Non-empty skips the pre-flight `prove-block` on a box with a GPU (#261).",
    "launcher:HAZYNC_HOME": "Where the handle check looks for `handle`.",
    "launcher:NOTIFY_FAIL_STREAK": "Failures in a row before a worker loop pushes an alert (`hazync notify`); exit 75, "
                                  "nothing to claim right now, does not count.",
}

# Variables the CLI or launcher sets for the processes it starts. Keyed "<source key>:<NAME>".
PASSED = {
    "cli:HAZYNC_BRIDGE_OUT": "Bundle directory for `prove-range-bridge`.",
    "cli:HAZYNC_WITNESS_DIR": "Witness directory for the replay path `prove-range`.",
    "cli:HAZYNC_SEG_PO2": "Segment size for this attempt, stepping down on failure; an inherited value "
                          "sets the first attempt.",
    "cli:HAZYNC_PROGRESS_EVERY": "Makes the host print one line per completed segment, which the watchdog "
                                 "reads (#256).",
    "launcher:BUNDLE_DIR": "One bundle directory per worker loop.",
}

MODES = {
    "prove": "every loop runs `hazync run`",
    "fold": "every loop runs `hazync fold`",
    "mixed": "with N > 1 loop N-1 folds, and with N > 2 loop N advances the spine; the rest prove. Run it on "
             "ONE box: the spine needs one worker fleet-wide",
    "spine": "every loop runs `hazync spine`",
}


# ── extraction ───────────────────────────────────────────────────────────────────────────────────────
def _src(node):
    return ast.unparse(node)


def _is_environ(node):
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _parents(tree):
    par = {}
    for n in ast.walk(tree):
        for ch in ast.iter_child_nodes(n):
            par[ch] = n
    return par


def _site(node, par):
    """Where a read happens, named by something that survives an edit: a function or a constant."""
    cur, assign = node, None
    while cur in par:
        cur = par[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return f"`{cur.name}()`"
        if isinstance(cur, ast.If) and "__main__" in _src(cur.test):
            return "`__main__`"
        if isinstance(cur, (ast.Assign, ast.AnnAssign)) and assign is None:
            assign = cur
    if assign is not None:
        tgt = assign.targets[0] if isinstance(assign, ast.Assign) else assign.target
        return f"constant `{_src(tgt)}`"
    return "module"


def python_env(path):
    """Every environment read: os.environ.get / os.getenv / os.environ[...], with the default as written."""
    tree = ast.parse(open(path).read())
    par = _parents(tree)
    out = {}

    def add(name, default, node):
        e = out.setdefault(name, {"defaults": [], "sites": [], "consts": [], "line": node.lineno})
        e["line"] = min(e["line"], node.lineno)
        if default not in e["defaults"]:
            e["defaults"].append(default)
        s = _site(node, par)
        if s not in e["sites"]:
            e["sites"].append(s)
        if s.startswith("constant "):
            e["consts"].append(s[len("constant `"):-1])

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.args \
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            f = n.func
            env_get = f.attr == "get" and _is_environ(f.value)
            getenv = f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os"
            if env_get or getenv:
                default = _src(n.args[1]) if len(n.args) > 1 else None
                p = par.get(n)
                if default is None and isinstance(p, ast.BoolOp) and isinstance(p.op, ast.Or) \
                        and p.values[0] is n:
                    default = " or ".join(_src(v) for v in p.values[1:])
                add(n.args[0].value, default, n)
        if isinstance(n, ast.Subscript) and _is_environ(n.value) \
                and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
            default = None
            cur = n
            while cur in par:
                cur = par[cur]
                if isinstance(cur, ast.IfExp) and f'"{n.slice.value}" in os.environ' in _src(cur.test).replace("'", '"'):
                    default = f"{_src(cur.orelse)} (when unset)"
                    break
                if isinstance(cur, ast.If) and f'"{n.slice.value}"' in _src(cur.test).replace("'", '"'):
                    default = "(only read when set)"
                    break
            add(n.slice.value, default, n)
    # A constant read from the environment and never referenced again is configuration that does nothing.
    loads = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            loads[n.id] = loads.get(n.id, 0) + 1
    for e in out.values():
        e["unused"] = bool(e["consts"]) and all(loads.get(c, 0) == 0 for c in e["consts"])
    return dict(sorted(out.items(), key=lambda kv: kv[1]["line"]))    # ast.walk is breadth-first


def python_passed(path):
    """Variables the CLI sets for the host: dict(os.environ/env, NAME=...) and env.setdefault("NAME", ...)."""
    tree = ast.parse(open(path).read())
    par = _parents(tree)
    out = {}
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name) and n.func.id == "dict" and n.args:
            for kw in n.keywords:
                if kw.arg and kw.arg.isupper():
                    e = out.setdefault(kw.arg, {"values": [], "sites": []})
                    v = _src(kw.value)
                    if v not in e["values"]:
                        e["values"].append(v)
                    s = _site(n, par)
                    if s not in e["sites"]:
                        e["sites"].append(s)
        if isinstance(n.func, ast.Attribute) and n.func.attr == "setdefault" and len(n.args) == 2 \
                and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).isupper():
            e = out.setdefault(n.args[0].value, {"values": [], "sites": []})
            v = _src(n.args[1]) + " (unless already set)"
            if v not in e["values"]:
                e["values"].append(v)
            s = _site(n, par)
            if s not in e["sites"]:
                e["sites"].append(s)
    return out


def _comments(path):
    """{line: trailing comment text} for a Python file."""
    out = {}
    with open(path, "rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string.lstrip("#").strip()
    return out


def _returned_codes(fn):
    codes = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple) and n.value.elts:
            first = n.value.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, int):
                codes.add(first.value)
            elif isinstance(first, ast.IfExp):
                for b in (first.body, first.orelse):
                    if isinstance(b, ast.Constant) and isinstance(b.value, int):
                        codes.add(b.value)
    return codes


def _sig_checks(fn):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "verify_sig" \
                and len(n.args) == 3:
            out.append((_src(n.args[0]), _src(n.args[2])))
    return out


def server_routes(path):
    src = open(path).read()
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "H")
    meth = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    routes = []

    def called(body):
        names, codes, params = [], set(), []
        for stmt in body:
            for n in ast.walk(stmt):
                if not isinstance(n, ast.Call):
                    continue
                if isinstance(n.func, ast.Name):
                    if n.func.id in funcs:
                        if not n.func.id.startswith("_") and n.func.id not in names:
                            names.append(n.func.id)
                        codes |= _returned_codes(funcs[n.func.id])
                    if n.func.id == "_int" and n.args and isinstance(n.args[0], ast.Constant):
                        if n.args[0].value not in params:
                            params.append(n.args[0].value)
                if isinstance(n.func, ast.Attribute):
                    a = n.func
                    if a.attr in ("_send", "send_response") and n.args \
                            and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, int):
                        codes.add(n.args[0].value)
                    if a.attr == "get" and n.args and isinstance(n.args[0], ast.Constant) \
                            and isinstance(n.args[0].value, str) \
                            and ("parse_qs" in _src(a.value) or _src(a.value) == "q"):
                        if n.args[0].value not in params:
                            params.append(n.args[0].value)
        return names, codes, params

    # GET: the if-chain in do_GET, in code order.
    for stmt in meth["do_GET"].body:
        if not isinstance(stmt, ast.If):
            continue
        t = stmt.test
        path = prefix = None
        if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "p" \
                and isinstance(t.ops[0], ast.Eq) and isinstance(t.comparators[0], ast.Constant):
            path = t.comparators[0].value
        elif isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute) and t.func.attr == "startswith" \
                and _src(t.func.value) == "p" and isinstance(t.args[0], ast.Constant):
            prefix = t.args[0].value
        elif isinstance(t, ast.BoolOp):
            continue                                    # the /api/ rate-limit guard, reported globally
        if path is None and prefix is None:
            continue
        names, codes, params = called(stmt.body)
        routes.append({"method": "GET", "path": path, "prefix": prefix, "handlers": names,
                       "codes": codes, "params": params, "auth": []})
    routes.append({"method": "GET", "path": None, "prefix": "/", "static": True, "handlers": [],
                   "codes": {200, 404}, "params": [], "auth": []})
    if "do_OPTIONS" in meth:
        _, codes, _ = called(meth["do_OPTIONS"].body)
        routes.append({"method": "OPTIONS", "path": "*", "prefix": None, "handlers": [], "codes": codes,
                       "params": [], "auth": []})

    # POST: the allow-list tuple and the dispatch dict must agree.
    allowed, dispatch = [], {}
    for n in ast.walk(meth["do_POST"]):
        if isinstance(n, ast.Compare) and _src(n.left) == "p" and isinstance(n.ops[0], ast.NotIn) \
                and isinstance(n.comparators[0], ast.Tuple):
            allowed = [e.value for e in n.comparators[0].elts]
        if _dispatch_dict(n):
            dispatch = {k.value: v.id for k, v in zip(n.keys, n.values)}
    problems = []
    if set(allowed) != set(dispatch):
        problems.append(f"do_POST allow-list {sorted(allowed)} and dispatch table {sorted(dispatch)} disagree")
    for p in allowed:
        fn = funcs.get(dispatch.get(p, ""))
        codes = _returned_codes(fn) if fn else set()
        routes.append({"method": "POST", "path": p, "prefix": None,
                       "handlers": [dispatch[p]] if p in dispatch else [], "codes": codes, "params": [],
                       "auth": _sig_checks(fn) if fn else [],
                       "fields": _body_fields(fn) if fn else []})

    stale_post_comment = "Allocation endpoints are GONE" in src
    return routes, problems, stale_post_comment


def _dispatch_dict(n):
    """A literal {"name": function, ...}: string keys, bare-name values. Excludes response bodies such as
    {"error": "not found"}, which share the shape of the keys but not of the values."""
    return (isinstance(n, ast.Dict) and n.keys
            and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.keys)
            and all(isinstance(v, ast.Name) for v in n.values))


def _body_fields(fn):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                and _src(n.func.value) == "body" and n.args and isinstance(n.args[0], ast.Constant):
            if n.args[0].value not in out:
                out.append(n.args[0].value)
    return out


def route_key(r):
    if r.get("static"):
        return "GET /<path>"
    if r["method"] == "OPTIONS":
        return "OPTIONS *"
    if r["path"] is not None:
        return f"{r['method']} {r['path']}"
    placeholder = {"/api/block/": "<height>", "/api/sponsor/status/": "<token>", "/api/proof/": "<id>",
                   "/api/witness/": "<height>"}.get(r["prefix"], "<rest>")
    return f"{r['method']} {r['prefix']}{placeholder}"


def cli_commands(path):
    src = open(path).read()
    tree = ast.parse(src)
    doc = ast.get_docstring(tree) or ""
    documented = {}
    for line in doc.splitlines():
        m = re.match(r"\s+hazync (\w+)(.*?)#\s*(.*)$", line)
        if m:
            documented[m.group(1)] = (("hazync " + m.group(1) + m.group(2)).strip(), m.group(3).strip())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    table = {}
    for n in ast.walk(main):
        if _dispatch_dict(n):
            table = {k.value: v.id for k, v in zip(n.keys, n.values)}
    version = re.search(r'^VERSION\s*=\s*"([^"]*)"', src, re.M)
    ua = re.search(r'^UA\s*=\s*f?"([^"]*)"', src, re.M)
    return table, documented, (version.group(1) if version else None), (ua.group(1) if ua else None)


def shell_env(path):
    src = open(path).read()
    reads, sets = {}, {}
    for m in re.finditer(r"\$\{([A-Z_][A-Z0-9_]*)(:?[-?=])([^}]*)\}", src):
        name, op, val = m.groups()
        e = reads.setdefault(name, [])
        d = "(required)" if "?" in op else val
        if d not in e:
            e.append(d)
    for m in re.finditer(r"^\s*export ([A-Z_][A-Z0-9_]*)=(.*)$", src, re.M):
        name, val = m.groups()
        if f"${{{name}:" in val:
            continue                                    # `export X="${X:-default}"` is a read, listed above
        # The export sits inside a single-quoted `bash -c` body, so the raw text is '"'"$X"'"' soup.
        # Quotes carry no meaning in the rendered value; drop them.
        sets.setdefault(name, []).append(val.strip().replace("'", "").replace('"', ""))
    modes = re.search(r'case "\$MODE" in\s*\n\s*([a-z|]+)\)', src)
    header = re.search(r"#\s+MODE\s+(.*?)\s+\(default", src)
    n_default = re.search(r'^N="\$\{1:-(\d+)\}"', src, re.M)
    return reads, sets, (modes.group(1).split("|") if modes else []), \
        (header.group(1) if header else None), (n_default.group(1) if n_default else None)


def nginx_locations(path):
    if not os.path.exists(path):
        return [], None
    src = open(path).read()
    locs = re.findall(r"location\s+(=\s+)?(\S+)\s*\{[^}]*?proxy_pass\s+(\S+);", src, re.S)
    rate = re.search(r"limit_req_zone\s+\S+\s+zone=(\S+)\s+rate=(\S+);", src)
    body = re.search(r"client_max_body_size\s+(\S+);", src)
    return [(("= " if eq else "") + loc, target) for eq, loc, target in locs], \
        (rate.group(2) if rate else None, body.group(1) if body else None)


# ── rendering ────────────────────────────────────────────────────────────────────────────────────────
def _cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def _codes(c):
    return ", ".join(str(x) for x in sorted(c)) if c else "—"


def build(root):
    paths = {k: os.path.join(root, v) for k, v in SOURCES.items()}
    problems = []
    routes, p, stale_post_comment = server_routes(paths["server"])
    problems += p
    senv = python_env(paths["server"])
    cenv = python_env(paths["cli"])
    cpassed = python_passed(paths["cli"])
    table, documented, version, ua = cli_commands(paths["cli"])
    lreads, lsets, modes, mode_header, n_default = shell_env(paths["launcher"])
    locs, (rate, body) = nginx_locations(paths["nginx"])

    # descriptions must match the code in both directions
    keys = [route_key(r) for r in routes]
    for k in keys:
        if k not in ROUTES:
            problems.append(f"route `{k}` is in coordinator/server.py but has no description in ROUTES")
    for k in ROUTES:
        if k not in keys:
            problems.append(f"ROUTES describes `{k}`, which coordinator/server.py no longer serves")
    have_env = {f"server:{n}" for n in senv} | {f"cli:{n}" for n in cenv} | {f"launcher:{n}" for n in lreads}
    for k in sorted(have_env - set(ENV)):
        problems.append(f"environment variable `{k}` is read but has no description in ENV")
    for k in sorted(set(ENV) - have_env):
        problems.append(f"ENV describes `{k}`, which is no longer read")
    have_passed = {f"cli:{n}" for n in cpassed} | {f"launcher:{n}" for n in lsets}
    for k in sorted(have_passed - set(PASSED)):
        problems.append(f"variable `{k}` is set for a child process but has no description in PASSED")
    for k in sorted(set(PASSED) - have_passed):
        problems.append(f"PASSED describes `{k}`, which is no longer set")
    for c in sorted(set(table) - set(documented)):
        problems.append(f"`hazync {c}` is dispatched in main() but missing from the module docstring")
    for c in sorted(set(documented) - set(table)):
        problems.append(f"the module docstring lists `hazync {c}`, which main() does not dispatch")
    for m in modes:
        if m not in MODES:
            problems.append(f"run-workers.sh MODE `{m}` has no description in MODES")
    for m in MODES:
        if m not in modes:
            problems.append(f"MODES describes `{m}`, which run-workers.sh no longer accepts")

    L = []
    w = L.append
    w("# Coordinator reference")
    w("")
    w("**Generated. Do not edit by hand.** Regenerate with `python3 scripts/gen-coordinator-reference.py`; "
      "CI runs `--check`, which fails when this page and the code disagree. The code is authoritative: "
      "`coordinator/server.py` for the HTTP API and its configuration, `coordinator/hazync` for the worker "
      "CLI (shipped as `hazync-worker`), `coordinator/run-workers.sh` for the launcher (shipped as "
      "`hazync-run-workers.sh`). Routes, variables, defaults, read sites, signature checks and status codes "
      "are extracted from the source; the one-line meanings are kept in the generator and checked against "
      "the code in both directions.")
    w("")
    w("Out of scope: `coordinator/sponsor_bot.py` (see `docs/SPONSOR_BOT.md`) and the deploy units under "
      "`coordinator/deploy/` (see `coordinator/deploy/RUNBOOK.md`).")
    w("")
    w("## HTTP API")
    w("")
    w("Paths are as `server.py` sees them. The public coordinator is behind nginx, which maps them as "
      "follows (`coordinator/deploy/nginx-hazync.conf`):")
    w("")
    if locs:
        w("| nginx location | proxied to |")
        w("|---|---|")
        for loc, target in locs:
            w(f"| `{_cell(loc)}` | `{_cell(target)}` |")
        w("")
    extras = []
    if rate:
        extras.append(f"nginx limits the API to `{rate}` per client address")
    if body:
        extras.append(f"`client_max_body_size {body}`")
    if extras:
        s = "; ".join(extras) + "."
        w(s[0].upper() + s[1:])
        w("")
    w("Applies to every route, from `H` in `server.py`:")
    w("")
    w("- Every response carries `Access-Control-Allow-Origin: *`.")
    w("- `GET /api/*` is limited per client IP by `rate_ok(ip, \"r\", RATE_MAX_GET)`, and POST by "
      "`rate_ok(ip)` (`RATE_MAX`), both over `RATE_WINDOW`; over the limit is `429`. `X-Forwarded-For` is "
      "used only from `TRUSTED_PROXIES`, and `RATE_EXEMPT` bypasses the limit.")
    w("- A POST body over `MAX_BODY` is `413`; an unknown POST path is `404`.")
    w("- A POST whose `User-Agent` starts `hazync-worker/` has that version recorded against the caller's key "
      "(`note_client_version`).")
    w("- Signatures are ed25519, `pubkey` 32 bytes and `sig` 64 bytes, hex.")
    w("")
    w("### GET")
    w("")
    w("| path | query | handler | status codes | purpose |")
    w("|---|---|---|---|---|")
    for r in routes:
        if r["method"] != "GET":
            continue
        k = route_key(r)
        path = k.split(" ", 1)[1]
        q = ", ".join(f"`{x}`" for x in r["params"]) or "—"
        h = ", ".join(f"`{x}()`" for x in r["handlers"]) or "—"
        w(f"| `{_cell(path)}` | {q} | {_cell(h)} | {_codes(r['codes'])} | {_cell(ROUTES.get(k, ''))} |")
    w("")
    w("### POST")
    w("")
    w("| path | body fields read | handler | signature checked (key over message, as named in the handler) | status codes | purpose |")
    w("|---|---|---|---|---|---|")
    for r in routes:
        if r["method"] != "POST":
            continue
        k = route_key(r)
        fields = ", ".join(f"`{x}`" for x in r.get("fields", [])) or "—"
        h = ", ".join(f"`{x}()`" for x in r["handlers"]) or "—"
        auth = "; ".join(f"`{pk}` over `{msg}`" for pk, msg in r["auth"]) or "**none**"
        w(f"| `{r['path']}` | {fields} | {h} | {_cell(auth)} | {_codes(r['codes'])} | {_cell(ROUTES.get(k, ''))} |")
    w("")
    for r in routes:
        if r["method"] == "OPTIONS":
            w(f"`OPTIONS` on any path: {ROUTES.get('OPTIONS *', '')}")
            w("")
    w("Status codes are those the handler can return directly; the global `404`, `413` and `429` above "
      "apply as well.")
    if stale_post_comment:
        w("")
        w("⚠ The comment above the dispatch in `H.do_POST` says allocation endpoints are gone (\"no claim, no "
          "heartbeat\"). It predates `/api/claim` and `/api/beat` coming back; the dispatch table is what runs.")
    w("")

    def env_table(env, key):
        w("| variable | default as written | read in | meaning |")
        w("|---|---|---|---|")
        for name in env:
            e = env[name]
            d = " / ".join("—" if x is None else f"`{_cell(x)}`" for x in e["defaults"])
            meaning = ENV.get(f"{key}:{name}", "")
            if e.get("unused"):
                meaning += " ⚠ Read into a constant that nothing in the file uses."
            w(f"| `{name}` | {d} | {_cell(', '.join(e['sites']))} | {_cell(meaning)} |")
        w("")

    w("## Coordinator environment (`coordinator/server.py`)")
    w("")
    w("In the order the file reads them. \"—\" means no default in the call: the variable is unset unless "
      "provided.")
    w("")
    env_table(senv, "server")
    w("Startup (`__main__`) refuses a non-loopback `COORD_BIND` when `VERIFY_MODE` is not `real`, "
      "`COORD_ALLOW_MOCK` or `COORD_ALLOW_UNSIGNED` is set, or the ed25519 library is missing, unless "
      "`COORD_ALLOW_PUBLIC_INSECURE` is set.")
    w("")
    w("## Worker CLI (`coordinator/hazync`, shipped as `hazync-worker`)")
    w("")
    w(f"Every request sends `User-Agent: {ua}`. `VERSION` is `\"{version}\"` in the source and is stamped "
      "with the release by `scripts/package-release.sh`; a `dev` build refuses to POST to the default "
      "public coordinator (`_guard_dev_writes`).")
    w("")
    w("### Commands")
    w("")
    w("From `main()`'s dispatch table; usage and summary from the module docstring.")
    w("")
    w("| command | handler | usage | summary |")
    w("|---|---|---|---|")
    for c, fn in table.items():
        usage, summary = documented.get(c, ("", ""))
        w(f"| `{c}` | `{fn}()` | `{_cell(usage)}` | {_cell(summary)} |")
    w("")
    w("### Environment")
    w("")
    env_table(cenv, "cli")
    w("### Set for the host binary")
    w("")
    w("| variable | value | set in | meaning |")
    w("|---|---|---|---|")
    for name, e in cpassed.items():
        v = " / ".join(f"`{_cell(x)}`" for x in e["values"])
        w(f"| `{name}` | {v} | {_cell(', '.join(e['sites']))} | {_cell(PASSED.get('cli:' + name, ''))} |")
    w("")
    w("## Launcher (`coordinator/run-workers.sh`, shipped as `hazync-run-workers.sh`)")
    w("")
    w(f"`run-workers.sh [N] [--stop]`: N defaults to `{n_default}`. It checks the host's guest id against "
      "`/api/meta` and runs a GPU smoke prove before starting any loop, then restarts each loop's command "
      "until it exits `78` (`EX_CONFIG`). Exit `75` (`EX_TEMPFAIL`, nothing to claim right now) waits 30 s and is not "
      "a failure. With alerts set up (`hazync notify`), a loop pushes after `NOTIFY_FAIL_STREAK` failures in a row, on "
      "recovery and when it stops, and the launcher pushes when it refuses to start.")
    w("")
    w("### Environment")
    w("")
    w("| variable | default as written | meaning |")
    w("|---|---|---|")
    for name, ds in lreads.items():
        d = " / ".join("`(empty)`" if x == "" else ("(required)" if x == "(required)" else f"`{_cell(x)}`")
                       for x in ds)
        w(f"| `{name}` | {d} | {_cell(ENV.get('launcher:' + name, ''))} |")
    w("")
    w("### Set for the workers")
    w("")
    w("| variable | value | meaning |")
    w("|---|---|---|")
    for name, vals in lsets.items():
        v = " / ".join(f"`{_cell(x)}`" for x in vals)
        w(f"| `{name}` | {v} | {_cell(PASSED.get('launcher:' + name, ''))} |")
    w("")
    w("### Modes")
    w("")
    w("| `MODE` | loops |")
    w("|---|---|")
    for m in modes:
        w(f"| `{m}` | {_cell(MODES.get(m, ''))} |")
    w("")
    if mode_header is not None:
        listed = [x.strip() for x in mode_header.split("|")]
        missing = [m for m in modes if m not in listed]
        if missing:
            w(f"⚠ The script's own header comment lists `MODE` as `{mode_header}`, omitting "
              + ", ".join(f"`{m}`" for m in missing) + "; the `case` statement accepts the modes above.")
            w("")
    return "\n".join(L).rstrip() + "\n", problems


# ── modes ────────────────────────────────────────────────────────────────────────────────────────────
def check(root, doc_path, quiet=False):
    text, problems = build(root)
    ok = True
    for p in problems:
        if not quiet:
            print(f"FAIL {p}")
        ok = False
    try:
        committed = open(doc_path).read()
    except FileNotFoundError:
        committed = ""
    if committed != text:
        ok = False
        if not quiet:
            print(f"FAIL {os.path.relpath(doc_path, ROOT)} is stale — regenerate with "
                  f"`python3 scripts/gen-coordinator-reference.py`:")
            for line in list(difflib.unified_diff(committed.splitlines(), text.splitlines(),
                                                  "committed", "generated", lineterm=""))[:60]:
                print("    " + line)
    return ok


def control():
    """Prove --check can fail: each mutation of a COPY of the sources must be caught, and the pristine
    copy must pass. A check that passes all four is broken, not green."""
    doc = os.path.join(ROOT, DOC)
    if not check(ROOT, doc, quiet=True):
        print("FAIL control: the unmodified tree does not pass --check; run --check for the reason")
        return 1
    cases = [
        ("a POST route removed (/api/rotate)", "server",
         lambda s: s.replace('"/api/beat", "/api/rotate", "/api/sponsor")', '"/api/beat", "/api/sponsor")')
                    .replace('"/api/rotate": rotate, ', "")),
        ("a GET route removed (/api/meta)", "server",
         lambda s: s.replace('if p == "/api/meta":', 'if p == "/api/meta-gone":')),
        ("a default changed (CLAIM_TTL)", "server",
         lambda s: s.replace('os.environ.get("CLAIM_TTL", "3600")', 'os.environ.get("CLAIM_TTL", "3601")')),
        ("an undescribed variable added to the CLI", "cli",
         lambda s: s.replace('COORD   = os.environ.get("COORD_URL"',
                             'NEWVAR  = os.environ.get("HAZYNC_NEW_UNDOCUMENTED", "1")\n'
                             'COORD   = os.environ.get("COORD_URL"')),
    ]
    failed = 0
    for label, key, mutate in cases:
        with tempfile.TemporaryDirectory() as tmp:
            for k, rel in SOURCES.items():
                src = os.path.join(ROOT, rel)
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
                    shutil.copy(src, os.path.join(tmp, rel))
            target = os.path.join(tmp, SOURCES[key])
            before = open(target).read()
            after = mutate(before)
            if after == before:
                print(f"FAIL control '{label}': the mutation did not apply — the source moved; update the control")
                failed = 1
                continue
            open(target, "w").write(after)
            if check(tmp, doc, quiet=True):
                print(f"FAIL control '{label}': --check PASSED on mutated source — it cannot detect this drift")
                failed = 1
            else:
                print(f"  ok   control '{label}': detected")
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="fail if the committed doc differs from the code")
    g.add_argument("--control", action="store_true", help="show that --check detects removed routes and drift")
    a = ap.parse_args()
    doc = os.path.join(ROOT, DOC)
    if a.control:
        sys.exit(control())
    if a.check:
        if check(ROOT, doc):
            print(f"  ok   {DOC} matches the code")
            sys.exit(0)
        sys.exit(1)
    text, problems = build(ROOT)
    if problems:
        for p in problems:
            print(f"FAIL {p}")
        sys.exit(1)
    with open(doc, "w") as f:
        f.write(text)
    print(f"wrote {DOC}")


if __name__ == "__main__":
    main()
