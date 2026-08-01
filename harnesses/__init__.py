"""Harness composition helpers.

A *harness* is a thin composition of :mod:`core.runner` +
:mod:`core.invariant` + a plugin — no new abstraction. This package exposes
the compound state the two Layer-1/Layer-2 harnesses share and adapter
invariants that read what they need from that compound state.

See ``discovery-strategy.md`` §9 (Layers 1..5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from core.framework_invariants import ResponseDeterminism, RouteRegistryStable
from core.invariant import CheckResult, JsonValue, Ok, Violation
from core.metrics import Sample
from core.runner import Cadence, RunResult

__all__ = [
    "FdReturnToBaselineOnHarnessState",
    "HarnessState",
    "ResponseDeterminismOnHarnessState",
    "RouteRegistryStableOnHarnessState",
    "RssReturnToBaselineOnHarnessState",
    "RssSlopeBoundedOnHarnessState",
    "collapse_repeated_violations",
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
    # ``rss_trajectory`` — populated by the harness ONLY on the final
    # iteration (empty tuple otherwise). Enables end-only slope invariants to
    # see the full RSS history without paying tuple-copy cost on every
    # iteration. Rule 1: per-iteration cost stays O(1); the O(N) copy fires
    # exactly once at the boundary.
    rss_trajectory: tuple[int, ...] = ()


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


# ---------------------------------------------------------------------------
# Slope-based RSS invariant. End-only cadence: fires exactly once, at the
# end of a run, over the whole recorded trajectory. Catches slow drips that
# never cross the fixed threshold of :class:`RssReturnToBaselineOnHarnessState`
# but do exhibit a persistent positive slope (real leaks).
# ---------------------------------------------------------------------------
_MIN_SLOPE_FIT_POINTS = 50
"""Below this many recorded samples we cannot compute a meaningful slope. We
report :class:`Ok` rather than producing a low-confidence Violation. Fifty is
chosen empirically: with a real framework's ~1-2 KB/iter allocator noise, 20
samples yields spurious slopes above 1 KB/iter (measured on FastAPI 0.141.1),
whereas 50 samples smooths the noise floor enough for a 1 KB/iter default
limit to only fire on true leaks. Short unit-test runs (typically 5-20 rounds)
stay silent by design."""


@dataclass(frozen=True, slots=True)
class RssSlopeBoundedOnHarnessState:
    """RSS-per-iteration slope must not exceed ``max_kb_per_iter``.

    Rationale: a slow lifecycle leak (e.g. FastAPI 0.141.1's ~9 KB/iter Python-
    heap growth) never trips a fixed-slack threshold cleanly — with slack N,
    the drift crosses at iteration ~N/slope, which just picks the alarm's
    firing point. A slope invariant is scale-free: it decides "does RSS grow
    proportionally to iterations?" — that is the real Layer-5 property.

    Cadence: end-only. The invariant reads
    :attr:`HarnessState.rss_trajectory`, which the harness populates on the
    final iteration. Rule 1: fit is O(N) but runs once at the boundary; per-
    iteration cost stays O(1).

    Rule 5 note: mypy-friendly dataclass, ``cadence`` exposed for
    :class:`core.runner.HasCadence` structural check.
    """

    max_kb_per_iter: float = 1.0
    name: str = "rss_slope_bounded"
    cadence: Cadence = field(default_factory=lambda: Cadence(end_only=True))

    def setup(self, state: HarnessState, /) -> int:
        # Baseline RSS is captured for provenance in the Violation evidence
        # (so a grader can see "started at 44928 KB, slope +9 KB/iter").
        return state.sample.rss_kb

    def check(self, state: HarnessState, baseline: int, iteration: int, /) -> CheckResult:
        trajectory = state.rss_trajectory
        n = len(trajectory)
        if n < _MIN_SLOPE_FIT_POINTS:
            # Rule 5: fail *safe* rather than reporting a low-confidence slope.
            # A short run legitimately cannot be judged; the threshold
            # invariant is still active and would have fired on catastrophic
            # growth.
            return Ok(invariant_name=self.name)
        slope_kb_per_iter, intercept_kb, r_squared = _linear_fit(trajectory)
        if slope_kb_per_iter <= self.max_kb_per_iter:
            return Ok(invariant_name=self.name)
        # Slope KB/iter is a float; round to 4 places for stable JSON output
        # (byte-stability contract — see ``core/reporter.py`` design notes).
        slope_rounded = round(slope_kb_per_iter, 4)
        r2_rounded = round(r_squared, 4)
        intercept_rounded = round(intercept_kb, 2)
        return Violation(
            invariant_name=self.name,
            detail=(
                f"RSS grows +{slope_rounded} KB/iter (limit {self.max_kb_per_iter}); "
                f"linear fit R²={r2_rounded} over {n} samples"
            ),
            evidence={
                "baseline_kb": baseline,
                "final_kb": trajectory[-1],
                "samples": n,
                "slope_kb_per_iter": slope_rounded,
                "intercept_kb": intercept_rounded,
                "r_squared": r2_rounded,
                "max_kb_per_iter": self.max_kb_per_iter,
            },
            iteration=iteration,
        )


def _linear_fit(trajectory: tuple[int, ...]) -> tuple[float, float, float]:
    """Least-squares linear regression of ``y = slope * i + intercept``.

    Returns ``(slope, intercept, r_squared)``. Input is per-iteration RSS
    samples; ``i`` is the iteration index (0-based). Rule 1: O(N) single
    pass, no allocations beyond a handful of accumulators. Rule 5: pure
    function — determinism trivially provable.

    Callers guarantee ``len(trajectory) >= _MIN_SLOPE_FIT_POINTS`` (checked
    by :class:`RssSlopeBoundedOnHarnessState.check` before calling), so the
    denominator ``sum((x-mx)^2)`` is strictly positive.
    """
    n = len(trajectory)
    # Sum-of-x and sum-of-y in one pass.
    sum_x = 0
    sum_y = 0
    for i, y in enumerate(trajectory):
        sum_x += i
        sum_y += y
    mean_x = sum_x / n
    mean_y = sum_y / n
    # Sum-of-squares in one more pass (kept separate for numerical clarity;
    # combining introduces catastrophic cancellation with small variances).
    ss_xy = 0.0
    ss_xx = 0.0
    ss_yy = 0.0
    for i, y in enumerate(trajectory):
        dx = i - mean_x
        dy = y - mean_y
        ss_xy += dx * dy
        ss_xx += dx * dx
        ss_yy += dy * dy
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    # R² — coefficient of determination. If ss_yy == 0 the trajectory is
    # flat; slope is 0 and R² is undefined — report 1.0 (perfect flat fit).
    r_squared = (ss_xy * ss_xy) / (ss_xx * ss_yy) if ss_yy > 0 else 1.0
    return slope, intercept, r_squared


# ---------------------------------------------------------------------------
# Reporting helper — collapse repeated same-invariant violations into one.
# ---------------------------------------------------------------------------
# Rationale: a slow lifecycle leak (e.g. FastAPI 0.141.1's ~9 KB/iter Python-
# heap growth) triggers ``rss_return_to_baseline`` on hundreds of consecutive
# iterations. All of them describe the *same* underlying bug. Emitting 418
# violation rows makes the grading report read like 418 bugs, which hurts the
# eval signal. This helper keeps the first Violation seen per invariant name
# and folds a small aggregate into its ``evidence`` — enough to reconstruct
# the trajectory shape without inflating the wire payload (Rule 1: single
# linear scan; Rule 5: fold is transparent and named).
_COLLAPSE_SUMMARY_KEY = "collapsed"


def _extract_int(evidence: Mapping[str, JsonValue], key: str) -> int | None:
    """Return ``evidence[key]`` iff it is an int, else ``None``.

    Kept private and defensive so evidence shapes that don't carry a numeric
    drift field are simply ignored during folding — the primary Violation
    still survives, just without a ``max_drift`` summary.
    """
    value = evidence.get(key)
    return value if isinstance(value, int) else None


def collapse_repeated_violations(result: RunResult) -> RunResult:
    """Return a new :class:`RunResult` with at most one Violation per invariant.

    For each invariant name, the *earliest* Violation is retained (so
    ``iteration`` points at when the property first broke) and its
    ``evidence`` gains a ``collapsed`` sub-mapping::

        {"count": N, "first_iteration": i0, "last_iteration": iN,
         "max_drift_kb": <if numeric>, "max_drift": <if numeric non-KB>}

    ``success``, ``iterations_completed`` and ``invariants_evaluated`` are
    preserved unchanged — this is a reporting-shape transform only, not a
    re-evaluation. Byte-stable per Rule 9: same inputs → same output.
    """
    if not result.violations:
        return result

    # Single linear scan (Rule 1: O(N_violations)).
    first_by_name: dict[str, Violation] = {}
    stats: dict[str, dict[str, int]] = {}
    for v in result.violations:
        name = v.invariant_name
        drift_kb = _extract_int(v.evidence, "drift_kb")
        drift_generic = _extract_int(v.evidence, "drift")
        iteration = v.iteration if v.iteration is not None else -1
        if name not in first_by_name:
            first_by_name[name] = v
            stats[name] = {
                "count": 1,
                "first_iteration": iteration,
                "last_iteration": iteration,
            }
            if drift_kb is not None:
                stats[name]["max_drift_kb"] = drift_kb
            if drift_generic is not None:
                stats[name]["max_drift"] = drift_generic
            continue
        s = stats[name]
        s["count"] += 1
        s["last_iteration"] = iteration
        if drift_kb is not None:
            s["max_drift_kb"] = max(s.get("max_drift_kb", drift_kb), drift_kb)
        if drift_generic is not None:
            s["max_drift"] = max(s.get("max_drift", drift_generic), drift_generic)

    collapsed: list[Violation] = []
    for name, first in first_by_name.items():
        merged: dict[str, JsonValue] = dict(first.evidence)
        # Preserve existing keys; add a namespaced summary so grading readers
        # can spot the fold without evidence key collisions.
        merged[_COLLAPSE_SUMMARY_KEY] = dict(stats[name])
        collapsed.append(replace(first, evidence=merged))

    return replace(result, violations=tuple(collapsed))
