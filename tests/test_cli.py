"""Tests for the ``bse`` CLI (:mod:`cli.main`).

Rule 5: every code path returns an integer exit status. Rule 9: no wall-clock
timing, no network, no reliance on ``bse`` being on ``PATH`` — we call
:func:`cli.main.main` directly with argv lists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cli.main import (
    EXIT_ALREADY_EXISTS,
    EXIT_OK,
    EXIT_UNKNOWN_PLUGIN,
    EXIT_USAGE,
    _is_safe_version,
    main,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="CLI's run subcommand exercises Linux /proc metrics",
)


# ---------------------------------------------------------------------------
# `bse list`
# ---------------------------------------------------------------------------


class TestList:
    def test_list_returns_ok(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["list"])
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # Both bundled plugins should appear.
        assert "fastapi" in out
        assert "stub" in out


# ---------------------------------------------------------------------------
# `bse run`
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_unknown_plugin_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["run", "does_not_exist"])
        assert rc == EXIT_UNKNOWN_PLUGIN
        err = capsys.readouterr().err
        assert "unknown plugin" in err.lower()

    def test_run_stub_writes_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out_dir = tmp_path / "stub_out"
        rc = main(
            [
                "run",
                "stub",
                "--iterations",
                "5",
                "--rounds-l2",
                "2",
                "--out",
                str(out_dir),
            ]
        )
        assert rc == EXIT_OK
        # Packager wrote the three files.
        assert (out_dir / "report.json").is_file()
        assert (out_dir / "summary.txt").is_file()
        assert (out_dir / "reproduce.py").is_file()
        # JSON has the expected schema wrapper.
        payload = json.loads((out_dir / "report.json").read_text())
        assert payload["discovery_schema_version"] == "1"
        # Stub has no variants → layer3 absent.
        layers = set(payload["layers"].keys())
        assert layers == {"layer1_repetition", "layer2_lifecycle", "layer4_sequence"}
        # Stdout carries a human summary line per layer.
        out = capsys.readouterr().out
        assert "→ wrote" in out
        assert "layer1_repetition" in out


# ---------------------------------------------------------------------------
# `bse install`
# ---------------------------------------------------------------------------


class TestInstall:
    def test_install_rejects_invalid_identifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rc = main(["install", "not-an-identifier"])
        assert rc == EXIT_USAGE

    def test_install_scaffolds_two_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plugins").mkdir()
        rc = main(["install", "my_new_plugin"])
        assert rc == EXIT_OK
        target = tmp_path / "plugins" / "my_new_plugin"
        assert (target / "__init__.py").is_file()
        assert (target / "manifest.py").is_file()
        # Scaffold carries the correct class name (Title-case, no underscores).
        content = (target / "__init__.py").read_text()
        assert "class MyNewPluginPlugin" in content
        # Manifest declares the same name.
        mani = (target / "manifest.py").read_text()
        assert 'name="my_new_plugin"' in mani

    def test_install_refuses_existing_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plugins" / "already_here").mkdir(parents=True)
        rc = main(["install", "already_here"])
        assert rc == EXIT_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# _is_safe_version — pure helper, worth locking.
# ---------------------------------------------------------------------------


class TestSafeVersion:
    def test_accepts_valid_pep440_shapes(self) -> None:
        for v in ("0.141.1", "5.4.0", "2.0.0rc1", "1.0", "1.0+local", "1.0-alpha"):
            assert _is_safe_version(v), f"{v!r} rejected but should be safe"

    def test_rejects_shell_metacharacters(self) -> None:
        for v in ("0.1;rm -rf /", "0.1|cat", "$(id)", "`id`", "0.1 && ls", "", "0.1 "):
            assert not _is_safe_version(v), f"{v!r} accepted but should be rejected"
