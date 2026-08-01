"""Harness composition helpers.

A *harness* is a thin composition of :mod:`core.runner` +
:mod:`core.invariant` + a plugin — no new abstraction. This package exposes
the compound state the two Layer-1/Layer-2 harnesses share and adapter
invariants that read what they need from that compound state.

See ``discovery-strategy.md`` §9 (Layers 1..5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.framework_invariants import ResponseDeterminism, RouteRegistryStable
from core.invariant import CheckResult, Ok, Violation
from core.metrics import Sample

__all__ = [
    "FdReturnToBaselineOnHarnessState",
    "HarnessState",
    "ResponseDeterminismOnHarnessState",
    "RouteRegistryStableOnHarnessState",
    "RssReturnToBaselineOnHarnessState",
]


# ---------------------------------------------------------------------------
# Compound state fed to the Runner from every harness layer.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HarnessState:
    """State passed to :func:`core.runner.Runner` inside a harness.

    ``sample`` is the current :class:`core.metrics.Sample`. ``route_signature``
    is a tuple of route path+method strings, or ``()`` if the harness does
    not track routes (Layer 1 exercises one long-lived app, so its routes
    cannot change; the field is present but empty). ``response_digest`` is
    an optional hash of the probe response — ``None`` disables the
    determinism check for this iteration.
    """

    sample: Sample
    route_signature: tuple[str, ...]
    response_digest: str | None = None


# ---------------------------------------------------------------------------
# Adapter invariants. Each reads the field it needs and delegates to an
# underlying framework-agnostic invariant. Kept as thin classes rather than
# a generic adapter helper for mypy-friendliness and Rule 5 clarity.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RssReturnToBaselineOnHarnessState:
    """RSS-drift invariant against a :class:`HarnessState`. Delegates to the
    same rule as :class:`core.metrics.RssReturnToBaseline`."""

    slack_kb: int = 1024
    name: str = "rss_return_to_baseline"

    def setup(self, state: HarnessState, /) -> int:
        return state.sample.rss_kb

    def check(self, state: HarnessState, baseline: int, iteration: int, /) -> CheckResult:
        drift = state.sample.rss_kb - baseline
        if drift > self.slack_kb:
            return Violation(
                invariant_name=self.name,
                detail=f"RSS drifted +{drift} KB above baseline (slack {self.slack_kb} KB)",
                evidence={
                    "baseline_kb": baseline,
                    "current_kb": state.sample.rss_kb,
                    "drift_kb": drift,
                    "slack_kb": self.slack_kb,
                },
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


@dataclass(frozen=True, slots=True)
class FdReturnToBaselineOnHarnessState:
    """FD-drift invariant against a :class:`HarnessState`."""

    slack: int = 0
    name: str = "fd_return_to_baseline"

    def setup(self, state: HarnessState, /) -> int:
        return state.sample.fd_count

    def check(self, state: HarnessState, baseline: int, iteration: int, /) -> CheckResult:
        drift = state.sample.fd_count - baseline
        if drift > self.slack:
            return Violation(
                invariant_name=self.name,
                detail=f"FD count drifted +{drift} above baseline (slack {self.slack})",
                evidence={
                    "baseline_count": baseline,
                    "current_count": state.sample.fd_count,
                    "drift": drift,
                    "slack": self.slack,
                },
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


@dataclass(frozen=True, slots=True)
class RouteRegistryStableOnHarnessState:
    """Route-registry invariant against a :class:`HarnessState`.

    Wraps :class:`core.framework_invariants.RouteRegistryStable` — the
    framework-agnostic invariant works on any object exposing
    ``.route_signature``; here we forward :class:`HarnessState`'s field.
    """

    name: str = "route_registry_stable"
    # ``default_factory`` avoids RUF009 (function-call in dataclass default).
    # RouteRegistryStable is itself frozen so all instances are equivalent.
    _delegate: RouteRegistryStable = field(default_factory=RouteRegistryStable)

    def setup(self, state: HarnessState, /) -> tuple[str, ...]:
        # Shim structural-equivalent to what RouteRegistryStable expects.
        return state.route_signature

    def check(
        self, state: HarnessState, baseline: tuple[str, ...], iteration: int, /
    ) -> CheckResult:
        # Delegate to the framework-agnostic checker via a tiny proxy so we
        # keep exactly one implementation of the diffing logic (Rule 4).
        return self._delegate.check(_RouteView(state.route_signature), baseline, iteration)


@dataclass(frozen=True, slots=True)
class _RouteView:
    """Minimal proxy for :class:`core.framework_invariants.HasRouteSignature`."""

    route_signature: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResponseDeterminismOnHarnessState:
    """Wraps :class:`core.framework_invariants.ResponseDeterminism` to read
    the ``response_digest`` field off a :class:`HarnessState`."""

    name: str = "response_determinism"
    _delegate: ResponseDeterminism = field(default_factory=ResponseDeterminism)

    def setup(self, state: HarnessState, /) -> str | None:
        return state.response_digest

    def check(self, state: HarnessState, baseline: str | None, iteration: int, /) -> CheckResult:
        return self._delegate.check(_DigestView(state.response_digest), baseline, iteration)


@dataclass(frozen=True, slots=True)
class _DigestView:
    """Minimal proxy for :class:`core.framework_invariants.HasResponseDigest`."""

    response_digest: str | None
