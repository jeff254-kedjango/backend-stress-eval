"""Manifest for the stub plugin. Auto-discovered by :mod:`plugins.registry`.

The stub is deliberately dependency-free — it's here so ``bse list`` and
the auto-discovery mechanism are testable without any framework installed.
"""

from __future__ import annotations

from typing import Final

from plugins.registry import Manifest
from plugins.stub import StubApp, StubPlugin

__all__ = ["MANIFEST"]


def _default_app() -> StubApp:
    """A well-behaved stub app (no planted leak)."""
    return StubApp()


MANIFEST: Final = Manifest(
    name="stub",
    description="In-memory fake framework — proves core drives a plugin end-to-end.",
    pip_packages=(),  # zero runtime deps
    plugin_factory=lambda _app_factory: StubPlugin(),
    default_app_factory=_default_app,
)
