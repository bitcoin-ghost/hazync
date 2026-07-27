#!/usr/bin/env bash
# Guard against the failure mode that actually bit us: a test that reports success while measuring
# nothing. Every instance below is something that really happened in this repo, silently, for weeks:
#
#   * guest-pure-fuzz DID NOT COMPILE — its build.rs extracts guest items verbatim and they had moved
#     to another file. FUZZING.md still advertised it as clean at 700k+ execs. Nothing ran it.
#   * prover/test_bip68_real.sh printed "expect REJECT", got VALID, and exited 0. It had no assertion
#     at all, so it could not fail. Nothing ran it either.
#   * prover/test_cov_negatives.sh duplicated coverage that ci_negative_tests.sh already had, and was
#     stale — two scripts claiming one guarantee, one of them wrong.
#   * the checked-in block fixtures predated coin_height/coin_mtp, so they could not express an
#     in-block spend and quietly carried a stray UTXO leaf each.
#
# The common shape is a green signal from an instrument that is not connected to anything. These
# checks make that state fail the build instead.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
WF=.github/workflows
fail=0
bad() { printf 'FAIL %s\n' "$*"; fail=1; }
ok()  { printf '  ok   %s\n' "$*"; }

# Harnesses deliberately NOT in CI. Each needs a reason — an unexplained entry here is how a test
# quietly leaves the build. Keep this list short and justified.
declare -A EXCLUDED=(
  [e2e_bundle_test.sh]="needs a live coordinator to serve a bundle; run before cutting a release"
  [cluster.sh]="multi-GPU prove fan-out, not a test"
  [rangecluster.sh]="multi-GPU prove fan-out, not a test"
  [build-release.sh]="release build tool, not a test"
)

echo "== 1. every Rust crate that has tests is run by CI =="
while read -r manifest; do
  d=$(dirname "$manifest")
  grep -rqs --include='*.rs' '#\[test\]' "$d/src" 2>/dev/null || continue
  if grep -rqs -- "--manifest-path $d/Cargo.toml" "$WF"; then ok "$d has tests and CI runs them"
  else bad "$d contains #[test] but no workflow runs it — a rotting test surface"; fi
done < <(git ls-files '*/Cargo.toml' | grep -v '/fuzz/Cargo.toml$')

echo "== 2. every test harness is either in CI or explicitly excluded with a reason =="
while read -r h; do
  b=$(basename "$h")
  case "$b" in make_negative_tests.py|fetch_block*.py) continue ;; esac   # generators, not tests
  if grep -rqs "$b" "$WF"; then ok "$b runs in CI"
  elif [ -n "${EXCLUDED[$b]:-}" ]; then ok "$b excluded — ${EXCLUDED[$b]}"
  else bad "$b is neither run by CI nor listed as excluded — nothing would notice if it broke"; fi
done < <(git ls-files 'prover/*.sh' 'coordinator/*fuzz*.py')

echo "== 3. every CI-run harness can actually FAIL =="
# A script with no failure path is decoration. test_bip68_real.sh printed a contradiction and exited 0.
while read -r h; do
  b=$(basename "$h")
  grep -rqs "$b" "$WF" || continue
  # Match conditional exits too: `sys.exit(0 if fails == 0 else 1)` is a failure path, and an earlier
  # version of this check called it decoration — a guard that cries wolf is a guard people switch off.
  if grep -qE 'exit 1|exit \$|fail=1|set -e|sys\.exit\([^)]*1' "$h"; then ok "$b has a failure path"
  else bad "$b runs in CI but contains no way to exit non-zero — it cannot fail"; fi
done < <(git ls-files 'prover/*.sh' 'coordinator/*fuzz*.py' 'scripts/*.sh')

echo "== 4. fuzz positive controls are still able to detect their bug class =="
# A fuzzer whose control has gone quiet is blind, and its clean runs mean nothing. seam_fuzz --control
# already exits 1 when it finds NOTHING; assert CI actually runs it in that mode.
if grep -rqs 'seam_fuzz.py --control' "$WF"; then ok "seam_fuzz control runs in CI"
else bad "seam_fuzz.py --control is not in CI — nothing proves the seam fuzzer can still catch H9"; fi
if [ -f audit-fuzz/seeds/sec2-position-crash.bin ]; then ok "SEC-2 control seed is committed"
else bad "audit-fuzz/seeds/sec2-position-crash.bin missing — the proof the accumulator fuzzer detects SEC-2"; fi

echo "== 5. block fixtures carry the fields the host needs =="
# Without coin_height a fixture cannot express an in-block spend, and the block still proves VALID —
# it just silently strands a UTXO leaf. Exactly the drift that hid for weeks.
for f in $(git ls-files 'prover/block_*.json' | grep -v badwit); do
  python3 - "$f" <<'PY' || fail=1
import json,sys
f=sys.argv[1]; d=json.load(open(f))
missing=[k for k in ("recent_times","txs") if k not in d]
for t in d.get("txs",[]):
    for p in t.get("prevouts",[]):
        missing += [k for k in ("coin_height","coin_is_coinbase","coin_mtp") if k not in p]
        break
    break
if missing: print(f"FAIL {f} missing {sorted(set(missing))} — regenerate with prover/fetch_block_rpc.py"); sys.exit(1)
print(f"  ok   {f} carries coin_height/coin_mtp/recent_times")
PY
done

echo
[ "$fail" -eq 0 ] && echo "test surfaces are all connected to something." \
                  || { echo "A test surface is disconnected — it would report green while measuring nothing."; exit 1; }
