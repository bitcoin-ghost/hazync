#!/usr/bin/env bash
# Refuse any GitHub Actions `run:` block that interpolates a ${{ }} expression.
#
#   ./scripts/check-workflow-injection.sh
#
# WHY THIS IS A GATE AND NOT A REVIEW NOTE
#
# GitHub substitutes ${{ }} into the script text BEFORE bash parses it. A value containing shell
# metacharacters is therefore executed as code, with whatever secrets and permissions that job holds.
# In this repo that job was `release-sign`, which imports the release-signing PGP private key and has
# `contents: write` — so the payoff was the signing key itself plus the ability to sign forged
# binaries with it, against a release-tag input that `workflow_dispatch` lets any write-access account
# supply directly. Found by external review, 2026-08-01.
#
# The fix is to pass values through `env:` and reference them as "$VAR": bash expands an environment
# value AFTER parsing and never re-parses its contents. That is a rule about how the file is written,
# which makes it mechanically checkable — so it is checked, rather than left as a habit that holds
# until someone in a hurry writes one more `gh release upload "${{ ... }}"`.
#
# Deliberately absolute: no allowlist for "obviously safe" expressions like github.repository. An
# allowlist is where this decays, because the next entry is always argued for individually and the
# reviewer has to re-derive the taint analysis each time. `env:` costs one line and always works.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

scan() {   # $1 = directory of workflow YAML; prints one line per offence
    python3 - "$1" <<'PY'
import sys, re, pathlib
try:
    import yaml
except ImportError:
    print("::error::PyYAML not available", file=sys.stderr); sys.exit(2)
bad = 0
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.yml")):
    try:
        doc = yaml.safe_load(f.read_text())
    except Exception as e:
        print(f"{f}: unparseable ({e})"); bad += 1; continue
    for jn, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for i, st in enumerate(job.get("steps") or []):
            if not isinstance(st, dict):
                continue
            run = st.get("run")
            if isinstance(run, str) and "${{" in run:
                for expr in sorted(set(re.findall(r"\$\{\{[^}]*\}\}", run))):
                    print(f"{f.name}: job '{jn}' step '{st.get('name') or i}' interpolates {expr}")
                    bad += 1
sys.exit(1 if bad else 0)
PY
}

echo "== no \${{ }} interpolation inside any run: block =="
if out=$(scan .github/workflows); then
    echo "ok   every workflow passes values through env:, not shell text"
else
    echo "$out" | sed 's/^/  /'
    echo "::error::a run: block interpolates a \${{ }} expression — pass it via env: and use \"\$VAR\"" >&2
    exit 1
fi

# ── positive control ──────────────────────────────────────────────────────────────────────────────
# A checker that cannot fail is not a checker. Prove it flags the exact shape that was fixed.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/injected.yml" <<'YAML'
name: control
on: [workflow_dispatch]
jobs:
  sign:
    runs-on: ubuntu-latest
    steps:
      - name: Upload
        run: gh release upload "${{ steps.tag.outputs.tag }}" SHA256SUMS.txt
YAML
if scan "$TMP" >/dev/null 2>&1; then
    echo "FAIL positive control passed — the checker cannot detect an injected run: block" >&2
    exit 1
fi
echo "ok   positive control: an interpolated run: block is still detected"
