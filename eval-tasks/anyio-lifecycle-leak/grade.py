#!/usr/bin/env python3
"""Grade a replay report against the anyio-lifecycle-leak baseline.

Implements RUBRIC.md as four independent gates (G1-G4). Exits 0 on
PASS, 1 on FAIL, 2 on setup error.

Usage:
    grade.py <baseline-attribution.json> <replay/report.json>

Both files must have ``schema_version == "1"``. Grader is stdlib-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_EXIT_PASS = 0
_EXIT_FAIL = 1
_EXIT_SETUP = 2

_EXPECTED_ARGV_LEN = 3
_EXPECTED_SCHEMA = "1"

# G1: slope must be at most this on the replay.
_G1_MAX_SLOPE_KB_PER_ITER = 1.0
# G2: total delta must be at most this on the replay.
_G2_MAX_TOTAL_DELTA_KB = 500.0
# G3: none of these anyio-backend lines may appear in the replay top-5.
_G3_BLACKLIST: tuple[tuple[str, int], ...] = (
    ("anyio/_backends/_asyncio.py", 2481),
    ("anyio/_backends/_asyncio.py", 2598),
    ("anyio/_backends/_asyncio.py", 2599),
    ("anyio/_backends/_asyncio.py", 2052),
    ("anyio/_backends/_asyncio.py", 2053),
)
_G3_TOP_K = 5  # only the top-5 entries are inspected — deeper doesn't count


def _load_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        raise SystemExit(_EXIT_SETUP)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(_EXIT_SETUP) from exc
    if not isinstance(data, dict):
        print(f"error: {path} does not decode to a JSON object", file=sys.stderr)
        raise SystemExit(_EXIT_SETUP)
    return data


def _check_schema(replay: dict[str, object]) -> None:
    schema = replay.get("schema_version")
    if schema != _EXPECTED_SCHEMA:
        print(
            f"error: replay schema_version {schema!r} != expected {_EXPECTED_SCHEMA!r}",
            file=sys.stderr,
        )
        raise SystemExit(_EXIT_SETUP)


def _check_g1(replay: dict[str, object]) -> tuple[bool, str]:
    slope = replay.get("slope_kb_per_iter")
    if not isinstance(slope, int | float):
        return False, "slope_kb_per_iter missing or non-numeric"
    if slope <= _G1_MAX_SLOPE_KB_PER_ITER:
        return True, f"slope {slope:.4f} KB/iter <= {_G1_MAX_SLOPE_KB_PER_ITER}"
    return False, f"slope {slope:.4f} KB/iter > {_G1_MAX_SLOPE_KB_PER_ITER}"


def _check_g2(replay: dict[str, object]) -> tuple[bool, str]:
    total = replay.get("total_delta_kb")
    if not isinstance(total, int | float):
        return False, "total_delta_kb missing or non-numeric"
    if total <= _G2_MAX_TOTAL_DELTA_KB:
        return True, f"total {total:.2f} KB <= {_G2_MAX_TOTAL_DELTA_KB}"
    return False, f"total {total:.2f} KB > {_G2_MAX_TOTAL_DELTA_KB}"


def _check_g3(replay: dict[str, object]) -> tuple[bool, str]:
    top = replay.get("top_lines")
    if not isinstance(top, list):
        return False, "top_lines missing or not a list"
    offenders: list[str] = []
    for entry in top[:_G3_TOP_K]:
        if not isinstance(entry, dict):
            continue
        f = entry.get("file")
        ln = entry.get("lineno")
        if isinstance(f, str) and isinstance(ln, int) and (f, ln) in _G3_BLACKLIST:
            offenders.append(f"{f}:{ln}")
    if not offenders:
        return True, "no blacklisted anyio backend line in top-5"
    return False, f"blacklisted line(s) still in top-{_G3_TOP_K}: {', '.join(offenders)}"


def _check_g4(baseline: dict[str, object], replay: dict[str, object]) -> tuple[bool, str]:
    """Environment sanity: same anyio + Python version, same schema, plausible span.

    Not a fix-quality check per se — it prevents grading a replay collected
    on a different anyio version and pretending the drop is a fix.
    """
    for key in ("anyio_version", "python_version"):
        if baseline.get(key) != replay.get(key):
            return False, (
                f"{key} mismatch: baseline={baseline.get(key)!r} " f"replay={replay.get(key)!r}"
            )
    b_span = baseline.get("span_iters")
    r_span = replay.get("span_iters")
    if not isinstance(b_span, int) or not isinstance(r_span, int) or r_span < b_span:
        return False, (f"span_iters shorter than baseline: baseline={b_span!r} replay={r_span!r}")
    return True, "env + span match baseline"


def main(argv: list[str]) -> int:
    if len(argv) != _EXPECTED_ARGV_LEN:
        print(
            f"usage: {argv[0]} <baseline-attribution.json> <replay-report.json>",
            file=sys.stderr,
        )
        return _EXIT_SETUP
    baseline = _load_json(Path(argv[1]))
    replay = _load_json(Path(argv[2]))
    _check_schema(replay)

    checks = [
        ("G1", "slope invariant clears (<=1.0 KB/iter)", _check_g1(replay)),
        ("G2", "total delta bounded (<=500 KB)", _check_g2(replay)),
        ("G3", "no blacklisted anyio backend line in top-5", _check_g3(replay)),
        ("G4", "environment matches baseline", _check_g4(baseline, replay)),
    ]

    fail = False
    for gate, label, (ok, detail) in checks:
        verdict = "PASS" if ok else "FAIL"
        print(f"  {gate}  {label:<52s}  {verdict}  {detail}")
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
