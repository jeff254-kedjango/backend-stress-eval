#!/usr/bin/env python3
"""Bare-anyio reproducer — no FastAPI, no Starlette, no ASGI.

Chunk 7b-5 (novelty end-to-end verification). ``findings.md`` claims the
leak lives in anyio's asyncio backend, based on tracemalloc attribution
of a FastAPI+Starlette lifecycle soak-test. This script tries to
reproduce the shape with just anyio itself:

- Mode ``run-only``: ``anyio.run(async_fn)`` where ``async_fn = async def f: pass``.
  Measures per-``AsyncIOBackend.run()`` scaffolding without invoking the
  worker pool.
- Mode ``run-and-thread``: ``anyio.run(async_fn)`` where ``async_fn`` does
  ``await anyio.to_thread.run_sync(lambda: None)`` once. Adds the worker
  pool + limiter allocations (``_asyncio.py:2598-2599``, ``:2052``).

The difference between the two modes = the anyio worker-pool contribution.
If both leak, and the run-and-thread mode leaks more, the claim in
``findings.md`` is proven end-to-end at the anyio level.

Not a runtime harness — one-shot investigation.
"""

from __future__ import annotations

import argparse
import sys
import tracemalloc
from pathlib import Path

import anyio

_DEFAULT_ROUNDS = 500
_SNAPSHOT_WARMUP_ITER = 10
_TOP_N = 20
_SHORT_PATH_COMPONENTS = 3


async def _noop() -> None:
    """Absolute minimum async body — spin the loop once, return."""


async def _with_to_thread() -> None:
    """Invokes anyio's worker pool once."""
    await anyio.to_thread.run_sync(int)  # int() as the cheapest sync callable


def _run(rounds: int, warmup_iter: int, top_n: int, mode: str) -> int:
    if rounds <= warmup_iter + 1:
        print(
            f"error: rounds ({rounds}) must exceed warmup_iter ({warmup_iter}) + 1",
            file=sys.stderr,
        )
        return 2

    if mode == "run-only":
        target = _noop
        label = "anyio.run(_noop) — no worker pool"
    elif mode == "run-and-thread":
        target = _with_to_thread
        label = "anyio.run(_with_to_thread) — invokes worker pool once/iter"
    else:
        print(f"error: unknown mode {mode!r}; use run-only or run-and-thread", file=sys.stderr)
        return 2

    tracemalloc.start(25)

    snap_early: tracemalloc.Snapshot | None = None
    snap_late: tracemalloc.Snapshot | None = None
    final_iter = rounds - 1

    for i in range(rounds):
        anyio.run(target)
        if i == warmup_iter:
            snap_early = tracemalloc.take_snapshot()
        elif i == final_iter:
            snap_late = tracemalloc.take_snapshot()

    tracemalloc.stop()

    if snap_early is None or snap_late is None:
        print("error: failed to capture both snapshots", file=sys.stderr)
        return 2

    diff = snap_late.compare_to(snap_early, "lineno")
    span_iters = final_iter - warmup_iter
    total_growth_kb = sum(s.size_diff for s in diff) / 1024
    per_iter = total_growth_kb / span_iters

    print(f"# bare-anyio attribution — {label}")
    print(f"# rounds: {rounds}   snapshot span: iter {warmup_iter} -> iter {final_iter}")
    print(f"# iterations spanned: {span_iters}")
    print(f"# total heap delta:  {total_growth_kb:+.2f} KB  ({per_iter:+.4f} KB/iter)")
    print()
    print(f"# Top {top_n} allocating locations by size_diff:")
    print(f"# {'size_diff_kb':>12}  {'count_diff':>10}  {'kb_per_iter':>11}  location")
    for stat in diff[:top_n]:
        loc = stat.traceback[0]
        kb_diff = stat.size_diff / 1024
        kb_per_iter = kb_diff / span_iters
        parts = Path(loc.filename).parts
        if len(parts) > _SHORT_PATH_COMPONENTS:
            short = "/".join(parts[-_SHORT_PATH_COMPONENTS:])
        else:
            short = loc.filename
        row = (
            f"  {kb_diff:>+11.2f}   {stat.count_diff:>+10d}  "
            f"{kb_per_iter:>+10.4f}  {short}:{loc.lineno}"
        )
        print(row)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rounds", type=int, default=_DEFAULT_ROUNDS)
    ap.add_argument("--warmup-iter", type=int, default=_SNAPSHOT_WARMUP_ITER)
    ap.add_argument("--top", type=int, default=_TOP_N)
    ap.add_argument(
        "--mode",
        choices=("run-only", "run-and-thread"),
        default="run-and-thread",
        help="run-only: anyio.run(noop); run-and-thread: anyio.run(to_thread_call)",
    )
    ns = ap.parse_args(argv[1:])
    return _run(rounds=ns.rounds, warmup_iter=ns.warmup_iter, top_n=ns.top, mode=ns.mode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
