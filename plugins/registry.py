"""Plugin registry — auto-discovers ``plugins/<name>/manifest.py``.

Rule 4 (no dead code) + Rule 6 (small chunks): the CLI does NOT hardcode a
list of plugins. It walks ``plugins/`` at import time and picks up any
subpackage that exposes a ``MANIFEST`` object matching :class:`Manifest`.

Adding a new framework is therefore ONE file — ``plugins/<name>/manifest.py``
— plus the plugin implementation itself (typically ``plugins/<name>/__init__.py``).
Neither the CLI nor the registry needs to change.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from core.plugin import Plugin

__all__ = ["Manifest", "load_manifests"]


@dataclass(frozen=True, slots=True)
class Manifest:
    """Declarative metadata for one plugin.

    * ``name`` — short kebab/snake identifier (``fastapi``, ``celery``). Must
      match the plugin package directory name.
    * ``description`` — one-line human summary shown by ``bse list``.
    * ``pip_packages`` — the pip distribution names the plugin needs at
      runtime. The CLI's ``install`` / ``run --version`` subcommands install
      or pin these. Example: ``("fastapi", "starlette", "httpx2")``.
    * ``version_package`` — which of ``pip_packages`` carries the "target
      version" concept — the one ``--version X.Y.Z`` pins. Falls back to
      ``pip_packages[0]``.
    * ``plugin_factory(app_factory)`` — build a fully-constructed
      :class:`core.plugin.Plugin` from a caller-supplied app factory.
    * ``default_app_factory`` — canonical example app used when the caller
      of ``bse run`` doesn't specify one.
    * ``variants`` — optional Layer-3 variant list. Each entry is
      ``(name, app_factory)``. If empty, ``bse run`` skips Layer 3.
    * ``default_target_commit(version)`` — how to compute the target-commit
      label baked into the report metadata. Defaults to ``f"{name}-{version}"``.
    """

    name: str
    description: str
    pip_packages: tuple[str, ...]
    plugin_factory: Callable[[Callable[[], Any]], Plugin[Any, Any]]
    default_app_factory: Callable[[], Any]
    variants: tuple[tuple[str, Callable[[], Any]], ...] = ()
    version_package: str | None = None

    def resolve_version_package(self) -> str | None:
        """Which pip package carries the version concept.

        Returns ``None`` for plugins with zero runtime deps (e.g. the stub) —
        the CLI treats that as "no version pin possible; report label is the
        plugin name plus 'installed'".
        """
        if self.version_package is not None:
            return self.version_package
        if not self.pip_packages:
            return None
        return self.pip_packages[0]

    def default_target_commit(self, version: str | None) -> str:
        v = version or "installed"
        return f"{self.name}-{v}"


def load_manifests() -> Mapping[str, Manifest]:
    """Walk ``plugins/`` and return a name → Manifest mapping.

    A plugin package is any subpackage of ``plugins`` that exposes a
    top-level ``MANIFEST: Manifest``. Malformed or missing manifests are
    silently skipped — the CLI reports the empty list rather than crashing.
    Rule 5: fail-loud where the user can act (``bse run <unknown>``), not
    at import time.
    """
    manifests: dict[str, Manifest] = {}
    # Walk the ``plugins`` package's own subpackages.
    import plugins as _plugins_pkg

    for module_info in pkgutil.iter_modules(_plugins_pkg.__path__):
        if not module_info.ispkg:
            continue
        pkg_name = module_info.name
        try:
            manifest_module = importlib.import_module(f"plugins.{pkg_name}.manifest")
        except ModuleNotFoundError:
            continue
        manifest = getattr(manifest_module, "MANIFEST", None)
        if not isinstance(manifest, Manifest):
            continue
        if manifest.name != pkg_name:
            # Mismatch is a plugin author bug; surface once but don't crash.
            continue
        manifests[pkg_name] = manifest
    # Return a read-only view so the CLI can't mutate the cache.
    return MappingProxyType(manifests)
