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


def _variants_report(target_commit: str = "abc") -> Report:
    # Mirrors what run_layer3_variants produces: ``target="variants"`` — the
    # aggregate sentinel that must NOT be treated as a plugin name by the
    # reproduce-stub packager. See test_reproduce_stub_prefers_plugin_over_variants_label.
    return Report(
        metadata=ReportMetadata(
            target="variants",
            target_commit=target_commit,
            seed=0,
            iterations_requested=1,
            harness_version="0.0.1",
        ),
        result=RunResult(
            success=True,
            iterations_completed=1,
            violations=(),
            invariants_evaluated=("x::y",),
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

    def test_reproduce_stub_inlines_plugin_name(self, tmp_path: Path) -> None:
        # Plugin name is lifted from a non-``"variants"`` report's target,
        # so the stub can resolve it via load_manifests() at replay time
        # rather than needing the discovery caller's plugin instance.
        reports = {"layer1_repetition": _tiny_report(target_commit="fastapi-0.141.1")}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        stub = (out / "reproduce.py").read_text(encoding="utf-8")
        assert "'fastapi'" in stub
        # The two placeholders must have been substituted — a raw
        # ``PLACEHOLDER`` token in the shipped stub would be a regression.
        assert "PLUGIN_NAME_PLACEHOLDER" not in stub
        assert "TARGET_COMMIT_PLACEHOLDER" not in stub

    def test_reproduce_stub_prefers_plugin_over_variants_label(self, tmp_path: Path) -> None:
        # Layer 3 aggregate reports target=``"variants"``; the stub must NOT
        # pick that as the plugin name — it would fail registry lookup at
        # replay time. Prefer any non-variants target instead.
        reports = {
            "layer3_variants": _variants_report(target_commit="fastapi-0.141.1"),
            "layer1_repetition": _tiny_report(target_commit="fastapi-0.141.1"),
        }
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        stub = (out / "reproduce.py").read_text(encoding="utf-8")
        assert "'fastapi'" in stub
        assert "'variants'" not in stub

    def test_reproduce_stub_parses_as_python(self, tmp_path: Path) -> None:
        # Bite-test: the stub was silently broken before this fix (called
        # run_discovery(target_commit=...) with an outdated signature). A
        # syntactically-invalid stub would blow up here. Rule 9: parse-then-
        # trust beats read-then-hope.
        import ast

        reports = {"layer1_repetition": _tiny_report(target_commit="fastapi-0.141.1")}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        stub = (out / "reproduce.py").read_text(encoding="utf-8")
        ast.parse(stub)  # raises SyntaxError on invalid Python

    def test_reproduce_stub_actually_runs_end_to_end(self, tmp_path: Path) -> None:
        # THE bite-test: invoke the packaged stub in a subprocess. This is
        # what the shipped chunk-2c reports could not do — their stubs called
        # run_discovery(target_commit=...) without a required plugin kwarg
        # and died with TypeError. Any future signature drift will surface
        # here rather than in a grader's environment.
        import subprocess

        reports = {"layer1_repetition": _tiny_report(target_commit="fastapi-0.141.1")}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        # Use the interpreter running this test so the stub sees our sys.path.
        # ``sys.executable`` + a test-generated path — no user input, no shell.
        result = subprocess.run(  # noqa: S603 — invocation is fully test-controlled
            [sys.executable, str(out / "reproduce.py")],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode == 0, (
            f"reproduce.py failed (rc={result.returncode}):\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        # It should have written a fresh replay/ directory.
        assert (out / "replay" / "report.json").is_file()
        assert (out / "replay" / "reproduce.py").is_file()
        assert (out / "replay" / "summary.txt").is_file()

    def test_summary_contains_layer_headers(self, tmp_path: Path) -> None:
        reports = {"layer1_repetition": _tiny_report()}
        out = package_eval_task(reports=reports, out_dir=tmp_path / "eval")
        summary = (out / "summary.txt").read_text(encoding="utf-8")
        assert "=== layer1_repetition ===" in summary

    def test_empty_reports_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            package_eval_task(reports={}, out_dir=tmp_path / "eval")
