"""Plugin protocol — the bridge from framework-agnostic ``core`` to an
ecosystem-specific adapter (``plugins/fastapi``, ``plugins/django``, ...).

Core knows nothing about any web framework. A plugin implements this small
surface, and the harness composes it with the framework-agnostic runner,
sequence, and reporter modules.

Design notes (see ``discovery-strategy.md`` §Decision 6 + ``rules.md``):

* Plugins are declared with :class:`typing.Protocol` (structural typing) —
  same idiom as :mod:`core.invariant`. No inheritance chain required; if it
  looks like a plugin, it *is* a plugin.
* Sync surface only. Frameworks with async setup provide sync facades. Keeps
  the runner loop synchronous and deterministic.
* Both TypeVars are **invariant** — ``App`` and ``Client`` each appear in
  both parameter and return position (measured against mypy --strict, same
  lesson as Chunk 2).
* Rule 1: every method is O(1) with respect to iteration count — plugins
  must not accumulate state across ``reset`` calls.

Refactor 2026-08-01 ("R1"): three helper methods hoisted out of the layer
harnesses into the plugin surface — ``probe``, ``route_signature``,
``response_digest``. Previously each layer took these as callables passed
by the harness caller; that meant every new framework had to duplicate a
copy of ``discovery.py``. With them on the plugin, ``run_discovery``
becomes a single generic function and adding a framework is one file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, Protocol, TypeVar, runtime_checkable

__all__ = ["App", "Client_co", "Plugin"]


# ---------------------------------------------------------------------------
# TypeVars.
#
# Variance analysis (mypy --strict enforced):
# * ``App`` appears in BOTH parameter position (client/lifecycle_*/reset/
#   route_signature/response_digest) AND return position (build_app) →
#   invariant.
# * ``Client`` appears in parameter position (probe) AND return position
#   (client) → *invariant*, despite the ``_co`` suffix left in place for
#   backwards compatibility with anything importing the name.
# ---------------------------------------------------------------------------
App = TypeVar("App")
# NOTE: ``Client_co`` is kept as the exported name for compatibility, but
# because ``probe`` now consumes a Client, it must be invariant. ``covariant``
# was a Chunk-6 mistake; R1 corrects it. Downstream code that imported the
# name still works — variance is a type-checker concept, not a runtime one.
Client_co = TypeVar("Client_co")  # noqa: PLC0105  # historical name kept, invariant now


# ---------------------------------------------------------------------------
# The Protocol.
# ---------------------------------------------------------------------------
@runtime_checkable
class Plugin(Protocol, Generic[App, Client_co]):
    """Structural protocol for ecosystem adapters.

    Implementations must expose:

    * ``name`` — stable identifier used in :class:`core.reporter.ReportMetadata`
      as ``target``. Kebab or snake case; no framework-specific characters.
    * ``build_app() -> App`` — construct a fresh app instance. **Called once
      per lifecycle iteration** in the runner. Must be deterministic — the
      same call with the same environment produces an app whose behaviour
      is bit-equal.
    * ``client(app) -> Client`` — return a request-issuing client bound to
      the given app. May be called multiple times per app.
    * ``lifecycle_start(app)`` — start the app's lifespan (startup events,
      background workers, etc.). No-op is allowed for stateless apps.
    * ``lifecycle_stop(app)`` — stop the app's lifespan (shutdown events,
      resource cleanup). Must be idempotent — the harness may call it
      multiple times during error recovery.
    * ``reset(app)`` — restore the app to a repeatable pre-request baseline
      between iterations (empty caches, reset counters). Used by Layer 2
      (lifecycle) testing. Must NOT leak state across calls (Rule 1: iteration
      cost stays constant).
    * ``feature_matrix() -> Mapping[str, bool]`` — which framework features
      this plugin exposes. Consumed by Layer 3 (feature-combination). Read-
      only; must not mutate plugin state.
    * ``probe(client)`` — issue exactly one canonical probe request. What
      "probe" means is framework-specific (``GET /`` for HTTP, ``send('noop')``
      for a task queue). Must not raise on success. On unexpected result the
      plugin should raise ``RuntimeError`` so the harness records a real
      failure rather than silent drift.
    * ``route_signature(app) -> tuple[str, ...]`` — a stable, sorted tuple
      of strings describing this app's registered operations (HTTP routes,
      registered task names, RPC method names). Feeds
      :class:`core.framework_invariants.RouteRegistryStable`.
    * ``response_digest(app) -> str | None`` — a stable hash of the probe
      response, or ``None`` if this plugin does not expose a probe response
      (e.g. a fire-and-forget task queue). Feeds
      :class:`core.framework_invariants.ResponseDeterminism`.
    """

    @property
    def name(self) -> str: ...

    def build_app(self) -> App: ...

    def client(self, app: App, /) -> Client_co: ...

    def lifecycle_start(self, app: App, /) -> None: ...

    def lifecycle_stop(self, app: App, /) -> None: ...

    def reset(self, app: App, /) -> None: ...

    def feature_matrix(self) -> Mapping[str, bool]: ...

    def probe(self, client: Client_co, /) -> None: ...

    def route_signature(self, app: App, /) -> tuple[str, ...]: ...

    def response_digest(self, app: App, /) -> str | None: ...
