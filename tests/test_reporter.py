"""Tests for :mod:`core.reporter` — the byte-stable grading contract.

Rule 9: every assertion is on synthetic RunResults we construct directly.
No wall-clock, no environmental state — bytes in must equal bytes out.
"""

from __future__ import annotations

import json

import pytest

from core.invariant import Violation
from core.reporter import (
    SCHEMA_VERSION,
    Report,
    ReportMetadata,
    human_summary,
    to_json,
)
from core.runner import RunResult

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _metadata(**overrides: object) -> ReportMetadata:
    defaults: dict[str, object] = {
        "target": "none",
        "target_commit": "dev",
        "seed": 0,
        "iterations_requested": 5,
        "harness_version": "0.0.1",
    }
    defaults.update(overrides)
    return ReportMetadata(**defaults)  # type: ignore[arg-type]


def _clean_result() -> RunResult:
    return RunResult(
        success=True,
        iterations_completed=5,
        violations=(),
        invariants_evaluated=("a", "b"),
    )


def _dirty_result() -> RunResult:
    return RunResult(
        success=False,
        iterations_completed=10,
        violations=(
            Violation(
                invariant_name="rss_return_to_baseline",
                detail="RSS drifted +500 KB",
                evidence={"drift_kb": 500, "baseline_kb": 10000, "current_kb": 10500},
                iteration=37,
            ),
            Violation(
                invariant_name="fd_return_to_baseline",
                detail="FD leaked +2",
                evidence={"drift": 2},
                iteration=42,
            ),
        ),
        invariants_evaluated=("rss_return_to_baseline", "fd_return_to_baseline"),
    )


# ---------------------------------------------------------------------------
# Byte stability — the whole point.
# ---------------------------------------------------------------------------


class TestByteStability:
    def test_identical_reports_produce_identical_bytes(self) -> None:
        report = Report(metadata=_metadata(), result=_dirty_result())
        outputs = [to_json(report) for _ in range(10)]
        first = outputs[0]
        for o in outputs[1:]:
            assert o == first

    def test_evidence_key_order_does_not_affect_output(self) -> None:
        # Two Violations with the same keys inserted in different orders.
        v_forward = Violation(
            invariant_name="x",
            detail="d",
            evidence={"a": 1, "b": 2, "c": 3},
            iteration=1,
        )
        v_reverse = Violation(
            invariant_name="x",
            detail="d",
            evidence={"c": 3, "b": 2, "a": 1},
            iteration=1,
        )
        report_forward = Report(
            metadata=_metadata(),
            result=RunResult(
                success=False,
                iterations_completed=1,
                violations=(v_forward,),
                invariants_evaluated=("x",),
            ),
        )
        report_reverse = Report(
            metadata=_metadata(),
            result=RunResult(
                success=False,
                iterations_completed=1,
                violations=(v_reverse,),
                invariants_evaluated=("x",),
            ),
        )
        assert to_json(report_forward) == to_json(report_reverse)

    def test_output_ends_with_newline(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        assert to_json(report).endswith(b"\n")

    def test_output_is_valid_utf8(self) -> None:
        # Unicode evidence must survive round-trip.
        v = Violation(
            invariant_name="unicode_probe",
            detail="drift 中文 → 500",
            evidence={"note": "café ☕"},
            iteration=1,
        )
        report = Report(
            metadata=_metadata(),
            result=RunResult(
                success=False,
                iterations_completed=1,
                violations=(v,),
                invariants_evaluated=("unicode_probe",),
            ),
        )
        raw = to_json(report)
        decoded = raw.decode("utf-8")
        # Must round-trip through JSON without losing characters.
        payload = json.loads(decoded)
        assert payload["result"]["violations"][0]["detail"] == "drift 中文 → 500"
        assert payload["result"]["violations"][0]["evidence"]["note"] == "café ☕"


# ---------------------------------------------------------------------------
# Schema shape.
# ---------------------------------------------------------------------------


class TestSchemaShape:
    def test_top_level_keys_are_locked(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        payload = json.loads(to_json(report))
        assert sorted(payload.keys()) == ["metadata", "result", "schema_version"]
        assert payload["schema_version"] == SCHEMA_VERSION

    def test_metadata_keys_are_locked(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        md = json.loads(to_json(report))["metadata"]
        assert sorted(md.keys()) == [
            "harness_version",
            "iterations_requested",
            "seed",
            "target",
            "target_commit",
        ]

    def test_result_keys_are_locked(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        r = json.loads(to_json(report))["result"]
        assert sorted(r.keys()) == [
            "invariants_evaluated",
            "iterations_completed",
            "success",
            "violations",
        ]

    def test_violation_keys_are_locked(self) -> None:
        report = Report(metadata=_metadata(), result=_dirty_result())
        v = json.loads(to_json(report))["result"]["violations"][0]
        assert sorted(v.keys()) == ["detail", "evidence", "invariant_name", "iteration"]


# ---------------------------------------------------------------------------
# Provenance ordering.
# ---------------------------------------------------------------------------


class TestOrderingPreserved:
    def test_violations_preserve_emission_order(self) -> None:
        # First violation was iteration 37; second was 42. That order carries
        # provenance and must survive serialisation.
        report = Report(metadata=_metadata(), result=_dirty_result())
        vs = json.loads(to_json(report))["result"]["violations"]
        assert [v["iteration"] for v in vs] == [37, 42]

    def test_invariants_evaluated_preserve_registration_order(self) -> None:
        rr = RunResult(
            success=True,
            iterations_completed=1,
            violations=(),
            invariants_evaluated=("z", "a", "m"),
        )
        report = Report(metadata=_metadata(), result=rr)
        assert json.loads(to_json(report))["result"]["invariants_evaluated"] == [
            "z",
            "a",
            "m",
        ]


# ---------------------------------------------------------------------------
# Human summary.
# ---------------------------------------------------------------------------


class TestHumanSummary:
    def test_pass_header(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        line = human_summary(report)
        assert "PASS" in line
        assert "iterations=5/5" in line
        assert "violations=0" in line

    def test_fail_header_and_one_line_per_violation(self) -> None:
        report = Report(metadata=_metadata(iterations_requested=100), result=_dirty_result())
        summary = human_summary(report)
        lines = summary.splitlines()
        assert "FAIL" in lines[0]
        assert "iterations=10/100" in lines[0]
        assert "violations=2" in lines[0]
        # One line per violation, in emission order.
        assert "iter=37" in lines[1]
        assert "rss_return_to_baseline" in lines[1]
        assert "iter=42" in lines[2]
        assert "fd_return_to_baseline" in lines[2]

    def test_end_iteration_rendered_as_end(self) -> None:
        # Violations with iteration=None (from end_only cadence) render as "end".
        v = Violation(
            invariant_name="tail_check",
            detail="final drift",
            evidence={},
            iteration=None,
        )
        rr = RunResult(
            success=False,
            iterations_completed=5,
            violations=(v,),
            invariants_evaluated=("tail_check",),
        )
        report = Report(metadata=_metadata(), result=rr)
        assert "iter=end" in human_summary(report)


# ---------------------------------------------------------------------------
# Empty-violations edge.
# ---------------------------------------------------------------------------


class TestEmptyViolations:
    def test_empty_violations_list_survives_roundtrip(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        payload = json.loads(to_json(report))
        assert payload["result"]["violations"] == []
        assert payload["result"]["success"] is True


# ---------------------------------------------------------------------------
# Malformed metadata.
# ---------------------------------------------------------------------------


class TestMetadataFrozen:
    def test_metadata_is_frozen(self) -> None:
        md = _metadata()
        with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError subclass
            md.target = "changed"  # type: ignore[misc]

    def test_report_is_frozen(self) -> None:
        report = Report(metadata=_metadata(), result=_clean_result())
        with pytest.raises(Exception):  # noqa: B017
            report.result = _dirty_result()  # type: ignore[misc]
