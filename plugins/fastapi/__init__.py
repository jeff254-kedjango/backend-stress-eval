"""FastAPI adapter — first real-framework plugin.

Implements :class:`core.plugin.Plugin` [FastAPI, TestClient] structurally,
using ``fastapi.testclient.TestClient`` as a sync facade over ASGI lifespan.

Design notes (see also ``discovery-strategy.md`` Decision 6, ``rules.md``):

* Sync surface — the runner is synchronous by design. ``TestClient`` runs
  the ASGI event loop internally; entering its context manager triggers
  ``lifespan`` startup, exiting triggers shutdown.
* Per-app client is memoised in a ``dict`` keyed by ``id(app)`` — O(1)
  lookup + one entry per live app. Cleared on ``lifecycle_stop``, so
  storage stays bounded.
* ``reset(app)`` clears ``dependency_overrides`` and rebuilds ``app.state``
  from scratch. It does NOT touch routes — those are declared at
  ``build_app`` time and would be a *new* app if replaced.
* ``feature_matrix()`` declares the framework features present. Layer 3
  (feature-combination testing) enumerates these to compose test matrices.
* Rule 1: every method is O(1). Rule 5: ``lifecycle_stop`` is idempotent —
  double-stop is a no-op, matching the Protocol contract.

Refactor R1 (2026-08-01): hoisted the four framework-specific helpers that
used to live in ``harnesses/discovery.py`` — ``probe`` (was
``_one_probe_request``), ``route_signature`` (was ``_fastapi_route_signature``),
``response_digest`` (was ``_digest_probe``), plus the two canonical example
factories (``canonical_example_app``, ``minimal_example_app``). Adding a new
framework now touches ONE file: ``plugins/<name>/__init__.py``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient
from starlette.datastructures import State

__all__ = [
    "FastAPIPlugin",
    "canonical_example_app",
    "minimal_example_app",
]


_FEATURES: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "dependency_injection": True,
        "middleware": True,
        "background_tasks": True,
        "streaming": True,
        "lifespan": True,
    }
)


_HTTP_OK: Final = 200


@dataclass(slots=True)
class FastAPIPlugin:
    """Adapter around a FastAPI app factory.

    Callers supply an ``app_factory`` — typically a small function that
    constructs and returns a fresh :class:`fastapi.FastAPI` instance. The
    plugin does NOT hold a single app across iterations; each
    :meth:`build_app` call invokes the factory anew, so lifecycle isolation
    is real, not simulated.

    A default factory is provided (:func:`canonical_example_app`) so callers
    who just want a discovery sweep don't have to supply one.
    """

    app_factory: Callable[[], FastAPI] = field(default=lambda: canonical_example_app())
    name: str = "fastapi"
    _clients: dict[int, TestClient] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------
    # Six core Plugin methods.
    # ------------------------------------------------------------------
    def build_app(self) -> FastAPI:
        """Construct a fresh FastAPI app via the injected factory. O(1)."""
        return self.app_factory()

    def client(self, app: FastAPI, /) -> TestClient:
        """Return a TestClient bound to ``app``.

        If :meth:`lifecycle_start` has not been called for this app, the
        client is eagerly created and its context manager entered — so
        callers who only need to fire a request don't need to remember
        the lifecycle dance.
        """
        key = id(app)
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        return self._enter(app, key)

    def lifecycle_start(self, app: FastAPI, /) -> None:
        """Enter the app's ASGI lifespan (fires FastAPI startup handlers)."""
        key = id(app)
        if key in self._clients:
            # Idempotent: already started.
            return
        self._enter(app, key)

    def lifecycle_stop(self, app: FastAPI, /) -> None:
        """Exit the app's ASGI lifespan (fires FastAPI shutdown handlers).

        Idempotent per the Protocol contract — safe to call on a stopped app.
        """
        key = id(app)
        client = self._clients.pop(key, None)
        if client is None:
            return
        # ``__exit__(None, None, None)`` runs the shutdown side of the lifespan.
        client.__exit__(None, None, None)

    def reset(self, app: FastAPI, /) -> None:
        """Restore request-scoped state to a clean baseline.

        Clears ``dependency_overrides`` and rebuilds ``app.state``. Does NOT
        touch routes, middleware, or the lifespan handler — those are part
        of the app's identity. If a plugin user needs a fresh identity they
        should call :meth:`build_app` again instead.
        """
        app.dependency_overrides.clear()
        app.state = State()

    def feature_matrix(self) -> Mapping[str, bool]:
        return _FEATURES

    # ------------------------------------------------------------------
    # Three R1-hoisted methods.
    # ------------------------------------------------------------------
    def probe(self, client: TestClient, /) -> None:
        """Fire one canonical probe request (``GET /``).

        Raises :class:`RuntimeError` on unexpected status so the harness
        records a real failure. Rule 5: no silent drift.
        """
        r = client.get("/")
        if r.status_code != _HTTP_OK:
            raise RuntimeError(f"probe returned {r.status_code}, expected {_HTTP_OK}")

    def route_signature(self, app: FastAPI, /) -> tuple[str, ...]:
        """Sorted tuple of ``METHOD PATH`` strings for every registered route.

        Feeds :class:`core.framework_invariants.RouteRegistryStable`. O(N_routes),
        which is bounded per app and does not grow across iterations.
        """
        sigs: list[str] = []
        for route in app.router.routes:
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)
            if methods is None or path is None:
                continue
            sigs.extend(f"{m} {path}" for m in methods)
        return tuple(sorted(sigs))

    def response_digest(self, app: FastAPI, /) -> str | None:
        """SHA-256 hex digest of the probe response body.

        Uses a fresh short-lived ``TestClient`` context so the digest fires
        the lifespan start/stop just like a real request. Returns ``None``
        only if the probe raised — the harness treats ``None`` as "no
        digest observed" (see :class:`ResponseDeterminism`).
        """
        try:
            with TestClient(app) as tc:
                r = tc.get("/")
        except Exception:
            # The harness must not crash on a bad app — a None digest disables
            # ResponseDeterminism for this iteration (documented invariant).
            return None
        return hashlib.sha256(r.content).hexdigest()

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------
    def _enter(self, app: FastAPI, key: int) -> TestClient:
        """Create a ``TestClient`` and run the lifespan startup. O(1)."""
        client = TestClient(app)
        client.__enter__()
        self._clients[key] = client
        return client


# ===========================================================================
# Canonical example apps — used by discovery when the caller supplies no
# ``app_factory``. Kept here (not in ``harnesses/``) so a framework's example
# apps live with the plugin that speaks that framework. Rule 4 — no dead code,
# and Rule 5 — clarity: one file to read per framework.
# ===========================================================================
def minimal_example_app() -> FastAPI:
    """A single-route app with a well-behaved lifespan. Layer-3 baseline."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, str]:
        return {"status": "ok"}

    return app


def canonical_example_app() -> FastAPI:
    """Minimal + dependency-injection + middleware. Responses stay
    deterministic; the harness compares SHA-256 across iterations."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def _hdr_middleware(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["x-harness"] = "on"
        return response

    def _magic_number() -> int:
        return 7

    @app.get("/")
    def _root() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/di")
    def _di(magic: int = Depends(_magic_number)) -> dict[str, int]:
        return {"magic": magic}

    return app
