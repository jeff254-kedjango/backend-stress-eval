"""T3.2 — Repro provenance lock.

The affidavit (Gate 1) captured a bench transcript at a pin. That
transcript is a *past* observation. Between the time it was captured
and any given later day, the upstream can:

* Yank the package version the pin resolves to.
* Push a patch release that transitive-installs a fix beneath the pin.
* Rewrite git history so the SHA resolves differently (rare, but
  possible on force-pushed branches).

This runner performs a fresh, ephemeral verification: create a scratch
venv, install the affidavit's pinned package == pinned version, run
the candidate's :file:`reproduce.sh`, and require the grader to exit
FAIL on the baseline. If it now PASSes, the fix has landed upstream
(or the deps have drifted) and the candidate is no longer reproducible.

Meant to run nightly via cron/CI. See :file:`scripts/nightly-verify-repro.sh`
for the pattern.

Design:

* uv-first with pip fallback — same pattern as
  :func:`cli.main._pip_install_at_version`. uv is dramatically faster
  when it's available; plain pip works everywhere else.
* Fresh tmpdir venv per run. The tmpdir is auto-cleaned unless
  ``keep_workdir`` is set (for post-mortem inspection).
* Rule 5: fail-loud on missing affidavit, invalid version string,
  install failure. The one thing we do NOT treat as an error is the
  reproduce.sh itself exiting FAIL — that's the whole point of the
  gate; we only care whether it matches what the affidavit claimed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.affidavit import AFFIDAVIT_FILENAME, AffidavitError, load_affidavit

__all__ = [
    "REPRO_VERIFICATION_FILENAME",
    "REPRO_VERIFICATION_SCHEMA_VERSION",
    "ReproVerificationReport",
    "ReproVerifierError",
    "run_repro_verification",
]


REPRO_VERIFICATION_FILENAME: Final = "repro-verification.json"
REPRO_VERIFICATION_SCHEMA_VERSION: Final = "1"
_REPRODUCE_SCRIPT_NAME: Final = "reproduce.sh"
_INSTALL_TIMEOUT_SECONDS: Final = 300
_REPRODUCE_TIMEOUT_SECONDS: Final = 900
_EXIT_PASS: Final = 0


class ReproVerifierError(RuntimeError):
    """Precondition failure — affidavit missing, install failure, timeout, etc."""


@dataclass(frozen=True, slots=True)
class ReproVerificationReport:
    """Artifact from one verification run."""

    schema_version: str
    candidate_dir: str
    pinned_package: str
    pinned_version: str
    install_returncode: int
    reproduce_returncode: int
    # A candidate is REPRODUCIBLE if reproduce.sh exits non-0 (grader
    # FAIL on baseline). If it exits 0, the fix has landed upstream
    # or deps drifted — candidate is no longer usable as-is.
    still_reproducible: bool
    stderr_head: str

    def summary_line(self) -> str:
        verdict = "STILL REPRODUCIBLE" if self.still_reproducible else "NO LONGER REPRODUCIBLE"
        return (
            f"{verdict} — reproduce.sh exit={self.reproduce_returncode} "
            f"against {self.pinned_package}=={self.pinned_version}"
        )

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "candidate_dir": self.candidate_dir,
            "pinned_package": self.pinned_package,
            "pinned_version": self.pinned_version,
            "install_returncode": self.install_returncode,
            "reproduce_returncode": self.reproduce_returncode,
            "still_reproducible": self.still_reproducible,
            "summary": self.summary_line(),
            "stderr_head": self.stderr_head,
        }
        return json.dumps(payload, sort_keys=True, indent=2)


def run_repro_verification(
    candidate_dir: Path,
    *,
    pinned_package: str,
    pinned_version: str | None = None,
    keep_workdir: bool = False,
) -> ReproVerificationReport:
    """Verify the candidate still reproduces at its pinned version.

    Args:
        candidate_dir: The candidate directory. Must contain a
            :file:`repro-affidavit.json` and an executable
            :file:`reproduce.sh`.
        pinned_package: Which pypi package to install for the pinned
            version. This is a caller-supplied contract; the affidavit
            names the *upstream repo*, not the pypi package (they can
            differ). Typical: the same package name the plugin's
            manifest declares.
        pinned_version: PEP-440 version string. If ``None``, the
            affidavit's ``pinned_commit`` is interpreted as a
            version tag with the leading ``v`` stripped — common but
            not universal, so ``None`` is a convenience for the
            common case.
        keep_workdir: If True, do not clean up the tmpdir on exit.
            Useful for post-mortem inspection of a failed run.

    Returns:
        :class:`ReproVerificationReport`.

    Raises:
        ReproVerifierError: preconditions unmet — missing affidavit,
            missing reproduce.sh, install failure, timeout.
    """
    if not candidate_dir.is_dir():
        raise ReproVerifierError(f"candidate directory {candidate_dir} does not exist")
    reproduce_path = candidate_dir / _REPRODUCE_SCRIPT_NAME
    if not reproduce_path.is_file():
        raise ReproVerifierError(
            f"{reproduce_path} missing — repro verifier needs {_REPRODUCE_SCRIPT_NAME} "
            "in the candidate dir"
        )
    aff_path = candidate_dir / AFFIDAVIT_FILENAME
    if not aff_path.is_file():
        raise ReproVerifierError(f"{aff_path} missing — Gate 1 not established")

    try:
        affidavit = load_affidavit(candidate_dir)
    except AffidavitError as exc:
        raise ReproVerifierError(f"affidavit invalid: {exc}") from exc

    if pinned_version is None:
        pinned_version = affidavit.pinned_commit.lstrip("v")

    if not _is_safe_version(pinned_version):
        raise ReproVerifierError(f"refusing unsafe version string {pinned_version!r}")
    if not _is_safe_pkg_name(pinned_package):
        raise ReproVerifierError(f"refusing unsafe package name {pinned_package!r}")

    if keep_workdir:
        # tempfile.mkdtemp leaves the directory on disk after the process
        # exits — intentional here for post-mortem inspection.
        workdir = Path(tempfile.mkdtemp(prefix="bse-verify-repro-"))
        return _run_in_workdir(
            candidate_dir=candidate_dir,
            reproduce_path=reproduce_path,
            workdir=workdir,
            pinned_package=pinned_package,
            pinned_version=pinned_version,
        )
    with tempfile.TemporaryDirectory(prefix="bse-verify-repro-") as workdir_str:
        return _run_in_workdir(
            candidate_dir=candidate_dir,
            reproduce_path=reproduce_path,
            workdir=Path(workdir_str),
            pinned_package=pinned_package,
            pinned_version=pinned_version,
        )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------
def _run_in_workdir(
    *,
    candidate_dir: Path,
    reproduce_path: Path,
    workdir: Path,
    pinned_package: str,
    pinned_version: str,
) -> ReproVerificationReport:
    """Create venv, install pin, run reproduce.sh, package the report."""
    venv_dir = workdir / "venv"
    install_rc, install_stderr = _create_venv_and_install(
        venv_dir=venv_dir,
        pinned_package=pinned_package,
        pinned_version=pinned_version,
    )
    if install_rc != 0:
        raise ReproVerifierError(
            f"install of {pinned_package}=={pinned_version} failed "
            f"(rc={install_rc}). stderr head: {install_stderr[:500]}"
        )

    reproduce_rc, reproduce_stderr = _run_reproduce(
        reproduce_path=reproduce_path,
        candidate_dir=candidate_dir,
        venv_dir=venv_dir,
    )

    return ReproVerificationReport(
        schema_version=REPRO_VERIFICATION_SCHEMA_VERSION,
        candidate_dir=str(candidate_dir),
        pinned_package=pinned_package,
        pinned_version=pinned_version,
        install_returncode=install_rc,
        reproduce_returncode=reproduce_rc,
        still_reproducible=(reproduce_rc != _EXIT_PASS),
        stderr_head=(reproduce_stderr or "")[:500],
    )


def _create_venv_and_install(
    *, venv_dir: Path, pinned_package: str, pinned_version: str
) -> tuple[int, str]:
    """Create a fresh venv and install the pinned package inside.

    Returns (install_returncode, stderr_head). Interpreter creation
    failure surfaces as a non-zero install rc with the venv creator's
    stderr in stderr_head.
    """
    # venv creation.
    venv_result = subprocess.run(  # noqa: S603 -- argv fully validated.
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        check=False,
        timeout=_INSTALL_TIMEOUT_SECONDS,
    )
    if venv_result.returncode != 0:
        return venv_result.returncode, venv_result.stderr or ""

    venv_python = venv_dir / "bin" / "python"
    if not venv_python.is_file():
        return 1, f"venv python not found at {venv_python}"

    spec = f"{pinned_package}=={pinned_version}"
    installers = (
        ["uv", "pip", "install", "--python", str(venv_python), spec],
        [str(venv_python), "-m", "pip", "install", spec],
    )
    last_stderr = ""
    for cmd in installers:
        try:
            result = subprocess.run(  # noqa: S603 -- argv fully validated.
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=_INSTALL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            last_stderr = f"install timed out after {_INSTALL_TIMEOUT_SECONDS}s"
            continue
        except FileNotFoundError:
            # uv not on PATH — fall through to plain pip.
            last_stderr = f"{cmd[0]} not found; falling back"
            continue
        if result.returncode == 0:
            return 0, ""
        last_stderr = result.stderr or ""
        # Only fall through to pip if uv itself is missing. If uv ran
        # and failed, don't retry with pip — the failure is the pin.
        if "not found" not in last_stderr.lower():
            return result.returncode, last_stderr
    return 1, last_stderr


def _run_reproduce(
    *,
    reproduce_path: Path,
    candidate_dir: Path,
    venv_dir: Path,
) -> tuple[int, str]:
    """Run reproduce.sh with PYTHON pointing at the venv's interpreter."""
    venv_python = venv_dir / "bin" / "python"
    env = {
        "PATH": f"{venv_dir / 'bin'}:/usr/bin:/bin",
        "PYTHON": str(venv_python),
        "HOME": str(candidate_dir),  # keep any stray writes contained.
    }
    try:
        result = subprocess.run(  # noqa: S603 -- argv fully validated.
            ["/bin/bash", str(reproduce_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_REPRODUCE_TIMEOUT_SECONDS,
            env=env,
            cwd=candidate_dir,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, f"reproduce.sh exceeded {_REPRODUCE_TIMEOUT_SECONDS}s: {exc}"
    return result.returncode, result.stderr or ""


def _is_safe_version(version: str) -> bool:
    """Same restriction as cli.main._is_safe_version — [A-Za-z0-9._+-]."""
    if not version:
        return False
    return all(c.isalnum() or c in "._+-" for c in version)


def _is_safe_pkg_name(name: str) -> bool:
    """PEP 508-ish safety: alnum + [-_.]. Refuses shell metacharacters."""
    if not name:
        return False
    return all(c.isalnum() or c in "-_." for c in name)
