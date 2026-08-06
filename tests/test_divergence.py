"""Tests for :mod:`core.divergence` and the ``bse triage`` verb.

Same discipline as tests/test_difficulty.py: no real claude session is
spawned. A fake shell script stands in as ``claude``; the fake writes
whatever ``diagnosis.json`` payload the test needs. This exercises
every branch of the driver — precondition failures, cluster math,
missing-diagnosis handling, CLI end-to-end — in seconds.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from cli.main import (
    EXIT_DIVERGENCE_PRECONDITION,
    EXIT_DIVERGENCE_REJECT,
    EXIT_OK,
    main,
)
from core.affidavit import AFFIDAVIT_FILENAME, AFFIDAVIT_SCHEMA_VERSION
from core.divergence import (
    DIVERGENCE_N_ATTEMPTS,
    TRIAGE_REPORT_FILENAME,
    DivergenceError,
    run_divergence_probe,
)

_GOOD_SHA = "0" * 40


def _chmod_exec(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _seed_candidate(
    candidate_dir: Path,
    *,
    include_affidavit: bool = True,
    include_prompt: bool = True,
) -> None:
    """A v2 affidavit + initial-prompt.md. Enough for divergence preconditions."""
    candidate_dir.mkdir(parents=True, exist_ok=True)
    if include_prompt:
        (candidate_dir / "initial-prompt.md").write_text(
            "The bug: the worker sometimes silently drops jobs after crash.\n",
            encoding="utf-8",
        )
    if include_affidavit:
        doc: dict[str, Any] = {
            "schema_version": AFFIDAVIT_SCHEMA_VERSION,
            "pinned_commit": _GOOD_SHA,
            "repo_url": "https://github.com/example/project",
            "upstream_issue_url": "https://github.com/example/project/issues/1",
            "bench_transcript_path": "bench.cast",
            "observed_behaviour": (
                "At the pinned commit, workers crashing between the accept "
                "and the ack of a delayed job leave the job in a held state. "
                "The maintenance loop does not requeue it."
            ),
            "divergence_from_thread": "",
            "upstream_status": "open",
            "signed_by": "Jeff",
            "signed_at": "2026-08-06T14:32:00Z",
        }
        (candidate_dir / AFFIDAVIT_FILENAME).write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _make_fake_claude(
    parent: Path,
    tag: str,
    *,
    root_causes: list[str],
) -> Path:
    """A fake claude that writes a diagnosis.json with a per-invocation root cause.

    Uses a counter file kept next to the fake to rotate through
    ``root_causes`` — invocation 0 gets ``root_causes[0]``, etc. This lets
    a single fake serve all three sessions with distinct diagnoses.
    """
    counter = parent / f"counter-{tag}.txt"
    counter.write_text("0", encoding="utf-8")
    fake = parent / f"fake-claude-{tag}.sh"
    # Encode root_causes as a bash array literal, escaping single quotes.
    escaped = [rc.replace("'", "'\"'\"'") for rc in root_causes]
    array_body = " ".join(f"'{rc}'" for rc in escaped)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        f"CTR='{counter}'\n"
        f"CAUSES=({array_body})\n"
        'idx="$(cat "$CTR")"\n'
        "next=$((idx + 1))\n"
        'echo "$next" > "$CTR"\n'
        'cause="${CAUSES[$idx]}"\n'
        "cat > diagnosis.json <<JSON\n"
        "{\n"
        '  "root_cause": "$cause",\n'
        '  "one_sentence": "because the mechanism fails silently under the observed condition",\n'
        '  "key_evidence": ["silently drops jobs"]\n'
        "}\n"
        "JSON\n",
        encoding="utf-8",
    )
    _chmod_exec(fake)
    return fake


def _make_no_diagnosis_fake(parent: Path, tag: str) -> Path:
    """A fake that exits cleanly but never writes diagnosis.json."""
    fake = parent / f"fake-claude-no-diag-{tag}.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n" "echo 'I decline to diagnose' >&2\n" "exit 0\n",
        encoding="utf-8",
    )
    _chmod_exec(fake)
    return fake


# ---------------------------------------------------------------------------
# Preconditions.
# ---------------------------------------------------------------------------
class TestPreconditions:
    def test_not_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(DivergenceError, match="is not a directory"):
            run_divergence_probe(tmp_path / "nope")

    def test_missing_affidavit(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path, include_affidavit=False)
        with pytest.raises(DivergenceError, match="affidavit prerequisite failed"):
            run_divergence_probe(tmp_path)

    def test_missing_initial_prompt(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path, include_prompt=False)
        with pytest.raises(DivergenceError, match="missing initial-prompt.md"):
            run_divergence_probe(tmp_path)

    def test_missing_claude_bin(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path)
        with pytest.raises(DivergenceError, match="not found on PATH"):
            run_divergence_probe(tmp_path, claude_bin="definitely-not-on-path-xyz")


# ---------------------------------------------------------------------------
# Cluster math.
# ---------------------------------------------------------------------------
class TestClustering:
    def test_all_converge_one_cluster_rejects(self, tmp_path: Path) -> None:
        """All three diagnoses share the same root cause → 1 cluster → REJECT."""
        _seed_candidate(tmp_path)
        shared = "the maintenance loop fails to requeue held delayed jobs after crash"
        fake = _make_fake_claude(
            tmp_path.parent,
            tag=tmp_path.name,
            root_causes=[shared, shared, shared],
        )
        report = run_divergence_probe(
            tmp_path, claude_bin=str(fake), write_report=False, ceiling_minutes=0.5
        )
        assert report.n_clusters == 1
        assert not report.passed
        assert all(d.diagnosis_produced for d in report.diagnoses)

    def test_all_diverge_three_clusters_passes(self, tmp_path: Path) -> None:
        """Three unrelated root causes → 3 clusters → PROCEED."""
        _seed_candidate(tmp_path)
        fake = _make_fake_claude(
            tmp_path.parent,
            tag=tmp_path.name,
            root_causes=[
                "asyncio timer handle keeps deferred callbacks alive forever",
                "postgres row lock leaks across connection pool recycle",
                "starlette middleware exception swallows the background task",
            ],
        )
        report = run_divergence_probe(
            tmp_path, claude_bin=str(fake), write_report=False, ceiling_minutes=0.5
        )
        assert report.n_clusters == 3
        assert report.passed

    def test_two_of_three_converge_still_passes(self, tmp_path: Path) -> None:
        """2/3 share root cause + 1 outlier → 2 clusters → PROCEED (barely).

        This is exactly the shape the plan calls "diagnosis-ambiguous" —
        strong models mostly find the same thing, but one goes a
        different way. That IS the signal Gate 4 wants to keep.
        """
        _seed_candidate(tmp_path)
        shared = "the maintenance loop fails to requeue held delayed jobs after crash"
        fake = _make_fake_claude(
            tmp_path.parent,
            tag=tmp_path.name,
            root_causes=[
                shared,
                shared,
                "postgres row lock leaks across connection pool recycle",
            ],
        )
        report = run_divergence_probe(
            tmp_path, claude_bin=str(fake), write_report=False, ceiling_minutes=0.5
        )
        assert report.n_clusters == 2
        assert report.passed

    def test_missing_diagnoses_excluded_from_clusters(self, tmp_path: Path) -> None:
        """Session that doesn't produce diagnosis.json is not a cluster of its own.

        Guards against the pathological pass where all three sessions
        crash and the driver counts three empty diagnoses as three
        distinct clusters.
        """
        _seed_candidate(tmp_path)
        fake = _make_no_diagnosis_fake(tmp_path.parent, tag=tmp_path.name)
        report = run_divergence_probe(
            tmp_path, claude_bin=str(fake), write_report=False, ceiling_minutes=0.5
        )
        assert report.n_clusters == 0
        assert not report.passed
        assert all(not d.diagnosis_produced for d in report.diagnoses)


# ---------------------------------------------------------------------------
# Report writing.
# ---------------------------------------------------------------------------
class TestReport:
    def test_writes_triage_report(self, tmp_path: Path) -> None:
        _seed_candidate(tmp_path)
        fake = _make_fake_claude(
            tmp_path.parent,
            tag=tmp_path.name,
            root_causes=[
                "asyncio timer handle keeps callbacks alive forever",
                "postgres row lock leaks across pool recycle",
                "starlette middleware swallows background exceptions",
            ],
        )
        run_divergence_probe(tmp_path, claude_bin=str(fake), ceiling_minutes=0.5)
        rep = tmp_path / TRIAGE_REPORT_FILENAME
        assert rep.is_file()
        payload = json.loads(rep.read_text())
        assert payload["passed"] is True
        assert payload["n_clusters"] == 3
        assert len(payload["diagnoses"]) == DIVERGENCE_N_ATTEMPTS

    def test_report_is_byte_stable(self, tmp_path: Path) -> None:
        """to_json() sorts keys — identical inputs produce identical bytes.

        Wall-clock elapsed varies per run, so we compare the payload
        minus the ``minutes`` fields.
        """
        _seed_candidate(tmp_path)
        fake = _make_fake_claude(
            tmp_path.parent,
            tag=tmp_path.name,
            root_causes=[
                "cause one distinct words alpha",
                "cause two distinct words beta",
                "cause three distinct words gamma",
            ],
        )
        report_1 = run_divergence_probe(
            tmp_path, claude_bin=str(fake), write_report=False, ceiling_minutes=0.5
        )
        # Second run — reset counter first.
        (tmp_path.parent / f"counter-{tmp_path.name}.txt").write_text("0")
        report_2 = run_divergence_probe(
            tmp_path, claude_bin=str(fake), write_report=False, ceiling_minutes=0.5
        )

        # Strip the volatile field.
        def _stable(payload: str) -> str:
            data = json.loads(payload)
            for d in data["diagnoses"]:
                d.pop("minutes", None)
                d.pop("working_dir", None)
            return json.dumps(data, sort_keys=True)

        assert _stable(report_1.to_json()) == _stable(report_2.to_json())


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
class TestCli:
    def test_precondition_maps_to_precondition_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Empty dir — no affidavit.
        rc = main(["triage", str(tmp_path), "--no-report"])
        assert rc == EXIT_DIVERGENCE_PRECONDITION
        assert "affidavit prerequisite failed" in capsys.readouterr().err

    def test_convergent_maps_to_reject_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_candidate(tmp_path)
        shared = "the maintenance loop fails to requeue held delayed jobs"
        fake = _make_fake_claude(
            tmp_path.parent, tag=tmp_path.name, root_causes=[shared, shared, shared]
        )
        rc = main(
            [
                "triage",
                str(tmp_path),
                "--claude-bin",
                str(fake),
                "--no-report",
            ]
        )
        assert rc == EXIT_DIVERGENCE_REJECT
        assert "CONVERGENT" in capsys.readouterr().out

    def test_divergent_maps_to_ok_exit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_candidate(tmp_path)
        fake = _make_fake_claude(
            tmp_path.parent,
            tag=tmp_path.name,
            root_causes=[
                "asyncio timer handle keeps callbacks alive forever",
                "postgres row lock leaks across pool recycle",
                "starlette middleware swallows background exceptions",
            ],
        )
        rc = main(
            [
                "triage",
                str(tmp_path),
                "--claude-bin",
                str(fake),
                "--no-report",
            ]
        )
        assert rc == EXIT_OK
        assert "DIVERGENT" in capsys.readouterr().out
