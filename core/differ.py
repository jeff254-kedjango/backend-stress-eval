"""Cross-version differ (Chunk E — the highest-yield unsaturated axis).

Framework-agnostic. See :file:`upgrade-plan.md` §7 T1.1 for the design
rationale: production traffic has already swept RSS/FD/route-stability
axes on any single version, so those axes are saturated per-version.
Diffing them across ADJACENT versions is unsaturated: nobody runs the
same byte-stable probe sequence against v0.140.0 and v0.141.1 and asks
"what changed?".

The runner:

1. Takes two byte-stable report bundles (`dict[layer_name, Report]`),
   one per pinned target commit.
2. Diffs them layer-by-layer, violation-by-violation.
3. Emits three lists per layer: violations present in B but not A
   (regressions), violations in A but not B (fixes), violations in
   both but with changed evidence (drift).

The diff IS the finding. The operator inspects `diff-report.json`,
picks the interesting rows, and — if any warrant it — runs
`bse scaffold-candidate` to start a new candidate. Nothing here
auto-packages anything, per the "harness refuses; author sources"
principle (upgrade-plan.md §4).

Design notes:

* Pure stdlib. Uses `core.reporter` byte-stable JSON as the
  canonical representation of each violation, so identity comparison
  is O(1) on hashed tuples.
* Rule 1: violation-identity dedup is O(V_a + V_b) per layer via
  set/dict lookups. The nested-loop shape is deliberately avoided.
* Rule 4: this module does NOT drive `run_discovery` itself. Callers
  invoke `run_discovery` twice with the two pins already pip-
  installed, then feed the two report dicts to :func:`diff_reports`.
  Wiring is the CLI's job in `cli/main.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.reporter import Report

__all__ = [
    "DIFF_REPORT_FILENAME",
    "DIFF_SCHEMA_VERSION",
    "DiffReport",
    "LayerDiff",
    "ViolationKey",
    "diff_reports",
    "load_report_json",
]

DIFF_REPORT_FILENAME: Final = "diff-report.json"
DIFF_SCHEMA_VERSION: Final = "1"


# ---------------------------------------------------------------------------
# Identity key for a violation.
#
# Two violations are the "same" if they name the same invariant at the
# same iteration index. Evidence-identical violations at different
# iterations are distinct findings (a leak on iter 500 is not the same
# datum as a leak on iter 501, even if the numeric evidence is the same).
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ViolationKey:
    """Hashable identity used to align violations across the two reports."""

    layer: str
    invariant_name: str
    iteration: int | None


@dataclass(frozen=True, slots=True)
class LayerDiff:
    """Per-layer diff. Every list is sorted for byte-stability."""

    layer: str
    only_in_a: tuple[dict[str, object], ...]  # violations only present in A
    only_in_b: tuple[dict[str, object], ...]  # violations only present in B
    evidence_changed: tuple[dict[str, object], ...]  # same key, differing evidence
    a_success: bool
    b_success: bool

    @property
    def has_changes(self) -> bool:
        return bool(self.only_in_a or self.only_in_b or self.evidence_changed)


@dataclass(frozen=True, slots=True)
class DiffReport:
    """Overall diff verdict across all shared layers."""

    schema_version: str
    target_a: str
    target_b: str
    layers: tuple[LayerDiff, ...]

    @property
    def has_changes(self) -> bool:
        return any(layer.has_changes for layer in self.layers)

    def summary_line(self) -> str:
        """One-liner for CLI stdout: `+ 3 regressions, - 1 fix, ~ 2 drift`."""
        regressions = sum(len(layer.only_in_b) for layer in self.layers)
        fixes = sum(len(layer.only_in_a) for layer in self.layers)
        drift = sum(len(layer.evidence_changed) for layer in self.layers)
        return f"+ {regressions} regressions, - {fixes} fixes, ~ {drift} drift"

    def to_json(self) -> str:
        """Byte-stable JSON serialisation for diff-report.json."""
        payload = {
            "schema_version": self.schema_version,
            "target_a": self.target_a,
            "target_b": self.target_b,
            "summary": self.summary_line(),
            "layers": [self._layer_payload(layer) for layer in self.layers],
        }
        return json.dumps(payload, sort_keys=True, indent=2)

    @staticmethod
    def _layer_payload(layer: LayerDiff) -> dict[str, object]:
        return {
            "layer": layer.layer,
            "a_success": layer.a_success,
            "b_success": layer.b_success,
            "only_in_a": list(layer.only_in_a),
            "only_in_b": list(layer.only_in_b),
            "evidence_changed": list(layer.evidence_changed),
        }


# ---------------------------------------------------------------------------
# Public API — two entry points: dict-of-Report input, or JSON-file input.
# ---------------------------------------------------------------------------
def diff_reports(
    reports_a: dict[str, Report],
    reports_b: dict[str, Report],
    *,
    target_a: str,
    target_b: str,
) -> DiffReport:
    """Diff two ``run_discovery`` output dicts.

    Layers absent from either side are treated as empty on that side so
    a violation appearing in a NEW layer in B (say, layer3_variants
    only meaningful for one version) shows up as `only_in_b`. Layers
    common to both are diffed by violation-key set arithmetic.
    """
    all_layer_names = sorted(set(reports_a) | set(reports_b))
    layer_diffs: list[LayerDiff] = []
    for name in all_layer_names:
        rep_a = reports_a.get(name)
        rep_b = reports_b.get(name)
        layer_diffs.append(
            _diff_one_layer(
                layer_name=name,
                violations_a=_violations_of(rep_a),
                violations_b=_violations_of(rep_b),
                success_a=rep_a.result.success if rep_a is not None else True,
                success_b=rep_b.result.success if rep_b is not None else True,
            )
        )
    return DiffReport(
        schema_version=DIFF_SCHEMA_VERSION,
        target_a=target_a,
        target_b=target_b,
        layers=tuple(layer_diffs),
    )


def load_report_json(path: Path) -> dict[str, dict[str, object]]:
    """Load a `report.json` produced by ``package_eval_task``.

    Returns a dict keyed by layer name. Each value is the raw layer
    payload (`{"metadata": ..., "result": ...}`). This lets `bse diff`
    consume artifacts already written to disk without re-running
    `run_discovery` — useful for cross-machine comparison.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object at top level")
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise ValueError(f"{path}: expected top-level `layers` mapping")
    out: dict[str, dict[str, object]] = {}
    for name, body in layers.items():
        if not isinstance(name, str) or not isinstance(body, dict):
            raise ValueError(f"{path}: malformed layer entry {name!r}")
        out[name] = body
    return out


def diff_report_dicts(
    layers_a: dict[str, dict[str, object]],
    layers_b: dict[str, dict[str, object]],
    *,
    target_a: str,
    target_b: str,
) -> DiffReport:
    """Diff two raw report-json layer dicts (loaded via :func:`load_report_json`).

    Same output shape as :func:`diff_reports` but operates on the
    already-serialised form. Used by the CLI when the operator passes
    two report.json paths rather than running discovery in-process.
    """
    all_layer_names = sorted(set(layers_a) | set(layers_b))
    layer_diffs: list[LayerDiff] = []
    for name in all_layer_names:
        layer_a = layers_a.get(name)
        layer_b = layers_b.get(name)
        layer_diffs.append(
            _diff_one_layer(
                layer_name=name,
                violations_a=_violations_from_payload(layer_a),
                violations_b=_violations_from_payload(layer_b),
                success_a=_success_from_payload(layer_a),
                success_b=_success_from_payload(layer_b),
            )
        )
    return DiffReport(
        schema_version=DIFF_SCHEMA_VERSION,
        target_a=target_a,
        target_b=target_b,
        layers=tuple(layer_diffs),
    )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------
def _diff_one_layer(
    *,
    layer_name: str,
    violations_a: list[dict[str, object]],
    violations_b: list[dict[str, object]],
    success_a: bool,
    success_b: bool,
) -> LayerDiff:
    """Set-arithmetic diff of two violation lists by ViolationKey."""
    by_key_a = {_key_of(layer_name, v): v for v in violations_a}
    by_key_b = {_key_of(layer_name, v): v for v in violations_b}
    keys_a = set(by_key_a)
    keys_b = set(by_key_b)

    only_a_keys = sorted(keys_a - keys_b, key=_sort_key)
    only_b_keys = sorted(keys_b - keys_a, key=_sort_key)
    shared_keys = sorted(keys_a & keys_b, key=_sort_key)

    only_in_a = tuple(_annotate(by_key_a[k], layer=layer_name) for k in only_a_keys)
    only_in_b = tuple(_annotate(by_key_b[k], layer=layer_name) for k in only_b_keys)

    evidence_changed: list[dict[str, object]] = []
    for k in shared_keys:
        va = by_key_a[k]
        vb = by_key_b[k]
        if va.get("evidence") != vb.get("evidence") or va.get("detail") != vb.get("detail"):
            evidence_changed.append(
                {
                    "layer": layer_name,
                    "invariant_name": va.get("invariant_name"),
                    "iteration": va.get("iteration"),
                    "a": {"detail": va.get("detail"), "evidence": va.get("evidence")},
                    "b": {"detail": vb.get("detail"), "evidence": vb.get("evidence")},
                }
            )

    return LayerDiff(
        layer=layer_name,
        only_in_a=only_in_a,
        only_in_b=only_in_b,
        evidence_changed=tuple(evidence_changed),
        a_success=success_a,
        b_success=success_b,
    )


def _violations_of(report: Report | None) -> list[dict[str, object]]:
    """Extract violations from a Report as JSON-shaped dicts."""
    if report is None:
        return []
    out: list[dict[str, object]] = []
    for v in report.result.violations:
        out.append(
            {
                "detail": v.detail,
                "evidence": dict(v.evidence),
                "invariant_name": v.invariant_name,
                "iteration": v.iteration,
            }
        )
    return out


def _violations_from_payload(layer_body: dict[str, object] | None) -> list[dict[str, object]]:
    """Same as :func:`_violations_of` but for the disk-serialised shape.

    ``layer_body`` is a top-level layer entry from a `report.json`; its
    ``result.violations`` list is already dict-shaped. We copy defensively
    so mutations here don't leak back into the loader's return value.
    """
    if layer_body is None:
        return []
    result = layer_body.get("result")
    if not isinstance(result, dict):
        return []
    raw = result.get("violations")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, object]] = []
    for v in raw:
        if isinstance(v, dict):
            out.append(dict(v))
    return out


def _success_from_payload(layer_body: dict[str, object] | None) -> bool:
    if layer_body is None:
        return True
    result = layer_body.get("result")
    if not isinstance(result, dict):
        return True
    success = result.get("success")
    return bool(success) if isinstance(success, bool) else True


def _key_of(layer: str, violation: dict[str, object]) -> ViolationKey:
    """Extract the ViolationKey from a JSON-shaped violation dict."""
    invariant_name = violation.get("invariant_name")
    iteration_raw = violation.get("iteration")
    if not isinstance(invariant_name, str):
        invariant_name = ""
    iteration: int | None
    if isinstance(iteration_raw, int) and not isinstance(iteration_raw, bool):
        iteration = iteration_raw
    else:
        iteration = None
    return ViolationKey(layer=layer, invariant_name=invariant_name, iteration=iteration)


def _sort_key(k: ViolationKey) -> tuple[str, str, int]:
    """Sort key that puts None iterations first (as -1), then numerically."""
    return (k.layer, k.invariant_name, -1 if k.iteration is None else k.iteration)


def _annotate(violation: dict[str, object], *, layer: str) -> dict[str, object]:
    """Return a copy of ``violation`` with the layer stamped in.

    The stored per-layer JSON does not carry a layer field (it's the
    dict key), but in the diff output every row needs to name its layer
    so a downstream consumer doesn't have to keep the layer context.
    """
    out: dict[str, object] = {"layer": layer}
    out.update(violation)
    return out
