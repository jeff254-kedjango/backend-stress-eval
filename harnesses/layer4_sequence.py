"""Layer 4 — sequence harness.

Executes an ordered list of :class:`LayerStep` s against one live app.
Between steps the harness samples metrics and (optionally) a response
digest, feeding a :class:`HarnessState` to each step's declared invariants.

We do NOT reuse :class:`core.sequence.Step` directly — its ``action`` is a
generic state → state transform. Layer-4 steps are more specific: they
receive the *app* (the caller wants to issue requests, mutate state), and
the harness takes care of sampling around them. Wrapping keeps the caller's
mental model clean.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.invariant import InvariantRegistry
from core.metrics import sample
from core.plugin import Plugin
from core.reporter import Report, ReportMetadata
from core.sequence import Sequence, Step
from harnesses import (
    FdReturnToBaselineOnHarnessState,
    HarnessState,
    ResponseDeterminismOnHarnessState,
    RssReturnToBaselineOnHarnessState,
)

__all__ = ["LayerStep", "run_layer4_sequence"]


@dataclass(frozen=True, slots=True)
class LayerStep:
    """One step in a Layer-4 sequence.

    ``action(app)`` mutates the app / issues a request; the harness handles
    sampling around it. ``invariants`` is a tuple of registered names to
    evaluate against the resulting :class:`HarnessState`.
    """

    name: str
    action: Callable[[Any], None]
    invariants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError(f"LayerStep.name must be non-empty and trimmed; got {self.name!r}")


def _default_registry() -> InvariantRegistry:
    reg = InvariantRegistry()
    reg.register(RssReturnToBaselineOnHarnessState())
    reg.register(FdReturnToBaselineOnHarnessState())
    reg.register(ResponseDeterminismOnHarnessState())
    return reg


def run_layer4_sequence(
    *,
    plugin: Plugin[Any, Any],
    steps: tuple[LayerStep, ...],
    response_digest_of: Callable[[Any], str | None],
    target_commit: str,
    seed: int = 0,
    harness_version: str = "0.0.1",
    registry: InvariantRegistry | None = None,
) -> Report:
    """Run ``steps`` in order against a fresh live app.

    Baselines are captured at :meth:`Sequence.run` from an initial
    :class:`HarnessState` snapshot (before any step runs). The
    ``response_digest_of`` callable should return a stable digest at
    baseline time so :class:`ResponseDeterminism` has something to
    compare against.
    """
    if not steps:
        raise ValueError("steps must not be empty")

    reg = registry if registry is not None else _default_registry()

    app = plugin.build_app()
    plugin.lifecycle_start(app)

    def _snapshot() -> HarnessState:
        return HarnessState(
            sample=sample(),
            route_signature=(),
            response_digest=response_digest_of(app),
        )

    initial_state = _snapshot()

    # Adapt LayerStep to core.sequence.Step. The wrapped action ignores the
    # incoming ``prev_state`` (Sequence passes the previous return value) and
    # instead calls the caller's app-oriented action, then snapshots.
    def _wrap(layer_step: LayerStep) -> Step:
        def _action(_prev_state: object) -> object:
            layer_step.action(app)
            return _snapshot()

        return Step(name=layer_step.name, action=_action, invariants=layer_step.invariants)

    wrapped = tuple(_wrap(s) for s in steps)

    try:
        result = Sequence(steps=wrapped, registry=reg).run(initial_state=initial_state)
    finally:
        plugin.lifecycle_stop(app)

    return Report(
        metadata=ReportMetadata(
            target=plugin.name,
            target_commit=target_commit,
            seed=seed,
            iterations_requested=len(steps),
            harness_version=harness_version,
        ),
        result=result,
    )
