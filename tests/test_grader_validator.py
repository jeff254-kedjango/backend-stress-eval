"""Tests for :mod:`core.grader_validator`.

Uses a fake grade.py script (bash+python one-liner is enough) that
reads a mapping from replay-path → exit-code out of an env var, so
tests can dial in per-invocation outcomes deterministically.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from core.grader_validator import (
    GRADER_VALIDATION_FILENAME,
    GRADER_VALIDATION_MIN_MUTATIONS,
    GRADER_VALIDATION_SCHEMA_VERSION,
    GraderInvocation,
    GraderValidationReport,
    GraderValidatorError,
    run_grader_validation,
)

# ---------------------------------------------------------------------------
# Fake grader — small python script that reads BSE_TEST_GRADER_MAP
# (JSON: replay-name -> exit_code) from the environment and exits
# accordingly. Lets tests script per-invocation behaviour without
# writing a real bug + fix.
# ---------------------------------------------------------------------------
_FAKE_GRADER = """\
#!/usr/bin/env python3
import json, os, sys
raw = os.environ.get("BSE_TEST_GRADER_MAP", "{}")
mapping = json.loads(raw)
replay = os.path.basename(sys.argv[2])
sys.exit(int(mapping.get(replay, 0)))
"""


def _make_candidate(
    tmp_path: Path,
    *,
    grader_body: str = _FAKE_GRADER,
    baseline_content: str = '{"schema_version": "1", "kind": "baseline"}',
    canonical_content: str = '{"schema_version": "1", "kind": "canonical"}',
    mutation_names: tuple[str, ...] = ("mut-1.json", "mut-2.json", "mut-3.json"),
) -> Path:
    """Build a minimal candidate dir under tmp_path."""
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "grade.py").write_text(grader_body, encoding="utf-8")
    (cand / "grade.py").chmod(
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IROTH
    )
    baseline_name = "baseline.json"
    canonical_name = "canonical.json"
    (cand / baseline_name).write_text(baseline_content, encoding="utf-8")
    (cand / canonical_name).write_text(canonical_content, encoding="utf-8")
    # Mutation reports live under validation/ to exercise the path resolver.
    val_dir = cand / "validation"
    val_dir.mkdir()
    mutation_paths: list[str] = []
    for name in mutation_names:
        p = val_dir / name
        p.write_text(json.dumps({"kind": name}), encoding="utf-8")
        mutation_paths.append(f"validation/{name}")

    manifest = {
        "schema_version": GRADER_VALIDATION_SCHEMA_VERSION,
        "baseline_report": baseline_name,
        "canonical_fix_report": canonical_name,
        "mutated_buggy_reports": mutation_paths,
    }
    (cand / GRADER_VALIDATION_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return cand


def _run(cand: Path, exit_map: dict[str, int]) -> GraderValidationReport:
    """Run the validator with the fake grader's outcomes configured."""
    old = os.environ.get("BSE_TEST_GRADER_MAP")
    os.environ["BSE_TEST_GRADER_MAP"] = json.dumps(exit_map)
    try:
        return run_grader_validation(cand)
    finally:
        if old is None:
            os.environ.pop("BSE_TEST_GRADER_MAP", None)
        else:
            os.environ["BSE_TEST_GRADER_MAP"] = old


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_all_invocations_match_expected_passes(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path)
        report = _run(
            cand,
            {
                "baseline.json": 1,  # baseline FAILs — good
                "canonical.json": 0,  # canonical PASSes — good
                "mut-1.json": 1,
                "mut-2.json": 1,
                "mut-3.json": 1,
            },
        )
        assert report.passed
        assert len(report.invocations) == 5  # baseline + canonical + 3 mutations
        summary = report.summary_line()
        assert "5/5" in summary

    def test_report_shape_is_byte_stable(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path)
        report = _run(
            cand,
            {
                "baseline.json": 1,
                "canonical.json": 0,
                "mut-1.json": 1,
                "mut-2.json": 1,
                "mut-3.json": 1,
            },
        )
        payload = report.to_json()
        parsed = json.loads(payload)
        assert parsed["schema_version"] == GRADER_VALIDATION_SCHEMA_VERSION
        assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# Rejection paths — grader over-specified vs under-specified.
# ---------------------------------------------------------------------------
class TestRejection:
    def test_grader_passes_baseline_is_rejected(self, tmp_path: Path) -> None:
        """Grader that PASSes an unfixed baseline is under-specified."""
        cand = _make_candidate(tmp_path)
        report = _run(
            cand,
            {
                "baseline.json": 0,  # BAD — baseline should FAIL
                "canonical.json": 0,
                "mut-1.json": 1,
                "mut-2.json": 1,
                "mut-3.json": 1,
            },
        )
        assert not report.passed
        bad = [i for i in report.invocations if not i.matched_expected]
        assert len(bad) == 1
        assert bad[0].label == "baseline"

    def test_grader_fails_canonical_fix_is_rejected(self, tmp_path: Path) -> None:
        """Grader that FAILs the canonical fix is over-specified."""
        cand = _make_candidate(tmp_path)
        report = _run(
            cand,
            {
                "baseline.json": 1,
                "canonical.json": 1,  # BAD — canonical fix should PASS
                "mut-1.json": 1,
                "mut-2.json": 1,
                "mut-3.json": 1,
            },
        )
        assert not report.passed

    def test_grader_passes_mutation_is_rejected(self, tmp_path: Path) -> None:
        """Grader that PASSes a mutated buggy variant is keying on shape."""
        cand = _make_candidate(tmp_path)
        report = _run(
            cand,
            {
                "baseline.json": 1,
                "canonical.json": 0,
                "mut-1.json": 1,
                "mut-2.json": 0,  # BAD — mutation should FAIL
                "mut-3.json": 1,
            },
        )
        assert not report.passed


# ---------------------------------------------------------------------------
# Precondition failures.
# ---------------------------------------------------------------------------
class TestPreconditions:
    def test_missing_candidate_dir(self, tmp_path: Path) -> None:
        with pytest.raises(GraderValidatorError, match="does not exist"):
            run_grader_validation(tmp_path / "does-not-exist")

    def test_missing_grade_py(self, tmp_path: Path) -> None:
        cand = tmp_path / "no-grader"
        cand.mkdir()
        (cand / GRADER_VALIDATION_FILENAME).write_text("{}", encoding="utf-8")
        with pytest.raises(GraderValidatorError, match="grade.py"):
            run_grader_validation(cand)

    def test_missing_manifest(self, tmp_path: Path) -> None:
        cand = tmp_path / "no-manifest"
        cand.mkdir()
        (cand / "grade.py").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with pytest.raises(GraderValidatorError, match="grader-validation.json"):
            run_grader_validation(cand)

    def test_bad_manifest_schema(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path)
        (cand / GRADER_VALIDATION_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": "99",
                    "baseline_report": "baseline.json",
                    "canonical_fix_report": "canonical.json",
                    "mutated_buggy_reports": [
                        "validation/mut-1.json",
                        "validation/mut-2.json",
                        "validation/mut-3.json",
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GraderValidatorError, match="schema_version"):
            run_grader_validation(cand)

    def test_too_few_mutations(self, tmp_path: Path) -> None:
        """Below GRADER_VALIDATION_MIN_MUTATIONS the false-positive suite is too small."""
        assert GRADER_VALIDATION_MIN_MUTATIONS == 3  # regression pin
        cand = _make_candidate(tmp_path, mutation_names=("mut-1.json",))
        with pytest.raises(GraderValidatorError, match="minimum is 3"):
            run_grader_validation(cand)

    def test_referenced_report_missing(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path)
        (cand / "baseline.json").unlink()
        with pytest.raises(GraderValidatorError, match="does not exist"):
            run_grader_validation(cand)

    def test_path_escape_rejected(self, tmp_path: Path) -> None:
        """A manifest that names ../../etc/passwd must be refused."""
        cand = _make_candidate(tmp_path)
        (cand / GRADER_VALIDATION_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": GRADER_VALIDATION_SCHEMA_VERSION,
                    "baseline_report": "../../etc/passwd",
                    "canonical_fix_report": "canonical.json",
                    "mutated_buggy_reports": [
                        "validation/mut-1.json",
                        "validation/mut-2.json",
                        "validation/mut-3.json",
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GraderValidatorError, match="escapes candidate"):
            run_grader_validation(cand)


# ---------------------------------------------------------------------------
# CLI wiring.
# ---------------------------------------------------------------------------
class TestCli:
    def test_reject_exits_reject_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cli.main import EXIT_GRADER_VALIDATION_REJECT, main

        cand = _make_candidate(tmp_path)
        os.environ["BSE_TEST_GRADER_MAP"] = json.dumps(
            {
                "baseline.json": 0,  # grader under-specified
                "canonical.json": 0,
                "mut-1.json": 1,
                "mut-2.json": 1,
                "mut-3.json": 1,
            }
        )
        try:
            rc = main(["validate-grader", str(cand)])
        finally:
            os.environ.pop("BSE_TEST_GRADER_MAP", None)
        assert rc == EXIT_GRADER_VALIDATION_REJECT
        out = capsys.readouterr().out
        assert "MISMATCH" in out

    def test_missing_candidate_exits_precondition(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cli.main import EXIT_GRADER_VALIDATION_PRECONDITION, main

        rc = main(["validate-grader", str(tmp_path / "does-not-exist")])
        assert rc == EXIT_GRADER_VALIDATION_PRECONDITION
        assert "does not exist" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# GraderInvocation is hashable + immutable (frozen dataclass).
# ---------------------------------------------------------------------------
def test_grader_invocation_is_frozen() -> None:
    inv = GraderInvocation(
        label="x",
        report_path="y",
        expected="pass",
        exit_code=0,
        matched_expected=True,
        stderr_head="",
    )
    with pytest.raises(Exception):  # noqa: B017 -- FrozenInstanceError or AttributeError
        inv.label = "z"  # type: ignore[misc]
