#!/usr/bin/env bash
# A prebuilt guest can be embedded, and ONLY the canonical one (hazync#615).
#
# 📏 WHY. The guest ELF carries the build machine's paths inside it — 8 occurrences on one laptop, all
# panic and debug strings from dependencies (`/home/<user>/.cargo/registry/…/risc0-zkvm-3.0.5/…`). So
# the image id is PATH-DEPENDENT, and reproduce/Dockerfile's fixed WORKDIR is what makes it
# reproducible. Measured 2026-09-30 on an unmodified tree with every flag unset:
#
#     native (laptop)     fb4d7352f9f059863035a4920eaa4e4467d7fa825451576975f43838f744066b
#     container (pinned)  37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd
#
# Same source, same flags, different id. That — not any toolchain limit — is why there has only ever
# been one supported platform: a native macOS or Windows host would embed a non-canonical guest and
# have every proof rejected (exit 78, guest id mismatch).
#
# ⭐ The guest is platform-INDEPENDENT: a RISC-V ELF, indifferent to the host's OS. So it is built
# once in the container and every platform's host embeds THOSE bytes. HAZYNC_GUEST_ELF is that door.
#
# ⛔ AND IT IS CHECKED, NOT TRUSTED. The build refuses unless the id computed from the supplied ELF
# equals reproduce/METHOD_ID — strictly stronger than the default path, where a native build silently
# produces a different id and only CI notices, on one platform.
#
#   ./prover/test-prebuilt-guest.sh             # a wrong guest is refused; the default path is unchanged
#   ./prover/test-prebuilt-guest.sh --control   # the check removed — the wrong guest must be accepted
#
# ⚠ THE ACCEPT PATH IS NOT TESTED HERE. It needs the canonical ELF, which only reproduce/Dockerfile
# produces; the CI job that builds that container asserts it. Testing a build refusal is what can be
# done anywhere, and it is the half that protects anything.
set -uo pipefail
trap 'echo "test-prebuilt-guest: ERR at line $LINENO (exit $?)" >&2' ERR

CONTROL=0
[ "${1:-}" = "--control" ] && CONTROL=1
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fails=0
check() { if [ "$1" = 1 ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi; }

PIN=$(grep -vE '^\s*#|^\s*$' "$HERE/../reproduce/METHOD_ID" | head -1 | tr -d '[:space:]')
check "$([ ${#PIN} -eq 64 ] && echo 1)" "the canonical pin is a 64-hex id (${PIN:0:12}…)"
# ⛔ THERE ARE NOW TWO PINS, and the one the build refuses on is the SHA. build.rs verifies the
# ELF by sha256 on every platform -- because risc0-binfmt does not link under MSVC -- and recomputes
# the image id only where it can. So the refusal an operator sees names the sha, not the id.
SHA=$(grep -vE '^\s*#|^\s*$' "$HERE/../reproduce/GUEST_ELF_SHA256" 2>/dev/null | head -1 | tr -d '[:space:]')
check "$([ ${#SHA} -eq 64 ] && echo 1)" "and a 64-hex guest sha256 pin (${SHA:0:12}…)"

# ── a guest that is NOT the canonical one ───────────────────────────────────────────────────────
# ⚠ Real bytes, not a fabricated file: the tree's own native build is a genuine, well-formed guest
# ELF that simply is not the canonical one — exactly the case that must be refused. A random blob
# would be rejected by compute_image_id() for the wrong reason and prove nothing.
ELF="$HERE/target/riscv-guest/methods/method/riscv32im-risc0-zkvm-elf/release/method.bin"
if [ ! -s "$ELF" ]; then
    echo "  SKIP no locally built guest at $ELF — run 'cargo build -p methods --release' first" >&2
    exit 0
fi

SRC="$HERE/methods/build.rs"
BAK=$(mktemp); cp "$SRC" "$BAK"
restore() { cp "$BAK" "$SRC"; rm -f "$BAK"; }
trap 'restore' EXIT

if [ "$CONTROL" = 1 ]; then
    # ⛔ Remove the comparison: the build then embeds whatever it is handed.
    python3 - "$SRC" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf8").read()
n0 = s.count("    if got_sha != want_sha {") + s.count("    verify_image_id(&elf, want, elf_path);")
s = s.replace("    if got_sha != want_sha {", "    if false {", 1)
s = s.replace("    verify_image_id(&elf, want, elf_path);", "    let _ = (want, elf_path);", 1)
# ⛔ Assert the patch LANDED: a str.replace that matches nothing succeeds silently, and
# this project has printed "patched" over an unchanged file more than once.
assert n0 == 2, f"expected both anchors, found {n0} -- build.rs has changed shape"
open(p, "w", encoding="utf8").write(s)
PY
fi

cd "$HERE" || exit 1
# ⚠ The ERR trap is off across this build: a non-zero exit here is the RESULT, not a fault, and
# letting the trap print "ERR at line …" beside an expected refusal makes a passing test look
# like a failing one.
trap - ERR
out=$(HAZYNC_GUEST_ELF="$ELF" cargo build -p methods --release -j 4 2>&1); rc=$?
trap 'echo "test-prebuilt-guest: ERR at line $LINENO (exit $?)" >&2' ERR
if [ "$CONTROL" = 1 ]; then
    check "$([ $rc -eq 0 ] && echo 1)" \
          "⛔ control: with the check gone, a NON-canonical guest is embedded (exit $rc) — the build \
would ship a program that is not the one published proofs attest to"
else
    check "$([ $rc -ne 0 ] && echo 1)" "a non-canonical guest is REFUSED (exit $rc)"
    check "$(printf '%s' "$out" | grep -q 'NOT the canonical guest' && echo 1)" "and says so plainly"
    check "$(printf '%s' "$out" | grep -q "$SHA" && echo 1)" \
          "printing the canonical SHA it wanted -- the pin it actually refused on"
    check "$(printf '%s' "$out" | grep -qE 'its sha256 *: [0-9a-f]{64}' && echo 1)" \
          "and the sha it was given, so the operator can see which guest they have"
    # ⚠ The ID is deliberately NOT asserted here. The sha is checked first, so a non-canonical
    # guest never reaches the id comparison -- asserting the id would fail a CORRECT build, which is
    # exactly how this test broke when the sha check went in.
fi

# ── the default path must be untouched ──────────────────────────────────────────────────────────
restore; trap - EXIT
out2=$(env -u HAZYNC_GUEST_ELF cargo build -p methods --release -j 4 2>&1); rc2=$?
check "$([ $rc2 -eq 0 ] && echo 1)" \
      "⚠ with HAZYNC_GUEST_ELF unset the ordinary build still works (exit $rc2) — this changes no default"
# ⚠ SAY WHY IT FAILED. This check was capturing the build output and throwing it away, so a broken
# default path reported an exit code and nothing else -- and a guest build failure is never
# self-evident from a number. (shellcheck SC2034 caught the unused capture; the fix is to use it.)
[ "$rc2" -eq 0 ] || printf '%s\n' "$out2" | tail -25

echo
if [ "$CONTROL" = 1 ]; then
    [ "$fails" = 0 ] && { echo "PASS (control): without the id check, any guest is embedded"; exit 0; }
    echo "FAIL (control): $fails"; exit 1
fi
[ "$fails" = 0 ] && { echo "PASS (real)"; exit 0; }
echo "FAIL: $fails"; exit 1
