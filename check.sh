#!/usr/bin/env bash
# check.sh — single gate for the whole repo.
# Runs: ruff lint, ruff format check, mypy --strict, pytest, pip-audit.
# Exits non-zero on the first failure. See rules.md.

set -euo pipefail

# Always run from the repo root, regardless of caller's CWD.
cd "$(dirname "$(readlink -f "$0")")"

VENV="$PWD/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "check.sh: venv missing at $VENV — see README/discovery-strategy.md" >&2
    exit 2
fi

# Force the venv Python for every step. Never inherit ambient interpreters.
# Scrub inherited venv/env vars so tools can't pick up another project's venv
# (observed: pip-audit warns when a sibling repo's .venv is exported).
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH
export PATH="$VENV/bin:$PATH"
PY="$VENV/bin/python"

echo "==> ruff lint"
"$PY" -m ruff check .

echo "==> ruff format --check"
"$PY" -m ruff format --check .

echo "==> mypy --strict"
"$PY" -m mypy

echo "==> pytest"
"$PY" -m pytest

echo "==> pip-audit"
# Audit installed third-party wheels for known vulnerabilities.
# --skip-editable: our own editable install (backend-stress-eval) has no PyPI
#   record — skip it. NOTE: pip-audit's --strict means "fail if any dep is
#   skipped", which combined with --skip-editable would falsely fail the gate.
#   Default (non-strict) mode still exits non-zero on any real vulnerability,
#   which is what we actually want.
"$PY" -m pip_audit --skip-editable

echo "==> all gates green"
