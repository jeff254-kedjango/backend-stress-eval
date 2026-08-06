#!/usr/bin/env bash
# nightly-verify-repro.sh — invoke `bse verify-repro` against every
# candidate under eval-tasks/ and record the outcome as a per-candidate
# repro-verification.json (plus a rolled-up ledger for triage).
#
# T3.2 (upgrade-plan.md §8): the affidavit captured a bench transcript
# at a pin. Between then and today, the upstream may have yanked, patched,
# or drifted. This script is the operator-owned nightly cron that keeps
# every packaged candidate's provenance honest.
#
# Not run by CI (that would slow the main pipeline). Wire this into
# systemd/cron/gh-actions-schedule and pipe the ledger to whatever
# dashboard you use for reviewer-triage.
#
# Usage:
#   nightly-verify-repro.sh                      # every candidate
#   nightly-verify-repro.sh anyio-lifecycle-leak # one candidate
#
# Exit codes:
#   0 — every attempted candidate still reproduces
#   1 — at least one candidate no longer reproduces (regression)
#   2 — setup error (bse not on PATH, eval-tasks/ missing, etc.)
#
# The pinned-package name is read from the plugin manifest that owns
# each candidate. Candidates lacking a plugin manifest are skipped.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
EVAL_TASKS="${REPO_ROOT}/eval-tasks"
LEDGER="${REPO_ROOT}/reports/repro-verifier/ledger.jsonl"

if ! command -v bse >/dev/null 2>&1; then
    echo "error: 'bse' not on PATH — activate the venv first" >&2
    exit 2
fi
if [[ ! -d "${EVAL_TASKS}" ]]; then
    echo "error: ${EVAL_TASKS} missing — expected candidates under eval-tasks/" >&2
    exit 2
fi

mkdir -p "$(dirname "${LEDGER}")"

# Which candidates to sweep. When no arg, walk every non-hidden non-underscore
# directory under eval-tasks/ (skip _shelved, .archive, etc.).
if [[ $# -gt 0 ]]; then
    CANDIDATES=("$@")
else
    CANDIDATES=()
    for d in "${EVAL_TASKS}"/*/; do
        base="$(basename "${d}")"
        case "${base}" in
            _*|.*) continue ;;
        esac
        CANDIDATES+=("${base}")
    done
fi

any_regression=0
timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

for cand in "${CANDIDATES[@]}"; do
    cand_dir="${EVAL_TASKS}/${cand}"
    if [[ ! -d "${cand_dir}" ]]; then
        echo "skip: ${cand} — directory not found" >&2
        continue
    fi

    # The pinned-package name is required. The operator hooks a lookup
    # here — this loop calls out to a per-candidate helper file if the
    # candidate ships one, else prints a stderr warning and skips.
    pin_hint="${cand_dir}/.pinned-package"
    if [[ ! -f "${pin_hint}" ]]; then
        echo "skip: ${cand} — no .pinned-package hint file" >&2
        continue
    fi
    pinned_package="$(head -n1 "${pin_hint}" | tr -d '[:space:]')"

    echo "==> verify-repro: ${cand} (pin=${pinned_package})"
    if bse verify-repro "${cand_dir}" --pinned-package "${pinned_package}"; then
        outcome="still-reproducible"
    else
        outcome="no-longer-reproducible"
        any_regression=1
    fi

    # Append one JSON line to the ledger for downstream triage.
    printf '{"timestamp":"%s","candidate":"%s","outcome":"%s"}\n' \
        "${timestamp}" "${cand}" "${outcome}" >> "${LEDGER}"
done

if [[ ${any_regression} -ne 0 ]]; then
    echo "==> at least one candidate no longer reproduces — see ${LEDGER}" >&2
    exit 1
fi
echo "==> all candidates still reproduce."
