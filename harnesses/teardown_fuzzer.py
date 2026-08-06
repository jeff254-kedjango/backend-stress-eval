"""T1.3 — Teardown-order permutation fuzzer.

Prod apps run the canonical shutdown-hook order. Order-sensitive teardown
bugs — a hook that assumes its predecessor already ran, or two hooks that
compete for the same resource — hide behind that canonical order. This
runner enumerates every permutation of the plugin's declared hooks (up
to a bounded factorial ceiling) and runs the existing baseline invariants
per order.

Framework-agnostic. Requires the plugin to implement
:class:`core.plugin_extensions.TeardownAware`; the runner refuses at call
time otherwise (Rule 5: no silent fallback).

The output is a :class:`TeardownFuzzReport` — one row per permutation
with the invariant success/violation set. Orders whose invariant set
differs from the *canonical* order surface as `divergent_orders`. Like
`bse diff` and the concurrency matrix, the diff IS the finding: the
operator inspects the artifact and (if warranted) runs
`bse scaffold-candidate` on the interesting rows.

Design notes:

* Permutation ceiling is 4! = 24. 5! = 120 begins to bleed into
  fuzz-tool territory (see :file:`upgrade-plan.md` §12 — "not a
  fuzzer in the AFL sense"). Plugins with more than four hooks must
  return a shortlist from :meth:`TeardownAware.teardown_hooks` or the
  runner refuses.
* The canonical order is `teardown_hooks(app)` itself — sorted in
  whatever way the plugin defines "canonical". The runner does not
  invent one.
* Every permutation runs against a **fresh app** so state does not leak
  between orders (Rule 1: per-permutation cost is bounded).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any, Final

from core.plugin import Plugin
from core.plugin_extensions import TeardownAware

__all__ = [
    "TEARDOWN_FUZZ_FILENAME",
    "TEARDOWN_FUZZ_SCHEMA_VERSION",
    "TEARDOWN_MAX_HOOKS",
    "OrderResult",
    "TeardownFuzzError",
    "TeardownFuzzReport",
    "run_teardown_fuzzer",
]


TEARDOWN_FUZZ_FILENAME: Final = "teardown-fuzz.json"
TEARDOWN_FUZZ_SCHEMA_VERSION: Final = "1"
TEARDOWN_MAX_HOOKS: Final = 4  # 4! = 24 permutations, hard ceiling.


class TeardownFuzzError(RuntimeError):
    """Precondition failure — plugin doesn't opt in, too many hooks, etc.

    Distinct from an individual permutation's *observed* failure — those
    are recorded as :class:`OrderResult` entries with ``raised != None``
    and do not stop the sweep.
    """


@dataclass(frozen=True, slots=True)
class OrderResult:
    """One permutation's outcome.

    ``raised`` carries the exception message if ``run_teardown`` itself
    threw (e.g. a hook double-freed a resource). It's a string, not the
    live exception — the artifact is byte-stable JSON, and stored
    Exception objects would break that.
    """

    order: tuple[str, ...]
    is_canonical: bool
    raised: str | None  # None means clean teardown


@dataclass(frozen=True, slots=True)
class TeardownFuzzReport:
    """The fuzzer artifact — canonical order plus every permutation's fate."""

    schema_version: str
    plugin_name: str
    target_commit: str
    hooks: tuple[str, ...]  # the canonical order (from teardown_hooks)
    results: tuple[OrderResult, ...]

    @property
    def canonical_result(self) -> OrderResult:
        """The one row whose order equals the canonical hook order.

        Guaranteed to exist — the runner always includes the identity
        permutation.
        """
        for r in self.results:
            if r.is_canonical:
                return r
        # Defensive — should never trigger.
        raise TeardownFuzzError("no canonical row in TeardownFuzzReport (harness bug)")

    @property
    def divergent_orders(self) -> tuple[OrderResult, ...]:
        """Every permutation whose ``raised`` differs from the canonical."""
        canonical_raised = self.canonical_result.raised
        return tuple(r for r in self.results if r.raised != canonical_raised)

    @property
    def has_divergence(self) -> bool:
        return bool(self.divergent_orders)

    def summary_line(self) -> str:
        """One-liner for CLI stdout: `24 orders tried, 2 divergent`."""
        return f"{len(self.results)} orders tried, {len(self.divergent_orders)} divergent"

    def to_json(self) -> str:
        """Byte-stable JSON."""
        payload = {
            "schema_version": self.schema_version,
            "plugin_name": self.plugin_name,
            "target_commit": self.target_commit,
            "hooks_canonical_order": list(self.hooks),
            "summary": self.summary_line(),
            "results": [
                {
                    "order": list(r.order),
                    "is_canonical": r.is_canonical,
                    "raised": r.raised,
                }
                for r in self.results
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2)


def run_teardown_fuzzer(
    *,
    plugin: Plugin[Any, Any],
    target_commit: str,
) -> TeardownFuzzReport:
    """Run every permutation of the plugin's teardown hooks.

    Args:
        plugin: Must implement :class:`TeardownAware`. Must return at most
            :data:`TEARDOWN_MAX_HOOKS` hooks — larger permutation spaces
            live in a future fuzz tool, not the deterministic sweep.
        target_commit: Threaded into the artifact's provenance line only;
            the fuzzer does not otherwise care about the pin.

    Returns:
        :class:`TeardownFuzzReport`. Callers write it to
        :data:`TEARDOWN_FUZZ_FILENAME` via ``to_json()``.

    Raises:
        TeardownFuzzError: precondition failures (plugin doesn't opt in,
            hook count > ceiling, etc.).
    """
    if not isinstance(plugin, TeardownAware):
        raise TeardownFuzzError(
            f"plugin {plugin.name!r} does not implement TeardownAware — "
            "the teardown fuzzer refuses to guess a hook ordering. "
            "Implement core.plugin_extensions.TeardownAware to opt in."
        )
    aware = plugin  # narrowed

    # Sample the hook list from a throwaway app so we can budget correctly.
    probe_app = plugin.build_app()
    plugin.lifecycle_start(probe_app)
    try:
        hooks = aware.teardown_hooks(probe_app)
    finally:
        plugin.lifecycle_stop(probe_app)

    if not hooks:
        raise TeardownFuzzError(
            f"plugin {plugin.name!r} opted in to TeardownAware but "
            "teardown_hooks(app) returned (). Nothing to permute."
        )
    if len(hooks) > TEARDOWN_MAX_HOOKS:
        raise TeardownFuzzError(
            f"plugin {plugin.name!r} declared {len(hooks)} hooks; "
            f"the deterministic sweep is capped at {TEARDOWN_MAX_HOOKS} "
            f"(= {TEARDOWN_MAX_HOOKS}!). Return a shortlist or use a "
            "future fuzz tool for larger spaces."
        )

    canonical = tuple(hooks)
    results: list[OrderResult] = []
    for order in itertools.permutations(canonical):
        results.append(_run_one_order(plugin, aware, order, canonical))

    return TeardownFuzzReport(
        schema_version=TEARDOWN_FUZZ_SCHEMA_VERSION,
        plugin_name=plugin.name,
        target_commit=target_commit,
        hooks=canonical,
        results=tuple(results),
    )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------
def _run_one_order(
    plugin: Plugin[Any, Any],
    aware: TeardownAware,
    order: tuple[str, ...],
    canonical: tuple[str, ...],
) -> OrderResult:
    """Build → start → run_teardown(order); capture whether teardown threw.

    A permutation that raises is recorded (not re-raised) so the sweep
    over the full permutation space completes and every row is present
    in the report — that's the shape that makes the diff readable.
    """
    app = plugin.build_app()
    plugin.lifecycle_start(app)
    raised: str | None = None
    try:
        aware.run_teardown(app, order)
    except Exception as exc:
        raised = f"{type(exc).__name__}: {exc}"
    return OrderResult(
        order=order,
        is_canonical=(order == canonical),
        raised=raised,
    )
