"""Starlette adapter — pure-Starlette plugin (no FastAPI layer).

Implements :class:`core.plugin.Plugin` [Starlette, TestClient] structurally,
using ``starlette.testclient.TestClient`` as a sync facade over the ASGI
lifespan. Mirrors ``plugins.fastapi`` exactly on the contract surface, but
speaks raw Starlette primitives (``Route``, ``BackgroundTask``, plain ASGI
middleware) so the hunt exercises a *thinner, less-picked-over* surface than
FastAPI — see ``investigations/l3-fastapi-interactions/findings.md``, which
found FastAPI 0.141.1 clean on route/response/teardown/cache invariants and
recommended Starlette as the next target.

Design notes (see ``discovery-strategy.md`` Decision 6, ``rules.md``):

* Sync surface — ``TestClient`` runs the ASGI event loop internally; entering
  its context manager fires ``lifespan`` startup, exiting fires shutdown.
* Per-app client memoised in a ``dict`` keyed by ``id(app)`` — O(1) lookup,
  one entry per live app, cleared on ``lifecycle_stop``. Rule 1.
* ``reset(app)`` rebuilds ``app.state`` only. Routes and middleware are part
  of the app's identity, declared at ``build_app`` time; replacing them would
  be a *new* app. Matches the FastAPI plugin's reset contract.
* Rule 5: ``lifecycle_stop`` is idempotent — double-stop is a no-op.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

__all__ = [
    "StarlettePlugin",
    "canonical_example_app",
    "minimal_example_app",
]


_FEATURES: Final[Mapping[str, bool]] = MappingProxyType(
    {
        "middleware": True,
        "background_tasks": True,
        "streaming": True,
        "lifespan": True,
        # No DI: raw Starlette has no dependency-injection system. Declaring
        # it False (rather than omitting it) keeps the matrix comparable with
        # the FastAPI plugin's feature_matrix so Layer 3 can diff surfaces.
        "dependency_injection": False,
    }
)


_HTTP_OK: Final = 200


@dataclass(slots=True)
class StarlettePlugin:
    """Adapter around a Starlette app factory.

    Callers supply an ``app_factory`` returning a fresh
    :class:`starlette.applications.Starlette`. Each :meth:`build_app` call
    invokes the factory anew — lifecycle isolation is real, not simulated.
    """

    app_factory: Callable[[], Starlette] = field(default=lambda: canonical_example_app())
    name: str = "starlette"
    _clients: dict[int, TestClient] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------
    # Six core Plugin methods.
    # ------------------------------------------------------------------
    def build_app(self) -> Starlette:
        """Construct a fresh Starlette app via the injected factory. O(1)."""
        return self.app_factory()

    def client(self, app: Starlette, /) -> TestClient:
        """Return a TestClient bound to ``app``, entering its lifespan if new."""
        key = id(app)
        existing = self._clients.get(key)
        if existing is not None:
            return existing
        return self._enter(app, key)

    def lifecycle_start(self, app: Starlette, /) -> None:
        """Enter the app's ASGI lifespan (fires startup handlers). Idempotent."""
        key = id(app)
        if key in self._clients:
            return
        self._enter(app, key)

    def lifecycle_stop(self, app: Starlette, /) -> None:
        """Exit the app's ASGI lifespan (fires shutdown handlers). Idempotent."""
        key = id(app)
        client = self._clients.pop(key, None)
        if client is None:
            return
        client.__exit__(None, None, None)

    def reset(self, app: Starlette, /) -> None:
        """Restore request-scoped state to a clean baseline.

        Rebuilds ``app.state``. Does NOT touch routes or middleware — those
        are the app's identity. Callers needing a fresh identity call
        :meth:`build_app` again.
        """
        app.state = State()

    def feature_matrix(self) -> Mapping[str, bool]:
        return _FEATURES

    # ------------------------------------------------------------------
    # Three R1-hoisted methods.
    # ------------------------------------------------------------------
    def probe(self, client: TestClient, /) -> None:
        """Fire one canonical probe request (``GET /``). Rule 5: no silent drift."""
        r = client.get("/")
        if r.status_code != _HTTP_OK:
            raise RuntimeError(f"probe returned {r.status_code}, expected {_HTTP_OK}")

    def route_signature(self, app: Starlette, /) -> tuple[str, ...]:
        """Sorted tuple of ``METHOD PATH`` strings for every registered route.

        Feeds :class:`core.framework_invariants.RouteRegistryStable`.
        O(N_routes), bounded per app and constant across iterations.
        """
        sigs: list[str] = []
        for route in app.router.routes:
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)
            if path is None:
                continue
            if methods is None:
                # Mounts / websocket routes have no ``methods``; record the path.
                sigs.append(f"* {path}")
                continue
            sigs.extend(f"{m} {path}" for m in methods)
        return tuple(sorted(sigs))

    def response_digest(self, app: Starlette, /) -> str | None:
        """SHA-256 hex digest of the probe response body, or ``None`` on error."""
        try:
            with TestClient(app) as tc:
                r = tc.get("/")
        except Exception:
            # A bad app must not crash the harness — None disables
            # ResponseDeterminism for this iteration (documented invariant).
            return None
        return hashlib.sha256(r.content).hexdigest()

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------
    def _enter(self, app: Starlette, key: int) -> TestClient:
        """Create a ``TestClient`` and run the lifespan startup. O(1)."""
        client = TestClient(app)
        client.__enter__()
        self._clients[key] = client
        return client


# ===========================================================================
# Canonical example apps. Kept with the plugin that speaks this framework.
# ===========================================================================
def minimal_example_app() -> Starlette:
    """A single-route app with a well-behaved lifespan. Layer-3 baseline."""

    async def _root(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield

    return Starlette(routes=[Route("/", _root)], lifespan=lifespan)


def canonical_example_app() -> Starlette:
    """Minimal + middleware + a background-task route. Responses stay
    deterministic; the harness compares SHA-256 across iterations."""

    async def _root(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def _bg(_request: Request) -> Response:
        # A route that schedules a background task after responding. The task
        # body is a no-op; we exercise the schedule/run/cleanup path, not a
        # side effect (Layer-2 must stay deterministic).
        async def _noop() -> None:
            return None

        return JSONResponse({"scheduled": True}, background=BackgroundTask(_noop))

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield

    async def _hdr_middleware(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["x-harness"] = "on"
        return response

    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    return Starlette(
        routes=[Route("/", _root), Route("/bg", _bg)],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=_hdr_middleware)],
        lifespan=lifespan,
    )
