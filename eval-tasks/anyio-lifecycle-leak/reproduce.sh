#!/usr/bin/env bash
# reproduce.sh — measure the anyio leak, then grade the replay.
#
# Assumes a Python 3.12 interpreter with anyio 4.14.2 installed is on PATH
# (or that ``$PYTHON`` is set to one). Writes into a ``replay/`` dir next
# to this script.
#
# Exit codes:
#   0 → PASS   (all four gates hold)
#   1 → FAIL   (a gate does not hold — the fix isn't there or is at the
#              wrong layer)
#   2 → SETUP  (environment / usage error)

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
REPLAY_DIR="${HERE}/replay"
BASELINE="${HERE}/baseline-attribution.json"

if [[ ! -f "${BASELINE}" ]]; then
    echo "error: baseline-attribution.json missing at ${BASELINE}" >&2
    exit 2
fi

if ! "${PY}" -c "import anyio" >/dev/null 2>&1; then
    echo "error: anyio not importable from ${PY} — run 'pip install anyio==4.14.2'" >&2
    exit 2
fi

mkdir -p "${REPLAY_DIR}"

echo "==> measure (${PY} $(basename measure.py) --out replay/report.json)"
"${PY}" "${HERE}/measure.py" --out "${REPLAY_DIR}/report.json"

echo
echo "==> grade"
exec "${PY}" "${HERE}/grade.py" "${BASELINE}" "${REPLAY_DIR}/report.json"
