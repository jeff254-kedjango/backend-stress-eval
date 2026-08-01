#!/usr/bin/env python3
"""Cut anyio out — drive FastAPI's ASGI app via raw asyncio.

Chunk 7b-4 (novelty sharpening). The 7b-2 discovery on fastapi 0.115.0
showed the lifecycle leak attributes to anyio + asyncio + weakref, NOT
to fastapi/dependencies/models.py. But that measurement uses
``fastapi.testclient.TestClient`` internally, which is built on
``anyio.from_thread.start_blocking_portal`` — so the leak could be:

  (i) intrinsic to asyncio itself (repeated new_event_loop() + close())
  (ii) intrinsic to anyio's TestClient portal scaffolding
  (iii) something FastAPI or Starlette adds on top of both

This script drives the FastAPI app directly through the ASGI protocol:
``app({"type": "lifespan"}, receive, send)`` for lifespan and
``app({"type": "http", ...}, receive, send)`` for the probe. No anyio
runtime is touched. If we still see per-iteration heap growth attributed
to the anyio/asyncio backend, it's asyncio itself (option (i)); if the
growth VANISHES, it's anyio's portal scaffolding (option (ii)).

Output: a JSON blob to stdout with the growth numbers, and a top-N
attribution list. Same shape as the shelved ``attribute.py`` so we can
diff them apples-to-apples.

Not a runtime harness — one-shot investigation. Same "keep the runtime
harness clean" principle as the shelved ``attribute.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tracemalloc
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402

_DEFAULT_ROUNDS = 500
_SNAPSHOT_WARMUP_ITER = 10
_TOP_N = 25
_SHORT_PATH_COMPONENTS = 3
_HTTP_OK = 200
_STARTUP_WAIT_TURNS = 100  # max event-loop turns to wait for lifespan.startup.complete


def async_only_example_app() -> FastAPI:
    """Purely-async single-route FastAPI app — no threadpool bridge.

    Deliberately uses ``async def _root`` (not sync). Starlette
    dispatches sync handlers through ``run_in_threadpool`` -> anyio's
    ``to_thread.run_sync`` -> ``AsyncIOBackend.run_sync_in_worker_thread``,
    which reaches into anyio's worker-pool state (line 2598). We're
    trying to ISOLATE anyio's involvement, so all handlers must be async.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    async def _root() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _drive_lifespan_and_probe(app: object) -> None:
    """One full lifecycle: lifespan.startup → probe HTTP request → lifespan.shutdown.

    Uses the raw ASGI protocol — no TestClient, no anyio wrappers.
    """
    lifespan_events: list[dict[str, str]] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    lifespan_responses: list[dict[str, object]] = []
    lifespan_complete = asyncio.Event()

    async def lifespan_receive() -> dict[str, str]:
        if lifespan_events:
            return lifespan_events.pop(0)
        # Wait until shutdown_complete is emitted, then keep the app alive
        # by never returning. In practice the app returns from its lifespan
        # coroutine when it gets shutdown; we just avoid a hang here.
        await lifespan_complete.wait()
        return {"type": "lifespan.shutdown"}

    async def lifespan_send(msg: dict[str, object]) -> None:
        lifespan_responses.append(msg)
        if msg.get("type") == "lifespan.shutdown.complete":
            lifespan_complete.set()

    # Fire lifespan in a task so we can concurrently issue an HTTP request.
    lifespan_task = asyncio.create_task(
        app({"type": "lifespan"}, lifespan_receive, lifespan_send)  # type: ignore[operator]
    )
    # Wait until startup completes before firing the probe.
    for _ in range(_STARTUP_WAIT_TURNS):
        await asyncio.sleep(0)
        if any(r.get("type") == "lifespan.startup.complete" for r in lifespan_responses):
            break
    else:
        raise RuntimeError(
            f"lifespan.startup never completed within {_STARTUP_WAIT_TURNS} event-loop turns"
        )

    # Now the HTTP probe.
    http_events: list[dict[str, object]] = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    http_responses: list[dict[str, object]] = []

    async def http_receive() -> dict[str, object]:
        return http_events.pop(0) if http_events else {"type": "http.disconnect"}

    async def http_send(msg: dict[str, object]) -> None:
        http_responses.append(msg)

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }
    await app(scope, http_receive, http_send)  # type: ignore[operator]

    # Verify the probe got a 200.
    start = next((m for m in http_responses if m.get("type") == "http.response.start"), None)
    if start is None or start.get("status") != _HTTP_OK:
        raise RuntimeError(f"probe did not return {_HTTP_OK}, got: {start}")

    # Now trigger lifespan shutdown by letting the lifespan_task see it.
    # Actually the shutdown event is already queued in lifespan_events, so
    # the next lifespan_receive call will drain it. Just await the task.
    await lifespan_task


def _one_lifecycle(app_factory: object) -> None:
    """Fresh event loop, fresh app, one lifecycle. No anyio, no TestClient."""
    app = app_factory()  # type: ignore[operator]
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_drive_lifespan_and_probe(app))
    finally:
        loop.close()


def _run(rounds: int, warmup_iter: int, top_n: int) -> int:
    if rounds <= warmup_iter + 1:
        print(
            f"error: rounds ({rounds}) must exceed warmup_iter ({warmup_iter}) + 1",
            file=sys.stderr,
        )
        return 2

    tracemalloc.start(25)

    snap_early: tracemalloc.Snapshot | None = None
    snap_late: tracemalloc.Snapshot | None = None
    final_iter = rounds - 1

    for i in range(rounds):
        _one_lifecycle(async_only_example_app)
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

    print("# raw-asyncio attribution — no anyio, no TestClient")
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
    ns = ap.parse_args(argv[1:])
    return _run(rounds=ns.rounds, warmup_iter=ns.warmup_iter, top_n=ns.top)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
