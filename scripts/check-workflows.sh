#!/usr/bin/env bash
# Fail if any GitHub Actions workflow file is not valid YAML. The trap this guards: an unquoted
# "colon space" inside a step `name:` (e.g. "regression: v0.9.0") reparses as a nested mapping and breaks
# the ENTIRE workflow run — GitHub reports a bare failure with no jobs, which is easy to misread. Run this
# locally before pushing any workflow edit; CI runs it on every push.
set -euo pipefail
python3 -c "import yaml" 2>/dev/null || pip install --quiet pyyaml
python3 - <<'PY'
import sys, glob, yaml
bad = 0
files = sorted(glob.glob(".github/workflows/*.yml") + glob.glob(".github/workflows/*.yaml"))
for f in files:
    try:
        yaml.safe_load(open(f)); print(f"ok   {f}")
    except Exception as e:
        print(f"FAIL {f}: {e}"); bad += 1
if not files: print("no workflow files found"); sys.exit(1)
sys.exit(1 if bad else 0)
PY
