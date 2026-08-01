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
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from harnesses.discovery import (
    DEFAULT_ITERATIONS_L1,
    DEFAULT_ROUNDS_L2,
    DEFAULT_ROUNDS_L3,
    run_discovery,
)
from harnesses.eval_task import package_eval_task
from plugins.registry import Manifest, load_manifests

__all__ = ["main"]


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNKNOWN_PLUGIN = 3
EXIT_INSTALL_FAILED = 4
EXIT_ALREADY_EXISTS = 5


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
# Argparse plumbing.
# ---------------------------------------------------------------------------
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    manifests = load_manifests()
    if args.cmd == "list":
        return _cmd_list(args, manifests)
    if args.cmd == "run":
        return _cmd_run(args, manifests)
    if args.cmd == "install":
        return _cmd_install(args, manifests)
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
