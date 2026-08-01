"""Layer 1 — repetition harness.

Fires the same ``request_callable`` ``iterations`` times against one
long-lived app. Between requests the harness samples :class:`Sample`
metrics; the registered invariants observe drift.

Framework-shape-agnostic: the caller supplies the plugin and the request
callable, and this module just composes them. Routes cannot change during
a single-app run so ``route_signature`` is left empty.
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
    RssReturnToBaselineOnHarnessState,
)

__all__ = ["run_layer1_repetition"]


def _default_registry() -> InvariantRegistry:
    """Default invariants for Layer 1 — memory and FD returns to baseline."""
    reg = InvariantRegistry()
    reg.register(RssReturnToBaselineOnHarnessState())
    reg.register(FdReturnToBaselineOnHarnessState())
    return reg


def run_layer1_repetition(
    *,
    plugin: Plugin[Any, Any],
    request_callable: Callable[[object], None],
    iterations: int,
    target_commit: str,
    seed: int = 0,
    harness_version: str = "0.0.1",
    registry: InvariantRegistry | None = None,
) -> Report:
    """Drive ``iterations`` requests against one app; return a byte-stable Report.

    ``request_callable`` receives the client returned by ``plugin.client(app)``
    and is responsible for issuing exactly one probe request. The harness
    handles metric sampling and invariant evaluation.
    """
    if iterations < 1:
        raise ValueError(f"iterations must be >= 1, got {iterations}")

    app = plugin.build_app()
    plugin.lifecycle_start(app)
    client = plugin.client(app)

    reg = registry if registry is not None else _default_registry()

    def state_producer(iteration: int) -> object:
        if iteration >= 0:
            request_callable(client)
        return HarnessState(sample=sample(), route_signature=())

    try:
        result = Runner(reg, state_producer, iterations=iterations).run()
    finally:
        plugin.lifecycle_stop(app)

    return Report(
        metadata=ReportMetadata(
            target=plugin.name,
            target_commit=target_commit,
            seed=seed,
            iterations_requested=iterations,
            harness_version=harness_version,
        ),
        result=result,
    )
