#!/usr/bin/env python3
"""Inject an operation counter into every field entry point of a COPY of libsecp256k1.

Works on a scratch copy. Neither the repo nor ~/hazync-build/secp256k1 is touched.

The injection point is `field_10x26_impl.h`, because that is the backend the RISC0 guest actually
compiles: riscv32im has no `__int128`, so libsecp selects SECP256K1_WIDEMUL_INT64 -> 10x26 field +
8x32 scalar. Counting the 5x52 backend would profile a machine we do not run on.

Each `secp256k1_fe_impl_<name>(...) {` gains a `hz_field_ops[HZ_<name>]++;` immediately after the
opening brace. Nothing else changes, so the arithmetic under test is stock.
"""
import re, sys, pathlib

SRC = pathlib.Path(sys.argv[1])            # scratch secp256k1 root
hdr = SRC / "src" / "field_10x26_impl.h"
text = hdr.read_text()

# Every backend entry point, in declaration order. The signature is always
#   [SECP256K1_INLINE] static <ret> secp256k1_fe_impl_<name>(<args>) {
pat = re.compile(
    r'((?:SECP256K1_INLINE\s+)?static\s+(?:[A-Za-z_][A-Za-z0-9_ *]*?)\s*'
    r'secp256k1_fe_impl_([a-z0-9_]+)\s*\([^;{]*?\)\s*\{)',
    re.S)

names = []
def inject(m):
    head, name = m.group(1), m.group(2)
    if name not in names:
        names.append(name)
    return f'{head}\n    hz_field_ops[HZ_{name.upper()}]++;'

patched, n = pat.subn(inject, text)
if n == 0:
    sys.exit("FATAL: injected nothing — the signature pattern no longer matches this libsecp version")

# The counter array has to exist before the first use, and the header is included from several TUs, so
# the array is declared extern here and defined once in the harness.
decl = ["/* hazync: field-op counters, injected by tools/field-op-profile */",
        "#ifndef HZ_FIELD_OPS_DECLARED",
        "#define HZ_FIELD_OPS_DECLARED"]
for i, nm in enumerate(names):
    decl.append(f"#define HZ_{nm.upper()} {i}")
decl.append(f"#define HZ_FIELD_OP_COUNT {len(names)}")
decl.append("extern unsigned long long hz_field_ops[HZ_FIELD_OP_COUNT];")
decl.append("extern const char *hz_field_op_names[HZ_FIELD_OP_COUNT];")
decl.append("#endif")
patched = "\n".join(decl) + "\n\n" + patched

hdr.write_text(patched)

# The names table, emitted for the harness to print.
tbl = SRC / "hz_field_ops.c"
tbl.write_text(
    "#include <stdio.h>\n"
    f"#define HZ_FIELD_OP_COUNT {len(names)}\n"
    f"unsigned long long hz_field_ops[HZ_FIELD_OP_COUNT];\n"
    "const char *hz_field_op_names[HZ_FIELD_OP_COUNT] = {\n"
    + "".join(f'    "{nm}",\n' for nm in names)
    + "};\n")

print(f"  patched {n} entry points across {len(names)} distinct functions")
print("  " + ", ".join(names))
