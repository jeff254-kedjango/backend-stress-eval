"""bse — the one-command CLI for backend-stress-eval.

Subcommands:

* ``bse list`` — show every discovered plugin (:mod:`plugins.registry`).
* ``bse run <name> [--version X.Y.Z] [--iterations N] [--rounds-l2 N]
                    [--rounds-l3 N] [--out PATH] [--no-install]``
  — run the full discovery sweep against a plugin, package the eval task.
* ``bse install <name>`` — scaffold a new plugin under ``plugins/<name>/``
  from a minimal template. The user fills in the framework specifics.

The CLI is pure stdlib (argparse + subprocess). Rule 5 clarity: every code
path returns an int exit status; nothing raises to the terminal except
argparse's own usage errors.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from core.affidavit import (
    AFFIDAVIT_FILENAME,
    AffidavitError,
    validate_affidavit,
)
from core.differ import (
    DIFF_REPORT_FILENAME,
    DiffReport,
    diff_report_dicts,
    diff_reports,
    load_report_json,
)
from core.difficulty import (
    ATTEMPTS_FILENAME,
    DIFFICULTY_MIN_MINUTES,
    DIFFICULTY_N_ATTEMPTS,
    DifficultyError,
    run_difficulty_check,
)
from core.divergence import (
    TRIAGE_REPORT_FILENAME,
    DivergenceError,
    run_divergence_probe,
)
from core.grader_validator import (
    GRADER_VALIDATION_FILENAME,
    GraderValidatorError,
    run_grader_validation,
)
from core.reporter import Report
from core.repro_verifier import (
    REPRO_VERIFICATION_FILENAME,
    ReproVerifierError,
    run_repro_verification,
)
from core.writeup_audit import (
    AUDIT_REPORT_FILENAME,
    WriteupAuditError,
    run_writeup_audit,
)
from harnesses.concurrency_matrix import (
    MODE_MATRIX_FILENAME,
    ModeMatrixError,
    run_concurrency_matrix,
)
from harnesses.discovery import (
    DEFAULT_ITERATIONS_L1,
    DEFAULT_ROUNDS_L2,
    DEFAULT_ROUNDS_L3,
    run_discovery,
)
from harnesses.eval_task import package_eval_task
from harnesses.fault_matrix import (
    FAULT_MATRIX_FILENAME,
    FaultMatrixError,
    run_fault_matrix,
)
from harnesses.teardown_fuzzer import (
    TEARDOWN_FUZZ_FILENAME,
    TeardownFuzzError,
    run_teardown_fuzzer,
)
from plugins.registry import Manifest, load_manifests

__all__ = ["main"]


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNKNOWN_PLUGIN = 3
EXIT_INSTALL_FAILED = 4
EXIT_ALREADY_EXISTS = 5
EXIT_AFFIDAVIT_INVALID = 6
EXIT_DIFFICULTY_PRECONDITION = 7
EXIT_DIFFICULTY_REJECT = 8
EXIT_WRITEUP_PRECONDITION = 9
EXIT_WRITEUP_REJECT = 10
EXIT_DIVERGENCE_PRECONDITION = 11
EXIT_DIVERGENCE_REJECT = 12
EXIT_DIFF_PRECONDITION = 13
EXIT_DIFF_HAS_CHANGES = 14
EXIT_MODE_MATRIX_PRECONDITION = 15
EXIT_MODE_MATRIX_HAS_DIVERGENCE = 16
EXIT_TEARDOWN_PRECONDITION = 17
EXIT_TEARDOWN_HAS_DIVERGENCE = 18
EXIT_GRADER_VALIDATION_PRECONDITION = 19
EXIT_GRADER_VALIDATION_REJECT = 20
EXIT_REPRO_VERIFIER_PRECONDITION = 21
EXIT_REPRO_VERIFIER_NO_LONGER_REPRODUCIBLE = 22
EXIT_FAULT_MATRIX_PRECONDITION = 23
EXIT_FAULT_MATRIX_HAS_DIVERGENCE = 24


# ---------------------------------------------------------------------------
# `bse list`
# ---------------------------------------------------------------------------
def _cmd_list(_args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    if not manifests:
        print("No plugins discovered under plugins/.")
        return EXIT_OK
    name_width = max(len(n) for n in manifests) + 2
    for name in sorted(manifests):
        m = manifests[name]
        pip_ver = m.resolve_version_package() or "(no runtime deps)"
        print(f"{name:<{name_width}} {m.description}")
        print(f"{'':<{name_width}}   pip: {pip_ver}   variants: {len(m.variants)}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse run <name>`
# ---------------------------------------------------------------------------
def _cmd_run(args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    name: str = args.name
    if name not in manifests:
        print(f"error: unknown plugin {name!r}. Try 'bse list'.", file=sys.stderr)
        return EXIT_UNKNOWN_PLUGIN
    manifest = manifests[name]

    version: str | None = args.version
    if version is not None and not args.no_install:
        rc = _pip_install_at_version(manifest, version)
        if rc != EXIT_OK:
            return rc

    target_commit = manifest.default_target_commit(version)
    plugin = manifest.plugin_factory(manifest.default_app_factory)

    variants = manifest.variants or None
    variant_factory = (lambda af: manifest.plugin_factory(af)) if variants is not None else None

    reports = run_discovery(
        plugin=plugin,
        target_commit=target_commit,
        iterations_l1=args.iterations,
        rounds_l2=args.rounds_l2,
        rounds_l3=args.rounds_l3,
        variants=variants,
        variant_plugin_factory=variant_factory,
    )

    out_dir = Path(args.out) if args.out else Path("reports/discovery") / target_commit
    package_eval_task(reports=reports, out_dir=out_dir)

    # Human-readable line per layer.
    print(f"→ wrote {out_dir}")
    for layer_name in sorted(reports):
        r = reports[layer_name]
        status = "PASS" if r.result.success else "FAIL"
        print(
            f"  [{r.metadata.target}@{r.metadata.target_commit}] "
            f"{status} iterations={r.result.iterations_completed}"
            f"/{r.metadata.iterations_requested} "
            f"invariants={len(r.result.invariants_evaluated)} "
            f"violations={len(r.result.violations)}   {layer_name}"
        )
    return EXIT_OK


def _pip_install_at_version(manifest: Manifest, version: str) -> int:
    """Force-reinstall the manifest's version-carrying package at ``version``.

    Uses ``uv pip`` if available (the project's chosen installer) and falls
    back to plain ``pip``. Non-zero exit is surfaced as
    ``EXIT_INSTALL_FAILED`` with the installer's output on stderr.
    """
    pkg = manifest.resolve_version_package()
    if pkg is None:
        print(
            f"error: plugin {manifest.name!r} has no version_package — "
            "--version is not applicable.",
            file=sys.stderr,
        )
        return EXIT_INSTALL_FAILED

    # Validate version defensively — the string ends up in a subprocess argv,
    # so a shell metacharacter would be a bug regardless of shell=False.
    if not _is_safe_version(version):
        print(f"error: refusing to pass unsafe version string {version!r}", file=sys.stderr)
        return EXIT_INSTALL_FAILED

    spec = f"{pkg}=={version}"
    installers = (
        ["uv", "pip", "install", "--force-reinstall", spec],
        [sys.executable, "-m", "pip", "install", "--force-reinstall", spec],
    )
    for installer_cmd in installers:
        result = subprocess.run(  # noqa: S603 -- argv is fully validated above
            installer_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return EXIT_OK
        # If uv isn't available we get FileNotFoundError semantics via
        # returncode 127-ish or subprocess raising — capture handles the
        # first cleanly; fall through to pip.
        if "not found" not in (result.stderr or "").lower():
            print(result.stderr, file=sys.stderr)
            return EXIT_INSTALL_FAILED
    return EXIT_INSTALL_FAILED


def _is_safe_version(version: str) -> bool:
    """PEP 440 doesn't allow shell metacharacters. Accept only [A-Za-z0-9._+-]."""
    if not version:
        return False
    return all(c.isalnum() or c in "._+-" for c in version)


# ---------------------------------------------------------------------------
# `bse install <name>`  (scaffold, not pip)
# ---------------------------------------------------------------------------
_SCAFFOLD_INIT = '''"""Plugin: {name}. See plugins/fastapi/__init__.py for a full worked example."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

__all__ = ["{class_name}App", "{class_name}Client", "{class_name}Plugin"]


_FEATURES: Final[Mapping[str, bool]] = MappingProxyType({{
    # Fill in the framework features this plugin exposes.
    "lifespan": True,
}})


@dataclass(slots=True)
class {class_name}App:
    """Fill in the framework-specific app type."""


@dataclass(slots=True)
class {class_name}Client:
    """Request-issuing facade for {name}."""


@dataclass(slots=True)
class {class_name}Plugin:
    """TODO: implement the ten Plugin methods for {name}."""

    name: str = "{name}"

    def build_app(self) -> {class_name}App:
        raise NotImplementedError

    def client(self, app: {class_name}App, /) -> {class_name}Client:
        raise NotImplementedError

    def lifecycle_start(self, app: {class_name}App, /) -> None:
        raise NotImplementedError

    def lifecycle_stop(self, app: {class_name}App, /) -> None:
        raise NotImplementedError

    def reset(self, app: {class_name}App, /) -> None:
        raise NotImplementedError

    def feature_matrix(self) -> Mapping[str, bool]:
        return _FEATURES

    def probe(self, client: {class_name}Client, /) -> None:
        raise NotImplementedError

    def route_signature(self, app: {class_name}App, /) -> tuple[str, ...]:
        raise NotImplementedError

    def response_digest(self, app: {class_name}App, /) -> str | None:
        raise NotImplementedError
'''

_SCAFFOLD_MANIFEST = '''"""Manifest for the {name} plugin."""

from __future__ import annotations

from typing import Final

from plugins.{name} import {class_name}App, {class_name}Plugin
from plugins.registry import Manifest

__all__ = ["MANIFEST"]


def _default_app() -> {class_name}App:
    return {class_name}App()


MANIFEST: Final = Manifest(
    name="{name}",
    description="TODO: one-line description of the {name} plugin.",
    pip_packages=(),  # e.g. ("{name}",) — the pip name(s) this plugin needs
    plugin_factory=lambda _app_factory: {class_name}Plugin(),
    default_app_factory=_default_app,
)
'''


def _cmd_install(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    """Scaffold ``plugins/<name>/{__init__.py, manifest.py}`` from templates."""
    name: str = args.name
    if not name.isidentifier():
        print(f"error: plugin name {name!r} is not a valid python identifier.", file=sys.stderr)
        return EXIT_USAGE

    target_dir = Path("plugins") / name
    if target_dir.exists():
        print(
            f"error: plugins/{name}/ already exists. " f"Move it aside or pick a different name.",
            file=sys.stderr,
        )
        return EXIT_ALREADY_EXISTS

    class_name = name.title().replace("_", "")
    target_dir.mkdir(parents=True)
    (target_dir / "__init__.py").write_text(
        _SCAFFOLD_INIT.format(name=name, class_name=class_name), encoding="utf-8"
    )
    (target_dir / "manifest.py").write_text(
        _SCAFFOLD_MANIFEST.format(name=name, class_name=class_name), encoding="utf-8"
    )
    print(f"→ scaffolded plugins/{name}/__init__.py + plugins/{name}/manifest.py")
    print("  Fill in the ten Plugin methods, then re-run 'bse list' to confirm discovery.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse affidavit <candidate-dir>` — Gate 1 of the sourcing gates.
#
# See upgrade-plan.md §4 Gate 1 and rules.md Rule 11. This verb is the
# mechanical enforcement of "no candidate packages without a personally-
# reproduced-on-bench affidavit". Exits nonzero on any structural or
# semantic failure; exits zero only when the affidavit fully passes.
# ---------------------------------------------------------------------------
def _cmd_affidavit(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    candidate_dir = Path(args.candidate_dir)
    if not candidate_dir.is_dir():
        print(
            f"error: {candidate_dir} is not a directory. "
            f"Point at the candidate folder containing {AFFIDAVIT_FILENAME}.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        failures = validate_affidavit(candidate_dir)
    except AffidavitError as exc:
        # File-level errors: missing file, bad JSON, missing/wrong-typed fields.
        # These are pre-conditions — we cannot even start semantic checks.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_AFFIDAVIT_INVALID

    if failures:
        print(
            f"affidavit REJECTED ({len(failures)} failure(s)) at {candidate_dir}:",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  [{f.field}] {f.detail}", file=sys.stderr)
        return EXIT_AFFIDAVIT_INVALID

    print(f"→ affidavit OK: {candidate_dir / AFFIDAVIT_FILENAME}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse difficulty-check <candidate-dir>` — Gate 2 of the sourcing gates.
#
# Drives N=3 headless `claude -p` sessions in isolated tmpdirs, runs the
# candidate's independent probe.sh after each, and rejects if the median
# time-to-fix is under 60 minutes. See upgrade-plan.md §4 Gate 2 and
# rules.md Rule 12 for the standing rules. Two distinct nonzero exits:
# preconditions failed (missing files, no claude binary) vs. the gate
# ran to completion and REJECTed — they are operationally different, so
# the operator can key alerting off the difference.
# ---------------------------------------------------------------------------
def _cmd_difficulty(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    candidate_dir = Path(args.candidate_dir)
    try:
        result = run_difficulty_check(
            candidate_dir,
            claude_bin=args.claude_bin,
            write_ledger=not args.no_ledger,
        )
    except DifficultyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DIFFICULTY_PRECONDITION

    print(result.to_summary())
    if not args.no_ledger:
        print(f"→ appended {len(result.sessions)} row(s) to {candidate_dir / ATTEMPTS_FILENAME}")

    if not result.passed:
        return EXIT_DIFFICULTY_REJECT
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse scaffold-candidate <name>` — stamp out a candidate dir that already
# satisfies the Gate 1 + Gate 2 + Gate 3 contract.
#
# Rationale: the candidate contract has grown across chunks A-C
# (repro-affidavit.json, initial-prompt.md, probe.sh, make-eval-dirs.sh,
# and now upstream_issue_url on the affidavit). An author starting fresh
# would otherwise have to reverse-engineer it from doc pages. This verb
# stamps out an empty-but-compliant skeleton; the author fills the
# specifics. Mirrors `bse install` for plugins.
# ---------------------------------------------------------------------------
# Assembled as a JSON object at scaffold time (see _cmd_scaffold_candidate).
# The long guidance strings live in module constants below so they don't
# force a per-line noqa inside a triple-quoted template.
_STUB_OBSERVED_GUIDANCE = (
    "FILL IN 80-2000 chars describing what you PERSONALLY saw on-bench "
    "at the pinned commit. This is written from your bench transcript, "
    "NOT from the upstream issue thread. Rule 11."
)
_STUB_DIVERGENCE_GUIDANCE = (
    "Empty string only if you have RE-READ the upstream issue and confirm "
    "on-bench behaviour matches. Otherwise describe every respect in which "
    "it differs. Rule 11."
)

_CAND_INITIAL_PROMPT_STUB = """# {name} — initial prompt

FILL IN the symptom description in your own words, from your bench
transcript. Do NOT paraphrase the upstream issue thread (Rule 13). Do
NOT ship a runnable reproducer alongside this file (Rule 10).

The model receives this file and nothing else.
"""

_CAND_PROBE_STUB = """#!/usr/bin/env bash
# probe.sh — independent probe. Runs in the model-facing working
# directory. Exit 0 iff the bug is fixed; exit non-zero otherwise.
# Rule 10: this is the grader's probe, promoted to a candidate-level
# artifact so `bse difficulty-check` can use it.
#
# FILL IN the probe logic. Example shapes:
#   pytest -q                                  # for python fixes
#   python -c 'import x; assert x.behaves()'   # for one-shot checks

set -euo pipefail
echo "probe.sh not yet implemented for {name}" >&2
exit 2
"""

_CAND_MAKE_DIRS_STUB = """#!/usr/bin/env bash
# make-eval-dirs.sh — populate the model-facing working directory.
# Contract (Gate 2): receives <destdir> as $1; must copy in every file
# the model may see, and NOTHING that would leak the fix (no probe.sh,
# no grader, no rubric, no reproducer). Rule 10.
#
# The `bse difficulty-check` driver invokes this once per session.

set -euo pipefail
DEST="${{1:?usage: make-eval-dirs.sh <destdir>}}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

mkdir -p "$DEST"
cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"

# FILL IN: install pinned deps, copy source under test, seed any fixtures.
# Example:
#   python3.12 -m venv "$DEST/.venv"
#   "$DEST/.venv/bin/pip" install "the-package==X.Y.Z"

echo "TODO: {name} make-eval-dirs.sh not yet complete" >&2
exit 2
"""


def _cmd_scaffold_candidate(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    """Stamp out a compliant candidate skeleton under eval-tasks/<name>/."""
    name: str = args.name
    if not name.replace("-", "").replace("_", "").isalnum():
        print(
            f"error: candidate name {name!r} must be alnum + '-' / '_' only.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    target = Path("eval-tasks") / name
    if target.exists():
        print(
            f"error: {target} already exists. Move it aside or pick another name.",
            file=sys.stderr,
        )
        return EXIT_ALREADY_EXISTS

    signer = args.signer or "FILL_YOUR_NAME"
    target.mkdir(parents=True)
    affidavit_stub = {
        "schema_version": "2",
        "pinned_commit": "FILL_IN_40_CHAR_LOWERCASE_HEX_SHA_HERE_XXXXX",
        "repo_url": "https://github.com/FILL_OWNER/FILL_REPO",
        "upstream_issue_url": "https://github.com/FILL_OWNER/FILL_REPO/issues/FILL_N",
        "bench_transcript_path": "bench.cast",
        "observed_behaviour": _STUB_OBSERVED_GUIDANCE,
        "divergence_from_thread": _STUB_DIVERGENCE_GUIDANCE,
        "upstream_status": "open",
        "signed_by": signer,
        "signed_at": "FILL_ISO_8601_TIMESTAMP_e.g._2026-08-06T14:32:00Z",
    }
    (target / AFFIDAVIT_FILENAME).write_text(
        json.dumps(affidavit_stub, indent=2) + "\n", encoding="utf-8"
    )
    (target / "initial-prompt.md").write_text(
        _CAND_INITIAL_PROMPT_STUB.format(name=name), encoding="utf-8"
    )
    probe = target / "probe.sh"
    probe.write_text(_CAND_PROBE_STUB.format(name=name), encoding="utf-8")
    probe.chmod(probe.stat().st_mode | 0o111)
    mked = target / "make-eval-dirs.sh"
    mked.write_text(_CAND_MAKE_DIRS_STUB.format(name=name), encoding="utf-8")
    mked.chmod(mked.stat().st_mode | 0o111)

    print(f"→ scaffolded {target}/")
    print("  Next steps:")
    print(f"    1. Fill in {target}/repro-affidavit.json (pin, URLs, observed_behaviour)")
    print(f"    2. Record bench: asciinema rec {target}/bench.cast")
    print(f"    3. Implement {target}/make-eval-dirs.sh and {target}/probe.sh")
    print(f"    4. Write {target}/initial-prompt.md in your own words")
    print(f"    5. bse affidavit {target}")
    print(f"    6. bse difficulty-check {target}")
    print(f"    7. bse writeup-audit {target}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse writeup-audit <candidate-dir>` — Gate 3 of the sourcing gates.
#
# Fetches the affidavit-linked upstream issue (live or snapshot), extracts
# every ≥ 8-word contiguous phrase from the candidate's writeup files, and
# flags any that appear verbatim in the upstream text. See upgrade-plan.md
# §4 Gate 3 and rules.md Rule 13.
# ---------------------------------------------------------------------------
def _cmd_writeup_audit(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    candidate_dir = Path(args.candidate_dir)
    if not candidate_dir.is_dir():
        print(f"error: {candidate_dir} is not a directory.", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = run_writeup_audit(
            candidate_dir,
            write_report=not args.no_report,
            fetch_live=not args.snapshot_only,
        )
    except WriteupAuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_WRITEUP_PRECONDITION

    print(report.to_text(), end="")
    if not args.no_report:
        print(f"→ wrote {candidate_dir / AUDIT_REPORT_FILENAME}")
    if not report.passed:
        return EXIT_WRITEUP_REJECT
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse diff <plugin> --a X.Y.Z --b X.Y.Z` — cross-version differential runner.
#
# The highest-yield unsaturated discovery axis (upgrade-plan.md §7 T1.1).
# Two operating modes:
#
#   In-process:  --a and --b are pip versions; pip-install and run discovery
#                twice in this process, then diff the two dicts of reports.
#   File-based:  --a-report and --b-report are paths to existing report.json
#                files produced by earlier `bse run` invocations; diff them
#                without re-running discovery.
#
# Exits:
#   EXIT_OK               — diff produced, NO changes detected between versions
#   EXIT_DIFF_HAS_CHANGES — diff produced, changes exist (surface for triage)
#   EXIT_DIFF_PRECONDITION — plugin unknown / install failed / bad report path
#
# "Changes exist" is NOT a reject — the operator decides whether any row is
# actually shippable. Diff is the finding; per upgrade-plan.md §7, nothing
# here auto-packages anything.
# ---------------------------------------------------------------------------
def _cmd_diff(args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    if args.a_report or args.b_report:
        return _cmd_diff_file_mode(args)
    return _cmd_diff_in_process(args, manifests)


def _cmd_diff_file_mode(args: argparse.Namespace) -> int:
    if not (args.a_report and args.b_report):
        print(
            "error: --a-report and --b-report must both be provided in file mode.",
            file=sys.stderr,
        )
        return EXIT_DIFF_PRECONDITION
    path_a = Path(args.a_report)
    path_b = Path(args.b_report)
    for p in (path_a, path_b):
        if not p.is_file():
            print(f"error: {p} does not exist or is not a regular file.", file=sys.stderr)
            return EXIT_DIFF_PRECONDITION
    try:
        layers_a = load_report_json(path_a)
        layers_b = load_report_json(path_b)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DIFF_PRECONDITION
    diff = diff_report_dicts(
        layers_a,
        layers_b,
        target_a=args.target_a or str(path_a),
        target_b=args.target_b or str(path_b),
    )
    return _emit_diff(args, diff)


def _cmd_diff_in_process(args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    name = args.plugin
    if not name:
        print(
            "error: `bse diff <plugin>` requires a plugin name in in-process mode.",
            file=sys.stderr,
        )
        return EXIT_DIFF_PRECONDITION
    if name not in manifests:
        print(f"error: unknown plugin {name!r}. Try 'bse list'.", file=sys.stderr)
        return EXIT_DIFF_PRECONDITION
    if not (args.a and args.b):
        print(
            "error: --a and --b (two version strings) are required in in-process mode.",
            file=sys.stderr,
        )
        return EXIT_DIFF_PRECONDITION

    manifest = manifests[name]
    reports_a = _run_discovery_at_version(manifest, args.a, args)
    if reports_a is None:
        return EXIT_DIFF_PRECONDITION
    reports_b = _run_discovery_at_version(manifest, args.b, args)
    if reports_b is None:
        return EXIT_DIFF_PRECONDITION

    diff = diff_reports(
        reports_a,
        reports_b,
        target_a=manifest.default_target_commit(args.a),
        target_b=manifest.default_target_commit(args.b),
    )
    return _emit_diff(args, diff)


def _run_discovery_at_version(
    manifest: Manifest, version: str, args: argparse.Namespace
) -> dict[str, Report] | None:
    """Pip-install ``version`` then run discovery. Returns None on install failure."""
    if not args.no_install:
        rc = _pip_install_at_version(manifest, version)
        if rc != EXIT_OK:
            return None
    target_commit = manifest.default_target_commit(version)
    plugin = manifest.plugin_factory(manifest.default_app_factory)
    variants = manifest.variants or None
    variant_factory = (lambda af: manifest.plugin_factory(af)) if variants is not None else None
    return run_discovery(
        plugin=plugin,
        target_commit=target_commit,
        iterations_l1=args.iterations,
        rounds_l2=args.rounds_l2,
        rounds_l3=args.rounds_l3,
        variants=variants,
        variant_plugin_factory=variant_factory,
    )


def _emit_diff(args: argparse.Namespace, diff: DiffReport) -> int:
    """Write the diff report and pick the exit code from `has_changes`."""
    out_dir = Path(args.out) if args.out else Path("reports/diff")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / DIFF_REPORT_FILENAME
    out_path.write_text(diff.to_json() + "\n", encoding="utf-8")
    print(f"→ wrote {out_path}")
    print(f"  {diff.target_a} → {diff.target_b}: {diff.summary_line()}")
    return EXIT_DIFF_HAS_CHANGES if diff.has_changes else EXIT_OK


# ---------------------------------------------------------------------------
# `bse triage <candidate-dir>` — Gate 4 of the sourcing gates (divergence probe).
#
# Spawns N=3 headless `claude -p` diagnosis sessions in sealed tmpdirs,
# reads each session's diagnosis.json, and clusters by shared normalised-
# word overlap. ≥ 2 clusters = DIVERGENT (proceed). 1 cluster = CONVERGENT
# (reject). See upgrade-plan.md §6.
# ---------------------------------------------------------------------------
def _cmd_triage(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    candidate_dir = Path(args.candidate_dir)
    try:
        report = run_divergence_probe(
            candidate_dir,
            claude_bin=args.claude_bin,
            write_report=not args.no_report,
        )
    except DivergenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DIVERGENCE_PRECONDITION

    print(report.to_text(), end="")
    if not args.no_report:
        print(f"→ wrote {candidate_dir / TRIAGE_REPORT_FILENAME}")
    if not report.passed:
        return EXIT_DIVERGENCE_REJECT
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse concurrency-matrix <plugin>` — T1.2, upgrade-plan.md §7.
#
# Run discovery under every concurrency mode the plugin declares and diff
# across modes. Divergence between modes is the finding — the operator
# inspects mode-matrix.json and (if warranted) runs `bse scaffold-candidate`
# on the interesting rows. Not a reject — divergence is *desired* output.
# ---------------------------------------------------------------------------
def _cmd_concurrency_matrix(args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    name: str = args.name
    if name not in manifests:
        print(f"error: unknown plugin {name!r}. Try 'bse list'.", file=sys.stderr)
        return EXIT_MODE_MATRIX_PRECONDITION

    manifest = manifests[name]
    version: str | None = args.version
    if version is not None and not args.no_install:
        rc = _pip_install_at_version(manifest, version)
        if rc != EXIT_OK:
            return EXIT_MODE_MATRIX_PRECONDITION

    target_commit = manifest.default_target_commit(version)
    plugin = manifest.plugin_factory(manifest.default_app_factory)

    modes: tuple[str, ...] | None = tuple(args.modes.split(",")) if args.modes else None

    try:
        matrix = run_concurrency_matrix(
            plugin=plugin,
            target_commit=target_commit,
            modes=modes,
            iterations_l1=args.iterations,
            rounds_l2=args.rounds_l2,
            rounds_l3=args.rounds_l3,
        )
    except ModeMatrixError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_MODE_MATRIX_PRECONDITION

    out_dir = Path(args.out) if args.out else Path("reports/concurrency-matrix") / target_commit
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / MODE_MATRIX_FILENAME
    out_path.write_text(matrix.to_json() + "\n", encoding="utf-8")
    print(f"→ wrote {out_path}")
    print(f"  {matrix.summary_line()}")
    return EXIT_MODE_MATRIX_HAS_DIVERGENCE if matrix.has_divergence else EXIT_OK


# ---------------------------------------------------------------------------
# `bse teardown-fuzz <plugin>` — T1.3, upgrade-plan.md §7.
#
# Enumerate permutations of the plugin's teardown-hook ordering (up to
# TEARDOWN_MAX_HOOKS = 4). Orders whose behaviour diverges from the
# canonical order surface as findings. Not a reject.
# ---------------------------------------------------------------------------
def _cmd_teardown_fuzz(args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    name: str = args.name
    if name not in manifests:
        print(f"error: unknown plugin {name!r}. Try 'bse list'.", file=sys.stderr)
        return EXIT_TEARDOWN_PRECONDITION

    manifest = manifests[name]
    version: str | None = args.version
    if version is not None and not args.no_install:
        rc = _pip_install_at_version(manifest, version)
        if rc != EXIT_OK:
            return EXIT_TEARDOWN_PRECONDITION

    target_commit = manifest.default_target_commit(version)
    plugin = manifest.plugin_factory(manifest.default_app_factory)

    try:
        report = run_teardown_fuzzer(plugin=plugin, target_commit=target_commit)
    except TeardownFuzzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TEARDOWN_PRECONDITION

    out_dir = Path(args.out) if args.out else Path("reports/teardown-fuzz") / target_commit
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / TEARDOWN_FUZZ_FILENAME
    out_path.write_text(report.to_json() + "\n", encoding="utf-8")
    print(f"→ wrote {out_path}")
    print(f"  {report.summary_line()}")
    return EXIT_TEARDOWN_HAS_DIVERGENCE if report.has_divergence else EXIT_OK


# ---------------------------------------------------------------------------
# `bse fault-matrix <plugin>` — T1.4, upgrade-plan.md §7.
#
# Multiplier on existing invariants. Client-disconnect / cancel /
# background-exception faults reveal state-desync bugs (litestar #3772
# shape) that a clean-probe sweep never touches. Divergence between
# faults is the finding.
# ---------------------------------------------------------------------------
def _cmd_fault_matrix(args: argparse.Namespace, manifests: Mapping[str, Manifest]) -> int:
    name: str = args.name
    if name not in manifests:
        print(f"error: unknown plugin {name!r}. Try 'bse list'.", file=sys.stderr)
        return EXIT_FAULT_MATRIX_PRECONDITION

    manifest = manifests[name]
    version: str | None = args.version
    if version is not None and not args.no_install:
        rc = _pip_install_at_version(manifest, version)
        if rc != EXIT_OK:
            return EXIT_FAULT_MATRIX_PRECONDITION

    target_commit = manifest.default_target_commit(version)
    plugin = manifest.plugin_factory(manifest.default_app_factory)

    faults: tuple[str, ...] | None = tuple(args.faults.split(",")) if args.faults else None

    try:
        matrix = run_fault_matrix(
            plugin=plugin,
            target_commit=target_commit,
            faults=faults,
            iterations_l1=args.iterations,
            rounds_l2=args.rounds_l2,
            rounds_l3=args.rounds_l3,
        )
    except FaultMatrixError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAULT_MATRIX_PRECONDITION

    out_dir = Path(args.out) if args.out else Path("reports/fault-matrix") / target_commit
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / FAULT_MATRIX_FILENAME
    out_path.write_text(matrix.to_json() + "\n", encoding="utf-8")
    print(f"→ wrote {out_path}")
    print(f"  {matrix.summary_line()}")
    return EXIT_FAULT_MATRIX_HAS_DIVERGENCE if matrix.has_divergence else EXIT_OK


# ---------------------------------------------------------------------------
# `bse validate-grader <candidate-dir>` — T3.1, upgrade-plan.md §8.
#
# Drive the candidate's grade.py against a manifest of expected outcomes:
# baseline (must FAIL), canonical-fix (must PASS), N mutated buggy trees
# (must FAIL). Rejects graders that key on implementation details of the
# canonical fix rather than on the fix itself.
# ---------------------------------------------------------------------------
def _cmd_validate_grader(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    candidate_dir = Path(args.candidate_dir)
    try:
        report = run_grader_validation(candidate_dir)
    except GraderValidatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_GRADER_VALIDATION_PRECONDITION

    if not args.no_report:
        out_path = candidate_dir / GRADER_VALIDATION_FILENAME.replace(".json", "-report.json")
        out_path.write_text(report.to_json() + "\n", encoding="utf-8")
        print(f"→ wrote {out_path}")
    print(f"  {report.summary_line()}")
    for inv in report.invocations:
        verdict = "OK" if inv.matched_expected else "MISMATCH"
        print(f"  [{verdict}] {inv.label:<20s} expected={inv.expected:<5s} exit={inv.exit_code}")
    if not report.passed:
        return EXIT_GRADER_VALIDATION_REJECT
    return EXIT_OK


# ---------------------------------------------------------------------------
# `bse verify-repro <candidate-dir>` — T3.2, upgrade-plan.md §8.
#
# Ephemeral venv, install the affidavit's pin, run reproduce.sh, assert
# baseline still FAILs. Intended to run nightly via cron — see
# scripts/nightly-verify-repro.sh for the pattern.
# ---------------------------------------------------------------------------
def _cmd_verify_repro(args: argparse.Namespace, _manifests: Mapping[str, Manifest]) -> int:
    candidate_dir = Path(args.candidate_dir)
    try:
        report = run_repro_verification(
            candidate_dir,
            pinned_package=args.pinned_package,
            pinned_version=args.pinned_version,
            keep_workdir=args.keep_workdir,
        )
    except ReproVerifierError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REPRO_VERIFIER_PRECONDITION

    if not args.no_report:
        out_path = candidate_dir / REPRO_VERIFICATION_FILENAME
        out_path.write_text(report.to_json() + "\n", encoding="utf-8")
        print(f"→ wrote {out_path}")
    print(f"  {report.summary_line()}")
    if not report.still_reproducible:
        return EXIT_REPRO_VERIFIER_NO_LONGER_REPRODUCIBLE
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argparse plumbing.
# ---------------------------------------------------------------------------
def _add_concurrency_matrix_subparser(
    subs: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = subs.add_parser(
        "concurrency-matrix",
        help=(
            "Run discovery under every concurrency mode the plugin exposes "
            "and diff across modes (T1.2, upgrade-plan.md §7)."
        ),
    )
    p.add_argument("name", help="Plugin name (see 'bse list').")
    p.add_argument("--version", default=None, help="Pin the target pip package to this version.")
    p.add_argument(
        "--modes",
        default=None,
        help=(
            "Comma-separated modes to run. Omit for all available modes "
            "the plugin declares. Unknown mode = precondition failure."
        ),
    )
    p.add_argument(
        "--iterations", type=int, default=DEFAULT_ITERATIONS_L1, help="Layer 1 iterations per mode."
    )
    p.add_argument(
        "--rounds-l2", type=int, default=DEFAULT_ROUNDS_L2, help="Layer 2 rounds per mode."
    )
    p.add_argument(
        "--rounds-l3", type=int, default=DEFAULT_ROUNDS_L3, help="Layer 3 rounds per mode."
    )
    p.add_argument("--out", default=None, help="Output directory. Default under reports/.")
    p.add_argument(
        "--no-install", action="store_true", help="Skip pip install even if --version is given."
    )


def _add_fault_matrix_subparser(subs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subs.add_parser(
        "fault-matrix",
        help=(
            "Run discovery under every fault the plugin exposes "
            "and diff across faults (T1.4, upgrade-plan.md §7)."
        ),
    )
    p.add_argument("name", help="Plugin name (see 'bse list').")
    p.add_argument("--version", default=None, help="Pin the target pip package to this version.")
    p.add_argument(
        "--faults",
        default=None,
        help=(
            "Comma-separated faults to run. Omit for every available fault "
            "the plugin declares. Unknown fault = precondition failure."
        ),
    )
    p.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS_L1,
        help="Layer 1 iterations per fault.",
    )
    p.add_argument(
        "--rounds-l2", type=int, default=DEFAULT_ROUNDS_L2, help="Layer 2 rounds per fault."
    )
    p.add_argument(
        "--rounds-l3", type=int, default=DEFAULT_ROUNDS_L3, help="Layer 3 rounds per fault."
    )
    p.add_argument("--out", default=None, help="Output directory. Default under reports/.")
    p.add_argument(
        "--no-install", action="store_true", help="Skip pip install even if --version is given."
    )


def _add_validate_grader_subparser(
    subs: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = subs.add_parser(
        "validate-grader",
        help=(
            "Drive the candidate's grade.py against a validation manifest; "
            "assert PASS on canonical fix + FAIL on baseline + FAIL on ≥3 "
            "mutated buggy variants (T3.1, upgrade-plan.md §8)."
        ),
    )
    p.add_argument(
        "candidate_dir",
        help=(
            "Path to the candidate directory (must contain grade.py "
            f"and {GRADER_VALIDATION_FILENAME})."
        ),
    )
    p.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write the report file; print outcome only.",
    )


def _add_verify_repro_subparser(
    subs: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = subs.add_parser(
        "verify-repro",
        help=(
            "Ephemeral venv, install the affidavit's pin, run reproduce.sh; "
            "confirm baseline still FAILs (T3.2, upgrade-plan.md §8)."
        ),
    )
    p.add_argument(
        "candidate_dir",
        help=(
            "Path to the candidate directory (must contain "
            "repro-affidavit.json and reproduce.sh)."
        ),
    )
    p.add_argument(
        "--pinned-package",
        required=True,
        help="pypi package name to install (the plugin's manifest usually names this).",
    )
    p.add_argument(
        "--pinned-version",
        default=None,
        help=(
            "PEP-440 version string. Omit to derive from the affidavit's "
            "pinned_commit (strips a leading 'v')."
        ),
    )
    p.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Leave the tmp workdir on disk for post-mortem inspection.",
    )
    p.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write repro-verification.json; print outcome only.",
    )


def _add_teardown_fuzz_subparser(subs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subs.add_parser(
        "teardown-fuzz",
        help=(
            "Enumerate every permutation of the plugin's teardown hooks "
            "(bounded at 4! = 24) and flag divergent orders (T1.3)."
        ),
    )
    p.add_argument("name", help="Plugin name (see 'bse list').")
    p.add_argument("--version", default=None, help="Pin the target pip package to this version.")
    p.add_argument("--out", default=None, help="Output directory. Default under reports/.")
    p.add_argument(
        "--no-install", action="store_true", help="Skip pip install even if --version is given."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bse",
        description="backend-stress-eval CLI. See manual.md.",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    subs.add_parser("list", help="Show every discovered plugin.")

    p_run = subs.add_parser("run", help="Run the full discovery sweep against a plugin.")
    p_run.add_argument("name", help="Plugin name (see 'bse list').")
    p_run.add_argument(
        "--version",
        default=None,
        help="Pin the target pip package to this version.",
    )
    p_run.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS_L1,
        help=f"Layer 1 iterations (default {DEFAULT_ITERATIONS_L1}).",
    )
    p_run.add_argument(
        "--rounds-l2",
        type=int,
        default=DEFAULT_ROUNDS_L2,
        help=f"Layer 2 rounds (default {DEFAULT_ROUNDS_L2}).",
    )
    p_run.add_argument(
        "--rounds-l3",
        type=int,
        default=DEFAULT_ROUNDS_L3,
        help=f"Layer 3 rounds per variant (default {DEFAULT_ROUNDS_L3}).",
    )
    p_run.add_argument(
        "--out",
        default=None,
        help="Output directory. Default: reports/discovery/<target>.",
    )
    p_run.add_argument(
        "--no-install",
        action="store_true",
        help="Skip pip install even if --version is given.",
    )

    p_install = subs.add_parser("install", help="Scaffold a new plugin under plugins/<name>/.")
    p_install.add_argument("name", help="Plugin package name (must be a python identifier).")

    p_aff = subs.add_parser(
        "affidavit",
        help=(
            "Validate the repro-affidavit under a candidate dir "
            "(Gate 1 of the sourcing gates; see upgrade-plan.md)."
        ),
    )
    p_aff.add_argument(
        "candidate_dir",
        help=("Path to the candidate directory containing " f"{AFFIDAVIT_FILENAME}."),
    )

    p_diff = subs.add_parser(
        "difficulty-check",
        help=(
            f"Drive N={DIFFICULTY_N_ATTEMPTS} headless sessions and reject if "
            f"median time-to-fix < {DIFFICULTY_MIN_MINUTES:.0f} min "
            "(Gate 2 of the sourcing gates)."
        ),
    )
    p_diff.add_argument(
        "candidate_dir",
        help=(
            "Path to the candidate directory containing initial-prompt.md, "
            "probe.sh (executable), and make-eval-dirs.sh (executable)."
        ),
    )
    p_diff.add_argument(
        "--claude-bin",
        default="claude",
        help="Path or PATH-name of the claude CLI (default 'claude').",
    )
    p_diff.add_argument(
        "--no-ledger",
        action="store_true",
        help=(
            "Do not append sessions to difficulty-attempts.jsonl. "
            "Intended for dry-runs and tests."
        ),
    )

    p_sc = subs.add_parser(
        "scaffold-candidate",
        help=(
            "Stamp out a compliant candidate skeleton under eval-tasks/<name>/. "
            "See manual.md and rules.md Rules 11-13 for the contract."
        ),
    )
    p_sc.add_argument(
        "name",
        help="Candidate directory name (alnum, '-', '_' only).",
    )
    p_sc.add_argument(
        "--signer",
        default=None,
        help="Prefill the affidavit's signed_by field.",
    )

    p_wa = subs.add_parser(
        "writeup-audit",
        help=(
            "Diff a candidate's writeup files against the upstream issue and "
            "flag verbatim overlaps (Gate 3 of the sourcing gates)."
        ),
    )
    p_wa.add_argument(
        "candidate_dir",
        help="Path to the candidate directory (containing a signed affidavit).",
    )
    p_wa.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write writeup-audit.txt (dry-run mode).",
    )
    p_wa.add_argument(
        "--snapshot-only",
        action="store_true",
        help=(
            "Skip the live GitHub fetch; audit against the committed "
            "upstream-issue-snapshot.txt. Fails if no snapshot exists."
        ),
    )

    p_di = subs.add_parser(
        "diff",
        help=(
            "Diff a plugin's discovery report across two versions "
            "(cross-version differential runner; Chunk E)."
        ),
    )
    p_di.add_argument(
        "plugin",
        nargs="?",
        default=None,
        help="Plugin name (see 'bse list'). Omit in file mode (--a-report/--b-report).",
    )
    p_di.add_argument("--a", default=None, help="Version A (pip version string).")
    p_di.add_argument("--b", default=None, help="Version B (pip version string).")
    p_di.add_argument(
        "--a-report",
        default=None,
        help="Path to an already-produced report.json for the A side (file mode).",
    )
    p_di.add_argument(
        "--b-report",
        default=None,
        help="Path to an already-produced report.json for the B side (file mode).",
    )
    p_di.add_argument(
        "--target-a",
        default=None,
        help="Human label for the A side in the diff report (file mode; default: path).",
    )
    p_di.add_argument(
        "--target-b",
        default=None,
        help="Human label for the B side in the diff report (file mode; default: path).",
    )
    p_di.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS_L1,
        help=f"Layer 1 iterations per side (default {DEFAULT_ITERATIONS_L1}).",
    )
    p_di.add_argument(
        "--rounds-l2",
        type=int,
        default=DEFAULT_ROUNDS_L2,
        help=f"Layer 2 rounds per side (default {DEFAULT_ROUNDS_L2}).",
    )
    p_di.add_argument(
        "--rounds-l3",
        type=int,
        default=DEFAULT_ROUNDS_L3,
        help=f"Layer 3 rounds per side (default {DEFAULT_ROUNDS_L3}).",
    )
    p_di.add_argument(
        "--out",
        default=None,
        help="Output directory. Default: reports/diff.",
    )
    p_di.add_argument(
        "--no-install",
        action="store_true",
        help="Skip pip installs; use whatever version is currently installed.",
    )

    _add_concurrency_matrix_subparser(subs)
    _add_fault_matrix_subparser(subs)
    _add_teardown_fuzz_subparser(subs)
    _add_validate_grader_subparser(subs)
    _add_verify_repro_subparser(subs)

    p_tr = subs.add_parser(
        "triage",
        help=(
            "Divergence probe: spawn N=3 diagnosis sessions and reject "
            "if they all converge on one root cause (Gate 4)."
        ),
    )
    p_tr.add_argument(
        "candidate_dir",
        help="Path to the candidate directory containing a signed affidavit.",
    )
    p_tr.add_argument(
        "--claude-bin",
        default="claude",
        help="Path or PATH-name of the claude CLI (default 'claude').",
    )
    p_tr.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write triage-report.json (dry-run mode).",
    )

    return parser


_DISPATCH: Mapping[str, Callable[[argparse.Namespace, Mapping[str, Manifest]], int]] = (
    MappingProxyType(
        {
            "list": _cmd_list,
            "run": _cmd_run,
            "install": _cmd_install,
            "affidavit": _cmd_affidavit,
            "concurrency-matrix": _cmd_concurrency_matrix,
            "difficulty-check": _cmd_difficulty,
            "diff": _cmd_diff,
            "fault-matrix": _cmd_fault_matrix,
            "scaffold-candidate": _cmd_scaffold_candidate,
            "teardown-fuzz": _cmd_teardown_fuzz,
            "triage": _cmd_triage,
            "validate-grader": _cmd_validate_grader,
            "verify-repro": _cmd_verify_repro,
            "writeup-audit": _cmd_writeup_audit,
        }
    )
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    manifests = load_manifests()
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    return handler(args, manifests)


if __name__ == "__main__":
    raise SystemExit(main())
