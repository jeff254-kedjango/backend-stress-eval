"""Framework-agnostic invariants that work against any state exposing a
duck-typed structural interface.

Currently just :class:`RouteRegistryStable` — asserts that a plugin's routing
signature does not change across a run. That is Layer-2 (lifecycle) territory:
a well-behaved app must expose exactly the same routes after each rebuild.

Design notes:

* No framework imports. The invariant reads ``state.route_signature`` — any
  object with that attribute works.
* Baseline is captured once at :func:`Runner.setup` from the initial state's
  ``route_signature``. Later checks compare exactly.
* Rule 1: comparison is O(len(routes)). Routes are bounded per app; a run
  does not cause routes to grow, so this is effectively O(1) at iteration
  scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.invariant import CheckResult, Ok, Violation

__all__ = [
    "HasResponseDigest",
    "HasRouteSignature",
    "ResponseDeterminism",
    "RouteRegistryStable",
]


class HasRouteSignature(Protocol):
    """Structural protocol: anything with a ``route_signature`` tuple works."""

    @property
    def route_signature(self) -> tuple[str, ...]: ...


class HasResponseDigest(Protocol):
    """Structural protocol: state exposes an optional response digest.

    ``None`` means "no probe response was captured this iteration" — the
    invariant treats that as :class:`Ok`. The caller (typically a harness
    state producer) is responsible for producing a stable digest from the
    response bytes, e.g. ``hashlib.sha256(response.content).hexdigest()``.
    """

    @property
    def response_digest(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class RouteRegistryStable:
    """The route signature must be equal to the baseline captured at setup.

    A grading-friendly Violation carries baseline + current + the symmetric
    difference (routes added, routes removed) so a report reader can point
    at the exact drift.
    """

    name: str = "route_registry_stable"

    def setup(self, state: HasRouteSignature, /) -> tuple[str, ...]:
        return state.route_signature

    def check(
        self, state: HasRouteSignature, baseline: tuple[str, ...], iteration: int, /
    ) -> CheckResult:
        current = state.route_signature
        if current == baseline:
            return Ok(invariant_name=self.name)
        baseline_set = frozenset(baseline)
        current_set = frozenset(current)
        added = sorted(current_set - baseline_set)
        removed = sorted(baseline_set - current_set)
        return Violation(
            invariant_name=self.name,
            detail=(f"route registry drifted: +{len(added)} added, -{len(removed)} removed"),
            evidence={
                "added": list(added),
                "baseline_count": len(baseline),
                "current_count": len(current),
                "removed": list(removed),
            },
            iteration=iteration,
        )


@dataclass(frozen=True, slots=True)
class ResponseDeterminism:
    """Repeated identical requests should return identical responses.

    Baseline: the first non-None digest observed at :meth:`setup`. If
    ``setup`` sees ``None`` (no probe fired for the baseline), the
    invariant is effectively disabled — the ``check`` method treats a
    ``None`` baseline as "not yet observed" and returns :class:`Ok` while
    remembering the first non-None digest as the *effective* baseline on
    the next check.

    Because :class:`core.runner.Runner` stores baselines by value and
    passes them positionally, we cannot mutate baseline after setup. To
    keep this invariant a *pure* value type, callers must ensure their
    state_producer produces a real digest at iteration ``-1`` (baseline
    capture). Documented explicitly per Rule 5.
    """

    name: str = "response_determinism"

    def setup(self, state: HasResponseDigest, /) -> str | None:
        return state.response_digest

    def check(
        self, state: HasResponseDigest, baseline: str | None, iteration: int, /
    ) -> CheckResult:
        current = state.response_digest
        if baseline is None or current is None:
            # No baseline captured OR no digest this iteration — skip check.
            return Ok(invariant_name=self.name)
        if current == baseline:
            return Ok(invariant_name=self.name)
        return Violation(
            invariant_name=self.name,
            detail="response digest changed from baseline",
            evidence={
                "baseline_digest": baseline,
                "current_digest": current,
            },
            iteration=iteration,
        )
