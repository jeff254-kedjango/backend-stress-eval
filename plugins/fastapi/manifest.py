"""Manifest for the FastAPI plugin. Auto-discovered by :mod:`plugins.registry`."""

from __future__ import annotations

from typing import Final

from plugins.fastapi import (
    FastAPIPlugin,
    canonical_example_app,
    minimal_example_app,
)
from plugins.registry import Manifest

__all__ = ["MANIFEST"]


MANIFEST: Final = Manifest(
    name="fastapi",
    description="FastAPI + Starlette + httpx2 — HTTP request/response, ASGI lifespan.",
    pip_packages=("fastapi", "starlette", "httpx2"),
    version_package="fastapi",
    plugin_factory=lambda app_factory: FastAPIPlugin(app_factory=app_factory),
    default_app_factory=canonical_example_app,
    variants=(
        ("minimal", minimal_example_app),
        ("canonical", canonical_example_app),
    ),
)
