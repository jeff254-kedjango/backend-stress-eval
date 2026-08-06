"""Optional plugin extensions — Chunks F+ unsaturated axes.

The base :class:`core.plugin.Plugin` Protocol declares the minimum surface
every ecosystem adapter must provide. Some unsaturated axes only apply to
frameworks that expose specific machinery:

* **Concurrency-mode matrix** (T1.2) — needs a way to build the app under a
  named concurrency mode (``asyncio``, ``anyio-trio``, ``anyio-asyncio``,
  ``sync-in-threadpool``). Not every framework has a meaningful choice
  here — a task queue plugin doesn't; a Django plugin has a very different
  mode-list from a FastAPI plugin.
* **Teardown-order permutation fuzzer** (T1.3) — needs a way to enumerate
  the app's shutdown hooks and drive them in a caller-specified order.
  Most plugins run the framework-canonical order; only frameworks that
  actually expose ordered shutdown hooks can meaningfully vary it.

Both extensions are declared as separate :class:`typing.Protocol` classes
so plugins opt in structurally. The harness runners check via
``isinstance(plugin, ConcurrencyAware)`` and refuse to run the matrix
against a plugin that doesn't implement it — surfacing the mismatch at
the CLI, not deep in a run. This mirrors the "harness refuses; author
sources" principle (upgrade-plan.md §4).

Rule 4: no dead code. Plugins that can't meaningfully vary a mode simply
don't implement the extension — no no-op stubs.
Rule 5: clarity. Every method returns a plain value with a documented
contract; no callbacks, no framework-specific types leaking through core.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CONCURRENCY_MODES_CANONICAL",
    "ConcurrencyAware",
    "TeardownAware",
]


# ---------------------------------------------------------------------------
# The canonical mode ordering.
#
# Ordering matters for byte-stable diff output: `diff_modes(reports)` sorts
# by mode name, but reporting sugar (summary lines, mode-pair diffs) walks
# them in this canonical order so the operator sees them the same way each
# run. If a plugin returns modes outside this list, they sort lexicographically
# after the canonical set — deliberate, so vendor-specific modes are visually
# grouped separately.
# ---------------------------------------------------------------------------
CONCURRENCY_MODES_CANONICAL: tuple[str, ...] = (
    "asyncio",
    "anyio-asyncio",
    "anyio-trio",
    "sync-threadpool",
)


@runtime_checkable
class ConcurrencyAware(Protocol):
    """Optional extension for plugins that support a concurrency-mode matrix.

    Implementations must expose:

    * ``available_modes()`` — the concurrency modes this plugin can build
      an app under. Empty tuple means "I opted in but there's nothing to
      try" — the matrix runner will treat that as a plugin bug and raise.
    * ``build_app_for_mode(mode)`` — analogue of :meth:`Plugin.build_app`
      that constructs the app configured for ``mode``. Must raise
      :class:`ValueError` if ``mode not in available_modes()`` so the
      harness fails loud rather than silently falling back.

    The plugin's regular :meth:`Plugin.build_app` remains available and
    typically corresponds to the plugin's default mode — the matrix
    runner does NOT call it; it goes through ``build_app_for_mode`` for
    every entry in the matrix, including the default, so every report
    row has the same provenance.

    Rule 5: fail-loud on unknown mode. Silent fallback would let a typo
    in a CLI flag produce a report that looks fine but tested nothing.
    """

    def available_modes(self) -> tuple[str, ...]: ...

    def build_app_for_mode(self, mode: str, /) -> Any: ...


@runtime_checkable
class TeardownAware(Protocol):
    """Optional extension for plugins whose shutdown order is inspectable.

    Implementations must expose:

    * ``teardown_hooks(app)`` — sorted tuple of stable string identifiers,
      one per registered shutdown hook. Empty tuple means "no ordered
      hooks, don't fuzz me" and the fuzzer skips this plugin.
    * ``run_teardown(app, order)`` — run the hooks in ``order``, which is
      a permutation of the strings returned by ``teardown_hooks``. Must
      raise :class:`ValueError` on a non-permutation input.

    The fuzzer enumerates permutations up to a bounded factorial (4! = 24;
    5! and above are refused by the harness — those live in a future
    fuzz-style tool, not in the deterministic sweep).

    Design note: the plugin owns the identifier strings. It's fine for
    them to be internal names (``"cleanup_pool"``, ``"close_broker"``) —
    they only need to be stable across calls within one process so the
    harness can round-trip them. If a plugin can't produce stable ids
    without touching framework internals, it should not implement this
    Protocol — that decision is intentional: we want machine-generated
    findings, not synthesised ones.
    """

    def teardown_hooks(self, app: Any, /) -> tuple[str, ...]: ...

    def run_teardown(self, app: Any, order: tuple[str, ...], /) -> None: ...
