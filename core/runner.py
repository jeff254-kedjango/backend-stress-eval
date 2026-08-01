"""Iteration runner + result shape shared with :mod:`core.sequence`.

Framework-agnostic. Given a :class:`InvariantRegistry` and a callable that
produces state for a given iteration index, :meth:`Runner.run` iterates ``N``
times, samples state per iteration, and evaluates each invariant on the
cadence it declares. Every :class:`core.invariant.Violation` seen is captured
in the returned :class:`RunResult`, whose shape is the grading contract
(Chunk 5's reporter serialises it verbatim).

Rule 9 (measure before theorize) governs the design: iteration-index-based
cadences make replays byte-identical under different CPU load — same inputs,
same output, always.

Rule 1: every hot-path step is O(1). Cadence dispatch is one dict lookup +
one integer mod. Baseline storage is a dict keyed by invariant name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

from core.invariant import (
    CheckResult,
    Invariant,
    InvariantRegistry,
    Violation,
)

__all__ = [
    "Cadence",
    "HasCadence",
    "RunResult",
    "Runner",
    "cadence_end_only",
    "cadence_every_iteration",
    "cadence_every_k",
]


# ---------------------------------------------------------------------------
# Cadence — how often an invariant fires. Immutable, hashable, JSON-safe.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Cadence:
    """When an invariant should fire.

    ``every`` is the iteration stride: ``1`` = every iteration, ``k`` = every
    k-th iteration (matched by ``iteration % k == 0``). ``end_only=True``
    fires exactly once, after the final iteration, and overrides ``every``.
    """

    every: int = 1
    end_only: bool = False

    def __post_init__(self) -> None:
        if self.every < 1:
            raise ValueError(f"Cadence.every must be >= 1, got {self.every}")


def cadence_every_iteration() -> Cadence:
    """Default cadence — fire on every iteration."""
    return Cadence(every=1)


def cadence_every_k(k: int) -> Cadence:
    """Fire every ``k``-th iteration (iterations 0, k, 2k, ...)."""
    return Cadence(every=k)


def cadence_end_only() -> Cadence:
    """Fire exactly once, after the final iteration."""
    return Cadence(end_only=True)


_DEFAULT_CADENCE: Final = Cadence(every=1)


@runtime_checkable
class HasCadence(Protocol):
    """Optional protocol — invariants may declare a ``cadence`` attribute.

    An invariant without a ``cadence`` is treated as :func:`cadence_every_iteration`.
    """

    @property
    def cadence(self) -> Cadence: ...


def _cadence_of(inv: Invariant[Any, Any]) -> Cadence:
    """Return the invariant's cadence, or the default. O(1)."""
    # getattr is O(1); we avoid ``isinstance(HasCadence)`` runtime cost
    # because Protocol checks scan attributes.
    return getattr(inv, "cadence", _DEFAULT_CADENCE)


def _should_fire(cadence: Cadence, iteration: int, is_final: bool) -> bool:
    """Return True iff the invariant should fire this iteration. O(1)."""
    if cadence.end_only:
        return is_final
    return iteration % cadence.every == 0


# ---------------------------------------------------------------------------
# Result shape. Frozen; the reporter (Chunk 5) serialises it directly.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of a run — same shape whether from :class:`Runner` or
    :class:`core.sequence.Sequence`.

    ``violations`` is in the order they were recorded (deterministic:
    iteration-ascending, then registry-insertion order within an iteration).
    ``invariants_evaluated`` is the registry order at run start.
    """

    success: bool
    iterations_completed: int
    violations: tuple[Violation, ...]
    invariants_evaluated: tuple[str, ...]


# ---------------------------------------------------------------------------
# Runner.
# ---------------------------------------------------------------------------
S = TypeVar("S")


@dataclass(slots=True)
class Runner:
    """Iterate a state producer, sampling invariants on their cadences.

    ``state_producer`` is called once with ``-1`` before iteration 0 to
    capture the baseline state (fed to each invariant's ``setup``), then
    with ``0..iterations-1`` inside the loop.
    """

    registry: InvariantRegistry
    state_producer: Callable[[int], object]
    iterations: int
    stop_on_first_violation: bool = False
    _baselines: dict[str, object] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.iterations < 1:
            raise ValueError(f"iterations must be >= 1, got {self.iterations}")

    # -- Rule 1: run() is O(iterations * n_invariants_scheduled_per_iter).
    #    Every per-step operation inside the loop is O(1).
    def run(self) -> RunResult:
        # 1) Baseline capture. Called once with -1 so the state producer can
        #    distinguish "warm-up" from real iterations if it cares.
        baseline_state = self.state_producer(-1)
        for inv in self.registry:
            self._baselines[inv.name] = inv.setup(baseline_state)

        # 2) Snapshot the registry order once — deterministic reporting.
        invariants_evaluated: tuple[str, ...] = tuple(inv.name for inv in self.registry)

        # 3) Main loop. Collect violations; keep the loop body tight.
        violations: list[Violation] = []
        completed = 0
        final_iteration = self.iterations - 1
        for i in range(self.iterations):
            state = self.state_producer(i)
            is_final = i == final_iteration
            hit_stop = self._evaluate_iteration(state, i, is_final, violations)
            completed = i + 1
            if hit_stop:
                break

        success = not violations
        return RunResult(
            success=success,
            iterations_completed=completed,
            violations=tuple(violations),
            invariants_evaluated=invariants_evaluated,
        )

    def _evaluate_iteration(
        self,
        state: object,
        iteration: int,
        is_final: bool,
        violations: list[Violation],
    ) -> bool:
        """Run every scheduled invariant for one iteration.

        Returns True iff caller should break the outer loop.
        """
        for inv in self.registry:
            cadence = _cadence_of(inv)
            if not _should_fire(cadence, iteration, is_final):
                continue
            baseline = self._baselines[inv.name]  # O(1)
            result: CheckResult = inv.check(state, baseline, iteration)
            if isinstance(result, Violation):
                violations.append(result)
                if self.stop_on_first_violation:
                    return True
            # else: result is Ok — Protocol guarantees CheckResult = Ok | Violation,
            # no runtime check needed. Silently discarding is intentional.
        return False
