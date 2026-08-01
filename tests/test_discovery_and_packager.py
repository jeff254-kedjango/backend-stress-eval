"""Tests for :mod:`harnesses.discovery` and :mod:`harnesses.eval_task`.

Rule 9: no wall-clock timing, no environmental randomness in assertions.
The discovery run must produce byte-stable output; the packager must write
byte-identical files for identical input.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.reporter import Report, ReportMetadata
from core.runner import RunResult
from harnesses.discovery import run_discovery
from harnesses.eval_task import DISCOVERY_SCHEMA_VERSION, package_eval_task
from plugins.fastapi import FastAPIPlugin, canonical_example_app, minimal_example_app

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="discovery harness needs Linux /proc metrics",
)


# ---------------------------------------------------------------------------
# Discovery sweep — small counts so tests run fast.
# ---------------------------------------------------------------------------


class TestDiscoveryRun:
    def test_returns_all_four_layer_reports(self) -> None:
        reports = run_discovery(
            plugin=FastAPIPlugin(app_factory=canonical_example_app),
            target_commit="test",
            iterations_l1=20,
            rounds_l2=5,
            rounds_l3=3,
            variants=(
                ("minimal", minimal_example_app),
                ("canonical", canonical_example_app),
            ),
            variant_plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
        )
        assert set(reports.keys()) == {
            "layer1_repetition",
            "layer2_lifecycle",
            "layer3_variants",
            "layer4_sequence",
        }
        # Metadata carries the target_commit we asked for.
        for r in reports.values():
            assert r.metadata.target_commit == "test"

    def test_layers_produce_iterations_completed(self) -> None:
        reports = run_discovery(
            plugin=FastAPIPlugin(app_factory=canonical_example_app),
            target_commit="test",
            iterations_l1=10,
            rounds_l2=3,
            rounds_l3=2,
            variants=(
                ("minimal", minimal_example_app),
                ("canonical", canonical_example_app),
            ),
            variant_plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
        )
        assert reports["layer1_repetition"].result.iterations_completed == 10
        assert reports["layer2_lifecycle"].result.iterations_completed == 3
        # Layer 3 aggregates rounds across both variants.
        assert reports["layer3_variants"].result.iterations_completed == 4
        # Layer 4 has 3 steps.
        assert reports["layer4_sequence"].result.iterations_completed == 3

    def test_layer3_skipped_when_no_variants_supplied(self) -> None:
        """R1 change: Layer 3 is optional — plugin authors decide."""
        reports = run_discovery(
            plugin=FastAPIPlugin(app_factory=canonical_example_app),
            target_commit="test",
            iterations_l1=5,
            rounds_l2=2,
        )
        assert set(reports.keys()) == {
            "layer1_repetition",
            "layer2_lifecycle",
            "layer4_sequence",
        }

    def test_variants_without_factory_raises(self) -> None:
        with pytest.raises(ValueError, match="variant_plugin_factory"):
            run_discovery(
                plugin=FastAPIPlugin(app_factory=canonical_example_app),
                target_commit="test",
                iterations_l1=5,
                rounds_l2=2,
                rounds_l3=1,
                variants=(("minimal", minimal_example_app),),
                variant_plugin_factory=None,
            )


# ---------------------------------------------------------------------------
# Packager — byte-stable file output.
# ---------------------------------------------------------------------------


def _tiny_report(target_commit: str = "abc") -> Report:
    return Report(
        metadata=ReportMetadata(
            target="fastapi",
            target_commit=target_commit,
            seed=0,
            iterations_requested=1,
            harness_version="0.0.1",
        ),
        result=RunResult(
            success=True,
            iterations_completed=1,
            violations=(),
            invariants_evaluated=("x",),
        ),
    )


class TestPackager:
    def test_writes_three_files(self, tmp_path: Path) -> None:
        reports = {"layer1_repetition": _tiny_report()}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval1")
        assert (out / "report.json").is_file()
        assert (out / "summary.txt").is_file()
        assert (out / "reproduce.py").is_file()

    def test_report_json_is_byte_identical_across_runs(self, tmp_path: Path) -> None:
        reports = {"layer1_repetition": _tiny_report()}
        first = tmp_path / "run1"
        second = tmp_path / "run2"
        package_eval_task(reports=reports, out_dir=first)
        package_eval_task(reports=reports, out_dir=second)
        assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
        assert (first / "summary.txt").read_bytes() == (second / "summary.txt").read_bytes()
        assert (first / "reproduce.py").read_bytes() == (second / "reproduce.py").read_bytes()

    def test_report_json_carries_schema_version(self, tmp_path: Path) -> None:
        reports = {"layer1_repetition": _tiny_report()}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert payload["discovery_schema_version"] == DISCOVERY_SCHEMA_VERSION
        assert "layers" in payload
        assert "layer1_repetition" in payload["layers"]

    def test_layer_key_order_is_sorted(self, tmp_path: Path) -> None:
        # Two layers inserted in reverse-alphabetical order — output must sort.
        reports = {
            "layer2_lifecycle": _tiny_report(),
            "layer1_repetition": _tiny_report(),
        }
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert list(payload["layers"].keys()) == ["layer1_repetition", "layer2_lifecycle"]

    def test_reproduce_stub_inlines_target_commit(self, tmp_path: Path) -> None:
        reports = {"layer1_repetition": _tiny_report(target_commit="fastapi-0.141.1")}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        stub = (out / "reproduce.py").read_text(encoding="utf-8")
        assert "'fastapi-0.141.1'" in stub

    def test_summary_contains_layer_headers(self, tmp_path: Path) -> None:
        reports = {"layer1_repetition": _tiny_report()}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        summary = (out / "summary.txt").read_text(encoding="utf-8")
        assert "=== layer1_repetition ===" in summary

    def test_empty_reports_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            package_eval_task(reports={}, out_dir=tmp_path / "eval")
