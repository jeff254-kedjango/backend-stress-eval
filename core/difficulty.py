"""Difficulty gate driver (Gate 2 of the sourcing gates).

Framework-agnostic. See :file:`upgrade-plan.md` §4 Gate 2 and :file:`rules.md`
Rule 12 for the standing rules that back this module.

This module drives the mechanical enforcement of "no candidate packages
without median frontier-model time-to-fix ≥ 60 minutes." A candidate
directory that opts into the gate must ship, in addition to the affidavit:

* ``initial-prompt.md`` — symptom-only prompt (Rule 10).
* ``probe.sh`` — an executable probe script exiting 0 iff the bug is fixed
  in the current working directory. This is the same independent probe the
  grader will use later, promoted to a first-class candidate-level artifact.
* ``make-eval-dirs.sh`` — the script that stamps out a clean model-facing
  working directory. Its contract is: given ``<destdir>``, populate it with
  everything the model may see (typically ``initial-prompt.md`` and any
  ``.venv``/pinned deps), and NOTHING that leaks the fix.

The driver spawns N=3 headless ``claude -p`` sessions, each in its own
tmpdir populated by ``make-eval-dirs.sh``, waits up to 180 minutes per
session, then runs ``probe.sh`` to determine PASS/FAIL. Wall-clock times
are recorded per session and the median is computed. Median < 60 min is
a hard reject; median ≥ 60 min is a pass and the ledger
(``difficulty-attempts.jsonl``) is stamped.

Design notes:

* Pure stdlib + ``subprocess``. Every wall-clock read goes through
  :func:`time.monotonic` — the only clock function safe for elapsed-time
  arithmetic across NTP adjustments.
* Per Rule 2, all subprocess argvs are fully constructed lists — never
  ``shell=True``. The tmpdir path and the executable paths are the only
  variable inputs; both are validated up front.
* Per Rule 5, every failure path returns a structured record with an
  actionable ``detail``. No swallowed exceptions.
* Per Rule 1, no hidden quadratic behaviour. The whole pipeline is
  O(N=3) sessions.
* Isolation: fresh tmpdir per session, cleaned up on success but retained
  on failure so the operator can inspect what the model did. No network
  filtering (the model needs pip and git); we trust the sessions not to
  ``curl`` the upstream issue thread, per the 2026-08-06 gate design.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from tempfile import mkdtemp
from typing import Final

__all__ = [
    "ATTEMPTS_FILENAME",
    "DIFFICULTY_MIN_MINUTES",
    "DIFFICULTY_N_ATTEMPTS",
    "DIFFICULTY_SCHEMA_VERSION",
    "DifficultyError",
    "DifficultyResult",
    "SessionOutcome",
    "run_difficulty_check",
]

# ---------------------------------------------------------------------------
# Gate constants. See upgrade-plan.md §4 Gate 2.
# ---------------------------------------------------------------------------
DIFFICULTY_N_ATTEMPTS: Final = 3
DIFFICULTY_MIN_MINUTES: Final = 60.0
DIFFICULTY_CEILING_MINUTES: Final = 180.0
DIFFICULTY_SCHEMA_VERSION: Final = "1"

ATTEMPTS_FILENAME: Final = "difficulty-attempts.jsonl"
_PROBE_FILENAME: Final = "probe.sh"
_MAKE_DIRS_FILENAME: Final = "make-eval-dirs.sh"
_INITIAL_PROMPT_FILENAME: Final = "initial-prompt.md"

# The default headless driver. Chosen 2026-08-06 (see the design Q&A in the
# implementation session): same product surface the harness user already
# lives inside, credentials/quotas already work, --print gives non-
# interactive output. Override via `claude_bin=` for testing.
_DEFAULT_CLAUDE_BIN: Final = "claude"

# The prompt handed to each headless session. Symptom-only per Rule 10.
# The driver appends the candidate's initial-prompt.md contents at the end;
# this preamble frames the task without revealing the fix.
_DRIVER_PREAMBLE: Final = (
    "You are attempting a debugging evaluation. Read the prompt below, "
    "investigate the code in the current directory, and produce a patch "
    "that makes the described symptom no longer occur. You have full "
    "shell access. When you believe you are done, stop responding — a "
    "grader will independently verify.\n\n"
    "---\n\n"
)


# ---------------------------------------------------------------------------
# Structured result records.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """One session's result. Serialized to a line of ``difficulty-attempts.jsonl``.

    ``minutes`` is the elapsed wall-clock time from session spawn to session
    exit (or timeout). ``fixed`` is True iff the post-session probe exited 0.
    ``timed_out`` is True iff we sent SIGTERM at the 180-min ceiling. When
    ``timed_out``, ``minutes`` is clamped to ``DIFFICULTY_CEILING_MINUTES``
    for the median (per the plan: "counts as ≥ 180 min").
    """

    index: int
    model_bin: str
    minutes: float
    fixed: bool
    timed_out: bool
    session_returncode: int | None
    probe_returncode: int | None
    working_dir: str
    stderr_tail: str = ""

    def to_json_line(self) -> str:
        """Render as a single JSONL row (no trailing newline)."""
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True, slots=True)
class DifficultyResult:
    """The overall gate verdict for one candidate."""

    schema_version: str
    candidate_dir: str
    sessions: tuple[SessionOutcome, ...]
    median_minutes: float
    threshold_minutes: float
    passed: bool

    def to_summary(self) -> str:
        """Human-readable one-block summary. Suitable for CLI stdout."""
        verdict = "PASS" if self.passed else "REJECT"
        lines = [
            f"difficulty gate: {verdict}",
            f"  candidate: {self.candidate_dir}",
            f"  N attempts: {len(self.sessions)}",
            f"  median: {self.median_minutes:.1f} min "
            f"(threshold: {self.threshold_minutes:.1f} min)",
        ]
        for s in self.sessions:
            tag = "fixed" if s.fixed else "no-fix"
            if s.timed_out:
                tag = "timed-out"
            lines.append(f"  session {s.index}: {s.minutes:.1f} min · {tag} · {s.working_dir}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------
class DifficultyError(RuntimeError):
    """Structural precondition failed (missing file, missing binary, etc.).

    Distinct from a *gate reject*: a REJECT means the candidate was
    evaluated and failed the median threshold. A :class:`DifficultyError`
    means we could not even start the evaluation. Callers surface both to
    the operator; the CLI distinguishes their exit codes.
    """


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def run_difficulty_check(
    candidate_dir: Path,
    /,
    *,
    n_attempts: int = DIFFICULTY_N_ATTEMPTS,
    threshold_minutes: float = DIFFICULTY_MIN_MINUTES,
    ceiling_minutes: float = DIFFICULTY_CEILING_MINUTES,
    claude_bin: str = _DEFAULT_CLAUDE_BIN,
    write_ledger: bool = True,
) -> DifficultyResult:
    """Drive N=3 headless sessions against ``candidate_dir`` and return the verdict.

    Preconditions (raise :class:`DifficultyError` on failure):

    * ``candidate_dir`` is a directory containing ``initial-prompt.md``,
      ``probe.sh`` (executable), and ``make-eval-dirs.sh`` (executable).
    * ``claude_bin`` resolves on PATH (or is an absolute path to an
      executable file).

    Writes a ``difficulty-attempts.jsonl`` ledger under ``candidate_dir``
    unless ``write_ledger=False`` (for tests). Each line is one
    :class:`SessionOutcome`.

    The keyword-only knobs (``n_attempts``, ``threshold_minutes``,
    ``ceiling_minutes``) exist for tests; production code path uses the
    module-level constants exclusively.
    """
    _validate_candidate_dir(candidate_dir)
    resolved_bin = _resolve_binary(claude_bin)
    initial_prompt = (candidate_dir / _INITIAL_PROMPT_FILENAME).read_text(encoding="utf-8")
    make_dirs_script = (candidate_dir / _MAKE_DIRS_FILENAME).resolve()
    probe_script = (candidate_dir / _PROBE_FILENAME).resolve()

    prompt = _DRIVER_PREAMBLE + initial_prompt

    sessions: list[SessionOutcome] = []
    for i in range(n_attempts):
        outcome = _run_one_session(
            index=i,
            claude_bin=resolved_bin,
            prompt=prompt,
            make_dirs_script=make_dirs_script,
            probe_script=probe_script,
            ceiling_minutes=ceiling_minutes,
        )
        sessions.append(outcome)

    minutes_for_median = [min(s.minutes, ceiling_minutes) for s in sessions]
    med = median(minutes_for_median) if minutes_for_median else 0.0
    result = DifficultyResult(
        schema_version=DIFFICULTY_SCHEMA_VERSION,
        candidate_dir=str(candidate_dir.resolve()),
        sessions=tuple(sessions),
        median_minutes=med,
        threshold_minutes=threshold_minutes,
        passed=med >= threshold_minutes,
    )

    if write_ledger:
        _write_ledger(candidate_dir, result)
    return result


# ---------------------------------------------------------------------------
# Preconditions.
# ---------------------------------------------------------------------------
def _validate_candidate_dir(candidate_dir: Path) -> None:
    """Every required artifact present + executable where it must be."""
    if not candidate_dir.is_dir():
        raise DifficultyError(
            f"{candidate_dir} is not a directory. "
            "Provide the candidate folder containing initial-prompt.md, "
            "probe.sh, and make-eval-dirs.sh."
        )
    for name in (_INITIAL_PROMPT_FILENAME, _PROBE_FILENAME, _MAKE_DIRS_FILENAME):
        p = candidate_dir / name
        if not p.is_file():
            raise DifficultyError(
                f"missing required file {name} under {candidate_dir}. "
                "See upgrade-plan.md §4 Gate 2 for the candidate contract."
            )
    for name in (_PROBE_FILENAME, _MAKE_DIRS_FILENAME):
        p = candidate_dir / name
        if not os.access(p, os.X_OK):
            raise DifficultyError(
                f"{p} is not executable. chmod +x it so the driver can invoke it."
            )


def _resolve_binary(name_or_path: str) -> str:
    """Return an absolute path to the driver binary, or raise DifficultyError."""
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise DifficultyError(
            f"claude_bin {name_or_path!r} is absolute but not an executable file."
        )
    resolved = shutil.which(name_or_path)
    if resolved is None:
        raise DifficultyError(
            f"claude_bin {name_or_path!r} not found on PATH. "
            "Install claude-code CLI or pass claude_bin=<absolute-path>."
        )
    return resolved


# ---------------------------------------------------------------------------
# One session.
# ---------------------------------------------------------------------------
def _run_one_session(
    *,
    index: int,
    claude_bin: str,
    prompt: str,
    make_dirs_script: Path,
    probe_script: Path,
    ceiling_minutes: float,
) -> SessionOutcome:
    """Spawn one headless session in a fresh tmpdir, then probe.

    The tmpdir is retained on failure (fix not achieved, session errored,
    timed out) so the operator can post-mortem. It is cleaned up on
    success. Rule 9 (measure before theorizing) applies here — we keep
    the evidence around for the reject case.
    """
    workdir = Path(mkdtemp(prefix=f"bse-difficulty-{index}-"))
    _populate_workdir(make_dirs_script=make_dirs_script, workdir=workdir)

    start = time.monotonic()
    proc, stderr_tail, timed_out = _spawn_session(
        claude_bin=claude_bin,
        prompt=prompt,
        workdir=workdir,
        ceiling_seconds=ceiling_minutes * 60.0,
    )
    elapsed_seconds = time.monotonic() - start
    minutes = elapsed_seconds / 60.0

    probe_rc = _run_probe(probe_script=probe_script, workdir=workdir)
    fixed = probe_rc == 0

    if fixed and not timed_out:
        # Success — the tmpdir has served its purpose; reclaim disk.
        shutil.rmtree(workdir, ignore_errors=True)
        workdir_str = f"<cleaned: was under {workdir.parent}>"
    else:
        workdir_str = str(workdir)

    return SessionOutcome(
        index=index,
        model_bin=claude_bin,
        minutes=minutes,
        fixed=fixed,
        timed_out=timed_out,
        session_returncode=proc.returncode if proc is not None else None,
        probe_returncode=probe_rc,
        working_dir=workdir_str,
        stderr_tail=stderr_tail,
    )


def _populate_workdir(*, make_dirs_script: Path, workdir: Path) -> None:
    """Invoke ``make-eval-dirs.sh <workdir>`` in the candidate's own directory.

    We call the script with the workdir as its single positional argument.
    Existing scripts under ``eval-tasks/`` accept tags rather than a
    destination; candidates adopting Gate 2 must expose a
    single-destination interface (documented via the contract error above).
    Refactoring the existing tasks is out of scope for this chunk — they
    are frozen per the 2026-08-06 decision.
    """
    result = subprocess.run(  # noqa: S603 -- argv is fully constructed
        [str(make_dirs_script), str(workdir)],
        cwd=make_dirs_script.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DifficultyError(
            f"make-eval-dirs.sh failed with exit {result.returncode} "
            f"populating {workdir}. stderr tail:\n{_tail(result.stderr)}"
        )


def _spawn_session(
    *,
    claude_bin: str,
    prompt: str,
    workdir: Path,
    ceiling_seconds: float,
) -> tuple[subprocess.CompletedProcess[str] | None, str, bool]:
    """Run one headless session; return (proc, stderr_tail, timed_out).

    ``timed_out`` is True iff we hit the ceiling and had to signal the
    process. In that case the process is still terminated cleanly (SIGTERM
    → grace → SIGKILL) and the returned ``proc.returncode`` reflects that.

    We rely on subprocess's own ``timeout=`` for wall-clock enforcement;
    it raises :class:`subprocess.TimeoutExpired`, at which point we kill
    the process explicitly to be sure. Popen would be more precise but
    ``subprocess.run(timeout=...)`` already does the right thing including
    reading pipes safely; no need to re-implement.
    """
    argv = [
        claude_bin,
        "--print",
        "--allow-dangerously-skip-permissions",
        prompt,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 -- argv fully validated
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=ceiling_seconds,
            check=False,
        )
        return proc, _tail(proc.stderr), False
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the child by the time this
        # raises; the partial stderr is on the exception object. The
        # typeshed stub for TimeoutExpired.stderr is bytes | None
        # regardless of the text= flag — the exception class doesn't know.
        # At runtime with text=True it's actually str, but we decode
        # unconditionally to match the stub and avoid any runtime surprise.
        raw = exc.stderr
        stderr_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw or ""
        return None, _tail(stderr_text), True


def _run_probe(*, probe_script: Path, workdir: Path) -> int:
    """Invoke the independent probe. Exit code 0 means the fix landed."""
    result = subprocess.run(  # noqa: S603 -- argv is fully constructed
        [str(probe_script)],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        # Probes are supposed to be fast (they run inline in
        # make-eval-dirs.sh normally). Cap generously so a broken probe
        # can't wedge the driver.
        timeout=600.0,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Ledger.
# ---------------------------------------------------------------------------
def _write_ledger(candidate_dir: Path, result: DifficultyResult) -> None:
    """Append the run's sessions to ``difficulty-attempts.jsonl``.

    Appended, not overwritten — the ledger is the durable record of every
    difficulty check ever run against this candidate. Multiple runs (e.g.
    after a candidate is refined) accumulate; the freshest run's median
    is the one that decides the gate but the history remains inspectable.
    Each session record carries its own timestamp via ``model_bin`` +
    numeric fields; timestamping the *run* is left to a future chunk if
    provenance-across-runs becomes important.
    """
    path = candidate_dir / ATTEMPTS_FILENAME
    with path.open("a", encoding="utf-8") as f:
        for s in result.sessions:
            f.write(s.to_json_line())
            f.write("\n")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _tail(text: str, *, max_chars: int = 2000) -> str:
    """Truncate a stderr blob to a bounded suffix. Bounded I/O in every field."""
    if len(text) <= max_chars:
        return text
    return "…\n" + text[-max_chars:]
