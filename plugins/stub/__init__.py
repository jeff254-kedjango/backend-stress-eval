"""Stub plugin — the minimal adapter that proves the plugin ABC works end-to-end.

No framework, no HTTP, no I/O. A :class:`StubApp` is an in-memory counter that
:class:`StubClient` mutates via ``issue_request()``. The plugin satisfies
:class:`core.plugin.Plugin` structurally, which lets the framework-agnostic
runner drive all 5 layers on a toy — proving core does not reach into any
framework.

Also carries a ``planted_leak`` toggle: when set, ``issue_request()``
increments a ``leaked_kb`` counter that ``reset`` does NOT clear. This is
the deterministic bug fixture used in :mod:`tests.test_plugin_stub` to
prove an invariant fires reliably across 10 runs (Rule 9).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

__all__ = ["StubApp", "StubClient", "StubPlugin"]


# ---------------------------------------------------------------------------
# In-memory "app" + client. Frozen would defeat the point (we mutate) but we
# keep the surface tiny.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class StubApp:
    """Toy in-memory app.

    ``request_count`` is reset by :meth:`StubPlugin.reset`. ``leaked_kb`` is
    intentionally *not* reset when ``planted_leak`` is True — that's the
    deterministic bug the harness must detect.
    """

    request_count: int = 0
    leaked_kb: int = 0
    started: bool = False
    stopped: bool = False
    planted_leak: bool = False


@dataclass(slots=True)
class StubClient:
    """Request-issuing client bound to a :class:`StubApp`."""

    app: StubApp

    def issue_request(self) -> int:
        """Simulate one request. Returns the new request count.

        Rule 1: O(1). If ``planted_leak`` is set, adds 1 KB to ``leaked_kb``.
        """
        self.app.request_count += 1
        if self.app.planted_leak:
            self.app.leaked_kb += 1
        return self.app.request_count


# ---------------------------------------------------------------------------
# Plugin. Satisfies :class:`core.plugin.Plugin` [StubApp, StubClient] structurally.
# ---------------------------------------------------------------------------
_FEATURES: Final[Mapping[str, bool]] = MappingProxyType(
    {
        # A stub has none of the real framework features. The keys are the
        # canonical set the harness looks for so Layer 3 can enumerate.
        "dependency_injection": False,
        "middleware": False,
        "background_tasks": False,
        "streaming": False,
        "lifespan": True,  # we honour lifecycle_start / lifecycle_stop.
    }
)


@dataclass(slots=True)
class StubPlugin:
    """Minimal plugin. Optional ``planted_leak`` toggles the bug fixture."""

    planted_leak: bool = False
    name: str = "stub"
    _features: Mapping[str, bool] = field(default_factory=lambda: _FEATURES, init=False, repr=False)

    def build_app(self) -> StubApp:
        return StubApp(planted_leak=self.planted_leak)

    def client(self, app: StubApp, /) -> StubClient:
        return StubClient(app=app)

    def lifecycle_start(self, app: StubApp, /) -> None:
        # Idempotent — matches Protocol contract.
        app.started = True
        app.stopped = False

    def lifecycle_stop(self, app: StubApp, /) -> None:
        # Idempotent — safe to call twice.
        app.stopped = True

    def reset(self, app: StubApp, /) -> None:
        """Restore request-scoped state. Does NOT clear ``leaked_kb`` — that
        is the whole point of the planted-leak fixture."""
        app.request_count = 0

    def feature_matrix(self) -> Mapping[str, bool]:
        return self._features

    # ------------------------------------------------------------------
    # Three R1-hoisted methods.
    # ------------------------------------------------------------------
    def probe(self, client: StubClient, /) -> None:
        """Fire one canonical probe — issue a request against the stub."""
        client.issue_request()

    def route_signature(self, _app: StubApp, /) -> tuple[str, ...]:
        """The stub has one conceptual endpoint: ``ISSUE /request``."""
        return ("ISSUE /request",)

    def response_digest(self, app: StubApp, /) -> str | None:
        """Hash a deterministic summary of the app's *steady* state.

        Rule 5 clarity: this must return the SAME digest across iterations
        for a well-behaved app. A running counter (``request_count``) would
        drift by design, which is a stub-implementation bug, not the
        signal the harness is meant to detect. We therefore hash only
        ``leaked_kb`` — reset() preserves it deliberately, so a clean stub
        returns the same digest every time and a planted-leak stub drifts
        (which IS the deterministic bug we want the harness to catch).
        """
        payload = f"leak={app.leaked_kb}".encode()
        return hashlib.sha256(payload).hexdigest()
