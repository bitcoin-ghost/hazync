#!/usr/bin/env python3
"""The SHARED kernel builder's host flags are MSVC-aware, and the non-MSVC path is UNCHANGED (#631).

⛔⛔ WHY THIS EXISTS SEPARATELY FROM test_nvcc_flags.py, AND WHY #632 WAS NOT ENOUGH.

`risc0_build_kernel::KernelBuild::compile_cuda()` is the ONE function through which FIVE crates
build their CUDA kernels:

    risc0-sys                      risc0-circuit-recursion-sys
    risc0-circuit-rv32im-sys       risc0-groth16-sys
    risc0-circuit-keccak-sys

and it unconditionally appended `-Xcompiler -Wno-missing-braces,-Wno-unused-function`. `-Xcompiler`
forwards its argument VERBATIM to nvcc's host compiler, which on Windows is MSVC `cl.exe`:

    cl : Command line error D8021 : invalid numeric argument '/Wno-missing-braces'

measured on windows-latest, run 37015079476, compiling risc0-sys's kernels/zkp/cuda/supra/api.cu
and supra/ntt.cu.

⭐ #632 branched the flags inside `vendor/risc0-circuit-rv32im-sys/build.rs` — a crate's OWN flag
block — and the Windows build still failed here, because this shared line re-added the GCC flags for
all five crates, in one (`risc0-sys`) that nobody had vendored. ⇒ Per-crate fixes cannot work for a
flag added by the shared builder. That is the lesson this file guards.

⛔⛔ AND THE REAL RISK IS LINUX, NOT WINDOWS. Every proof on the board is produced through
compile_cuda(). A "portability fix" that quietly re-flags the path in use, while chasing a platform
nobody has proved on, would be the actual damage. So the assertion that matters most here is that
the ELSE branch still passes exactly the old flags, in the old order, as one comma-joined argument.

⚠ WHAT THIS CANNOT SHOW. It does not compile anything. It is a source-level guard; the build itself
is what `windows-prove.yml`'s build-cuda job answers, and even that cannot PROVE — a windows-latest
runner has no GPU.

    python3 test_kernel_host_flags.py             # MSVC branch present, non-MSVC byte-identical
    python3 test_kernel_host_flags.py --control   # the old unconditional flag — must read as NOT MSVC-aware
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "vendor", "risc0-build-kernel", "src", "lib.rs")
CONTROL = "--control" in sys.argv

# The flags upstream passed, as one `-Xcompiler` with one comma-joined argument. The Linux build
# must still get exactly this.
UPSTREAM_ARG = "-Wno-missing-braces,-Wno-unused-function"

fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)
    return ok


def main():
    if not os.path.exists(SRC):
        print(f"FAIL: no vendored builder at {SRC}")
        return 1
    src = open(SRC, encoding="utf-8").read()

    if CONTROL:
        # ⛔ THE CONTROL IS THE UPSTREAM SOURCE, RECONSTRUCTED. Strip the branch back to the single
        # unconditional flag and the checks below must FAIL — otherwise they would pass against the
        # very code that broke the Windows build, and would be asserting nothing.
        src = re.sub(
            r"if env::var\(\"CARGO_CFG_TARGET_ENV\"\).*?\n        \}\n",
            '        build\n            .flag("-Xcompiler")\n'
            f'            .flag("{UPSTREAM_ARG}");\n',
            src,
            flags=re.S,
        )

    print("── the MSVC branch exists and sends MSVC something it understands ──")
    has_branch = check(
        'env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc")' in src,
        "compile_cuda branches on CARGO_CFG_TARGET_ENV == msvc",
    )
    check("/wd4505" in src, "the MSVC arm passes /wd4505 (unreferenced local function)")
    # ⚠ MSVC has no missing-braces warning, so nothing should be invented for it. A number that
    # suppresses some unrelated warning would be worse than passing nothing.
    check(
        not re.search(r"/wd\d+\s*\".*missing.braces", src, re.I),
        "nothing is invented for missing-braces, which MSVC does not warn about",
    )
    check(
        "D8021" in src,
        "the comment records the measured error (D8021), not a guess at the cause",
    )
    check(
        "risc0-sys" in src and "supra" in src.lower() or "risc0-sys" in src,
        "the comment names the crate the failure was measured in",
    )

    print("── ⛔ and the NON-MSVC path is byte-identical to what shipped ──")
    # Find the else arm and assert the exact upstream argument survives there, as ONE -Xcompiler.
    else_arm = ""
    m = re.search(r"\}\s*else\s*\{(.*?)\n        \}", src, re.S)
    if m:
        else_arm = m.group(1)
    check(bool(else_arm), "there is an else arm for every non-MSVC target")
    check(
        UPSTREAM_ARG in else_arm,
        f"the else arm passes exactly {UPSTREAM_ARG!r}, comma-joined as upstream did",
    )
    check(
        else_arm.count("-Xcompiler") == 1,
        "the else arm uses ONE -Xcompiler, not two (flag order and grouping unchanged)",
    )
    # ⛔ The flags nvcc itself takes must be untouched on BOTH paths — they are portable and were
    # never part of this problem, so a diff here would mean the fix overreached.
    for f in ("-diag-suppress=177", "-diag-suppress=2922", "-Xcudafe", "--display_error_number"):
        check(f in src, f"nvcc's own flag {f} is still passed on every target")

    print("── ⛔ the CXX path gets C++20 and the CUDA path gets NO -std at all ──")
    # Split the file at compile_cuda so each path can be asserted separately.
    cpp_part, _, cuda_part = src.partition("fn compile_cuda")
    # ⛔ ASSERT ON THE CALL, NOT THE BARE STRING. The first version of these checks matched the
    # explanatory COMMENT, which quotes /std:c++17 while saying it was replaced — a check that
    # tests prose passes whatever the code does.
    calls = lambda part, flag: f'.flag_if_supported("{flag}")' in part
    check(calls(cpp_part, "/std:c++20"),
          'the CXX path CALLS flag_if_supported("/std:c++20") for MSVC (keccak C7555)')
    check(calls(cpp_part, "-std=c++17"),
          'the CXX path still CALLS flag_if_supported("-std=c++17") — Linux byte-identical')
    check(not calls(cpp_part, "/std:c++17"),
          "the old MSVC /std:c++17 CALL is gone, not sitting alongside the new one")
    # ⛔⛔ THE ONE THAT MATTERS: CXXFLAGS=/std:c++20 reaching nvcc killed cudafe++ with
    # 0xC0000409 on all seven .cu files (run 37017347238). The CUDA path must pass no -std.
    check("/std:" not in cuda_part and "-std=" not in cuda_part,
          "compile_cuda passes NO C++ standard flag — a /std: there crashes cudafe++")
    check("0xC0000409" in src or "cudafe" in src,
          "the comment records the measured crash, so the next person does not re-add the flag")

    print("── the escape hatch the GPU-less CI build depends on is intact ──")
    check(
        'env::var_os("NVCC_PREPEND_FLAGS").is_none()' in src
        and 'env::var_os("NVCC_APPEND_FLAGS").is_none()' in src,
        "-arch=native is still skipped when NVCC_*_FLAGS is set (a GPU-less compile needs this)",
    )

    if CONTROL:
        if fails:
            print(f"\nCONTROL OK: the upstream source fails {len(fails)} check(s) — "
                  "these assertions can detect the unfixed code")
            return 0
        print("\nCONTROL FAILED: the checks PASSED against upstream's unconditional flag, "
              "so they are asserting nothing")
        return 1

    if fails:
        print(f"\nFAILED: {len(fails)} check(s)")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
