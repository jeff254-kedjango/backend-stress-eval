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
    RssSlopeBoundedOnHarnessState,
    collapse_repeated_violations,
)

__all__ = ["run_layer1_repetition"]


def _default_registry() -> InvariantRegistry:
    """Default invariants for Layer 1.

    Layer 1 exercises one long-lived app under repetition. RSS drift here is
    a per-*request* leak (as opposed to Layer 2's per-*lifecycle* leak), so
    both fixed-threshold and slope-based invariants make sense — a slow
    per-request leak may never trip the threshold but exhibits a persistent
    positive slope (§9 Layer 5's "memory returns to baseline" property).
    """
    reg = InvariantRegistry()
    reg.register(RssReturnToBaselineOnHarnessState())
    reg.register(RssSlopeBoundedOnHarnessState())
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

    # Per-iteration RSS accumulator; only exposed on the final iteration via
    # ``HarnessState.rss_trajectory`` — end-only slope invariants read it
    # there. Rule 1: append is O(1); the O(N) tuple copy fires once at
    # iteration == iterations - 1.
    trajectory_rss_kb: list[int] = []
    final_iteration = iterations - 1

    def state_producer(iteration: int) -> object:
        if iteration >= 0:
            request_callable(client)
        snapshot_sample = sample()
        if iteration >= 0:
            trajectory_rss_kb.append(snapshot_sample.rss_kb)
        rss_trajectory: tuple[int, ...] = (
            tuple(trajectory_rss_kb) if iteration == final_iteration else ()
        )
        return HarnessState(
            sample=snapshot_sample,
            route_signature=(),
            rss_trajectory=rss_trajectory,
        )

    try:
        raw_result = Runner(reg, state_producer, iterations=iterations).run()
    finally:
        plugin.lifecycle_stop(app)
    # Fold repeated same-invariant violations for parity with Layer 2 — a
    # slow per-request leak that trips the threshold on every iteration
    # should surface as ONE row, not N. See collapse_repeated_violations.
    result = collapse_repeated_violations(raw_result)

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
