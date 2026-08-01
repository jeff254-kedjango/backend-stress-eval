"""Generic discovery harness — runs Layers 1..4 against ANY plugin.

Before R1 (2026-08-01) this module inlined four FastAPI-specific helpers
(``_one_probe_request``, ``_fastapi_route_signature``, ``_digest_probe``,
plus the two canonical example factories) and every new framework required
a copy of this whole file. After R1 the plugin surface owns those four
concerns and this function is genuinely one-size-fits-all: pass any object
that satisfies :class:`core.plugin.Plugin` and Layer 1..4 just work.

Rule 4 (no dead code) + Rule 5 (clarity): the caller supplies **one** thing
— the plugin. Optional variant list still comes from the caller because
which app-shape combinations to explore is not a plugin decision.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.plugin import Plugin
from core.reporter import Report
from harnesses.layer1_repetition import run_layer1_repetition
from harnesses.layer2_lifecycle import run_layer2_lifecycle
from harnesses.layer3_variants import run_layer3_variants
from harnesses.layer4_sequence import LayerStep, run_layer4_sequence

__all__ = [
    "DEFAULT_ITERATIONS_L1",
    "DEFAULT_ROUNDS_L2",
    "DEFAULT_ROUNDS_L3",
    "run_discovery",
]


# ---------------------------------------------------------------------------
# Discovery-scale defaults — "modest" per Chunk 0 design.
# Under a minute total on a laptop. Bump when the harness is trusted.
#
# L2 + L3 defaults are >= the slope invariant's fit floor (50 samples) so
# that :class:`RssSlopeBoundedOnHarnessState` is meaningfully active on both
# layers; below the floor it silently returns Ok (per fail-safe design) and
# per-variant slope drift never surfaces. L3 lifted 20 → 50 for this reason.
# ---------------------------------------------------------------------------
DEFAULT_ITERATIONS_L1: int = 500
DEFAULT_ROUNDS_L2: int = 50
DEFAULT_ROUNDS_L3: int = 50


def run_discovery(
    *,
    plugin: Plugin[Any, Any],
    target_commit: str,
    iterations_l1: int = DEFAULT_ITERATIONS_L1,
    rounds_l2: int = DEFAULT_ROUNDS_L2,
    rounds_l3: int = DEFAULT_ROUNDS_L3,
    variants: tuple[tuple[str, Callable[[], Any]], ...] | None = None,
    variant_plugin_factory: Callable[[Callable[[], Any]], Plugin[Any, Any]] | None = None,
    harness_version: str = "0.0.1",
) -> dict[str, Report]:
    """Run all four layers against ``plugin`` and return a dict of reports.

    Framework-agnostic. Given any object that satisfies
    :class:`core.plugin.Plugin`, this drives the full sweep with zero
    framework-specific code in this function.

    Optional ``variants`` controls Layer 3. Callers who don't care can leave
    it ``None`` and Layer 3 will be skipped from the returned dict — the
    plugin author decides whether variant-testing is meaningful for their
    framework. When ``variants`` is provided, ``variant_plugin_factory`` must
    also be provided (typical: ``lambda af: type(plugin)(app_factory=af)``).
    """
    reports: dict[str, Report] = {}

    # ---- Layer 1: repetition against one long-lived app. -----------------
    reports["layer1_repetition"] = run_layer1_repetition(
        plugin=plugin,
        request_callable=_bind_probe(plugin),
        iterations=iterations_l1,
        target_commit=target_commit,
        harness_version=harness_version,
    )

    # ---- Layer 2: lifecycle — routes + leak checks across restarts. ------
    reports["layer2_lifecycle"] = run_layer2_lifecycle(
        plugin=plugin,
        request_callable=_bind_probe(plugin),
        route_signature_of=plugin.route_signature,
        rounds=rounds_l2,
        target_commit=target_commit,
        harness_version=harness_version,
    )

    # ---- Layer 3: variants — optional per plugin. ------------------------
    if variants is not None:
        if variant_plugin_factory is None:
            raise ValueError(
                "variants supplied but variant_plugin_factory is None — "
                "pass a factory that builds a plugin from an app_factory"
            )
        reports["layer3_variants"] = run_layer3_variants(
            plugin_factory=variant_plugin_factory,
            variants=variants,
            request_callable=_bind_probe(plugin),
            route_signature_of=plugin.route_signature,
            rounds=rounds_l3,
            target_commit=target_commit,
            harness_version=harness_version,
        )

    # ---- Layer 4: an ordered probe sequence. -----------------------------
    def _issue(app: Any) -> None:
        c = plugin.client(app)
        plugin.probe(c)

    steps = (
        LayerStep(
            name="probe_1",
            action=_issue,
            invariants=("rss_return_to_baseline", "fd_return_to_baseline"),
        ),
        LayerStep(
            name="probe_2",
            action=_issue,
            invariants=("rss_return_to_baseline", "response_determinism"),
        ),
        LayerStep(
            name="probe_3",
            action=_issue,
            invariants=("rss_return_to_baseline", "fd_return_to_baseline"),
        ),
    )
    reports["layer4_sequence"] = run_layer4_sequence(
        plugin=plugin,
        steps=steps,
        response_digest_of=plugin.response_digest,
        target_commit=target_commit,
        harness_version=harness_version,
    )

    return reports


def _bind_probe(plugin: Plugin[Any, Any]) -> Callable[[object], None]:
    """Adapt ``plugin.probe(client)`` to the layer harness's callable shape.

    The layer harnesses were designed with a caller-supplied
    ``request_callable(client)`` for backwards compatibility. Here we just
    forward — the plugin owns the actual behaviour.
    """

    def _fire(client: object) -> None:
        plugin.probe(client)

    return _fire
