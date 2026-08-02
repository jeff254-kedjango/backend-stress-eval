"""Manifest for the Starlette plugin. Auto-discovered by :mod:`plugins.registry`."""

from __future__ import annotations

from typing import Final

from plugins.registry import Manifest
from plugins.starlette import (
    StarlettePlugin,
    canonical_example_app,
    minimal_example_app,
)

__all__ = ["MANIFEST"]


MANIFEST: Final = Manifest(
    name="starlette",
    description="Starlette (no FastAPI) — raw ASGI routes, middleware, background tasks, lifespan.",
    pip_packages=("starlette", "httpx2"),
    version_package="starlette",
    plugin_factory=lambda app_factory: StarlettePlugin(app_factory=app_factory),
    default_app_factory=canonical_example_app,
    variants=(
        ("minimal", minimal_example_app),
        ("canonical", canonical_example_app),
    ),
)
