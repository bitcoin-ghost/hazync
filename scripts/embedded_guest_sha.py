#!/usr/bin/env python3
"""Verify a host binary carries the canonical guest — from the FILE, with no GPU (hazync#631).

⛔⛔ WHY RUNNING THE BINARY CANNOT BE THE CHECK ON A CI RUNNER.

`host.exe method-id` was the gate on the most important property in this project: that a proof from
this binary is worth anything. On a windows runner it exited **127** three times. 127 is "command
not found", and the exe was demonstrably there — the same run uploaded it, 251,232,768 bytes.

I guessed a missing `cudart64_12.dll` and shipped it. It exited 127 again, with cudart sitting
beside it. ⇒ So I read the import table instead of guessing a third time, and it says:

    nvcuda.dll        ← the CUDA DRIVER API

`nvcuda.dll` ships with the NVIDIA **driver**, not with the CUDA redist. A GPU-less runner has no
driver, so Windows cannot load the image, and NOTHING copied next to the exe will change that.
⚠ `cudart64_12.dll` is not even in the import table — the cudart theory was wrong twice over.

⇒ Running a CUDA-linked host is not a check CI can perform. It is a check the OWNER of a GPU
machine can perform in one line, and that is where it belongs.

⭐ WHAT *CAN* BE CHECKED HERE, AND IT IS NOT A WEAKER CLAIM BY MUCH. The guest is embedded in the
binary verbatim. Measured on the 177 MB artifact of run 37155135563:

    'R0BF' at offset 12,281,328 — 69,980,208 bytes — sha256 35e3f55e…  == reproduce/GUEST_ELF_SHA256

⚠ THE GUEST IS NOT AN ELF, despite the `HAZYNC_GUEST_ELF` variable that names it. It is risc0's
R0BF container and begins "R0BF". Searching for `\\x7fELF` finds 51 coincidental hits in this binary
and none of them is the guest — which is exactly the wrong turn this script exists to prevent.

⇒ So: find the embedded guest, hash it, compare with the pin. That proves the binary carries the
canonical guest. It does NOT recompute the image id — `reproduce/GUEST_ELF_SHA256` explains at
length why no MSVC-linked build can (risc0-zkvm-platform's `sys_alloc_aligned` is an unresolved
external under link.exe), and why this second pin exists for precisely this reason.

⛔ SAY WHICH OF THE TWO WAS ESTABLISHED. "The binary is canonical" was once printed by a step that
had only checked that a FILE EXISTED, in a run where the id check had just failed. Never again:
this reports the guest as verified and the image id as NOT read, separately.

    python3 scripts/embedded_guest_sha.py host.exe                 # verify against the pin
    python3 scripts/embedded_guest_sha.py host.exe --imports       # also list imported DLLs
    python3 scripts/embedded_guest_sha.py --control                # the naive searches must fail
"""
import hashlib
import struct
import sys
from pathlib import Path

GUEST_MAGIC = b"R0BF"
PIN_FILE = "reproduce/GUEST_ELF_SHA256"


def read_pin(root="."):
    """The pinned sha256: the FIRST non-empty, non-comment line.

    ⛔ NOT a greedy 64-hex grep over the file. The prose in that file quotes METHOD_ID, so a greedy
    match returns the WRONG hash and compares the guest against an image id.
    """
    for line in Path(root, PIN_FILE).read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    raise SystemExit(f"{PIN_FILE}: no pin line found")


def imported_dlls(data):
    """The DLLs a PE64 imports, from its import directory. [] if it is not a PE64."""
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    coff = pe + 4
    nsec, = struct.unpack_from("<H", data, coff + 2)
    opt_size, = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    if struct.unpack_from("<H", data, opt)[0] != 0x20B:
        return []
    imp_rva = struct.unpack_from("<I", data, opt + 112 + 8)[0]
    secs = []
    st = opt + opt_size
    for i in range(nsec):
        o = st + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
        secs.append((vaddr, vsize, rawptr, rawsize))

    def off(rva):
        for vaddr, vsize, rawptr, rawsize in secs:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        return None

    base, out, i = off(imp_rva), [], 0
    if base is None:
        return []
    while True:
        f = struct.unpack_from("<IIIII", data, base + i * 20)
        if not any(f):
            break
        no = off(f[3])
        if no is None:
            break
        out.append(data[no:data.index(b"\0", no)].decode("latin1"))
        i += 1
    return out


def find_guest(data, size):
    """(offset, sha256) of an embedded `size`-byte blob starting at R0BF, or (None, None).

    ⚠ Every R0BF occurrence is tried, not just the first: a 251 MB binary contains the magic more
    than once, and the first hit is not necessarily the guest (here it was the second).
    """
    start = 0
    while True:
        i = data.find(GUEST_MAGIC, start)
        if i < 0:
            return None, None
        start = i + 1
        if i + size <= len(data):
            h = hashlib.sha256(data[i:i + size]).hexdigest()
            if h == find_guest.want:
                return i, h


def main(argv):
    if "--control" in argv:
        return control()
    if len(argv) < 2:
        raise SystemExit(__doc__.strip().splitlines()[-4].strip())
    path = Path(argv[1])
    root = argv[argv.index("--root") + 1] if "--root" in argv else "."
    pin = read_pin(root)
    data = path.read_bytes()
    print(f"binary : {path}  ({len(data):,} bytes)")
    print(f"pin    : {pin}  ({PIN_FILE})")

    dlls = imported_dlls(data)
    if "--imports" in argv or any(d.lower() == "nvcuda.dll" for d in dlls):
        print(f"imports: {len(dlls)} DLL(s)")
        for d in sorted(dlls, key=str.lower):
            note = ""
            if d.lower() == "nvcuda.dll":
                note = "   ⛔ CUDA DRIVER API — ships with the driver, so this binary cannot START here"
            print(f"         {d}{note}")

    size = GUEST_SIZE
    find_guest.want = pin
    off, got = find_guest(data, size)
    print()
    if off is None:
        print(f"⛔ NO embedded {size:,}-byte R0BF blob hashes to the pin.")
        print("   This binary does not demonstrably carry the canonical guest.")
        return 1
    print(f"✅ canonical guest embedded at offset {off:,} — {size:,} bytes, sha256 {got}")
    print("   ⇒ ESTABLISHED: the binary carries the canonical guest.")
    print("   ⚠ NOT established: the binary's own METHOD_ID was not read. On a machine with an")
    print("     NVIDIA driver, confirm it directly:  host.exe method-id")
    return 0


GUEST_SIZE = 69_980_208


def control():
    """The two naive searches must FAIL on the real binary, or this script proves nothing.

    Exits 0 when the naive versions break as expected — this repo's control convention.
    """
    import re
    print("── control: the naive searches, on a synthetic binary shaped like the real one ──")
    pin = read_pin()
    guest = bytes(GUEST_MAGIC) + b"\x11" * (GUEST_SIZE - 4)
    real = hashlib.sha256(guest).hexdigest()
    # a decoy ELF magic and a decoy R0BF, then the "guest" — mirroring the real layout
    blob = b"MZ" + b"\x00" * 64 + b"\x7fELF" + b"\x22" * 4096 + GUEST_MAGIC + b"\x33" * 8192 + guest
    fails = 0

    find_guest.want = real
    off, got = find_guest(blob, GUEST_SIZE)
    ok = off is not None and got == real
    print(f"  {'ok  ' if ok else 'FAIL'} the real search finds the guest past both decoys (offset {off})")
    if not ok:
        fails += 1

    # ⛔ naive 1: search for \x7fELF, because the variable is called HAZYNC_GUEST_ELF
    i = blob.find(b"\x7fELF")
    naive_elf_ok = i >= 0 and hashlib.sha256(blob[i:i + GUEST_SIZE]).hexdigest() == real
    print(f"  {'ok  ' if not naive_elf_ok else 'FAIL'} ⛔ control: searching \\x7fELF does NOT find the guest "
          f"(it is R0BF, not an ELF)")
    if naive_elf_ok:
        fails += 1

    # ⛔ naive 2: take the FIRST R0BF hit only
    j = blob.find(GUEST_MAGIC)
    naive_first_ok = hashlib.sha256(blob[j:j + GUEST_SIZE]).hexdigest() == real
    print(f"  {'ok  ' if not naive_first_ok else 'FAIL'} ⛔ control: taking the FIRST R0BF hit gets the "
          f"decoy, not the guest")
    if naive_first_ok:
        fails += 1

    # ⛔ naive 3: a greedy 64-hex grep of the pin file returns METHOD_ID from the prose
    raw = Path(PIN_FILE).read_text()
    greedy = re.findall(r"\b[0-9a-f]{64}\b", raw)
    print(f"  {'ok  ' if greedy and greedy[0] != pin else 'FAIL'} ⛔ control: a greedy 64-hex grep returns "
          f"{greedy[0][:12] if greedy else '<none>'}…, NOT the pin {pin[:12]}…")
    if not (greedy and greedy[0] != pin):
        fails += 1

    print()
    if fails:
        print(f"⛔ CONTROL BROKEN: {fails} check(s) did not behave as expected")
        return 1
    print("CONTROL OK: every naive search fails on a binary shaped like the real one")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
