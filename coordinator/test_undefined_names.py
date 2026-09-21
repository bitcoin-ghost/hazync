#!/usr/bin/env python3
"""Catch a name that is USED but bound NOWHERE in its module — the bug CI cannot see.

⛔ WHY THIS EXISTS. hazync#429 added seven `phase(...)` calls to `tip_smoke.py` and never defined
`phase`. Every run from that merge onward died on the first one:

    NameError: name 'phase' is not defined      # at "PREPARING · renting 4 cards"

It cost nothing — the teardown ran and reported `0 cards, account check: clean` — but the tip runner
could not work at all, and nothing noticed, because **no test imports or invokes `tip_smoke.main()`**.
Every suite in this directory tests a module that is imported; the one driver nobody imports is the
one that broke. A compile check does not help: `py_compile` accepts an undefined global, because in
Python it is only an error when the line actually runs.

⚠ DELIBERATELY CRUDE, AND THAT IS THE POINT. It does not model scopes, closures or comprehensions —
it asks one question: *is this name bound ANYWHERE in the module?* That cannot flag a real local, and
it catches exactly the class of bug above: a helper someone meant to write and did not. A cleverer
check would need a real type checker; this needs nothing but the stdlib and runs in milliseconds.

  python3 test_undefined_names.py            # must PASS
  python3 test_undefined_names.py --control  # re-introduces the #429 bug; MUST FAIL
"""
import ast
import builtins
import os
import pathlib
import sys

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
CONTROL = "--control" in sys.argv

# Module globals Python always provides. Not "names we gave up on" — names that genuinely exist.
DUNDERS = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__", "__builtins__"}


def bound_names(tree):
    """Every name bound anywhere in the module, by any means."""
    out = set(dir(builtins)) | DUNDERS
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchAs) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchStar) and n.name:
            out.add(n.name)
    return out


def undefined_in(src, label):
    tree = ast.parse(src, label)
    bound = bound_names(tree)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - bound)


def main():
    fails = []
    checked = 0
    for p in sorted(HERE.glob("*.py")):
        if p.name == os.path.basename(__file__):
            continue
        try:
            src = p.read_text(encoding="utf8")
        except OSError as e:
            fails.append(f"{p.name}: unreadable ({e})")
            continue
        # ⛔ The control puts the #429 bug back, in the file it actually happened in.
        if CONTROL and p.name == "tip_smoke.py":
            src = src.replace("    def phase(text):", "    def _phase_removed_by_control(text):", 1)
        try:
            miss = undefined_in(src, p.name)
        except SyntaxError as e:
            fails.append(f"{p.name}: SyntaxError line {e.lineno}")
            continue
        checked += 1
        if miss:
            fails.append(f"{p.name}: used but bound nowhere -> {', '.join(miss)}")

    print(f"  checked {checked} module(s) in {HERE}")
    for f in fails:
        print(f"  FAIL {f}")
    print()
    if CONTROL:
        if any("tip_smoke.py" in f and "phase" in f for f in fails):
            print("CONTROL OK — the #429 bug was re-introduced and this test caught it:")
            for f in fails:
                if "tip_smoke.py" in f:
                    print(f"  - {f}")
            return 0
        print("CONTROL FAILED — `phase` was removed and nothing noticed, which is how #429 shipped.")
        return 1
    if fails:
        print(f"FAILED {len(fails)}")
        return 1
    print("all good — every name used is bound somewhere in its module")
    return 0


if __name__ == "__main__":
    sys.exit(main())
