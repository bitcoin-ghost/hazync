#!/usr/bin/env bash
# Consensus ACTIVATION BOUNDARIES against real chain blocks (hazync#83).
#
# WHY THIS EXISTS. Before it, block fixtures sat at 130000/140000 (before every activation) and 741000
# (after all of them). So every height-gated branch in validate_block had exactly one side exercised
# and no boundary at all: BIP34/v2 (227931), BIP66/v3 (363725), BIP65/v4 (388381), BIP113 locktime
# (419328), segwit witness_ok (481824).
#
# That is the shape audit #3's F-1 came in through — a canonical-chain break that survived because a
# comment, a test name and a fixture all agreed with each other and none with the chain. These are the
# real blocks, so they cannot agree with anything but the chain.
#
# THE SHARP ONE IS 434499. witness_ok's own comment records that running the BIP141 commitment check at
# every height "rejected the canonical pre-activation blocks that already carried an early commitment
# output yet have a witness-free coinbase — a reject-valid liveness bug in the 433k-481823 range". That
# was fixed and NOTHING drove the window it broke. 434499 is the first such block (434504 and 434535
# follow, so the shape is not a one-off), and it is the one case that cannot be synthesised.
#
# TWO SETS, split on measured cost. Block 741000 has 670 prevouts and executes in ~45s; prevout count
# is the driver:
#   cheap (~2,760 prevouts, ~3 min)  — runs on every push
#   heavy (~30,700 prevouts, ~34 min) — schedule / manual only
# Adding half an hour to every push to re-execute blocks that never change is how a suite gets
# disabled six months later.
#
#   usage: ci_boundary_tests.sh [cheap|heavy|all]
set -uo pipefail
cd "$(dirname "$0")" || exit 1
MODE="${1:-cheap}"
H="${HAZYNC_HOST:-./target/release/host}"
DIR="${HAZYNC_FIXTURES:-testdata/boundaries}"

CHEAP="227930 227931 363724 363725 388381"
HEAVY="388380 419327 419328 434499 481823 481824"

case "$MODE" in
  cheap) SET="$CHEAP" ;;
  heavy) SET="$HEAVY" ;;
  all)   SET="$CHEAP $HEAVY" ;;
  *) echo "usage: $0 [cheap|heavy|all]" >&2; exit 2 ;;
esac

[ -x "$H" ] || { echo "FAIL host binary not found at $H (build it first)" >&2; exit 1; }

fail=0; ran=0
for h in $SET; do
  f="$DIR/block_$h.json"
  if [ ! -f "$f" ]; then
    echo "FAIL missing fixture $f — a boundary silently not tested is the bug this file exists to stop"
    fail=1; continue
  fi
  # Capture, THEN match. Piping into `grep -q` under `set -o pipefail` is a race, not a test:
  # grep exits on its first match and closes the pipe, host takes SIGPIPE, and pipefail turns that
  # into a non-zero pipeline — so a block that printed VALID is reported as REJECTED. Fast blocks
  # finish writing before grep leaves and pass; slow ones do not. The first version of this file did
  # exactly that and reported two real, valid mainnet blocks as consensus failures.
  out="$(HAZYNC_BLOCK="$f" "$H" check-full 2>&1)"
  if printf '%s' "$out" | grep -q "BLOCK $h VALID"; then
    echo "  ok   $h VALID"
  else
    echo "FAIL $h did NOT validate — a real mainnet block is being rejected"
    printf '%s\n' "$out" | tail -3 | sed 's/^/       /'
    fail=1
  fi
  ran=$((ran + 1))
done

# Vacuity guard: an empty set would exit 0 and report nothing.
[ "$ran" -gt 0 ] || { echo "FAIL no boundary fixtures ran at all"; exit 1; }
[ "$fail" = 0 ] || { echo; echo "A consensus activation boundary failed. Do NOT release."; exit 1; }
echo "all $ran boundary blocks ($MODE) validate against the current guest."
