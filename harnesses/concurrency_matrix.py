"""T1.2 — Concurrency-mode matrix runner.

Same probe sequence, N concurrency modes, one report per mode. State-desync
bugs across modes (the exact shape of `anyio-lifecycle-leak`) surface as
mode-only violations: an invariant that FAILs under one mode and PASSes
under another is diagnosis-ambiguous by construction — see
upgrade-plan.md §7 T1.2.

The runner does not automate the diagnosis. It emits a
:class:`ModeMatrixReport` — a byte-stable JSON artifact naming which
invariants differ across which mode pairs — and stops. The operator
inspects the artifact, picks the interesting rows, and (if any warrant it)
runs `bse scaffold-candidate` to start a new candidate. This mirrors the
Chunk-E principle: the diff IS the finding.

Framework-agnostic. The plugin under test must implement
:class:`core.plugin_extensions.ConcurrencyAware`; the runner refuses at
call time otherwise (Rule 5: no silent fallback).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from core.plugin import Plugin
from core.plugin_extensions import CONCURRENCY_MODES_CANONICAL, ConcurrencyAware
from core.reporter import Report
from harnesses.discovery import (
    DEFAULT_ITERATIONS_L1,
    DEFAULT_ROUNDS_L2,
    DEFAULT_ROUNDS_L3,
    run_discovery,
)

__all__ = [
    "MODE_MATRIX_FILENAME",
    "MODE_MATRIX_SCHEMA_VERSION",
    "ModeDivergence",
    "ModeMatrixError",
    "ModeMatrixReport",
    "diff_modes",
    "run_concurrency_matrix",
]


MODE_MATRIX_FILENAME: Final = "mode-matrix.json"
MODE_MATRIX_SCHEMA_VERSION: Final = "1"


class ModeMatrixError(RuntimeError):
    """Raised when the caller violates the matrix contract.

    Distinct from a mode's own probe failure — which surfaces as a
    Violation in that mode's Report. This exception is *precondition*:
    plugin doesn't implement ConcurrencyAware, empty mode list, unknown
    mode, and so on.
    """


@dataclass(frozen=True, slots=True)
class ModeDivergence:
    """One row of the cross-mode diff.

    An invariant/iteration pair that VIOLATES in some modes and does NOT
    violate in others. Byte-stable: modes tuples sorted, JSON output
    with ``sort_keys=True``.
    """

    layer: str
    invariant_name: str
    iteration: int | None
    violating_modes: tuple[str, ...]
    passing_modes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModeMatrixReport:
    """The matrix artifact — reports per mode plus the cross-mode diff.

    Byte-stable JSON via :meth:`to_json`. The diff surface intentionally
    mirrors :class:`core.differ.LayerDiff` (three-array shape, sorted).
    """

    schema_version: str
    plugin_name: str
    target_commit: str
    modes: tuple[str, ...]  # sorted, actually-run
    per_mode: dict[str, dict[str, Report]]  # mode -> layer -> Report
    divergences: tuple[ModeDivergence, ...]

    @property
    def has_divergence(self) -> bool:
        return bool(self.divergences)

    def summary_line(self) -> str:
        """One-liner for CLI stdout: `2 modes ran, 5 divergences found`."""
        return f"{len(self.modes)} modes ran, {len(self.divergences)} divergences found"

    def to_json(self) -> str:
        """Byte-stable JSON serialisation."""
        payload = {
            "schema_version": self.schema_version,
            "plugin_name": self.plugin_name,
            "target_commit": self.target_commit,
            "modes": list(self.modes),
            "summary": self.summary_line(),
            "divergences": [
                {
                    "layer": d.layer,
                    "invariant_name": d.invariant_name,
                    "iteration": d.iteration,
                    "violating_modes": list(d.violating_modes),
                    "passing_modes": list(d.passing_modes),
                }
                for d in self.divergences
            ],
            # Per-mode success rollup — not the full per-mode Report payload;
            # the full reports are written per-mode alongside this file by the
            # CLI. Keeping this artifact focused on the diff shape.
            "per_mode_success": {
                mode: {
                    layer_name: report.result.success
                    for layer_name, report in sorted(layers.items())
                }
                for mode, layers in sorted(self.per_mode.items())
            },
        }
        return json.dumps(payload, sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def run_concurrency_matrix(
    *,
    plugin: Plugin[Any, Any],
    target_commit: str,
    modes: tuple[str, ...] | None = None,
    iterations_l1: int = DEFAULT_ITERATIONS_L1,
    rounds_l2: int = DEFAULT_ROUNDS_L2,
    rounds_l3: int = DEFAULT_ROUNDS_L3,
    variants: tuple[tuple[str, Callable[[], Any]], ...] | None = None,
    variant_plugin_factory: Callable[[Callable[[], Any]], Plugin[Any, Any]] | None = None,
    harness_version: str = "0.0.1",
) -> ModeMatrixReport:
    """Run ``run_discovery`` under each mode; diff across modes.

    Args:
        plugin: Must implement :class:`ConcurrencyAware`. The runner uses
            :meth:`ConcurrencyAware.build_app_for_mode` to construct a
            fresh plugin instance per mode (via a small ``_ModeBoundPlugin``
            adapter — the source plugin is not mutated).
        modes: Which modes to run. ``None`` means "every mode the plugin
            declares available". Explicit modes must all appear in
            :meth:`ConcurrencyAware.available_modes` — the runner raises
            :class:`ModeMatrixError` on any unknown mode (fail-loud).

    Returns:
        :class:`ModeMatrixReport`. Caller writes it to
        ``mode-matrix.json`` via ``to_json()`` and the per-mode Reports
        via ``package_eval_task`` (one dir per mode).

    Raises:
        ModeMatrixError: precondition failures — plugin doesn't opt in,
            empty modes, unknown mode, etc.
    """
    if not isinstance(plugin, ConcurrencyAware):
        raise ModeMatrixError(
            f"plugin {plugin.name!r} does not implement ConcurrencyAware — "
            "the concurrency matrix runner refuses to guess a default mode. "
            "Implement core.plugin_extensions.ConcurrencyAware to opt in."
        )
    aware = plugin  # narrowed
    available = aware.available_modes()
    if not available:
        raise ModeMatrixError(
            f"plugin {plugin.name!r} opted in to ConcurrencyAware but "
            "available_modes() returned (). Nothing to run."
        )
    requested = modes if modes is not None else available
    if not requested:
        raise ModeMatrixError("explicit modes=() is a caller bug — pass None or a non-empty tuple")
    unknown = tuple(m for m in requested if m not in available)
    if unknown:
        raise ModeMatrixError(
            f"unknown mode(s) {unknown!r} — plugin {plugin.name!r} declares "
            f"available_modes()={available!r}"
        )

    per_mode: dict[str, dict[str, Report]] = {}
    for mode in requested:
        mode_plugin = _ModeBoundPlugin(source=plugin, aware=aware, mode=mode)
        per_mode[mode] = run_discovery(
            plugin=mode_plugin,
            target_commit=f"{target_commit}+{mode}",
            iterations_l1=iterations_l1,
            rounds_l2=rounds_l2,
            rounds_l3=rounds_l3,
            variants=variants,
            variant_plugin_factory=variant_plugin_factory,
            harness_version=harness_version,
        )

    divergences = _cross_mode_divergences(per_mode)
    return ModeMatrixReport(
        schema_version=MODE_MATRIX_SCHEMA_VERSION,
        plugin_name=plugin.name,
        target_commit=target_commit,
        modes=_sorted_canonical_first(tuple(requested)),
        per_mode=per_mode,
        divergences=divergences,
    )


def diff_modes(per_mode: dict[str, dict[str, Report]]) -> tuple[ModeDivergence, ...]:
    """Public entry for cross-mode diffing of an already-materialised matrix.

    Same output as the internal `_cross_mode_divergences` — exposed so
    callers who ran discovery themselves (e.g. from a Python REPL) can
    still get the diff shape without re-running the matrix.
    """
    return _cross_mode_divergences(per_mode)


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------
def _sorted_canonical_first(modes: tuple[str, ...]) -> tuple[str, ...]:
    """Sort modes with the canonical set first (in canonical order),
    then any vendor-specific modes lexicographically after.
    """
    canonical_present = tuple(m for m in CONCURRENCY_MODES_CANONICAL if m in modes)
    others = tuple(sorted(m for m in modes if m not in CONCURRENCY_MODES_CANONICAL))
    return canonical_present + others


def _cross_mode_divergences(
    per_mode: dict[str, dict[str, Report]],
) -> tuple[ModeDivergence, ...]:
    """For every (layer, invariant, iteration) triple, if some modes report
    a violation and others don't, emit a :class:`ModeDivergence`.

    Byte-stable: divergences sorted by (layer, invariant, iteration), then
    each modes tuple sorted lexicographically.
    """
    # Build the universe of triples across all modes.
    # A "triple" is (layer_name, invariant_name, iteration).
    # For each triple we track {mode: violated_bool}.
    matrix: dict[tuple[str, str, int | None], dict[str, bool]] = {}
    all_modes = tuple(per_mode)

    for mode, layers in per_mode.items():
        for layer_name, report in layers.items():
            # Every invariant that ran on this layer is either violating or
            # passing on this iteration axis. We only key by iterations that
            # actually appeared in some violation (there is no "iteration
            # count" surfaced by Report at the invariant level). This means
            # a bug that fires on iter 500 in mode A and iter 500 in mode B
            # with identical shape produces NO divergence — as intended.
            for v in report.result.violations:
                key = (layer_name, v.invariant_name, v.iteration)
                matrix.setdefault(key, {})[mode] = True

    # Fill in the passers: any mode not marked True on a given key is False.
    for mode_map in matrix.values():
        for mode in all_modes:
            mode_map.setdefault(mode, False)

    # Now emit divergences: keys where at least one mode is True AND at
    # least one is False.
    divergences: list[ModeDivergence] = []
    for (layer_name, invariant_name, iteration), mode_map in matrix.items():
        violating = tuple(sorted(m for m, v in mode_map.items() if v))
        passing = tuple(sorted(m for m, v in mode_map.items() if not v))
        if violating and passing:
            divergences.append(
                ModeDivergence(
                    layer=layer_name,
                    invariant_name=invariant_name,
                    iteration=iteration,
                    violating_modes=violating,
                    passing_modes=passing,
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
# _ModeBoundPlugin — small adapter that swaps build_app for the mode-bound
# variant while forwarding every other Plugin method to the source plugin.
# ---------------------------------------------------------------------------
class _ModeBoundPlugin:
    """Wraps a ConcurrencyAware plugin, forcing every ``build_app`` call
    through ``build_app_for_mode(mode)``.

    Does NOT inherit from the source plugin — it forwards structurally so
    the source's dataclass slots stay untouched (frozen source plugins
    work fine). All non-``build_app`` methods delegate.
    """

    __slots__ = ("_source", "_aware", "_mode")

    def __init__(
        self,
        source: Plugin[Any, Any],
        aware: ConcurrencyAware,
        mode: str,
    ) -> None:
        self._source = source
        self._aware = aware
        self._mode = mode

    @property
    def name(self) -> str:
        return f"{self._source.name}[{self._mode}]"

    def build_app(self) -> Any:
        return self._aware.build_app_for_mode(self._mode)

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
        self._source.probe(client)

    def route_signature(self, app: Any, /) -> tuple[str, ...]:
        return self._source.route_signature(app)

    def response_digest(self, app: Any, /) -> str | None:
        return self._source.response_digest(app)
