"""Tests for the anyio-lifecycle-leak eval-task grader.

The grader (``eval-tasks/anyio-lifecycle-leak/grade.py``) is the
objective PASS/FAIL layer of the shipped eval task. If the grader is
wrong, the whole grading contract is wrong — so we test each of the
four gates (G1-G4) end-to-end via subprocess against synthetic
JSON blobs.

Planted fixtures only. We never rely on a live ``measure.py`` here;
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
_TASK_DIR = _REPO_ROOT / "eval-tasks" / "anyio-lifecycle-leak"
_GRADER = _TASK_DIR / "grade.py"
_BASELINE = _TASK_DIR / "baseline-attribution.json"


def _run(baseline: Path, replay: Path) -> subprocess.CompletedProcess[str]:
    # sys.executable + hardcoded test-controlled paths. No shell, no user input.
    return subprocess.run(  # noqa: S603 — invocation is fully test-controlled
        [sys.executable, str(_GRADER), str(baseline), str(replay)],
        check=False,
        capture_output=True,
        text=True,
    )


def _line(file: str, lineno: int, kb: float, count: int) -> dict[str, object]:
    return {"file": file, "lineno": lineno, "kb_diff": kb, "count_diff": count}


_CLEAN_TOP_LINES: list[dict[str, object]] = [
    _line("lib/python3.12/tracemalloc.py", 558, 10.3, 120),
    _line("lib/python3.12/typing.py", 2137, 5.1, 240),
    _line("lib/python3.12/asyncio/base_events.py", 421, 3.9, 33),
    _line("lib/python3.12/weakref.py", 295, 2.1, 34),
    _line("lib/python3.12/selectors.py", 468, 1.8, 32),
]


def _make_replay(
    *,
    slope_kb_per_iter: float = 0.15,
    total_delta_kb: float = 73.5,
    top_lines: list[dict[str, object]] | None = None,
    anyio_version: str = "4.14.2",
    python_version: str = "3.12.13",
    span_iters: int = 489,
) -> dict[str, object]:
    """Synthesise a report-shaped dict matching schema_version 1."""
    if top_lines is None:
        top_lines = list(_CLEAN_TOP_LINES)
    return {
        "schema_version": "1",
        "anyio_version": anyio_version,
        "python_version": python_version,
        "rounds": 500,
        "warmup_iter": 10,
        "span_iters": span_iters,
        "slope_kb_per_iter": slope_kb_per_iter,
        "total_delta_kb": total_delta_kb,
        "top_lines": top_lines,
    }


def _write(tmp_path: Path, replay: dict[str, object]) -> Path:
    p = tmp_path / "replay.json"
    p.write_text(json.dumps(replay), encoding="utf-8")
    return p


@pytest.mark.skipif(
    not _BASELINE.is_file(),
    reason="baseline-attribution.json not shipped (eval-task removed?)",
)
class TestGraderPass:
    """A well-formed post-fix replay must PASS all four gates."""

    def test_clean_replay_passes_all_gates(self, tmp_path: Path) -> None:
        replay = _make_replay()
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "OVERALL: PASS" in r.stdout
        assert r.stdout.count("PASS") >= 4  # 4 gates + overall


@pytest.mark.skipif(
    not _BASELINE.is_file(),
    reason="baseline-attribution.json not shipped (eval-task removed?)",
)
class TestGraderFail:
    """Each gate must fire independently on its specific failure shape."""

    def test_g1_fires_on_slope_over_limit(self, tmp_path: Path) -> None:
        replay = _make_replay(slope_kb_per_iter=5.21)
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G1  slope invariant clears" in r.stdout
        assert "FAIL  slope 5.2100 KB/iter > 1.0" in r.stdout

    def test_g1_passes_at_exactly_the_limit(self, tmp_path: Path) -> None:
        replay = _make_replay(slope_kb_per_iter=1.0)
        r = _run(_BASELINE, _write(tmp_path, replay))
        # The check uses <=, so 1.0 is PASS.
        assert "G1  slope invariant clears" in r.stdout
        # But other gates may still fail if we didn't set totals — check row
        assert "PASS" in r.stdout.split("G1")[1].split("\n")[0]

    def test_g2_fires_on_total_delta_over_limit(self, tmp_path: Path) -> None:
        replay = _make_replay(total_delta_kb=600.0)
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G2  total delta bounded" in r.stdout
        assert "FAIL  total 600.00 KB > 500.0" in r.stdout

    def test_g3_fires_on_blacklisted_line_in_top_5(self, tmp_path: Path) -> None:
        # Insert a blacklisted line in position 3 of top-5.
        offenders = list(_CLEAN_TOP_LINES)
        offenders[2] = _line("anyio/_backends/_asyncio.py", 2598, 4.0, 5)
        replay = _make_replay(top_lines=offenders)
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G3  no blacklisted anyio backend line" in r.stdout
        assert "anyio/_backends/_asyncio.py:2598" in r.stdout

    def test_g3_ignores_blacklisted_line_deeper_than_top_5(self, tmp_path: Path) -> None:
        # Blacklisted line at position 6 → not inspected → G3 passes.
        deeper = [
            *_CLEAN_TOP_LINES,
            _line("anyio/_backends/_asyncio.py", 2598, 1.0, 2),
        ]
        replay = _make_replay(top_lines=deeper)
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 0
        assert "G3  no blacklisted anyio backend line" in r.stdout
        g3_row = next(ln for ln in r.stdout.splitlines() if "G3" in ln)
        assert "PASS" in g3_row

    def test_g4_fires_on_anyio_version_mismatch(self, tmp_path: Path) -> None:
        replay = _make_replay(anyio_version="99.99.99")
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "G4  environment matches baseline" in r.stdout
        assert "anyio_version mismatch" in r.stdout

    def test_g4_fires_on_python_version_mismatch(self, tmp_path: Path) -> None:
        replay = _make_replay(python_version="3.11.9")
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "python_version mismatch" in r.stdout

    def test_g4_fires_on_truncated_run(self, tmp_path: Path) -> None:
        replay = _make_replay(span_iters=100)  # baseline is 489
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 1
        assert "span_iters shorter than baseline" in r.stdout


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

    def test_wrong_schema_version_exits_setup(self, tmp_path: Path) -> None:
        replay = _make_replay()
        replay["schema_version"] = "2"
        r = _run(_BASELINE, _write(tmp_path, replay))
        assert r.returncode == 2
        assert "schema_version" in r.stderr

    def test_missing_args_exits_setup(self) -> None:
        r = subprocess.run(  # noqa: S603 — invocation is fully test-controlled
            [sys.executable, str(_GRADER)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2
        assert "usage:" in r.stderr
