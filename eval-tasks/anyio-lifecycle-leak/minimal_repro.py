#!/usr/bin/env python3
"""The minimum reproducer for the anyio lifecycle leak.

10 lines, one third-party dependency, no framework:

    import anyio

    async def work():
        await anyio.to_thread.run_sync(int)

    if __name__ == "__main__":
        for _ in range(500):
            anyio.run(work)

Baseline (anyio 4.14.2, Python 3.12.13, Linux WSL2, tracemalloc snapshots
at iter 10 → iter 499): +2.5 MB heap growth, +5.2 KB/iter slope, top
attribution five lines of ``anyio/_backends/_asyncio.py``. See
``baseline-attribution.json`` for the byte-stable record.

This file is deliberately identical to the code block a task-taker sees
in README.md — kept as its own file so a task-taker can ``python
minimal_repro.py`` and confirm the leak reproduces before starting on
a fix.
"""

from __future__ import annotations

import anyio

_ROUNDS = 500


async def work() -> None:
    await anyio.to_thread.run_sync(int)


if __name__ == "__main__":
    for _ in range(_ROUNDS):
        anyio.run(work)
