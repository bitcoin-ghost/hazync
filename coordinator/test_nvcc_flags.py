#!/usr/bin/env python3
"""The CUDA kernels' host-compiler flags are MSVC-aware, and the non-MSVC path is UNCHANGED (#631).

⛔ WHY. `-Xcompiler` forwards its argument verbatim to nvcc's host compiler. On Windows that is MSVC
`cl.exe`, which does not speak `-Wno-unused-function` or `-O3`. Everything else the build passes --
`-diag-suppress=…`, `-std=c++17`, `-Xptxas -O3`, `-arch=native` -- is nvcc's own and portable, so the
two `-Xcompiler` groups are the whole of it.

⛔⛔ AND THE REAL RISK IS NOT WINDOWS, IT IS LINUX. This file guards a "portability fix" that quietly
re-flags the prover everyone actually runs. `vendor/risc0-circuit-rv32im-sys` compiles the CUDA
kernels that produce every proof on the board; changing its optimisation or warning flags on the
path in use, while chasing a platform nobody has yet proved on, would be the damage. So the
assertion that matters here is that the ELSE branch still passes exactly the old flags, in the old
order.

⚠ WHAT THIS CANNOT SHOW. Nothing here compiles a CUDA kernel under MSVC -- neither CI nor the
development box has a Windows machine with an NVIDIA GPU, and a `windows-latest` runner has no GPU
at all, so CI could at most compile and never prove. This is a source-level guard, and #631 says so.

⚠ AND MSVC MAY NOT HAVE NEEDED IT: it usually downgrades an unknown option to warning D9002 rather
than erroring. If a Windows CUDA build ever succeeds, check whether reverting the branch still works
before believing it was load-bearing.

    python3 test_nvcc_flags.py             # MSVC branch present, non-MSVC byte-identical
    python3 test_nvcc_flags.py --control   # the old unconditional flags — must read as NOT MSVC-aware
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vendor", "risc0-circuit-rv32im-sys", "build.rs")
CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


src = open(SRC, encoding="utf8").read()
if CONTROL:
    # ⛔ THE SHAPE THAT SHIPPED: both GCC flag groups passed unconditionally, no branch at all.
    src = re.sub(
        r'if env::var\("CARGO_CFG_TARGET_ENV"\).*?\n    \}\n',
        '        .flag("-Xcompiler")\n        .flag("-Wno-unused-function,-Wno-unused-parameter")\n'
        '        .flag("-Xcompiler")\n        .flag("-O3");\n',
        src, flags=re.S)

# ── 1. is the host-compiler choice made on the TARGET ENV at all? ───────────────────────────────
aware = 'env::var("CARGO_CFG_TARGET_ENV")' in src and '"msvc"' in src
if CONTROL:
    check(not aware,
          "⛔ control: the shipped code passes GCC host flags unconditionally — no MSVC awareness, "
          "which is the state that produced the #631 report")
else:
    check(aware, "the host-compiler flags branch on CARGO_CFG_TARGET_ENV == \"msvc\"")

# ── 2. ⛔ THE ONE THAT MATTERS: the non-MSVC path is unchanged ───────────────────────────────────
# The flags Linux and macOS get, in order. If this drifts, the prover on the board changed.
SHIPPED = ['-Xcompiler', '-Wno-unused-function,-Wno-unused-parameter', '-Xcompiler', '-O3']
if not CONTROL:
    else_block = src.split("} else {", 1)
    check(len(else_block) == 2, "there is an else branch for the platforms in use")
    body = else_block[1].split("\n    }", 1)[0] if len(else_block) == 2 else ""
    got = re.findall(r'\.flag\("([^"]+)"\)', body)
    check(got == SHIPPED,
          f"⛔ the non-MSVC branch passes EXACTLY the shipped flags, in order: {got}")
    # ⚠ And nothing MSVC-only leaked into it.
    check(not any(f.startswith("/") for f in got),
          "⚠ and no MSVC-style flag leaked into the path Linux uses")

    msvc_body = src.split('Ok("msvc") {', 1)[1].split("\n    } else {", 1)[0]
    mgot = re.findall(r'\.flag\("([^"]+)"\)', msvc_body)
    check(mgot.count("-Xcompiler") == 3 and "/O2" in mgot,
          f"the MSVC branch forwards MSVC-style flags ({[f for f in mgot if f != '-Xcompiler']})")
    check(not any(f.startswith("-W") or f == "-O3" for f in mgot),
          "⚠ and no GCC-style flag is left in the MSVC branch")

# ── 3. the portable nvcc flags stay OUTSIDE the branch, on every platform ───────────────────────
# ⚠ These are nvcc's own, not the host compiler's. Moving one into a branch would silently drop it
# from a platform, and a dropped -Xptxas -O3 is a slower prover with no error anywhere.
for f in ("-diag-suppress=177", "-diag-suppress=550", "-diag-suppress=2922", "-std=c++17",
          "-Xptxas"):
    if CONTROL:
        continue
    before_branch = src.split('if env::var("CARGO_CFG_TARGET_ENV")', 1)[0]
    after_branch = src.split("\n    }\n\n    build\n", 1)
    present = f in before_branch or (len(after_branch) == 2 and f in after_branch[1])
    check(present, f"{f} is passed on every platform, outside the branch")

# ── 4. the marker, so the next person can find and drop it ──────────────────────────────────────
if not CONTROL:
    check("HAZYNC_631_MSVC_FLAGS" in src,
          "⚠ the local patch carries a grep-able marker, like HAZYNC_119_ACCUM_FIX does")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — the shipped shape must read as NOT MSVC-aware")
        sys.exit(1)
    print("PASS (control): unconditional GCC host flags, which is what #631 reported")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")
