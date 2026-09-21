#!/usr/bin/env python3
"""hazync#252 lever 2: NOTHING MAY BE PROVED WHILE THE JOBS MUTEX IS HELD.

`seg_serve_cmd` hands work to the fleet through a single `Mutex<VecDeque<..>>`. Every connection
thread takes its next job through that same mutex, so the lock is on the hot path of every worker
in the fleet.

Lever 2 proves the narrow top join levels in this process instead of shipping them out. A join is
~354 ms of GPU work (measured 2026-09-21). Proving one INSIDE the lock scope would stall every
worker for that whole time -- turning a latency optimisation into a fleet-wide stall, and doing it
silently: the run still produces a correct proof, just slower, which is exactly the kind of
regression that survives review.

The shipped code therefore decides under the lock and proves after it. That ordering is invisible
in a diff -- moving four lines up a block reads as a tidy-up -- so it is pinned here.

⛔ This is a SOURCE check, not a behavioural one. It cannot tell you the lock is held for a short
time; only that no proving call appears inside its scope.
"""
import pathlib
import re
import sys

MAIN = pathlib.Path(__file__).resolve().parents[1] / "src" / "main.rs"

# Calls that do real proving work. `join` is lever 2's; the others are here so that a future lever
# moving `prove_segment`/`lift`/`resolve` work around cannot land inside a lock scope unnoticed.
PROVING = re.compile(r"\b(?:server|prover)\s*\.\s*(join|lift|resolve|prove_segment|prove)\s*\(")

LOCK = re.compile(r"\.lock\(\)")

# A guard lives to the end of its BLOCK only when it is bound by a `let`. When the lock is part of a
# larger expression -- `wire.lock().unwrap().len()`, `*n.lock().unwrap() += 1` -- it is a temporary
# and Rust drops it at the end of the STATEMENT. Getting this wrong is not a harmless over-report:
# treating every temporary as block-scoped flagged three innocent lines in seg_serve_cmd, and a
# check that cries wolf is a check that gets silenced.
BOUND = re.compile(r"\A\s*\.\s*(?:unwrap\(\)|expect\(\s*(?P<q>[\"']).*?(?P=q)\s*\))\s*\Z", re.S)
# The receiver sits between the `=` and the `.lock()` -- `let q = wire.lock()` -- so the name match
# has to step over it. Without the trailing `[^;{}]*` this matched nothing at all, which silently
# disabled the `drop()` handling below.
BINDING_NAME = re.compile(r"\blet\s+(?:mut\s+)?(\w+)\s*(?::[^=]+)?=\s*[^;{}]*\Z", re.S)


def _scan_to(src: str, start: int, stop_chars: str) -> int:
    """Index of the first char in `stop_chars` at brace depth 0, or of the block's closing `}`."""
    depth = 0
    i = start
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and c in stop_chars:
            return i
        i += 1
    return len(src)


def lock_scopes(src: str):
    """Yield (line_no, text) for the span each `.lock()` guard is actually alive."""
    for m in LOCK.finditer(src):
        stmt_end = _scan_to(src, m.end(), ";")
        tail = src[m.end():stmt_end]
        line_no = src.count("\n", 0, m.start()) + 1

        if not BOUND.match(tail):
            # Temporary: dropped at the `;`, so the span is THE WHOLE STATEMENT -- not just the
            # tail. A lock passed as an argument, `server.join(&a, m.lock().unwrap().peek())`, is
            # held across the call while sitting textually to its LEFT; scanning forward only would
            # walk straight past it.
            stmt_start = max(
                (src.rfind(c, 0, m.start()) for c in ";{}"),
                default=-1,
            )
            yield line_no, src[stmt_start + 1:stmt_end]
            continue

        block_end = _scan_to(src, m.end(), "")
        scope = src[stmt_end:block_end]

        # An explicit `drop(guard)` releases it early; honour it so the check stays usable.
        name = BINDING_NAME.search(src[max(0, m.start() - 200):m.start()])
        if name:
            early = re.search(r"\bdrop\(\s*%s\s*\)" % re.escape(name.group(1)), scope)
            if early:
                scope = scope[:early.start()]
        yield line_no, scope


def violations(src: str):
    out = []
    for line_no, scope in lock_scopes(src):
        for hit in PROVING.finditer(scope):
            out.append((line_no, hit.group(1)))
    return out


# ⛔ The scope rule is the whole check, so it is tested against cases that must be caught AND cases
# that must not be. The "must not" half is not padding: an over-eager walker flagged three innocent
# `wire.lock().unwrap().len()` lines, and a check that cries wolf gets deleted rather than fixed.
SELF_TESTS = [
    ("bound guard, then a join",
     "fn f() {\n    let q = w.lock().unwrap();\n    let j = server.join(&a, &b);\n}\n", True),
    ("lock held as an argument to the join",
     "fn f() {\n    let n = server.join(&a, m.lock().unwrap().peek()).unwrap();\n}\n", True),
    ("decide under the lock, prove after it (the shipped shape)",
     "fn f() {\n    { let mut q = w.lock().unwrap(); q.push(1); }\n"
     "    let j = server.join(&a, &b);\n}\n", False),
    ("temporary, then an unrelated join",
     "fn f() {\n    let n = w.lock().unwrap().len();\n    let j = server.join(&a, &b);\n}\n", False),
    ("bound guard explicitly dropped first",
     "fn f() {\n    let q = w.lock().unwrap();\n    drop(q);\n"
     "    let j = server.join(&a, &b);\n}\n", False),
]


def main() -> int:
    src = MAIN.read_text(encoding="utf8")

    for name, sample, want in SELF_TESTS:
        got = bool(violations(sample))
        if got != want:
            verb = "missed" if want else "falsely flagged"
            print(f"SELF-TEST FAILED: {verb} -- {name}")
            print("The scope rule is wrong; this check proves nothing until it is fixed.")
            return 1

    # ⛔ POSITIVE CONTROL AGAINST THE REAL FILE. The samples above are synthetic; this one plants
    # the exact mistake in main.rs itself, so the check cannot pass because it stopped finding the
    # code it is meant to be reading.
    planted = src.replace(
        "            let mut q = jobs.lock().unwrap();",
        "            let mut q = jobs.lock().unwrap();\n"
        "            let _ = server.join(&a, &b).expect(\"planted\");",
        1,
    )
    if planted == src:
        print("CONTROL COULD NOT BE PLANTED: the `jobs.lock()` line in seg_serve_cmd has moved.")
        print("Update the anchor in this file -- do not delete the control.")
        return 1
    if not violations(planted):
        print("CONTROL DID NOT FAIL: a join planted inside the lock scope was not detected.")
        print("The scope walker is broken; this check proves nothing until it is fixed.")
        return 1

    found = violations(src)
    if found:
        print(f"{MAIN}: proving under a held lock:")
        for line_no, what in found:
            print(f"  lock taken at line {line_no}: `.{what}(` called before it is released")
        print()
        print("Decide inside the lock, prove after it -- see hazync#252.")
        return 1

    n = sum(1 for _ in lock_scopes(src))
    print(f"ok: {n} lock scopes checked, none prove; "
          f"{len(SELF_TESTS)} self-tests and a planted join all behaved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
