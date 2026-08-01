"""Layer 2 — lifecycle harness.

Repeats ``build_app → lifecycle_start → probe request → lifecycle_stop``
``rounds`` times, sampling metrics and capturing the route signature after
each round. Registered invariants observe drift *across* the whole
sequence — this is where lifecycle leaks surface.

Framework-specific extraction:

* ``request_callable(client) -> None`` — how a single probe request is
  issued (framework-specific request shape).
* ``route_signature_of(app) -> tuple[str, ...]`` — how to project the
  app's route table into a stable tuple of strings. FastAPI supplies
  ``app.router.routes``; other frameworks differ; keep this off core.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.invariant import InvariantRegistry
from core.metrics import sample
from core.plugin import Plugin
from core.reporter import Report, ReportMetadata
from core.runner import Runner
from harnesses import (
    FdReturnToBaselineOnHarnessState,
    HarnessState,
    RouteRegistryStableOnHarnessState,
    RssReturnToBaselineOnHarnessState,
)

__all__ = ["run_layer2_lifecycle"]


def _default_registry() -> InvariantRegistry:
    """Default invariants for Layer 2."""
    reg = InvariantRegistry()
    reg.register(RssReturnToBaselineOnHarnessState())
    reg.register(FdReturnToBaselineOnHarnessState())
    reg.register(RouteRegistryStableOnHarnessState())
    return reg


def run_layer2_lifecycle(
    *,
    plugin: Plugin[Any, Any],
    request_callable: Callable[[object], None],
    route_signature_of: Callable[[Any], tuple[str, ...]],
    rounds: int,
    target_commit: str,
    seed: int = 0,
    harness_version: str = "0.0.1",
    registry: InvariantRegistry | None = None,
) -> Report:
    """Repeat build → start → probe → stop ``rounds`` times; return a Report.

    Each round builds a fresh app, so lifecycle leaks (module-level state that
    a shutdown handler fails to clean up) surface as drift in RSS / FDs /
    route signature.
    """
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")

    reg = registry if registry is not None else _default_registry()

    def state_producer(iteration: int) -> object:
        # Baseline (iteration == -1) uses a probe app that is fully torn
        # down before we return, so nothing lingers from setup.
        app = plugin.build_app()
        plugin.lifecycle_start(app)
        try:
            if iteration >= 0:
                client = plugin.client(app)
                request_callable(client)
            snapshot_sample = sample()
            snapshot_routes = route_signature_of(app)
        finally:
            plugin.lifecycle_stop(app)
        return HarnessState(sample=snapshot_sample, route_signature=snapshot_routes)

    result = Runner(reg, state_producer, iterations=rounds).run()

    return Report(
        metadata=ReportMetadata(
            target=plugin.name,
            target_commit=target_commit,
            seed=seed,
            iterations_requested=rounds,
            harness_version=harness_version,
        ),
        result=result,
    )
