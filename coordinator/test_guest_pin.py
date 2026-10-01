#!/usr/bin/env python3
"""The canonical guest's pin, and the hex -> [u32; 8] conversion that decides what gets embedded.

⛔ WHY THIS EXISTS. `prover/methods/build.rs` no longer DERIVES the image id from the ELF on every
platform — it verifies the ELF by sha256 and takes METHOD_ID from `reproduce/METHOD_ID`, converting
the hex to the `[u32; 8]` that lands in `methods.rs`. That conversion is now load-bearing: get the
byte order wrong and the build embeds a WRONG id while reporting success, and the only symptom is
proofs rejected on someone else's machine.

⛔ WHY THE DERIVATION MOVED. `risc0-binfmt` — the crate that computes an image id — pulls in
`risc0-zkvm-platform`, whose GUEST-side syscall `sys_alloc_aligned` is an unresolved external when
MSVC's `link.exe` links the build script. Measured on windows-latest, run 36824751222:

    librisc0_zkvm_platform.rlib : error LNK2019: unresolved external symbol sys_alloc_aligned
    build_script_build.exe : fatal error LNK1120: 1 unresolved externals

So the id check is kept where it links (every non-MSVC host, plus `reproducible-image-id` on every
push) and the sha256 holds the guest to an exact byte sequence everywhere.

📏 THE ENDIANNESS IS MEASURED, NOT ASSUMED. This box's native guest reports id
`fb4d7352f9f0...` and the `methods.rs` generated from that same build holds
`[1383288315, ...]`. 1383288315 == 0x52734DFB == the bytes `fb 4d 73 52` read LITTLE-endian.
Big-endian gives `52734dfb...` — the same bytes in the wrong order, which is exactly the failure
that would be invisible.

    python3 test_guest_pin.py             # the conversion and the pins agree
    python3 test_guest_pin.py --control   # big-endian instead — must NOT reproduce the pair
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── the one recorded (hex, words) pair, from this box's own non-canonical build ──────────────────
PAIR_HEX = "fb4d7352f9f059863035a4920eaa4e4467d7fa825451576975f43838f744066b"
PAIR_WORDS = [1383288315, 2254041337, 2460235056, 1146006030,
              2197477223, 1767330132, 943256693, 1795572983]
ORDER = "big" if CONTROL else "little"


def words_from_hex(h):
    b = bytes.fromhex(h)
    return [int.from_bytes(b[i:i + 4], ORDER) for i in range(0, 32, 4)]


got = words_from_hex(PAIR_HEX)
if CONTROL:
    check(got != PAIR_WORDS,
          f"⛔ control: big-endian does NOT reproduce the recorded pair — first word {got[0]} "
          f"vs {PAIR_WORDS[0]}, so the byte order is a real choice and not a formality")
else:
    check(got == PAIR_WORDS,
          f"little-endian reproduces the recorded (hex, words) pair exactly ({got[0]} == "
          f"{PAIR_WORDS[0]})")
    back = b"".join(w.to_bytes(4, "little") for w in got).hex()
    check(back == PAIR_HEX, f"and it round-trips back to the same hex ({back[:16]}…)")

# ── build.rs must actually use that order, and read the pin from the file ───────────────────────
SRC = open(os.path.join(ROOT, "prover", "methods", "build.rs"), encoding="utf8").read()
check("u32::from_le_bytes" in SRC,
      "build.rs converts with from_le_bytes — the order the pair above measures")
check("from_be_bytes" not in SRC, "⚠ and nothing in it reads big-endian")
check("GUEST_ELF_SHA256" in SRC, "it reads the sha256 pin from reproduce/GUEST_ELF_SHA256")
check(SRC.index("got_sha != want_sha") < SRC.index("verify_image_id(&elf"),
      "⚠ the sha256 is checked BEFORE the id — the cheap, portable check refuses first")
# ⛔ The MSVC escape must exist AND must be narrow: the sha check is not allowed to be skipped.
check('#[cfg(target_env = "msvc")]' in SRC and '#[cfg(not(target_env = "msvc"))]' in SRC,
      "the id cross-check is target-conditional, with both arms present")
msvc_arm = SRC.split('#[cfg(target_env = "msvc")]', 1)[1][:600]
check("cargo:warning" in msvc_arm,
      "⛔ and the MSVC arm WARNS that the id was not recomputed, rather than passing silently")
check("sha256_hex" not in msvc_arm,
      "⚠ the MSVC arm does not touch the sha256 check — only the id cross-check is skipped")

# ── and the two pins must both exist, well-formed ───────────────────────────────────────────────
def pin(name):
    p = os.path.join(ROOT, "reproduce", name)
    if not os.path.exists(p):
        return None
    for ln in open(p, encoding="utf8"):
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            return ln
    return None


mid, gsha = pin("METHOD_ID"), pin("GUEST_ELF_SHA256")
check(bool(mid) and re.fullmatch(r"[0-9a-f]{64}", mid or ""),
      f"reproduce/METHOD_ID is a 64-hex id ({(mid or '')[:12]}…)")
check(bool(gsha) and re.fullmatch(r"[0-9a-f]{64}", gsha or ""),
      f"reproduce/GUEST_ELF_SHA256 is a 64-hex hash ({(gsha or '')[:12]}…)")
check(mid != gsha, "⚠ and they are different values — an id is not a file hash")

# ⛔ Cargo must keep risc0-binfmt OUT of the MSVC build graph, or none of the above helps.
TOML = open(os.path.join(ROOT, "prover", "methods", "Cargo.toml"), encoding="utf8").read()
check('[target.\'cfg(not(target_env = "msvc"))\'.build-dependencies]' in TOML,
      "⛔ risc0-binfmt is declared only for non-MSVC hosts")
# ⚠ COMMENTS STRIPPED FIRST. The block above `[target...]` EXPLAINS why risc0-binfmt is
# conditional, so a naive substring search finds it in the prose and fails on its own comment —
# which it did when this check was written.
head = TOML.split("[target.", 1)[0]
head_code = "\n".join(l for l in head.splitlines() if not l.strip().startswith("#"))
check("risc0-binfmt" not in head_code,
      "⚠ and NOT also in the plain [build-dependencies], which would put it back in every graph")
check("sha2" in head, "sha2 is an unconditional build-dependency")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — big-endian must not reproduce the pair")
        sys.exit(1)
    print("PASS (control): the byte order is load-bearing; big-endian gets a different id")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")
