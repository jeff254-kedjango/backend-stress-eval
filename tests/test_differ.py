"""Tests for :mod:`core.differ` and the ``bse diff`` verb.

The differ is pure data manipulation — no subprocess, no network. Tests
construct Report/RunResult/Violation instances directly and assert on the
resulting DiffReport. The CLI file-mode path is tested end-to-end using
two report.json files written under tmp_path.

We do NOT test the in-process CLI mode here (that requires actual pip
installs at specific versions, which is a real integration test and
belongs in a manual smoke script — see scripts/smoke_difficulty_check.sh
for the pattern). The core differ logic driving that mode is fully
covered via direct calls to diff_reports().
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from cli.main import (
    EXIT_DIFF_HAS_CHANGES,
    EXIT_DIFF_PRECONDITION,
    EXIT_OK,
    main,
)
from core.differ import (
    DIFF_REPORT_FILENAME,
    DIFF_SCHEMA_VERSION,
    ViolationKey,
    diff_report_dicts,
    diff_reports,
    load_report_json,
)
from core.invariant import Violation
from core.reporter import Report, ReportMetadata
from core.runner import RunResult


# ---------------------------------------------------------------------------
# Small factories so each test spells out only what it varies.
# ---------------------------------------------------------------------------
def _violation(
    invariant_name: str,
    detail: str = "boom",
    iteration: int | None = 42,
    evidence: dict[str, int | float | str] | None = None,
) -> Violation:
    body: dict[str, int | float | str] = evidence if evidence is not None else {"k": 1}
    return Violation(
        invariant_name=invariant_name,
        detail=detail,
        evidence=MappingProxyType(body),
        iteration=iteration,
    )


def _report(
    *,
    target_commit: str,
    violations: tuple[Violation, ...] = (),
    invariants_evaluated: tuple[str, ...] = ("rss_return_to_baseline",),
    iterations_completed: int = 500,
) -> Report:
    return Report(
        metadata=ReportMetadata(
            target="stub",
            target_commit=target_commit,
            seed=0,
            iterations_requested=500,
            harness_version="0.0.1",
        ),
        result=RunResult(
            invariants_evaluated=invariants_evaluated,
            iterations_completed=iterations_completed,
            success=not violations,
            violations=violations,
        ),
    )


# ---------------------------------------------------------------------------
# diff_reports — the core function.
# ---------------------------------------------------------------------------
class TestDiffReports:
    def test_identical_reports_produce_empty_diff(self) -> None:
        """Same violations on both sides → no changes → CLI would exit 0."""
        v = _violation("rss_return_to_baseline")
        a = {"layer1_repetition": _report(target_commit="A", violations=(v,))}
        b = {"layer1_repetition": _report(target_commit="B", violations=(v,))}
        diff = diff_reports(a, b, target_a="A", target_b="B")
        assert not diff.has_changes
        assert diff.layers[0].only_in_a == ()
        assert diff.layers[0].only_in_b == ()
        assert diff.layers[0].evidence_changed == ()

    def test_new_violation_in_b_surfaces_as_only_in_b(self) -> None:
        """Regression: B has a violation A does not."""
        v_new = _violation("fd_return_to_baseline", iteration=100)
        a = {"layer1_repetition": _report(target_commit="A", violations=())}
        b = {"layer1_repetition": _report(target_commit="B", violations=(v_new,))}
        diff = diff_reports(a, b, target_a="A", target_b="B")
        layer = diff.layers[0]
        assert len(layer.only_in_b) == 1
        assert layer.only_in_b[0]["invariant_name"] == "fd_return_to_baseline"
        assert layer.only_in_b[0]["layer"] == "layer1_repetition"
        assert not layer.only_in_a
        assert diff.summary_line() == "+ 1 regressions, - 0 fixes, ~ 0 drift"

    def test_removed_violation_in_b_surfaces_as_only_in_a(self) -> None:
        """Upstream fix: A had a violation, B does not."""
        v_gone = _violation("rss_return_to_baseline", iteration=250)
        a = {"layer1_repetition": _report(target_commit="A", violations=(v_gone,))}
        b = {"layer1_repetition": _report(target_commit="B", violations=())}
        diff = diff_reports(a, b, target_a="A", target_b="B")
        assert len(diff.layers[0].only_in_a) == 1
        assert diff.summary_line() == "+ 0 regressions, - 1 fixes, ~ 0 drift"

    def test_shared_key_with_changed_evidence_is_drift(self) -> None:
        """Same invariant/iteration on both sides but evidence differs."""
        v_a = _violation("rss_return_to_baseline", iteration=500, evidence={"delta_kb": 100})
        v_b = _violation("rss_return_to_baseline", iteration=500, evidence={"delta_kb": 250})
        a = {"layer1_repetition": _report(target_commit="A", violations=(v_a,))}
        b = {"layer1_repetition": _report(target_commit="B", violations=(v_b,))}
        diff = diff_reports(a, b, target_a="A", target_b="B")
        layer = diff.layers[0]
        assert not layer.only_in_a
        assert not layer.only_in_b
        assert len(layer.evidence_changed) == 1
        changed = layer.evidence_changed[0]
        assert changed["invariant_name"] == "rss_return_to_baseline"
        assert changed["a"] == {"detail": "boom", "evidence": {"delta_kb": 100}}
        assert changed["b"] == {"detail": "boom", "evidence": {"delta_kb": 250}}

    def test_layer_present_only_in_b_still_diffs(self) -> None:
        """B has layer3_variants; A doesn't. B's violations show up as only_in_b."""
        v = _violation("route_registry_stable", iteration=None)
        a = {"layer1_repetition": _report(target_commit="A")}
        b = {
            "layer1_repetition": _report(target_commit="B"),
            "layer3_variants": _report(target_commit="B", violations=(v,)),
        }
        diff = diff_reports(a, b, target_a="A", target_b="B")
        assert {layer.layer for layer in diff.layers} == {
            "layer1_repetition",
            "layer3_variants",
        }
        # The new-in-B layer's violation is a "regression" per the summary.
        assert "1 regressions" in diff.summary_line()

    def test_violations_with_iteration_none_still_diff_correctly(self) -> None:
        """iteration=None (end-of-run violations) must not collide with iteration=0."""
        v_none = _violation("route_registry_stable", iteration=None)
        v_zero = _violation("route_registry_stable", iteration=0)
        a = {"layer1_repetition": _report(target_commit="A", violations=(v_none,))}
        b = {"layer1_repetition": _report(target_commit="B", violations=(v_zero,))}
        diff = diff_reports(a, b, target_a="A", target_b="B")
        layer = diff.layers[0]
        # Different keys because iteration differs → each appears on its own side.
        assert len(layer.only_in_a) == 1
        assert len(layer.only_in_b) == 1


# ---------------------------------------------------------------------------
# ViolationKey — the identity dataclass.
# ---------------------------------------------------------------------------
class TestViolationKey:
    def test_hashable(self) -> None:
        # frozen=True + slots=True means keys can go in a set.
        k = ViolationKey(layer="l1", invariant_name="rss", iteration=1)
        assert k in {k}


# ---------------------------------------------------------------------------
# to_json byte-stability.
# ---------------------------------------------------------------------------
class TestJsonSerialization:
    def test_to_json_is_sort_keyed(self) -> None:
        v = _violation("rss_return_to_baseline")
        a = {"layer1_repetition": _report(target_commit="A")}
        b = {"layer1_repetition": _report(target_commit="B", violations=(v,))}
        diff = diff_reports(a, b, target_a="A", target_b="B")
        payload = diff.to_json()
        parsed = json.loads(payload)
        assert parsed["schema_version"] == DIFF_SCHEMA_VERSION
        assert parsed["target_a"] == "A"
        assert parsed["target_b"] == "B"
        # sort_keys=True — verify keys appear in sorted order.
        assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# load_report_json and diff_report_dicts — the disk-serialised path.
# ---------------------------------------------------------------------------
class TestLoadReportJson:
    def _write_report_json(self, path: Path, layers: dict[str, dict[str, object]]) -> None:
        payload = {"discovery_schema_version": "1", "layers": layers}
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_load_and_diff_file_mode(self, tmp_path: Path) -> None:
        report_a = tmp_path / "a.json"
        report_b = tmp_path / "b.json"
        self._write_report_json(
            report_a,
            {
                "layer1_repetition": {
                    "metadata": {"target_commit": "A"},
                    "result": {"success": True, "violations": []},
                }
            },
        )
        self._write_report_json(
            report_b,
            {
                "layer1_repetition": {
                    "metadata": {"target_commit": "B"},
                    "result": {
                        "success": False,
                        "violations": [
                            {
                                "invariant_name": "rss_return_to_baseline",
                                "detail": "leaked",
                                "evidence": {"delta_kb": 500},
                                "iteration": 100,
                            }
                        ],
                    },
                }
            },
        )
        layers_a = load_report_json(report_a)
        layers_b = load_report_json(report_b)
        diff = diff_report_dicts(layers_a, layers_b, target_a="A", target_b="B")
        assert diff.has_changes
        assert len(diff.layers[0].only_in_b) == 1
        assert diff.layers[0].only_in_b[0]["invariant_name"] == "rss_return_to_baseline"

    def test_load_rejects_non_object(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="expected JSON object"):
            load_report_json(p)

    def test_load_rejects_missing_layers(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text('{"something_else": {}}', encoding="utf-8")
        with pytest.raises(ValueError, match="expected top-level `layers`"):
            load_report_json(p)


# ---------------------------------------------------------------------------
# CLI — file mode only. In-process mode requires real pip installs.
# ---------------------------------------------------------------------------
class TestCliFileMode:
    def _write_report_json(self, path: Path, layers: dict[str, dict[str, object]]) -> None:
        payload = {"discovery_schema_version": "1", "layers": layers}
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_no_changes_exits_ok(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        report_a = tmp_path / "a.json"
        report_b = tmp_path / "b.json"
        empty = {
            "layer1_repetition": {
                "metadata": {"target_commit": "X"},
                "result": {"success": True, "violations": []},
            }
        }
        self._write_report_json(report_a, empty)
        self._write_report_json(report_b, empty)
        out_dir = tmp_path / "out"
        rc = main(
            [
                "diff",
                "--a-report",
                str(report_a),
                "--b-report",
                str(report_b),
                "--out",
                str(out_dir),
            ]
        )
        assert rc == EXIT_OK
        assert (out_dir / DIFF_REPORT_FILENAME).is_file()
        assert "0 regressions" in capsys.readouterr().out

    def test_changes_exit_has_changes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report_a = tmp_path / "a.json"
        report_b = tmp_path / "b.json"
        self._write_report_json(
            report_a,
            {
                "layer1_repetition": {
                    "metadata": {"target_commit": "A"},
                    "result": {"success": True, "violations": []},
                }
            },
        )
        self._write_report_json(
            report_b,
            {
                "layer1_repetition": {
                    "metadata": {"target_commit": "B"},
                    "result": {
                        "success": False,
                        "violations": [
                            {
                                "invariant_name": "rss_return_to_baseline",
                                "detail": "leaked",
                                "evidence": {"delta_kb": 500},
                                "iteration": 100,
                            }
                        ],
                    },
                }
            },
        )
        out_dir = tmp_path / "out"
        rc = main(
            [
                "diff",
                "--a-report",
                str(report_a),
                "--b-report",
                str(report_b),
                "--out",
                str(out_dir),
            ]
        )
        assert rc == EXIT_DIFF_HAS_CHANGES
        assert "1 regressions" in capsys.readouterr().out

    def test_missing_report_exits_precondition(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report_a = tmp_path / "a.json"  # never created
        report_b = tmp_path / "b.json"
        self._write_report_json(
            report_b,
            {"layer1_repetition": {"metadata": {}, "result": {"violations": []}}},
        )
        rc = main(
            [
                "diff",
                "--a-report",
                str(report_a),
                "--b-report",
                str(report_b),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        assert rc == EXIT_DIFF_PRECONDITION
        assert "does not exist" in capsys.readouterr().err

    def test_only_one_report_path_provided_exits_precondition(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = tmp_path / "a.json"
        self._write_report_json(p, {"l1": {"metadata": {}, "result": {"violations": []}}})
        rc = main(["diff", "--a-report", str(p)])
        assert rc == EXIT_DIFF_PRECONDITION
        assert "both be provided" in capsys.readouterr().err
