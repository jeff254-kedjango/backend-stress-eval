#!/usr/bin/env python3
"""Grade a discovery replay against the frozen baseline.

Implements RUBRIC.md verbatim as four checks (G1-G4). Exits 0 on PASS,
1 on FAIL, 2 on setup error. Stdout carries the human-readable verdict;
stderr carries setup/environmental errors.

Callable both directly (``python grade.py replay/report.json``) and from
``reproduce.sh`` (which is a thin shell wrapper).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_EXIT_PASS = 0
_EXIT_FAIL = 1
_EXIT_SETUP = 2
_EXIT_SHELVED = 3  # eval task shelved — refuse to grade (Rule 5, fail-loud)

_MAX_KB_PER_ITER = 1.0
_EXPECTED_ARGV_LEN = 3  # script name + baseline path + replay path
_SHELVED_MARKER = "_shelved"  # any path component named this → refuse


def _load_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        raise SystemExit(_EXIT_SETUP)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(_EXIT_SETUP) from exc


def _layer(report: dict[str, object], name: str) -> dict[str, object] | None:
    layers = report.get("layers")
    if not isinstance(layers, dict):
        return None
    layer = layers.get(name)
    if not isinstance(layer, dict):
        return None
    return layer


def _result(layer: dict[str, object]) -> dict[str, object]:
    result = layer.get("result")
    if not isinstance(result, dict):
        return {}
    return result


def _violations(layer: dict[str, object]) -> list[dict[str, object]]:
    result = _result(layer)
    vs = result.get("violations")
    if not isinstance(vs, list):
        return []
    return [v for v in vs if isinstance(v, dict)]


def _check_g1(replay: dict[str, object]) -> tuple[bool, str]:
    """Layer-2 slope invariant clears: no rss_slope_bounded violation with
    slope_kb_per_iter > 1.0."""
    layer = _layer(replay, "layer2_lifecycle")
    if layer is None:
        return False, "layer2_lifecycle missing from replay"
    offenders: list[float] = []
    for v in _violations(layer):
        if v.get("invariant_name") != "rss_slope_bounded":
            continue
        evidence = v.get("evidence")
        if not isinstance(evidence, dict):
            continue
        slope = evidence.get("slope_kb_per_iter")
        if isinstance(slope, int | float) and slope > _MAX_KB_PER_ITER:
            offenders.append(float(slope))
    if not offenders:
        return True, "layer2 slope invariant clears"
    return False, (
        f"{len(offenders)} offending slope violation(s): "
        f"{', '.join(f'{s:.4f} KB/iter' for s in offenders)}"
    )


def _check_g2(replay: dict[str, object]) -> tuple[bool, str]:
    """Layer-2 threshold invariant clears: no rss_return_to_baseline violation."""
    layer = _layer(replay, "layer2_lifecycle")
    if layer is None:
        return False, "layer2_lifecycle missing from replay"
    hits = [v for v in _violations(layer) if v.get("invariant_name") == "rss_return_to_baseline"]
    if not hits:
        return True, "layer2 threshold invariant clears"
    return False, f"{len(hits)} threshold violation(s)"


def _check_g3(replay: dict[str, object]) -> tuple[bool, str]:
    """Layer-2 result.success is true."""
    layer = _layer(replay, "layer2_lifecycle")
    if layer is None:
        return False, "layer2_lifecycle missing from replay"
    success = _result(layer).get("success")
    if success is True:
        return True, "layer2 result.success is true"
    return False, f"layer2 result.success is {success!r}"


def _check_g4(baseline: dict[str, object], replay: dict[str, object]) -> tuple[bool, str]:
    """No layer that was PASS in the baseline is FAIL in the replay."""
    baseline_layers = baseline.get("layers")
    replay_layers = replay.get("layers")
    if not isinstance(baseline_layers, dict) or not isinstance(replay_layers, dict):
        return False, "layers missing from baseline or replay"
    regressed: list[str] = []
    for name, layer in baseline_layers.items():
        if not isinstance(layer, dict):
            continue
        if _result(layer).get("success") is not True:
            continue
        # Was PASS in baseline. Must still be PASS in replay.
        replay_layer = replay_layers.get(name)
        if not isinstance(replay_layer, dict):
            regressed.append(name)
            continue
        if _result(replay_layer).get("success") is not True:
            regressed.append(name)
    if not regressed:
        return True, "no previously-green layer regressed"
    return False, f"regressed: {', '.join(sorted(regressed))}"


def main(argv: list[str]) -> int:
    # Refuse to grade if this grader lives under a "_shelved" directory. The
    # eval task was retired as a negative result — see SHELVED.md next to
    # this script for the full audit trail. Rule 5: fail loud and refuse to
    # produce a misleading PASS/FAIL that a caller might mistake for a
    # submission-quality verdict.
    grader_path = Path(argv[0]).resolve()
    if _SHELVED_MARKER in grader_path.parts:
        shelved_md = grader_path.parent / "SHELVED.md"
        print(
            "error: this eval task is shelved as a documented negative result "
            f"(see {shelved_md}). Grading is disabled. "
            "Do not submit; see Chunk 7b for the replacement search.",
            file=sys.stderr,
        )
        return _EXIT_SHELVED
    if len(argv) != _EXPECTED_ARGV_LEN:
        print(
            f"usage: {argv[0]} <baseline-report.json> <replay-report.json>",
            file=sys.stderr,
        )
        return _EXIT_SETUP
    baseline = _load_json(Path(argv[1]))
    replay = _load_json(Path(argv[2]))

    checks = [
        ("G1", "layer2 slope invariant clears", _check_g1(replay)),
        ("G2", "layer2 threshold invariant clears", _check_g2(replay)),
        ("G3", "layer2 result.success is true", _check_g3(replay)),
        ("G4", "no previously-green layer regressed", _check_g4(baseline, replay)),
    ]

    fail = False
    for gate, label, (ok, detail) in checks:
        verdict = "PASS" if ok else "FAIL"
        print(f"  {gate}  {label:<40s}  {verdict}  {detail}")
        if not ok:
            fail = True

    print()
    if fail:
        print("==> OVERALL: FAIL")
        return _EXIT_FAIL
    print("==> OVERALL: PASS")
    return _EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
