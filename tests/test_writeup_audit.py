"""Tests for :mod:`core.writeup_audit` and the ``bse writeup-audit`` verb.

Live GitHub API fetches are NOT tested here — they would introduce network
flakiness and rate-limit hazards. Instead every test uses
``fetch_live=False`` plus a committed ``upstream-issue-snapshot.txt`` under
the candidate directory, exercising every code path except the raw HTTP
plumbing. The HTTP path is thin urllib usage; if it ever grows non-trivial
logic, add integration tests behind an env-gated marker.

The affidavit fixture is v2 (adds ``upstream_issue_url``); v1 affidavits
are auto-rejected by the schema-version validator.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from cli.main import (
    EXIT_OK,
    EXIT_USAGE,
    EXIT_WRITEUP_PRECONDITION,
    EXIT_WRITEUP_REJECT,
    main,
)
from core.affidavit import AFFIDAVIT_FILENAME, AFFIDAVIT_SCHEMA_VERSION
from core.writeup_audit import (
    AUDIT_REPORT_FILENAME,
    SNAPSHOT_FILENAME,
    AuditFinding,
    WriteupAuditError,
    run_writeup_audit,
)

_GOOD_SHA = "0" * 40


def _seed_candidate(
    candidate_dir: Path,
    *,
    initial_prompt: str = "This is a fine original prompt.\n",
    readme: str | None = None,
    snapshot: str | None = None,
    include_affidavit: bool = True,
) -> Path:
    """Assemble a candidate directory suitable for writeup-audit tests.

    The affidavit is a v2, structurally-valid fixture with an
    upstream_issue_url pointing to a fictional issue. We do not need the
    affidavit to pass every semantic check for the audit to run —
    load_affidavit only enforces structural correctness.
    """
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "initial-prompt.md").write_text(initial_prompt, encoding="utf-8")
    if readme is not None:
        (candidate_dir / "README.md").write_text(readme, encoding="utf-8")
    if snapshot is not None:
        (candidate_dir / SNAPSHOT_FILENAME).write_text(snapshot, encoding="utf-8")

    if include_affidavit:
        doc: dict[str, Any] = {
            "schema_version": AFFIDAVIT_SCHEMA_VERSION,
            "pinned_commit": _GOOD_SHA,
            "repo_url": "https://github.com/example/project",
            "upstream_issue_url": "https://github.com/example/project/issues/42",
            "bench_transcript_path": "bench.cast",
            "observed_behaviour": "x" * 100,
            "divergence_from_thread": "",
            "upstream_status": "open",
            "signed_by": "Jeff",
            "signed_at": "2026-08-06T14:32:00Z",
        }
        (candidate_dir / AFFIDAVIT_FILENAME).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return candidate_dir


# ---------------------------------------------------------------------------
# Preconditions.
# ---------------------------------------------------------------------------
class TestPreconditions:
    def test_no_affidavit_raises(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path, include_affidavit=False, snapshot="anything")
        with pytest.raises(WriteupAuditError, match="affidavit prerequisite failed"):
            run_writeup_audit(tmp_path, write_report=False, fetch_live=False)

    def test_no_writeup_files_raises(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path, snapshot="anything")
        (tmp_path / "initial-prompt.md").unlink()
        with pytest.raises(WriteupAuditError, match="no writeup files"):
            run_writeup_audit(tmp_path, write_report=False, fetch_live=False)

    def test_no_snapshot_and_fetch_disabled_raises(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path)
        with pytest.raises(WriteupAuditError, match="fetch_live=False and no"):
            run_writeup_audit(tmp_path, write_report=False, fetch_live=False)


# ---------------------------------------------------------------------------
# Matching semantics.
# ---------------------------------------------------------------------------
class TestMatching:
    def test_clean_writeup_passes(self, tmp_path: Path) -> None:
        _seed_candidate(
            tmp_path,
            initial_prompt="A completely original prompt that shares nothing with upstream.\n",
            snapshot="Upstream text has entirely different phrasing from the writeup body.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert report.passed
        assert report.findings == ()

    def test_verbatim_eight_word_phrase_flagged(self, tmp_path: Path) -> None:
        shared = "the maintenance loop does not requeue the held job"
        _seed_candidate(
            tmp_path,
            initial_prompt=f"On our bench {shared} at all.\n",
            snapshot=f"Upstream reports that {shared} in some cases.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert not report.passed
        assert any(shared in f.phrase for f in report.findings)

    def test_seven_word_overlap_is_not_flagged(self, tmp_path: Path) -> None:
        # Below the 8-word threshold — deliberate. Prevents flagging
        # generic English idioms.
        shared_seven = "the loop does not requeue the job"
        _seed_candidate(
            tmp_path,
            initial_prompt=f"Bench observation: {shared_seven}.\n",
            snapshot=f"Thread said {shared_seven} when idle.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert report.passed

    def test_case_and_whitespace_are_normalised(self, tmp_path: Path) -> None:
        shared = "the maintenance loop does not requeue the held job"
        _seed_candidate(
            tmp_path,
            initial_prompt=f"OUR BENCH: {shared.upper()}!\n",
            snapshot=f"upstream note - {shared}\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert not report.passed


# ---------------------------------------------------------------------------
# Own-words annotation exemption.
# ---------------------------------------------------------------------------
class TestOwnWordsAnnotation:
    def test_annotated_paragraph_is_exempt(self, tmp_path: Path) -> None:
        """A ≥ 8-word verbatim overlap that sits inside an annotated
        paragraph must not be flagged. This is the shared-technical-term
        escape hatch documented in Rule 13.
        """
        shared = "the maintenance loop does not requeue the held job"
        writeup = (
            "The function called dead_worker_maintenance handles this.\n"
            "\n"
            f"Description: {shared} in normal operation.\n"
            "<!-- own-words: dead_worker_maintenance is a public API name -->\n"
            "\n"
            "Elsewhere in the writeup, unrelated text.\n"
        )
        _seed_candidate(
            tmp_path,
            initial_prompt=writeup,
            snapshot=f"Upstream reports {shared} in every worker shutdown.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert (
            report.passed
        ), f"expected annotated paragraph to be exempt, got findings: {report.findings}"

    def test_unannotated_paragraph_still_flagged(self, tmp_path: Path) -> None:
        """The annotation must be inside the same paragraph. A stray marker
        in a different paragraph does NOT exempt matches elsewhere.
        """
        shared = "the maintenance loop does not requeue the held job"
        writeup = (
            "One paragraph with a stray marker.\n"
            "<!-- own-words: irrelevant term -->\n"
            "\n"
            f"Second paragraph {shared} without annotation.\n"
        )
        _seed_candidate(
            tmp_path,
            initial_prompt=writeup,
            snapshot=f"Upstream: {shared} on bench.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert not report.passed


# ---------------------------------------------------------------------------
# Multi-file audit.
# ---------------------------------------------------------------------------
class TestMultiFile:
    def test_readme_and_prompt_both_scanned(self, tmp_path: Path) -> None:
        shared = "the maintenance loop does not requeue the held job"
        _seed_candidate(
            tmp_path,
            initial_prompt="Clean prompt without shared phrasing.\n",
            readme=f"README: {shared} elsewhere in text.\n",
            snapshot=f"Thread: {shared} on bench.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert not report.passed
        assert any(f.writeup_file == "README.md" for f in report.findings)
        assert "initial-prompt.md" in report.files_scanned
        assert "README.md" in report.files_scanned


# ---------------------------------------------------------------------------
# Dedup — the sliding window produces N overlapping matches per real hit.
# ---------------------------------------------------------------------------
class TestDedup:
    def test_overlapping_matches_collapsed(self, tmp_path: Path) -> None:
        """A 12-word verbatim overlap produces (12 - 8 + 1) = 5 sliding-window
        matches. After dedup the report should show exactly one row.
        """
        shared = "the maintenance loop does not requeue the held job when idle worker crashes"
        _seed_candidate(
            tmp_path,
            initial_prompt=f"Observation: {shared} at all.\n",
            snapshot=f"Thread: {shared} on bench.\n",
        )
        report = run_writeup_audit(tmp_path, write_report=False, fetch_live=False)
        assert len(report.findings) == 1
        f = report.findings[0]
        # The extended finding covers all 12 words, not just the first 8.
        assert f.word_count >= 12


# ---------------------------------------------------------------------------
# Report and snapshot file writing.
# ---------------------------------------------------------------------------
class TestReportWriting:
    def test_writes_audit_txt(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path, snapshot="unrelated upstream text\n")
        run_writeup_audit(tmp_path, fetch_live=False)
        report_path = tmp_path / AUDIT_REPORT_FILENAME
        assert report_path.is_file()
        content = report_path.read_text()
        assert "writeup audit:" in content
        assert "PASS" in content

    def test_reject_report_lists_findings(self, tmp_path: Path) -> None:
        shared = "the maintenance loop does not requeue the held job"
        _seed_candidate(
            tmp_path,
            initial_prompt=f"Observation: {shared}.\n",
            snapshot=f"Thread: {shared}.\n",
        )
        run_writeup_audit(tmp_path, fetch_live=False)
        content = (tmp_path / AUDIT_REPORT_FILENAME).read_text()
        assert "REJECT" in content
        assert "findings: 1" in content

    def test_snapshot_only_does_not_overwrite_snapshot(self, tmp_path: Path) -> None:
        """When fetch_live=False, we do NOT touch the snapshot file — it is
        the authoritative source, not a cache to refresh.
        """
        original_snapshot = "original snapshot content preserved unchanged\n"
        _seed_candidate(tmp_path, snapshot=original_snapshot)
        run_writeup_audit(tmp_path, fetch_live=False)
        assert (tmp_path / SNAPSHOT_FILENAME).read_text() == original_snapshot


# ---------------------------------------------------------------------------
# AuditFinding is trivially serialisable.
# ---------------------------------------------------------------------------
class TestAuditFinding:
    def test_dataclass_frozen(self) -> None:
        f = AuditFinding(
            writeup_file="initial-prompt.md",
            phrase="the maintenance loop does not requeue the held job",
            word_count=9,
            starting_word_index=2,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.phrase = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CLI: `bse writeup-audit <dir>`.
# ---------------------------------------------------------------------------
class TestCli:
    def test_not_a_directory_exits_usage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "not-a-dir"
        f.write_text("x")
        rc = main(["writeup-audit", str(f)])
        assert rc == EXIT_USAGE
        assert "not a directory" in capsys.readouterr().err

    def test_missing_affidavit_maps_to_precondition_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_candidate(tmp_path, include_affidavit=False, snapshot="x")
        rc = main(["writeup-audit", str(tmp_path), "--snapshot-only"])
        assert rc == EXIT_WRITEUP_PRECONDITION
        assert "affidavit prerequisite failed" in capsys.readouterr().err

    def test_pass_maps_to_ok_exit(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed_candidate(
            tmp_path,
            snapshot="different words entirely from any writeup content here.\n",
        )
        rc = main(["writeup-audit", str(tmp_path), "--snapshot-only", "--no-report"])
        assert rc == EXIT_OK
        assert "PASS" in capsys.readouterr().out

    def test_reject_maps_to_reject_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shared = "the maintenance loop does not requeue the held job"
        _seed_candidate(
            tmp_path,
            initial_prompt=f"Bench notes: {shared}.\n",
            snapshot=f"Thread: {shared}.\n",
        )
        rc = main(["writeup-audit", str(tmp_path), "--snapshot-only", "--no-report"])
        assert rc == EXIT_WRITEUP_REJECT
        assert "REJECT" in capsys.readouterr().out
