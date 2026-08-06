"""T1.4 — Fault-injected probe adapter.

Multiplier on every existing invariant. Client-disconnect / cancel-
mid-request / background-exception faults reveal state-desync bugs
(litestar #3772 was exactly this shape) that a clean-probe sweep
never touches.

Design:

* Plugin opts in via :class:`core.plugin_extensions.FaultInjectable`.
  Base :class:`core.plugin.Plugin` Protocol is untouched — plugins
  that can't meaningfully vary a fault simply don't implement the
  extension. Rule 4.
* For each requested fault, wrap the plugin in a small
  :class:`_FaultBoundPlugin` adapter that routes every ``probe`` call
  through ``probe_with_fault(client, fault_name)``. Then run the
  standard :func:`run_discovery` sweep under that wrapper.
* The result is ``dict[fault_name, dict[layer_name, Report]]``.
  :func:`diff_faults` walks the matrix using the same set-arithmetic
  idiom as :func:`harnesses.concurrency_matrix.diff_modes` and
  surfaces violations that appear under some faults and not others.

The diff IS the finding. Nothing here auto-packages anything — the
operator inspects ``fault-matrix.json``, picks the interesting rows,
and (if warranted) runs ``bse scaffold-candidate`` on them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from core.plugin import Plugin
from core.plugin_extensions import CANONICAL_FAULTS, FaultInjectable
from core.reporter import Report
from harnesses.discovery import (
    DEFAULT_ITERATIONS_L1,
    DEFAULT_ROUNDS_L2,
    DEFAULT_ROUNDS_L3,
    run_discovery,
)

__all__ = [
    "FAULT_MATRIX_FILENAME",
    "FAULT_MATRIX_SCHEMA_VERSION",
    "FaultDivergence",
    "FaultMatrixError",
    "FaultMatrixReport",
    "diff_faults",
    "run_fault_matrix",
]


FAULT_MATRIX_FILENAME: Final = "fault-matrix.json"
FAULT_MATRIX_SCHEMA_VERSION: Final = "1"


class FaultMatrixError(RuntimeError):
    """Precondition failure — plugin doesn't opt in, unknown fault, etc.

    Distinct from a fault's own probe-crash — that is *data* recorded
    on the fault's Report as a Violation (or as
    ``iterations_completed`` short-circuiting). This exception is
    precondition-only.
    """


@dataclass(frozen=True, slots=True)
class FaultDivergence:
    """One row of the cross-fault diff.

    An invariant/iteration pair that VIOLATES under some faults and
    passes under others. Byte-stable: modes tuples sorted.
    """

    layer: str
    invariant_name: str
    iteration: int | None
    violating_faults: tuple[str, ...]
    passing_faults: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FaultMatrixReport:
    """Fault-matrix artifact.

    Same three-array shape as :class:`ModeMatrixReport` for consistency
    across the three unsaturated-axis runners.
    """

    schema_version: str
    plugin_name: str
    target_commit: str
    faults: tuple[str, ...]  # sorted actually-run
    per_fault: dict[str, dict[str, Report]]  # fault -> layer -> Report
    divergences: tuple[FaultDivergence, ...]

    @property
    def has_divergence(self) -> bool:
        return bool(self.divergences)

    def summary_line(self) -> str:
        """One-liner for CLI stdout: `3 faults ran, 5 divergences found`."""
        return f"{len(self.faults)} faults ran, {len(self.divergences)} divergences found"

    def to_json(self) -> str:
        """Byte-stable JSON."""
        payload = {
            "schema_version": self.schema_version,
            "plugin_name": self.plugin_name,
            "target_commit": self.target_commit,
            "faults": list(self.faults),
            "summary": self.summary_line(),
            "divergences": [
                {
                    "layer": d.layer,
                    "invariant_name": d.invariant_name,
                    "iteration": d.iteration,
                    "violating_faults": list(d.violating_faults),
                    "passing_faults": list(d.passing_faults),
                }
                for d in self.divergences
            ],
            "per_fault_success": {
                fault: {
                    layer_name: report.result.success
                    for layer_name, report in sorted(layers.items())
                }
                for fault, layers in sorted(self.per_fault.items())
            },
        }
        return json.dumps(payload, sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def run_fault_matrix(
    *,
    plugin: Plugin[Any, Any],
    target_commit: str,
    faults: tuple[str, ...] | None = None,
    iterations_l1: int = DEFAULT_ITERATIONS_L1,
    rounds_l2: int = DEFAULT_ROUNDS_L2,
    rounds_l3: int = DEFAULT_ROUNDS_L3,
    variants: tuple[tuple[str, Callable[[], Any]], ...] | None = None,
    variant_plugin_factory: Callable[[Callable[[], Any]], Plugin[Any, Any]] | None = None,
    harness_version: str = "0.0.1",
) -> FaultMatrixReport:
    """Run ``run_discovery`` under each fault; diff across faults.

    Args:
        plugin: Must implement :class:`FaultInjectable`. The runner
            wraps it in a per-fault adapter that routes every probe
            call through :meth:`FaultInjectable.probe_with_fault`.
        faults: Which faults to run. ``None`` means "every fault the
            plugin declares available". Explicit faults must all
            appear in :meth:`FaultInjectable.available_faults` —
            unknown fault raises :class:`FaultMatrixError`.

    Returns:
        :class:`FaultMatrixReport`.

    Raises:
        FaultMatrixError: precondition failures.
    """
    if not isinstance(plugin, FaultInjectable):
        raise FaultMatrixError(
            f"plugin {plugin.name!r} does not implement FaultInjectable — "
            "the fault-matrix runner refuses to guess a default fault. "
            "Implement core.plugin_extensions.FaultInjectable to opt in."
        )
    injector = plugin  # narrowed
    available = injector.available_faults()
    if not available:
        raise FaultMatrixError(
            f"plugin {plugin.name!r} opted in to FaultInjectable but "
            "available_faults() returned (). Nothing to run."
        )
    requested = faults if faults is not None else available
    if not requested:
        raise FaultMatrixError(
            "explicit faults=() is a caller bug — pass None or a non-empty tuple"
        )
    unknown = tuple(f for f in requested if f not in available)
    if unknown:
        raise FaultMatrixError(
            f"unknown fault(s) {unknown!r} — plugin {plugin.name!r} declares "
            f"available_faults()={available!r}"
        )

    per_fault: dict[str, dict[str, Report]] = {}
    for fault in requested:
        fault_plugin = _FaultBoundPlugin(source=plugin, injector=injector, fault=fault)
        per_fault[fault] = run_discovery(
            plugin=fault_plugin,
            target_commit=f"{target_commit}+{fault}",
            iterations_l1=iterations_l1,
            rounds_l2=rounds_l2,
            rounds_l3=rounds_l3,
            variants=variants,
            variant_plugin_factory=variant_plugin_factory,
            harness_version=harness_version,
        )

    divergences = _cross_fault_divergences(per_fault)
    return FaultMatrixReport(
        schema_version=FAULT_MATRIX_SCHEMA_VERSION,
        plugin_name=plugin.name,
        target_commit=target_commit,
        faults=_sorted_canonical_first(tuple(requested)),
        per_fault=per_fault,
        divergences=divergences,
    )


def diff_faults(per_fault: dict[str, dict[str, Report]]) -> tuple[FaultDivergence, ...]:
    """Public entry for cross-fault diffing of an already-materialised matrix."""
    return _cross_fault_divergences(per_fault)


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------
def _sorted_canonical_first(faults: tuple[str, ...]) -> tuple[str, ...]:
    """Canonical faults first (in canonical order), then vendor-specific."""
    canonical_present = tuple(f for f in CANONICAL_FAULTS if f in faults)
    others = tuple(sorted(f for f in faults if f not in CANONICAL_FAULTS))
    return canonical_present + others


def _cross_fault_divergences(
    per_fault: dict[str, dict[str, Report]],
) -> tuple[FaultDivergence, ...]:
    """Same idiom as :func:`concurrency_matrix._cross_mode_divergences`.

    Kept as a parallel implementation rather than factored to a shared
    helper because the row types differ (ModeDivergence vs
    FaultDivergence carry different field names) and the shared
    surface would be dominated by generic-shape ceremony.
    """
    matrix: dict[tuple[str, str, int | None], dict[str, bool]] = {}
    all_faults = tuple(per_fault)

    for fault, layers in per_fault.items():
        for layer_name, report in layers.items():
            for v in report.result.violations:
                key = (layer_name, v.invariant_name, v.iteration)
                matrix.setdefault(key, {})[fault] = True

    for fault_map in matrix.values():
        for fault in all_faults:
            fault_map.setdefault(fault, False)

    divergences: list[FaultDivergence] = []
    for (layer_name, invariant_name, iteration), fault_map in matrix.items():
        violating = tuple(sorted(f for f, v in fault_map.items() if v))
        passing = tuple(sorted(f for f, v in fault_map.items() if not v))
        if violating and passing:
            divergences.append(
                FaultDivergence(
                    layer=layer_name,
                    invariant_name=invariant_name,
                    iteration=iteration,
                    violating_faults=violating,
                    passing_faults=passing,
                )
            )

    return tuple(
        sorted(
            divergences,
            key=lambda d: (
                d.layer,
                d.invariant_name,
                -1 if d.iteration is None else d.iteration,
            ),
        )
    )


# ---------------------------------------------------------------------------
# _FaultBoundPlugin — wraps a FaultInjectable plugin, forcing every
# ``probe`` call through ``probe_with_fault(client, fault)``. Every
# other Plugin method delegates to the source.
# ---------------------------------------------------------------------------
class _FaultBoundPlugin:
    """Wraps a FaultInjectable plugin, routing ``probe`` through the fault path.

    Does NOT inherit — forwards structurally so the source plugin's
    slots/frozen state stays untouched. Same pattern as
    :class:`harnesses.concurrency_matrix._ModeBoundPlugin`.
    """

    __slots__ = ("_source", "_injector", "_fault")

    def __init__(
        self,
        source: Plugin[Any, Any],
        injector: FaultInjectable,
        fault: str,
    ) -> None:
        self._source = source
        self._injector = injector
        self._fault = fault

    @property
    def name(self) -> str:
        return f"{self._source.name}[fault={self._fault}]"

    def build_app(self) -> Any:
        return self._source.build_app()

    def client(self, app: Any, /) -> Any:
        return self._source.client(app)

    def lifecycle_start(self, app: Any, /) -> None:
        self._source.lifecycle_start(app)

    def lifecycle_stop(self, app: Any, /) -> None:
        self._source.lifecycle_stop(app)

    def reset(self, app: Any, /) -> None:
        self._source.reset(app)

    def feature_matrix(self) -> Any:
        return self._source.feature_matrix()

    def probe(self, client: Any, /) -> None:
        # The fault-bound version: route to probe_with_fault.
        self._injector.probe_with_fault(client, self._fault)

    def route_signature(self, app: Any, /) -> tuple[str, ...]:
        return self._source.route_signature(app)

    def response_digest(self, app: Any, /) -> str | None:
        return self._source.response_digest(app)
