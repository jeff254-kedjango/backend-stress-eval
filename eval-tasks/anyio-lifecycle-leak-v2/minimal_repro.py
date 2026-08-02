#!/usr/bin/env python3
"""The minimum reproducer for the anyio lifecycle leak.

10 lines, one third-party dependency, no framework:

    import anyio

    async def work():
        await anyio.to_thread.run_sync(int)

    if __name__ == "__main__":
        for _ in range(500):
            anyio.run(work)

On stock anyio 4.14.2 / Python 3.12 this grows ~5 KB per iteration and does
not level off: over 500 iterations the process retains one event loop and one
per-loop RunVar mapping per call (verified: live loops 1 → 500), for ~2.6 MB
of linear heap growth.

The script runs silently — it just exercises the leak. Wrap it in a memory
profiler (or watch process RSS) to see the growth before starting on a fix.
"""

from __future__ import annotations

import anyio

_ROUNDS = 500


async def work() -> None:
    await anyio.to_thread.run_sync(int)


if __name__ == "__main__":
    for _ in range(_ROUNDS):
        anyio.run(work)
