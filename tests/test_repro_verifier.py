"""Tests for :mod:`core.repro_verifier`.

We do NOT actually create venvs or install packages in the test suite —
that would make CI depend on the network. The tests cover:

* Precondition failures (missing candidate, missing reproduce.sh,
  missing/invalid affidavit, unsafe version/package names).
* Report shape + summary line + JSON byte-stability.

Full end-to-end venv-install-and-run is exercised by the manual smoke
script (`scripts/nightly-verify-repro.sh` invoked against an existing
candidate). Same tier as `smoke_difficulty_check.sh`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.affidavit import AFFIDAVIT_FILENAME, AFFIDAVIT_SCHEMA_VERSION
from core.repro_verifier import (
    REPRO_VERIFICATION_SCHEMA_VERSION,
    ReproVerificationReport,
    ReproVerifierError,
    _is_safe_pkg_name,
    _is_safe_version,
    run_repro_verification,
)

_GOOD_SHA = "a" * 40


def _asciicast(commands: list[str]) -> str:
    header = json.dumps({"version": 2, "width": 80, "height": 24})
    events = [json.dumps([float(i), "o", cmd + "\n"]) for i, cmd in enumerate(commands)]
    return "\n".join([header, *events]) + "\n"


def _make_candidate(tmp_path: Path, *, with_reproduce: bool = True) -> Path:
    """Build a candidate dir with a valid affidavit and reproduce.sh."""
    cand = tmp_path / "cand"
    cand.mkdir()
    transcript = cand / "bench.cast"
    transcript.write_text(
        _asciicast([f"$ git checkout {_GOOD_SHA}", "$ pip install -e .", "$ python repro.py"]),
        encoding="utf-8",
    )
    doc: dict[str, Any] = {
        "schema_version": AFFIDAVIT_SCHEMA_VERSION,
        "pinned_commit": _GOOD_SHA,
        "repo_url": "https://github.com/example/project",
        "upstream_issue_url": "https://github.com/example/project/issues/42",
        "bench_transcript_path": "bench.cast",
        "observed_behaviour": (
            "At the pinned commit, running the repro raises. The failure reproduces "
            "on every run; nothing masks it."
        ),
        "divergence_from_thread": "None observed.",
        "upstream_status": "open",
        "signed_by": "Jeff",
        "signed_at": "2026-08-06T14:32:00Z",
    }
    (cand / AFFIDAVIT_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
    if with_reproduce:
        (cand / "reproduce.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    return cand


# ---------------------------------------------------------------------------
# Precondition failures.
# ---------------------------------------------------------------------------
class TestPreconditions:
    def test_missing_candidate_dir(self, tmp_path: Path) -> None:
        with pytest.raises(ReproVerifierError, match="does not exist"):
            run_repro_verification(tmp_path / "does-not-exist", pinned_package="anyio")

    def test_missing_reproduce_sh(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path, with_reproduce=False)
        with pytest.raises(ReproVerifierError, match="reproduce.sh"):
            run_repro_verification(cand, pinned_package="anyio")

    def test_missing_affidavit(self, tmp_path: Path) -> None:
        cand = tmp_path / "cand"
        cand.mkdir()
        (cand / "reproduce.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        with pytest.raises(ReproVerifierError, match=r"(missing|affidavit)"):
            run_repro_verification(cand, pinned_package="anyio")

    def test_unsafe_version_rejected(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path)
        with pytest.raises(ReproVerifierError, match="unsafe version"):
            run_repro_verification(cand, pinned_package="anyio", pinned_version="1.0; rm -rf /")

    def test_unsafe_package_rejected(self, tmp_path: Path) -> None:
        cand = _make_candidate(tmp_path)
        with pytest.raises(ReproVerifierError, match="unsafe package"):
            run_repro_verification(cand, pinned_package="anyio && evil", pinned_version="1.0")


# ---------------------------------------------------------------------------
# Report shape + JSON byte-stability.
# ---------------------------------------------------------------------------
class TestReportShape:
    def test_summary_line_still_reproducible(self) -> None:
        report = ReproVerificationReport(
            schema_version=REPRO_VERIFICATION_SCHEMA_VERSION,
            candidate_dir="/tmp/cand",  # noqa: S108
            pinned_package="anyio",
            pinned_version="4.14.2",
            install_returncode=0,
            reproduce_returncode=1,
            still_reproducible=True,
            stderr_head="",
        )
        assert "STILL REPRODUCIBLE" in report.summary_line()
        assert "anyio==4.14.2" in report.summary_line()

    def test_summary_line_no_longer_reproducible(self) -> None:
        report = ReproVerificationReport(
            schema_version=REPRO_VERIFICATION_SCHEMA_VERSION,
            candidate_dir="/tmp/cand",  # noqa: S108
            pinned_package="anyio",
            pinned_version="4.14.2",
            install_returncode=0,
            reproduce_returncode=0,
            still_reproducible=False,
            stderr_head="",
        )
        assert "NO LONGER REPRODUCIBLE" in report.summary_line()

    def test_to_json_sort_keyed(self) -> None:
        report = ReproVerificationReport(
            schema_version=REPRO_VERIFICATION_SCHEMA_VERSION,
            candidate_dir="/tmp/cand",  # noqa: S108
            pinned_package="anyio",
            pinned_version="4.14.2",
            install_returncode=0,
            reproduce_returncode=1,
            still_reproducible=True,
            stderr_head="",
        )
        parsed = json.loads(report.to_json())
        assert parsed["schema_version"] == REPRO_VERIFICATION_SCHEMA_VERSION
        assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# Validators.
# ---------------------------------------------------------------------------
class TestValidators:
    def test_safe_version_accepts_pep440(self) -> None:
        for v in ("1.0", "1.0.0", "1.0.0a1", "0.141.1", "1.0+local.foo"):
            assert _is_safe_version(v), v

    def test_safe_version_rejects_shell_meta(self) -> None:
        for v in ("", "1.0; rm -rf /", "1.0 && evil", "1.0`whoami`", "1.0$foo"):
            assert not _is_safe_version(v), v

    def test_safe_pkg_name_accepts_pep508(self) -> None:
        for n in ("anyio", "anyio-4", "sqlalchemy", "package.core"):
            assert _is_safe_pkg_name(n), n

    def test_safe_pkg_name_rejects_shell_meta(self) -> None:
        for n in ("", "anyio && evil", "anyio;drop", "anyio/../etc"):
            assert not _is_safe_pkg_name(n), n


# ---------------------------------------------------------------------------
# CLI wiring — precondition path only. Full end-to-end deferred to smoke.
# ---------------------------------------------------------------------------
class TestCli:
    def test_missing_candidate_exits_precondition(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cli.main import EXIT_REPRO_VERIFIER_PRECONDITION, main

        rc = main(
            [
                "verify-repro",
                str(tmp_path / "does-not-exist"),
                "--pinned-package",
                "anyio",
            ]
        )
        assert rc == EXIT_REPRO_VERIFIER_PRECONDITION
        assert "does not exist" in capsys.readouterr().err
