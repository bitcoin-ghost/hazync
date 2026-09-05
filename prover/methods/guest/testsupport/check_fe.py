#!/usr/bin/env python3
"""Cross-check the bigint2 field backend's mod-p operations against arbitrary precision.

Usage: check_fe.py <path to fe_harness binary>
Exits non-zero, and prints the first few disagreements, if anything is wrong.

The value set deliberately includes representations that are >= p -- p, p+1, p+2, 2^256-1 -- because
the backend is LAZY and those are legal elements. libsecp's own test suite cannot construct them.
"""
import random, subprocess, sys

P = 2**256 - 2**32 - 977
SMALL = [0, 1, 2, 3, 4, 5, 7, 8, 9, 16, 27, 255, 1000, 65535]

def limbs(x): return " ".join("%x" % ((x >> (32 * i)) & 0xFFFFFFFF) for i in range(8))

def build_cases(seed=20260830, n_random=60):
    random.seed(seed)
    edges = [0, 1, 2, P - 2, P - 1, P, P + 1, P + 2, 2**256 - 1, 2**256 - 2, 2**255, 2**255 - 1,
             (P - 1) // 2, (P + 1) // 2, 2**32 + 977, 2**32 + 976, 2**224,
             2**256 - (2**32 + 977), 2**256 - (2**32 + 978), 2**256 - 1 - 977]
    vals = [v for v in edges if 0 <= v < 2**256]
    vals += [random.randrange(0, 2**256) for _ in range(n_random)]
    cmds, checks = [], []
    def emit(cmd, kind, *meta):
        cmds.append(cmd); checks.append((kind, meta))
    for a in vals:
        for b in random.sample(vals, 6) + [0, 1, P - 1, 2**256 - 1]:
            emit(f"add {limbs(a)} {limbs(b)}", "add", a, b)
        emit(f"neg {limbs(a)}", "neg", a)
        emit(f"half {limbs(a)}", "half", a)
        emit(f"canon {limbs(a)}", "canon", a)
        emit(f"iszero {limbs(a)}", "iszero", a)
        emit(f"signed30 {limbs(a)}", "signed30", a)
        for k in SMALL:
            emit(f"mulint {limbs(a)} {k}", "mulint", a, k)
        for k in [0, 1, 2, 7, 255, 65535]:
            emit(f"addint {limbs(a)} {k}", "addint", a, k)
        hx = " ".join("%02x" % c for c in a.to_bytes(32, "big"))
        emit("limit " + hx, "limit", a)
        emit("roundtrip " + hx, "roundtrip", a)
    for m in range(0, 32):
        emit(f"bounds {m}", "bounds", m)
    return cmds, checks

def verify(kind, meta, got):
    """True when the backend's answer is right. Lazy results need only be congruent and < 2^256."""
    if kind in ("add", "addint", "mulint"):
        a, k = meta
        want = {"add": a + k, "addint": a + k, "mulint": a * k}[kind]
        r = int(got, 16)
        return (r % P) == (want % P) and r < 2**256
    if kind == "neg":
        return int(got, 16) == ((-meta[0]) % P)          # negate must return canonical
    if kind == "half":
        r = int(got, 16); return (2 * r % P) == (meta[0] % P) and r < 2**256
    if kind == "canon":
        r = int(got, 16); return r == (meta[0] % P) and r < P
    if kind == "signed30":
        return int(got, 16) == (meta[0] % P)             # to/from must round-trip a LAZY input
    if kind == "iszero":
        return int(got) == (1 if meta[0] % P == 0 else 0)
    if kind == "limit":
        return int(got) == (1 if meta[0] < P else 0)
    if kind == "roundtrip":
        return int(got, 16) == (meta[0] % P)
    if kind == "bounds":
        m = meta[0]; r = int(got, 16)
        # magnitude 0 means every limb is zero; libsecp's run_field_half also needs an even low limb
        return (r == 0) if m == 0 else (r % 2 == 0 and r > 0)
    raise AssertionError(f"no verifier for {kind}")

def main():
    if len(sys.argv) != 2:
        print(__doc__); return 2
    cmds, checks = build_cases()
    out = subprocess.run([sys.argv[1]], input="\n".join(cmds), capture_output=True, text=True)
    if out.returncode != 0:
        print(f"harness exited {out.returncode}: {out.stderr[:400]}"); return 1
    lines = [l.split(None, 1)[1] for l in out.stdout.strip().split("\n") if l.startswith("R ")]
    if len(lines) != len(checks):
        print(f"got {len(lines)} results for {len(checks)} checks"); return 1
    bad = [(k, m, g) for (k, m), g in zip(checks, lines) if not verify(k, m, g)]
    print(f"{len(checks)} checks, {len(bad)} failures")
    for k, m, g in bad[:10]:
        print(f"  FAIL {k} {[hex(v) for v in m]} -> {g[:70]}")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
