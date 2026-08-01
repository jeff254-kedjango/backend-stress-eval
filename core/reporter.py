"""Grading-contract serialiser for :class:`core.runner.RunResult`.

The output of :func:`to_json` is the **grading contract**: given identical
:class:`Report` inputs it produces **byte-identical** JSON. That is the only
way an eval harness can compare model runs objectively (Rule 9's thesis:
deterministic, machine-checkable failures).

Design (see also ``discovery-strategy.md`` §10):

* Split into ``metadata`` (run identity) and ``result`` (the projection of
  ``RunResult``). No wall-clock timestamps inside the byte-compared blob —
  callers wrap the JSON with their own timing if they need it.
* Every mapping is serialised with ``sort_keys=True``. Every nested evidence
  ``Mapping`` is normalised before serialisation so callers who used
  different insertion orders still get identical output.
* Ordered lists (``violations``, ``invariants_evaluated``) preserve the
  order the runner emitted them — that ordering is already deterministic
  and carries provenance.
* Rule 1: O(N_violations + total_evidence_keys). Single pass, no nested
  rebuilds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from core.invariant import JsonValue, Violation
from core.runner import RunResult

__all__ = [
    "SCHEMA_VERSION",
    "Report",
    "ReportMetadata",
    "human_summary",
    "to_json",
]


SCHEMA_VERSION: Final = "1"


# ---------------------------------------------------------------------------
# Metadata + Report — both frozen so the byte-stability guarantee holds.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReportMetadata:
    """Run identity. Every field must be deterministic across replays.

    ``target`` — the ecosystem plugin name (e.g. ``"fastapi"``) or ``"none"``
    for pure-core tests. ``target_commit`` — a commit SHA or version tag that
    pins the target under test. ``seed`` — deterministic seed threaded to the
    state producer. ``iterations_requested`` — what the runner was configured
    with (may exceed ``result.iterations_completed`` if the run short-
    circuited). ``harness_version`` — this package's version.
    """

    target: str
    target_commit: str
    seed: int
    iterations_requested: int
    harness_version: str


@dataclass(frozen=True, slots=True)
class Report:
    """A :class:`ReportMetadata` paired with a :class:`RunResult`."""

    metadata: ReportMetadata
    result: RunResult


# ---------------------------------------------------------------------------
# Serialisers.
# ---------------------------------------------------------------------------
def _normalise_evidence(evidence: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a copy with keys inserted in sorted order.

    ``json.dumps(sort_keys=True)`` already sorts top-level and nested dicts,
    but ``Mapping`` subclasses without deterministic iteration would still
    change the *input* representation Python uses. Normalising here means
    the intermediate dict is also stable — useful for debugging.

    O(k * log k) where k is the number of top-level evidence keys. Bounded
    per violation; documented per Rule 1.
    """
    return {k: evidence[k] for k in sorted(evidence)}


def _violation_dict(v: Violation) -> dict[str, JsonValue]:
    """Project one :class:`Violation` into a JSON-ready dict. O(k)."""
    return {
        "detail": v.detail,
        "evidence": _normalise_evidence(v.evidence),
        "invariant_name": v.invariant_name,
        "iteration": v.iteration,
    }


def _report_dict(report: Report) -> dict[str, JsonValue]:
    """Project a :class:`Report` into a JSON-ready dict.

    O(N_violations + total_evidence_keys). One pass through violations.
    """
    md = report.metadata
    rr = report.result
    return {
        "metadata": {
            "harness_version": md.harness_version,
            "iterations_requested": md.iterations_requested,
            "seed": md.seed,
            "target": md.target,
            "target_commit": md.target_commit,
        },
        "result": {
            "invariants_evaluated": list(rr.invariants_evaluated),
            "iterations_completed": rr.iterations_completed,
            "success": rr.success,
            "violations": [_violation_dict(v) for v in rr.violations],
        },
        "schema_version": SCHEMA_VERSION,
    }


def to_json(report: Report) -> bytes:
    """Serialise ``report`` to byte-stable JSON.

    * ``sort_keys=True`` for stable key order at every depth.
    * ``ensure_ascii=False`` so UTF-8 evidence round-trips without escapes.
    * Compact separators so no whitespace-related churn.
    * Trailing newline for POSIX-friendly file writes.
    """
    payload = _report_dict(report)
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Human summary — never mixed into the byte-stable JSON.
# ---------------------------------------------------------------------------
def human_summary(report: Report) -> str:
    """One-shot human summary. Rule 5: actionable, includes real numbers."""
    rr = report.result
    md = report.metadata
    header = (
        f"[{md.target}@{md.target_commit}] "
        f"{'PASS' if rr.success else 'FAIL'} "
        f"iterations={rr.iterations_completed}/{md.iterations_requested} "
        f"invariants={len(rr.invariants_evaluated)} "
        f"violations={len(rr.violations)}"
    )
    if not rr.violations:
        return header
    lines = [header]
    # One line per violation. Ordered as emitted (deterministic).
    for v in rr.violations:
        iter_str = "end" if v.iteration is None else str(v.iteration)
        lines.append(f"  - iter={iter_str} {v.invariant_name}: {v.detail}")
    return "\n".join(lines)
