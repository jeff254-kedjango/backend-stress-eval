"""Invariant protocol + registry.

Framework-agnostic. An *invariant* is a property that must hold across a stress
run — e.g. "process RSS returns to baseline", "route table is unchanged after
lifespan shutdown", "identical requests produce identical responses". The
runner (Chunk 4) invokes each registered invariant at configured cadences and
records structured :class:`Violation` reports for any that fail. Those reports
are the grading contract (see ``discovery-strategy.md`` §10).

Design notes (see also ``rules.md``):

* Invariants are declared with :class:`typing.Protocol` (structural typing).
  Plugins do not import a base class; core stays plugin-free.
* :class:`CheckResult` is a sum type of :class:`Ok` and :class:`Violation`,
  both frozen dataclasses. Immutable results survive concurrent reporting.
* The registry is a thin dict wrapper. Every public operation is O(1)
  (Rule 1). Duplicate registration and blank names raise at register time —
  fail fast, not at check time.
* Evidence is constrained to :data:`JsonValue`. No :class:`Any` leaks into
  the grading payload; reports are trivially JSON-serialisable in Chunk 5.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeAlias, TypeVar, runtime_checkable

__all__ = [
    "DuplicateInvariantError",
    "Invariant",
    "InvariantRegistry",
    "Ok",
    "UnknownInvariantError",
    "Violation",
    "CheckResult",
    "JsonValue",
]

# ---------------------------------------------------------------------------
# JSON-value type. Recursive alias so nested evidence is fully typed.
# ---------------------------------------------------------------------------
# Note: written with ``TypeAlias`` rather than PEP 695 ``type`` statements
# because mypy 1.11 still gates ``type`` behind ``--enable-incomplete-feature``.
# When we bump mypy past the point where it lands stable, switch this and
# ``CheckResult`` below to the ``type`` keyword form and drop the ``TypeAlias``
# import (ruff already prefers it — see UP040).
JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | Mapping[str, "JsonValue"]
)


# ---------------------------------------------------------------------------
# Result sum type. Frozen; both branches carry the invariant name for
# provenance so reporter code never has to correlate by index.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Ok:
    """The invariant held for this check."""

    invariant_name: str


@dataclass(frozen=True, slots=True)
class Violation:
    """The invariant was violated.

    ``evidence`` must be a JSON-serialisable mapping. ``detail`` is a short
    human-readable summary; ``evidence`` is the machine-checkable payload
    the grading contract keys off.
    """

    invariant_name: str
    detail: str
    evidence: Mapping[str, JsonValue]
    iteration: int | None = None


CheckResult: TypeAlias = Ok | Violation


# ---------------------------------------------------------------------------
# Invariant protocol. Generic over the state type ``S`` and baseline type ``B``.
#
# Both TypeVars are **invariant** — each appears in both parameter and return
# position across ``setup`` / ``check``, so covariant/contravariant markers
# would be a type-checker error (measured with mypy --strict). Plugins
# parameterize their own concrete invariants; the registry stores them as
# ``Invariant[Any, Any]`` (heterogeneous container) because the runner always
# feeds a given instance's ``setup`` output straight back into its own
# ``check``, so type-safety is preserved at the callsite level.
# ---------------------------------------------------------------------------
S_contra = TypeVar("S_contra", contravariant=True)
B = TypeVar("B")


@runtime_checkable
class Invariant(Protocol, Generic[S_contra, B]):
    """Structural protocol for stress-run invariants.

    Implementations must expose:

    * ``name``: stable identifier used by the registry and the grading report.
      Non-empty, whitespace-trimmed; validated at registration.
    * ``setup(state)``: capture a baseline (e.g. RSS at t=0). Called once
      before the runner enters its iteration loop. Returning ``None`` is fine
      for stateless invariants.
    * ``check(state, baseline, iteration)``: return :class:`Ok` if the
      invariant holds, otherwise a :class:`Violation` with structured
      evidence. ``iteration`` is the current run index; pass it through to
      the returned :class:`Violation` for pinpoint grading.
    """

    @property
    def name(self) -> str: ...

    def setup(self, state: S_contra, /) -> B: ...

    def check(
        self,
        state: S_contra,
        baseline: B,
        iteration: int,
        /,
    ) -> CheckResult: ...


# ---------------------------------------------------------------------------
# Registry errors.
# ---------------------------------------------------------------------------
class DuplicateInvariantError(ValueError):
    """Raised when two invariants share a name."""


class UnknownInvariantError(KeyError):
    """Raised when the registry is asked for a name it does not hold."""


# ---------------------------------------------------------------------------
# Registry. Thin dict wrapper; every op is O(1).
# ---------------------------------------------------------------------------
def _validate_name(name: str) -> str:
    """Reject blank / whitespace-only names at register time (fail fast)."""
    if not isinstance(name, str):  # runtime guard — Protocol is structural
        raise TypeError(f"invariant name must be str, got {type(name).__name__}")
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("invariant name must be a non-empty, non-whitespace string")
    if trimmed != name:
        raise ValueError(
            f"invariant name {name!r} has leading/trailing whitespace; "
            "trim it to keep grading keys stable"
        )
    return trimmed


@dataclass(slots=True)
class InvariantRegistry:
    """Ordered set of invariants keyed by name.

    ``dict`` preserves insertion order in Python 3.7+; the runner uses that
    order for deterministic reporting (Chunk 4/5). All accessors are O(1).
    """

    _items: dict[str, Invariant[Any, Any]] = field(default_factory=dict)

    def register(self, invariant: Invariant[Any, Any]) -> None:
        """Add ``invariant``. Raises on duplicate names or blank names."""
        name = _validate_name(invariant.name)
        if name in self._items:
            raise DuplicateInvariantError(f"invariant {name!r} already registered")
        self._items[name] = invariant

    def get(self, name: str) -> Invariant[Any, Any]:
        """Return the invariant registered under ``name``. O(1)."""
        try:
            return self._items[name]
        except KeyError as exc:
            raise UnknownInvariantError(name) from exc

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._items

    def __iter__(self) -> Iterator[Invariant[Any, Any]]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def names(self) -> Mapping[str, Invariant[Any, Any]]:
        """Read-only view of the underlying mapping. O(1)."""
        return MappingProxyType(self._items)
