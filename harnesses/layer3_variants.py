"""Layer 3 — feature-combination harness.

Runs Layer 2 (lifecycle) for a list of variant apps and aggregates the
results into a single :class:`Report` whose ``violations`` are tagged with
the variant name via the invariant name prefix.

Design decision (locked 2026-08-01, Chunk 9): variants are supplied as
``(variant_name, app_factory)`` tuples. This does NOT extend the
:class:`core.plugin.Plugin` ABC (Chunk 6). Users compose feature subsets
in the factory itself — DI-only, DI+middleware, DI+middleware+streaming,
etc. — and the harness sees each as an opaque app.

Rule 5 — clarity: variant identity is preserved in the aggregate Report by
prefixing invariant names in Violation records with ``"<variant>::"``. The
byte-stable JSON contract is unaffected: names are strings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.invariant import InvariantRegistry, Violation
from core.plugin import Plugin
from core.reporter import Report, ReportMetadata
from core.runner import RunResult
from harnesses.layer2_lifecycle import run_layer2_lifecycle

__all__ = ["run_layer3_variants"]


def run_layer3_variants(
    *,
    plugin_factory: Callable[[Callable[[], Any]], Plugin[Any, Any]],
    variants: tuple[tuple[str, Callable[[], Any]], ...],
    request_callable: Callable[[object], None],
    route_signature_of: Callable[[Any], tuple[str, ...]],
    rounds: int,
    target_commit: str,
    seed: int = 0,
    harness_version: str = "0.0.1",
    registry_factory: Callable[[], InvariantRegistry] | None = None,
) -> Report:
    """Run Layer-2 lifecycle for each variant; aggregate violations.

    ``plugin_factory(app_factory) -> Plugin`` — receives the variant's
    ``app_factory`` and returns a plugin bound to it. Typical impl:
    ``lambda af: FastAPIPlugin(app_factory=af)``.
    """
    if not variants:
        raise ValueError("variants must not be empty")

    seen_variant_names: set[str] = set()
    for variant_name, _ in variants:
        if not variant_name or variant_name != variant_name.strip():
            raise ValueError(f"variant name must be non-empty and trimmed; got {variant_name!r}")
        if variant_name in seen_variant_names:
            raise ValueError(f"duplicate variant name: {variant_name!r}")
        seen_variant_names.add(variant_name)

    all_violations: list[Violation] = []
    all_invariants_evaluated: list[str] = []
    total_completed = 0
    overall_success = True

    for variant_name, app_factory in variants:
        plugin = plugin_factory(app_factory)
        registry = registry_factory() if registry_factory is not None else None
        variant_report = run_layer2_lifecycle(
            plugin=plugin,
            request_callable=request_callable,
            route_signature_of=route_signature_of,
            rounds=rounds,
            target_commit=target_commit,
            seed=seed,
            harness_version=harness_version,
            registry=registry,
        )
        # Tag violations with the variant name so aggregated reports remain
        # attributable. Prepend to the invariant_name — a plain string swap,
        # so the byte-stable JSON contract still holds.
        for v in variant_report.result.violations:
            all_violations.append(replace(v, invariant_name=f"{variant_name}::{v.invariant_name}"))
        for name in variant_report.result.invariants_evaluated:
            all_invariants_evaluated.append(f"{variant_name}::{name}")
        total_completed += variant_report.result.iterations_completed
        if not variant_report.result.success:
            overall_success = False

    aggregated = RunResult(
        success=overall_success,
        iterations_completed=total_completed,
        violations=tuple(all_violations),
        invariants_evaluated=tuple(all_invariants_evaluated),
    )
    # Aggregate target is a stable label — first variant's plugin name would
    # require constructing a plugin here; we tag it "variants" so the report
    # is honest about being multi-variant.
    return Report(
        metadata=ReportMetadata(
            target="variants",
            target_commit=target_commit,
            seed=seed,
            iterations_requested=rounds * len(variants),
            harness_version=harness_version,
        ),
        result=aggregated,
    )
