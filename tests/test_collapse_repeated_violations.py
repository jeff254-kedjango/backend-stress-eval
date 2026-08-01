"""Tests for :func:`harnesses.collapse_repeated_violations`.

Planted-fixture tests — every input is a hand-built :class:`RunResult`, so
these tests are fully deterministic (Rule 9 discipline) and do not depend on
real RSS / real FastAPI iterations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from core.invariant import JsonValue, Ok, Violation  # noqa: F401 — Ok reserved for symmetry
from core.reporter import Report, ReportMetadata, to_json
from core.runner import RunResult
from harnesses import collapse_repeated_violations


def _summary_of(v: Violation) -> Mapping[str, JsonValue]:
    """Narrow ``evidence['collapsed']`` to a Mapping for mypy-strict indexing.

    The helper writes a dict there; :data:`JsonValue` is a wide union so this
    tiny cast keeps tests readable without ``# type: ignore`` per line.
    """
    return cast(Mapping[str, JsonValue], v.evidence["collapsed"])


def _v(name: str, iteration: int, drift_kb: int) -> Violation:
    return Violation(
        invariant_name=name,
        detail=f"RSS drifted +{drift_kb} KB above baseline (slack 1024 KB)",
        evidence={
            "baseline_kb": 44928,
            "current_kb": 44928 + drift_kb,
            "drift_kb": drift_kb,
            "slack_kb": 1024,
        },
        iteration=iteration,
    )


def _rr(violations: tuple[Violation, ...], invariants: tuple[str, ...]) -> RunResult:
    return RunResult(
        success=not violations,
        iterations_completed=500,
        violations=violations,
        invariants_evaluated=invariants,
    )


class TestCollapseRepeatedViolations:
    def test_empty_violations_pass_through_unchanged(self) -> None:
        rr = _rr((), ("rss_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        assert out is rr  # short-circuit — identity preserved

    def test_single_violation_still_gets_summary_fold(self) -> None:
        # A single hit is folded too — the ``collapsed`` summary is uniform
        # so grading readers never have to special-case the "just one" path.
        rr = _rr((_v("rss_return_to_baseline", 82, 1152),), ("rss_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        assert len(out.violations) == 1
        summary = _summary_of(out.violations[0])
        assert summary == {
            "count": 1,
            "first_iteration": 82,
            "last_iteration": 82,
            "max_drift_kb": 1152,
        }

    def test_repeated_same_invariant_collapsed_to_first(self) -> None:
        # The FastAPI-heavy-load shape: 418 threshold crossings over iters 82..499.
        vs = tuple(_v("rss_return_to_baseline", i, 1152 + (i - 82) * 9) for i in range(82, 500))
        rr = _rr(vs, ("rss_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        assert len(out.violations) == 1
        first = out.violations[0]
        # First-iteration Violation is retained verbatim (aside from evidence fold).
        assert first.iteration == 82
        assert first.evidence["drift_kb"] == 1152
        summary = _summary_of(first)
        assert summary["count"] == 418
        assert summary["first_iteration"] == 82
        assert summary["last_iteration"] == 499
        # Max drift = 1152 + (499-82)*9 = 4905
        assert summary["max_drift_kb"] == 1152 + (499 - 82) * 9

    def test_distinct_invariants_kept_separately(self) -> None:
        rss = _v("rss_return_to_baseline", 82, 1152)
        fd = Violation(
            invariant_name="fd_return_to_baseline",
            detail="FD count drifted +2 above baseline (slack 0)",
            evidence={"baseline_count": 4, "current_count": 6, "drift": 2, "slack": 0},
            iteration=90,
        )
        rr = _rr((rss, fd), ("rss_return_to_baseline", "fd_return_to_baseline"))
        out = collapse_repeated_violations(rr)
        names = sorted(v.invariant_name for v in out.violations)
        assert names == ["fd_return_to_baseline", "rss_return_to_baseline"]

    def test_generic_drift_evidence_key_folded_as_max_drift(self) -> None:
        # FD invariants use ``drift`` (no _kb suffix). The helper folds them
        # into ``max_drift`` rather than ``max_drift_kb`` so the two shapes
        # don't collide.
        fd1 = Violation(
            invariant_name="fd_return_to_baseline",
            detail="fd +2",
            evidence={"drift": 2},
            iteration=10,
        )
        fd2 = Violation(
            invariant_name="fd_return_to_baseline",
            detail="fd +5",
            evidence={"drift": 5},
            iteration=15,
        )
        rr = _rr((fd1, fd2), ("fd_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        assert len(out.violations) == 1
        summary = _summary_of(out.violations[0])
        assert summary["max_drift"] == 5
        assert "max_drift_kb" not in summary

    def test_success_and_metadata_unchanged(self) -> None:
        rr = _rr((_v("rss_return_to_baseline", 82, 1152),), ("rss_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        assert out.success is rr.success
        assert out.iterations_completed == rr.iterations_completed
        assert out.invariants_evaluated == rr.invariants_evaluated

    def test_first_violation_preserved_when_earlier_smaller(self) -> None:
        # First = smaller drift, last = larger drift. First is retained; max
        # is tracked in the summary. Guarantees the report always points at
        # the *earliest* moment the property broke (Rule 9's "point at the
        # first observable failure, not the loudest one").
        a = _v("rss_return_to_baseline", 82, 1152)
        b = _v("rss_return_to_baseline", 499, 4864)
        rr = _rr((a, b), ("rss_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        assert out.violations[0].iteration == 82
        assert out.violations[0].evidence["drift_kb"] == 1152
        assert _summary_of(out.violations[0])["max_drift_kb"] == 4864

    def test_deterministic_across_repeats(self) -> None:
        # Rule 9 — the whole project's thesis. Same input → identical output
        # (including field ordering after JSON serialisation).
        vs = tuple(_v("rss_return_to_baseline", i, 1152 + (i - 82) * 9) for i in range(82, 200))
        rr = _rr(vs, ("rss_return_to_baseline",))
        blobs = {
            to_json(
                Report(
                    metadata=ReportMetadata(
                        target="fastapi",
                        target_commit="fastapi-0.141.1",
                        seed=0,
                        iterations_requested=500,
                        harness_version="0.0.1",
                    ),
                    result=collapse_repeated_violations(rr),
                )
            )
            for _ in range(5)
        }
        assert len(blobs) == 1  # byte-identical across 5 repeats

    def test_evidence_is_json_serialisable(self) -> None:
        vs = tuple(_v("rss_return_to_baseline", i, 1152 + i * 4) for i in range(82, 100))
        rr = _rr(vs, ("rss_return_to_baseline",))
        out = collapse_repeated_violations(rr)
        payload = json.dumps({k: v for k, v in out.violations[0].evidence.items()})
        loaded = json.loads(payload)
        assert loaded["collapsed"]["count"] == 18
