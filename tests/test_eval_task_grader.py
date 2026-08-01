"""Tests for the eval-task grader script.

The grader (``eval-tasks/fastapi-0.141.1-lifecycle-leak/grade.py``) is the
objective PASS/FAIL layer of the shipped eval task. If the grader is wrong,
the whole grading contract is wrong — so we test each of the four gates
(G1-G4) end-to-end via subprocess against synthetic report JSON.

Planted fixtures only (Rule 9). We never rely on a live ``bse run`` here;
the grader is a pure function over two JSON blobs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_GRADER = _REPO_ROOT / "eval-tasks" / "fastapi-0.141.1-lifecycle-leak" / "grade.py"
_BASELINE = _REPO_ROOT / "eval-tasks" / "fastapi-0.141.1-lifecycle-leak" / "baseline-report.json"


def _run(baseline: Path, replay: Path) -> subprocess.CompletedProcess[str]:
    # sys.executable + hardcoded test-controlled paths. No shell, no user input.
    return subprocess.run(  # noqa: S603 — invocation is fully test-controlled
        [sys.executable, str(_GRADER), str(baseline), str(replay)],
        check=False,
        capture_output=True,
        text=True,
    )


def _make_replay(
    *,
    layer2_success: bool,
    layer2_violations: list[dict[str, object]],
    layer1_success: bool = True,
    layer4_success: bool = True,
    include_layer3: bool = True,
    layer3_success: bool = False,
) -> dict[str, object]:
    """Synthesise a discovery-report-shaped dict for the grader.

    Keeps the exact key structure the grader reads: ``layers.<name>.result``.
    """
    layers: dict[str, object] = {
        "layer1_repetition": {"result": {"success": layer1_success, "violations": []}},
        "layer2_lifecycle": {
            "result": {"success": layer2_success, "violations": layer2_violations}
        },
        "layer4_sequence": {"result": {"success": layer4_success, "violations": []}},
    }
    if include_layer3:
        layers["layer3_variants"] = {"result": {"success": layer3_success, "violations": []}}
    return {"discovery_schema_version": "1", "layers": layers}


def _write(tmp_path: Path, replay: dict[str, object]) -> Path:
    p = tmp_path / "replay.json"
    p.write_text(json.dumps(replay), encoding="utf-8")
    return p


@pytest.mark.skipif(
    not _BASELINE.is_file(),
    reason="baseline-report.json not shipped (eval-task directory removed?)",
)
class TestGraderPass:
    """A well-formed post-fix replay must PASS all four gates."""

    def test_clean_replay_passes_all_gates(self, tmp_path: Path) -> None:
        replay = _make_replay(
            layer2_success=True,
            layer2_violations=[],
            layer1_success=True,
            layer4_success=True,
        )
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "OVERALL: PASS" in r.stdout
        assert r.stdout.count("PASS") >= 4  # 4 gates + overall


@pytest.mark.skipif(
    not _BASELINE.is_file(),
    reason="baseline-report.json not shipped (eval-task directory removed?)",
)
class TestGraderFail:
    """Each gate must fire independently on its specific failure shape."""

    def test_g1_fires_on_slope_over_limit(self, tmp_path: Path) -> None:
        # Slope invariant present with slope_kb_per_iter > 1.0 → G1 FAIL.
        replay = _make_replay(
            layer2_success=False,
            layer2_violations=[
                {
                    "invariant_name": "rss_slope_bounded",
                    "evidence": {"slope_kb_per_iter": 8.9237},
                    "iteration": 499,
                }
            ],
        )
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G1  layer2 slope invariant clears" in r.stdout
        assert "FAIL  1 offending slope violation(s)" in r.stdout
        assert "OVERALL: FAIL" in r.stdout

    def test_g1_does_not_fire_on_slope_within_limit(self, tmp_path: Path) -> None:
        # Slope invariant PRESENT but slope_kb_per_iter <= 1.0 → G1 PASS.
        # (No violation is emitted for slope<=1.0 in practice, but if one
        # somehow appeared, the rubric explicitly gates on > 1.0.)
        replay = _make_replay(
            layer2_success=True,
            layer2_violations=[
                {
                    "invariant_name": "rss_slope_bounded",
                    "evidence": {"slope_kb_per_iter": 0.5},
                    "iteration": 499,
                }
            ],
        )
        r = _run(_BASELINE, _write(tmp_path, replay))
        # G1 passes; but G3 fails because success=True with a violation
        # is contradictory — real reports won't have this shape. Test
        # only that G1 is PASS in the row.
        assert "G1  layer2 slope invariant clears             PASS" in r.stdout

    def test_g2_fires_on_threshold_violation(self, tmp_path: Path) -> None:
        replay = _make_replay(
            layer2_success=False,
            layer2_violations=[
                {
                    "invariant_name": "rss_return_to_baseline",
                    "evidence": {"drift_kb": 1152},
                    "iteration": 55,
                }
            ],
        )
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G2  layer2 threshold invariant clears" in r.stdout
        assert "FAIL  1 threshold violation(s)" in r.stdout

    def test_g3_fires_on_success_false(self, tmp_path: Path) -> None:
        replay = _make_replay(
            layer2_success=False,
            layer2_violations=[],  # no violations, but success is False → G3 fires
        )
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G3  layer2 result.success is true" in r.stdout
        assert "FAIL" in r.stdout

    def test_g4_fires_when_previously_green_layer_regresses(self, tmp_path: Path) -> None:
        # Baseline has L1 as PASS. A replay where L1 is FAIL → G4 FAIL.
        replay = _make_replay(
            layer2_success=True,
            layer2_violations=[],
            layer1_success=False,  # <— regression
        )
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G4  no previously-green layer regressed" in r.stdout
        assert "regressed: layer1_repetition" in r.stdout


class TestGraderSetupErrors:
    """Setup / usage errors exit 2, not 1 — grader can't judge a broken input."""

    def test_missing_replay_file_exits_setup(self, tmp_path: Path) -> None:
        r = _run(_BASELINE, tmp_path / "does-not-exist.json")
        assert r.returncode == 2
        assert "not found" in r.stderr

    def test_malformed_replay_json_exits_setup(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        r = _run(_BASELINE, bad)
        assert r.returncode == 2
        assert "not valid JSON" in r.stderr

    def test_missing_args_exits_setup(self) -> None:
        # Rely on grader's own usage handling. Uses sys.executable, no shell.
        r = subprocess.run(  # noqa: S603 — invocation is fully test-controlled
            [sys.executable, str(_GRADER)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2
        assert "usage:" in r.stderr
