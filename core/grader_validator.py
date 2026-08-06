"""T3.1 — Grader validator.

A grader is only worth what the bug narrative under it is worth
(upgrade-plan.md §11). But even a well-narrated bug can carry a grader
that keys on an implementation detail of the canonical fix — passing
for the wrong reason. This validator refuses to trust a grader until
it demonstrates three properties:

1. **PASS on the canonical fix** — the shipped model diff should
   satisfy the grader. If it doesn't, the grader is over-specified.
2. **FAIL on the baseline (unfixed) tree** — the grader must trip on
   the bug. If it doesn't, the grader is under-specified.
3. **FAIL on ≥ N author-supplied mutated buggy trees** — variants
   that fix a different thing, or trivially perturb the baseline, or
   fake the canonical fix's shape without addressing the root cause.
   These are the false-positive suite. If the grader PASSes any of
   them, it keys on an implementation detail instead of the fix.

The validator drives the candidate's own `grade.py` (invoked as a
subprocess so the grader's argv shape is preserved) against report
JSONs listed in a manifest. The manifest lives beside the grader as
:data:`GRADER_VALIDATION_FILENAME` and looks like::

    {
      "schema_version": "1",
      "baseline_report": "baseline-attribution.json",
      "canonical_fix_report": "validation/canonical-fix.json",
      "mutated_buggy_reports": [
        "validation/mutation-01-perturbed.json",
        "validation/mutation-02-wrong-layer.json",
        "validation/mutation-03-partial.json"
      ]
    }

Author responsibility, not harness auto-mutation: the mutations are
the false-positive suite for the specific bug's fix, and only the
author knows which classes of near-miss matter for their bug.

Rule 5 (fail-loud): every kind of malformed manifest raises
:class:`GraderValidatorError`. Rule 1 (bounded): validator's cost is
O(len(mutated_buggy_reports) + 2) grader invocations.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "GRADER_VALIDATION_FILENAME",
    "GRADER_VALIDATION_MIN_MUTATIONS",
    "GRADER_VALIDATION_SCHEMA_VERSION",
    "GraderInvocation",
    "GraderValidationReport",
    "GraderValidatorError",
    "run_grader_validation",
]


GRADER_VALIDATION_FILENAME: Final = "grader-validation.json"
GRADER_VALIDATION_SCHEMA_VERSION: Final = "1"
# Minimum number of mutated buggy trees required. The floor is 3 —
# below that the false-positive suite is too small to catch grader
# over-specification with meaningful signal. See upgrade-plan.md §8
# T3.1 for the design rationale.
GRADER_VALIDATION_MIN_MUTATIONS: Final = 3
_GRADER_SCRIPT_NAME: Final = "grade.py"
_GRADER_TIMEOUT_SECONDS: Final = 120
_EXIT_PASS: Final = 0
_EXIT_FAIL: Final = 1


class GraderValidatorError(RuntimeError):
    """Precondition failure — missing files, malformed manifest, etc.

    Distinct from a grader-invocation failure: an invocation that
    exits FAIL (or crashes) is *data*, recorded on the
    :class:`GraderInvocation`. This exception is precondition-only.
    """


@dataclass(frozen=True, slots=True)
class GraderInvocation:
    """One grader run's outcome.

    ``expected`` is what the validator asserted:
      * ``"pass"`` — grader must exit 0 (canonical fix)
      * ``"fail"`` — grader must exit non-0 (baseline or mutation)
    """

    label: str
    report_path: str
    expected: str  # "pass" | "fail"
    exit_code: int
    matched_expected: bool
    stderr_head: str  # first ~500 chars, for triage


@dataclass(frozen=True, slots=True)
class GraderValidationReport:
    """The validator artifact."""

    schema_version: str
    candidate_dir: str
    invocations: tuple[GraderInvocation, ...]

    @property
    def passed(self) -> bool:
        return all(inv.matched_expected for inv in self.invocations)

    def summary_line(self) -> str:
        """One-liner for CLI stdout."""
        passes = sum(1 for i in self.invocations if i.matched_expected)
        return f"{passes}/{len(self.invocations)} invocations matched expected"

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "candidate_dir": self.candidate_dir,
            "passed": self.passed,
            "summary": self.summary_line(),
            "invocations": [
                {
                    "label": inv.label,
                    "report_path": inv.report_path,
                    "expected": inv.expected,
                    "exit_code": inv.exit_code,
                    "matched_expected": inv.matched_expected,
                    "stderr_head": inv.stderr_head,
                }
                for inv in self.invocations
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2)


def run_grader_validation(candidate_dir: Path) -> GraderValidationReport:
    """Validate the grader under ``candidate_dir``.

    Reads :data:`GRADER_VALIDATION_FILENAME`, drives the candidate's
    :file:`grade.py` against each declared report, asserts each
    invocation's exit code matches its expected outcome.

    Raises:
        GraderValidatorError: candidate dir missing, grade.py missing,
            manifest missing or malformed, mutation count below floor,
            or a referenced report path doesn't exist.
    """
    if not candidate_dir.is_dir():
        raise GraderValidatorError(f"candidate directory {candidate_dir} does not exist")

    grader_path = candidate_dir / _GRADER_SCRIPT_NAME
    if not grader_path.is_file():
        raise GraderValidatorError(
            f"{grader_path} missing — grader validator needs {_GRADER_SCRIPT_NAME} "
            "in the candidate dir"
        )

    manifest_path = candidate_dir / GRADER_VALIDATION_FILENAME
    if not manifest_path.is_file():
        raise GraderValidatorError(
            f"{manifest_path} missing — author must supply the validation "
            "manifest declaring baseline + canonical-fix + mutated-buggy reports"
        )

    baseline_rel, canonical_rel, mutations = _load_manifest(manifest_path)

    invocations: list[GraderInvocation] = []
    invocations.append(
        _invoke_grader(
            candidate_dir=candidate_dir,
            grader_path=grader_path,
            baseline_rel=baseline_rel,
            replay_rel=baseline_rel,
            expected="fail",
            label="baseline",
        )
    )
    invocations.append(
        _invoke_grader(
            candidate_dir=candidate_dir,
            grader_path=grader_path,
            baseline_rel=baseline_rel,
            replay_rel=canonical_rel,
            expected="pass",
            label="canonical-fix",
        )
    )
    for i, mut_rel in enumerate(mutations, start=1):
        invocations.append(
            _invoke_grader(
                candidate_dir=candidate_dir,
                grader_path=grader_path,
                baseline_rel=baseline_rel,
                replay_rel=mut_rel,
                expected="fail",
                label=f"mutation-{i:02d}",
            )
        )

    return GraderValidationReport(
        schema_version=GRADER_VALIDATION_SCHEMA_VERSION,
        candidate_dir=str(candidate_dir),
        invocations=tuple(invocations),
    )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------
def _load_manifest(path: Path) -> tuple[str, str, tuple[str, ...]]:
    """Parse + validate the shape of grader-validation.json.

    Returns ``(baseline_rel, canonical_rel, mutation_rels)`` with all
    strings guaranteed non-empty and the mutation tuple of length ≥
    :data:`GRADER_VALIDATION_MIN_MUTATIONS`.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GraderValidatorError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise GraderValidatorError(f"{path}: top level must be a JSON object")
    schema = raw.get("schema_version")
    if schema != GRADER_VALIDATION_SCHEMA_VERSION:
        raise GraderValidatorError(
            f"{path}: schema_version {schema!r} != "
            f"expected {GRADER_VALIDATION_SCHEMA_VERSION!r}"
        )
    for key in ("baseline_report", "canonical_fix_report", "mutated_buggy_reports"):
        if key not in raw:
            raise GraderValidatorError(f"{path}: missing required field {key!r}")
    baseline = raw["baseline_report"]
    canonical = raw["canonical_fix_report"]
    mutations = raw["mutated_buggy_reports"]
    if not isinstance(baseline, str) or not baseline:
        raise GraderValidatorError(f"{path}: baseline_report must be a non-empty string")
    if not isinstance(canonical, str) or not canonical:
        raise GraderValidatorError(f"{path}: canonical_fix_report must be a non-empty string")
    if not isinstance(mutations, list) or not all(isinstance(m, str) and m for m in mutations):
        raise GraderValidatorError(
            f"{path}: mutated_buggy_reports must be a list of non-empty strings"
        )
    if len(mutations) < GRADER_VALIDATION_MIN_MUTATIONS:
        raise GraderValidatorError(
            f"{path}: mutated_buggy_reports has {len(mutations)} entries; "
            f"minimum is {GRADER_VALIDATION_MIN_MUTATIONS} "
            "(below floor the false-positive suite is too small — "
            "see upgrade-plan.md §8 T3.1)"
        )
    return baseline, canonical, tuple(mutations)


def _invoke_grader(
    *,
    candidate_dir: Path,
    grader_path: Path,
    baseline_rel: str,
    replay_rel: str,
    expected: str,
    label: str,
) -> GraderInvocation:
    """Run grade.py <baseline> <replay> as a subprocess, record outcome.

    We use ``sys.executable`` to make the interpreter explicit — the
    grader is stdlib-only per its own contract, so this is safe.
    """
    baseline_abs = _resolve_under(candidate_dir, baseline_rel, label="baseline_report")
    replay_abs = _resolve_under(candidate_dir, replay_rel, label=f"{label} report_path")

    argv = [sys.executable, str(grader_path), str(baseline_abs), str(replay_abs)]
    try:
        result = subprocess.run(  # noqa: S603 -- argv is fully validated
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GRADER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GraderValidatorError(
            f"{label}: grader exceeded {_GRADER_TIMEOUT_SECONDS}s timeout"
        ) from exc

    matched = (
        (result.returncode == _EXIT_PASS)
        if expected == "pass"
        else (result.returncode != _EXIT_PASS)
    )
    return GraderInvocation(
        label=label,
        report_path=replay_rel,
        expected=expected,
        exit_code=result.returncode,
        matched_expected=matched,
        stderr_head=(result.stderr or "")[:500],
    )


def _resolve_under(root: Path, rel: str, *, label: str) -> Path:
    """Resolve ``rel`` relative to ``root``. Refuse if it escapes.

    Prevents a manifest with ``"../../../etc/passwd"`` from having the
    validator invoke the grader against a file outside the candidate
    directory.
    """
    candidate = (root / rel).resolve()
    root_res = root.resolve()
    try:
        candidate.relative_to(root_res)
    except ValueError as exc:
        raise GraderValidatorError(f"{label} path {rel!r} escapes candidate directory") from exc
    if not candidate.is_file():
        raise GraderValidatorError(f"{label} {candidate} does not exist or is not a regular file")
    return candidate
