"""Sequence — ordered, stateful steps with per-step invariants.

Complements :mod:`core.runner`. Where :class:`core.runner.Runner` repeats one
operation N times to find drift, a :class:`Sequence` executes a *history*
(login → create → delete → restore → logout) so we can find bugs that only
appear after a specific ordering — Layer 4 in ``discovery-strategy.md``.

Shared with the runner:
* Same :class:`core.runner.RunResult` shape (grading contract stable).
* Same "baselines captured once at run start" model — the sequence detects
  drift *across* the whole history, not per-step.

Framework-agnostic. Deterministic. Rule 1 hot path is O(steps * n_invariants).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.invariant import (
    CheckResult,
    InvariantRegistry,
    UnknownInvariantError,
    Violation,
)
from core.runner import RunResult

__all__ = ["Sequence", "Step"]


# ---------------------------------------------------------------------------
# Step. Frozen; ``action`` is a pure transform on state, ``invariants`` is a
# tuple of registered names (validated at run start, not per-step).
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Step:
    """One step in a :class:`Sequence`.

    ``action`` receives the current state and returns the next state.
    ``invariants`` are registered names to evaluate *after* this step runs.
    ``invariants`` may be empty (a state-mutation step with no checks).
    """

    name: str
    action: Callable[[object], object]
    invariants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise ValueError(f"Step.name must be a non-empty, trimmed string; got {self.name!r}")


# ---------------------------------------------------------------------------
# Sequence.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Sequence:
    """Ordered list of :class:`Step`\\ s sharing one :class:`InvariantRegistry`.

    Baselines are captured once at :meth:`run` start using the initial state;
    each step's declared invariants check drift relative to that shared
    baseline — same model as :class:`core.runner.Runner`.
    """

    steps: tuple[Step, ...]
    registry: InvariantRegistry
    stop_on_first_violation: bool = False
    _baselines: dict[str, object] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("Sequence.steps must not be empty")
        # Fail fast (Rule 3): every step's declared invariant must exist.
        # O(total_declared_names) — bounded by sum of step invariants, tiny.
        for step in self.steps:
            for name in step.invariants:
                if name not in self.registry:
                    raise UnknownInvariantError(
                        f"step {step.name!r} references invariant {name!r} "
                        "which is not registered"
                    )

    def run(self, initial_state: object) -> RunResult:
        # 1) Baselines — one setup call per invariant against ``initial_state``.
        for inv in self.registry:
            self._baselines[inv.name] = inv.setup(initial_state)

        invariants_evaluated: tuple[str, ...] = tuple(inv.name for inv in self.registry)

        # 2) Execute steps in declared order. Each step index doubles as the
        #    ``iteration`` value passed to invariant.check — deterministic
        #    provenance for the grading report.
        violations: list[Violation] = []
        state = initial_state
        completed = 0
        stopped_early = False
        for step_index, step in enumerate(self.steps):
            state = step.action(state)
            hit_stop = self._evaluate_step(step, state, step_index, violations)
            completed = step_index + 1
            if hit_stop:
                stopped_early = True
                break
        del stopped_early  # completed already records the truth

        success = not violations
        return RunResult(
            success=success,
            iterations_completed=completed,
            violations=tuple(violations),
            invariants_evaluated=invariants_evaluated,
        )

    def _evaluate_step(
        self,
        step: Step,
        state: object,
        step_index: int,
        violations: list[Violation],
    ) -> bool:
        """Evaluate this step's declared invariants. Returns True iff outer
        loop should break (stop-on-first-violation)."""
        if not step.invariants:
            return False
        for name in step.invariants:
            inv: Any = self.registry.get(name)
            baseline = self._baselines[name]  # O(1)
            result: CheckResult = inv.check(state, baseline, step_index)
            if isinstance(result, Violation):
                violations.append(result)
                if self.stop_on_first_violation:
                    return True
            # else: result is Ok — Protocol guarantees CheckResult = Ok | Violation.
        return False
