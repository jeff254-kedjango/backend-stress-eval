"""Tests for the SHELVED fastapi-0.141.1-lifecycle-leak grader.

The eval task under ``eval-tasks/_shelved/fastapi-0.141.1-lifecycle-leak/``
was retired 2026-08-02 after Chunk 6b (see the directory's SHELVED.md
and attribution.md). The grader must fail loudly and refuse to grade
whenever it detects it lives under a ``_shelved`` path component —
otherwise a downstream caller could mistake its output for a
submission-quality PASS/FAIL.

Rule 5 (fail-loud) applied to the shelve boundary itself. These tests
replace the earlier 9 G1-G4 gate tests, which are dead code now that
the grader is retired (Rule 4 — no dead code kept "just in case").
The gate logic is preserved intact inside ``grade.py`` and remains
readable as a reference for the next eval task's grader.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_SHELVED_GRADER = (
    _REPO_ROOT / "eval-tasks" / "_shelved" / "fastapi-0.141.1-lifecycle-leak" / "grade.py"
)
_SHELVED_MD = _SHELVED_GRADER.parent / "SHELVED.md"
_ATTRIBUTION_MD = _SHELVED_GRADER.parent / "attribution.md"
_BASELINE = _SHELVED_GRADER.parent / "baseline-report.json"

_EXIT_SHELVED = 3


class TestShelveBoundary:
    """The shelved grader must refuse to run and must exit with code 3."""

    def test_grader_exists_at_shelved_path(self) -> None:
        assert (
            _SHELVED_GRADER.is_file()
        ), f"shelved grader missing at {_SHELVED_GRADER} — did the move revert?"

    def test_shelved_md_documents_reason(self) -> None:
        assert (
            _SHELVED_MD.is_file()
        ), f"SHELVED.md missing at {_SHELVED_MD} — the shelve is not self-documenting"
        text = _SHELVED_MD.read_text(encoding="utf-8")
        assert "#16049" in text, "SHELVED.md must cite the PR that killed novelty"
        assert "novelty" in text.lower(), "SHELVED.md must name the reason (novelty)"

    def test_attribution_md_preserved_alongside(self) -> None:
        # The audit trail (attribution.md, baseline-report.json) must move
        # with the eval task; verify at least the two most-cited artefacts.
        assert _ATTRIBUTION_MD.is_file(), "attribution.md must survive shelving"
        assert _BASELINE.is_file(), "baseline-report.json must survive shelving"

    def test_invocation_returns_exit_3_and_error_message(self) -> None:
        # No args at all — should still hit the shelve check first, before
        # the argv-length check. That's the whole point of the fail-loud
        # placement inside main().
        r = subprocess.run(  # noqa: S603 — invocation is fully test-controlled
            [sys.executable, str(_SHELVED_GRADER)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert r.returncode == _EXIT_SHELVED, (
            f"expected exit {_EXIT_SHELVED} (shelved), got {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "shelved" in r.stderr.lower()
        assert "SHELVED.md" in r.stderr

    def test_invocation_with_full_args_still_refuses(self, tmp_path: Path) -> None:
        # Even a syntactically-valid invocation must be refused. The shelve
        # boundary is unconditional — otherwise a caller could bypass it by
        # supplying the right arg shape.
        fake_replay = tmp_path / "replay.json"
        fake_replay.write_text('{"layers": {}}', encoding="utf-8")
        r = subprocess.run(  # noqa: S603 — invocation is fully test-controlled
            [sys.executable, str(_SHELVED_GRADER), str(_BASELINE), str(fake_replay)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert r.returncode == _EXIT_SHELVED
