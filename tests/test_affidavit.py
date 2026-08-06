"""Tests for :mod:`core.affidavit` and the ``bse affidavit`` CLI verb.

Covers:

* Structural errors (missing file, bad JSON, wrong types, missing fields) —
  raised as :class:`AffidavitError` by :func:`load_affidavit`.
* Semantic errors (bad SHA, closed-fixed upstream, transcript missing pin,
  short observed_behaviour, bad timestamp, unsafe URL) — returned as
  :class:`ValidationFailure` records by :func:`validate_affidavit`.
* A full end-to-end happy path via the CLI (``bse affidavit <dir>``).

Fixtures build affidavit dirs on disk under ``tmp_path``; no network, no
subprocess, no wall clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.main import EXIT_AFFIDAVIT_INVALID, EXIT_OK, EXIT_USAGE, main
from core.affidavit import (
    AFFIDAVIT_FILENAME,
    AFFIDAVIT_SCHEMA_VERSION,
    AffidavitError,
    load_affidavit,
    validate_affidavit,
)

# A real full SHA (fastapi 0.141.1 tag commit shape — 40 lowercase hex chars).
_GOOD_SHA = "0" * 40
_ALT_SHA = "1" * 40


def _asciicast_v2(commands: list[str]) -> str:
    """Assemble a minimal asciinema v2 recording containing ``commands``.

    v2 spec: header dict on line 1 (with ``version: 2``); each subsequent
    line is a ``[t, "o", data]`` JSON tuple. We only need the parser to
    accept the header and to find substrings — no timing accuracy needed.
    """
    header = json.dumps({"version": 2, "width": 80, "height": 24})
    events = [json.dumps([float(i), "o", cmd + "\n"]) for i, cmd in enumerate(commands)]
    return "\n".join([header, *events]) + "\n"


def _write_affidavit(
    candidate_dir: Path,
    *,
    transcript_relpath: str = "bench.cast",
    transcript_body: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Path:
    """Write a valid affidavit + transcript, then apply any field overrides.

    Returns the candidate_dir for chaining. Overrides are applied AFTER a
    known-good baseline so tests express one violation at a time.
    """
    candidate_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = candidate_dir / transcript_relpath
    if transcript_body is None:
        transcript_body = _asciicast_v2(
            [
                f"$ git checkout {_GOOD_SHA}",
                "$ pip install -e .",
                "$ python repro.py",
                "AssertionError: expected 1 got 0",
            ]
        )
    transcript_path.write_text(transcript_body, encoding="utf-8")

    doc: dict[str, Any] = {
        "schema_version": AFFIDAVIT_SCHEMA_VERSION,
        "pinned_commit": _GOOD_SHA,
        "repo_url": "https://github.com/example/project",
        "upstream_issue_url": "https://github.com/example/project/issues/42",
        "bench_transcript_path": transcript_relpath,
        "observed_behaviour": (
            "At the pinned commit, running the repro script raised an "
            "AssertionError in the worker path. The failure reproduces on "
            "every run; the maintenance loop does not requeue the held job "
            "as the upstream thread describes."
        ),
        "divergence_from_thread": (
            "Thread claims silent job loss; on-bench I saw a raised assertion. "
            "Symptom shape differs."
        ),
        "upstream_status": "open",
        "signed_by": "Jeff",
        "signed_at": "2026-08-06T14:32:00Z",
    }
    if overrides:
        doc.update(overrides)
    (candidate_dir / AFFIDAVIT_FILENAME).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return candidate_dir


# ---------------------------------------------------------------------------
# load_affidavit — file-level failures.
# ---------------------------------------------------------------------------
class TestLoadAffidavit:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AffidavitError, match="no repro-affidavit.json"):
            load_affidavit(tmp_path)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / AFFIDAVIT_FILENAME).write_text("{ not json", encoding="utf-8")
        with pytest.raises(AffidavitError, match="not valid JSON"):
            load_affidavit(tmp_path)

    def test_json_array_raises(self, tmp_path: Path) -> None:
        (tmp_path / AFFIDAVIT_FILENAME).write_text("[]", encoding="utf-8")
        with pytest.raises(AffidavitError, match="must contain a JSON object"):
            load_affidavit(tmp_path)

    def test_missing_fields_raises(self, tmp_path: Path) -> None:
        (tmp_path / AFFIDAVIT_FILENAME).write_text(
            json.dumps({"schema_version": "1"}), encoding="utf-8"
        )
        with pytest.raises(AffidavitError, match="missing required field"):
            load_affidavit(tmp_path)

    def test_non_string_field_raises(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"signed_by": 42})
        with pytest.raises(AffidavitError, match="must be a JSON string"):
            load_affidavit(tmp_path)


# ---------------------------------------------------------------------------
# validate_affidavit — semantic checks. Each test asserts on the specific
# field so a change elsewhere doesn't collapse two tests into one.
# ---------------------------------------------------------------------------
class TestValidateHappyPath:
    def test_full_valid_affidavit_passes(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path)
        assert validate_affidavit(tmp_path) == []


class TestValidateSchemaVersion:
    def test_wrong_schema_version_flagged(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"schema_version": "99"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "schema_version" for f in failures)


class TestValidatePinnedCommit:
    def test_short_sha_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"pinned_commit": "abc1234"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "pinned_commit" for f in failures)

    def test_uppercase_sha_rejected(self, tmp_path: Path) -> None:
        # Full length, but uppercase — reject to keep the on-disk contract normalised.
        _write_affidavit(tmp_path, overrides={"pinned_commit": "A" * 40})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "pinned_commit" for f in failures)

    def test_tag_alias_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"pinned_commit": "v0.141.1"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "pinned_commit" for f in failures)


class TestValidateRepoUrl:
    def test_empty_url_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"repo_url": "   "})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "repo_url" for f in failures)

    def test_shell_metacharacter_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(
            tmp_path,
            overrides={"repo_url": "https://example.com/x;rm -rf /"},
        )
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "repo_url" for f in failures)


class TestValidateUpstreamIssueUrl:
    def test_empty_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"upstream_issue_url": "  "})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "upstream_issue_url" for f in failures)

    def test_non_issue_url_rejected(self, tmp_path: Path) -> None:
        # A repo root, not an issue.
        _write_affidavit(
            tmp_path,
            overrides={"upstream_issue_url": "https://github.com/example/project"},
        )
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "upstream_issue_url" for f in failures)

    def test_shell_metacharacter_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(
            tmp_path,
            overrides={"upstream_issue_url": "https://github.com/example/project/issues/1;rm"},
        )
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "upstream_issue_url" for f in failures)


class TestValidateObservedBehaviour:
    def test_too_short_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"observed_behaviour": "it broke."})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "observed_behaviour" for f in failures)

    def test_too_long_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"observed_behaviour": "x" * 5000})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "observed_behaviour" for f in failures)


class TestValidateUpstreamStatus:
    def test_open_accepted(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"upstream_status": "open"})
        assert validate_affidavit(tmp_path) == []

    def test_merged_pr_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"upstream_status": "merged-pr-431"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "upstream_status" for f in failures)

    def test_closed_fixed_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"upstream_status": "closed-fixed"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "upstream_status" for f in failures)

    def test_unknown_status_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"upstream_status": "maybe"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "upstream_status" for f in failures)


class TestValidateSignedBy:
    def test_empty_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"signed_by": "   "})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "signed_by" for f in failures)


class TestValidateSignedAt:
    def test_empty_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"signed_at": ""})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "signed_at" for f in failures)

    def test_not_iso8601_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, overrides={"signed_at": "yesterday"})
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "signed_at" for f in failures)


class TestValidateTranscript:
    def test_missing_transcript_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path)
        (tmp_path / "bench.cast").unlink()
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "bench_transcript_path" for f in failures)

    def test_empty_transcript_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, transcript_body="")
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "bench_transcript_path" for f in failures)

    def test_non_asciicast_rejected(self, tmp_path: Path) -> None:
        _write_affidavit(tmp_path, transcript_body="not json at all\n")
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "bench_transcript_path" for f in failures)

    def test_wrong_version_rejected(self, tmp_path: Path) -> None:
        header = json.dumps({"version": 1, "width": 80, "height": 24})
        _write_affidavit(tmp_path, transcript_body=header + "\n")
        failures = validate_affidavit(tmp_path)
        assert any(f.field == "bench_transcript_path" for f in failures)

    def test_transcript_without_pin_rejected(self, tmp_path: Path) -> None:
        # Valid v2 recording, but the pinned SHA is nowhere in the body.
        body = _asciicast_v2(
            [
                f"$ git checkout {_ALT_SHA}",  # different commit
                "$ python repro.py",
            ]
        )
        _write_affidavit(tmp_path, transcript_body=body)
        failures = validate_affidavit(tmp_path)
        assert any(
            f.field == "bench_transcript_path" and "does not appear" in f.detail for f in failures
        )

    def test_absolute_transcript_path_accepted(self, tmp_path: Path) -> None:
        # Author supplies an absolute path — should resolve without needing
        # candidate_dir as the base.
        transcript = tmp_path / "elsewhere" / "bench.cast"
        transcript.parent.mkdir()
        transcript.write_text(_asciicast_v2([f"git checkout {_GOOD_SHA}"]), encoding="utf-8")
        _write_affidavit(
            tmp_path,
            overrides={"bench_transcript_path": str(transcript)},
        )
        # Also remove the default local bench.cast so we know we hit the abs path.
        (tmp_path / "bench.cast").unlink()
        assert validate_affidavit(tmp_path) == []


# ---------------------------------------------------------------------------
# CLI: `bse affidavit <dir>` — end-to-end.
# ---------------------------------------------------------------------------
class TestCliAffidavit:
    def test_ok_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_affidavit(tmp_path)
        rc = main(["affidavit", str(tmp_path)])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "affidavit OK" in out

    def test_missing_file_exit_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["affidavit", str(tmp_path)])
        assert rc == EXIT_AFFIDAVIT_INVALID
        err = capsys.readouterr().err
        assert "no repro-affidavit.json" in err

    def test_semantic_failures_all_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Stack multiple failures so we can confirm they all surface, not
        # just the first one.
        _write_affidavit(
            tmp_path,
            overrides={
                "pinned_commit": "abc123",
                "upstream_status": "closed-fixed",
                "signed_by": "",
            },
        )
        rc = main(["affidavit", str(tmp_path)])
        assert rc == EXIT_AFFIDAVIT_INVALID
        err = capsys.readouterr().err
        assert "pinned_commit" in err
        assert "upstream_status" in err
        assert "signed_by" in err

    def test_not_a_directory_exits_usage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "not-a-dir"
        f.write_text("x")
        rc = main(["affidavit", str(f)])
        assert rc == EXIT_USAGE
        err = capsys.readouterr().err
        assert "not a directory" in err
