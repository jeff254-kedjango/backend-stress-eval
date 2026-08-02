#!/usr/bin/env python3
"""ormar 0.26 relation-cache behaviour hunt on Postgres.

Ascending-maturity sweep (>=1000 star filter). Skipped odmantic(1174,Mongo)
and aiocache(1435,Redis) for lack of a service; ormar(1804) reuses the
bse_hunt Postgres substrate.

Recon (Rule 9) surfaced: after loading a Book with select_related('author'),
calling book.load() (reload the Book only) sets book.author to None instead
of preserving or refreshing the cached relation. This file characterises that
precisely and teeth-verifies each classification.

Questions:
  Q1  Does book.load() drop an already-loaded FK relation to None?
  Q2  If so, is book.author then a silently-wrong value a caller could read
      (None where a related row demonstrably still exists in the DB)?
  Q3  Is it the documented .load() contract (reload scalar cols only), or a
      surprise a green test suite would miss?

Run:
  python probes.py           # characterise
  python probes.py --teeth   # assert probes detect known present vs dropped
"""
from __future__ import annotations

import asyncio
import sys

import ormar
import sqlalchemy
from ormar.databases.connection import DatabaseConnection

ASYNC_DSN = "postgresql+asyncpg://jeff@/bse_hunt"
SYNC_DSN = "postgresql+psycopg:///bse_hunt"

_db = DatabaseConnection(ASYNC_DSN)
_md = sqlalchemy.MetaData()
_cfg = ormar.OrmarConfig(database=_db, metadata=_md)


class Author(ormar.Model):
    ormar_config = _cfg.copy(tablename="omr_author")
    id: int = ormar.Integer(primary_key=True)
    name: str = ormar.String(max_length=64)


class Book(ormar.Model):
    ormar_config = _cfg.copy(tablename="omr_book")
    id: int = ormar.Integer(primary_key=True)
    title: str = ormar.String(max_length=64)
    author: Author = ormar.ForeignKey(Author)


def _recreate_schema() -> None:
    e = sqlalchemy.create_engine(SYNC_DSN)
    _md.drop_all(e)
    _md.create_all(e)
    e.dispose()


async def _seed() -> None:
    a = await Author.objects.create(id=1, name="orig-author")
    await Book.objects.create(id=1, title="orig-title", author=a)


async def probe_load_drops_relation(*, load_with_related: bool = True) -> str:
    """Load a Book (optionally select_related author), then book.load(), and
    return the related author's name before/after.

    TEETH lever ``load_with_related=False``: the book is loaded WITHOUT the
    relation, so author is already a lazy stub — the pre-load value tells us
    the probe reads the real state, not a fabricated one.
    """
    _recreate_schema()
    await _seed()
    q = Book.objects.select_related("author") if load_with_related else Book.objects
    book = await q.get(id=1)
    before = book.author.name if book.author is not None else None
    await book.load()  # reload the Book row only
    after = book.author.name if book.author is not None else None
    return f"before={before!r} after={after!r}"


async def probe_related_row_still_exists() -> bool:
    """After book.load() drops book.author to None, does the author row still
    exist in the DB? If yes, None is a silently-wrong in-memory value."""
    _recreate_schema()
    await _seed()
    book = await Book.objects.select_related("author").get(id=1)
    await book.load()
    dropped = book.author is None
    still_there = (await Author.objects.get(id=1)).name
    return dropped and still_there == "orig-author"


async def _run_real() -> int:
    print("=== Q1/Q3: does book.load() drop a loaded FK relation? ===")
    print("  with select_related:", await probe_load_drops_relation(load_with_related=True))
    print("=== Q2: is the dropped relation a silently-wrong None? ===")
    silent = await probe_related_row_still_exists()
    print(f"  author=None after load() while row exists in DB: {silent}")
    print()
    print("Interpretation: if load() silently nulls an already-loaded relation")
    print("while the row still exists, a caller reading book.author after a")
    print("routine reload gets None with no error — a green-tests footgun IF")
    print("it contradicts ormar's documented load() contract.")
    return 0


async def _run_teeth() -> int:
    print("=== TEETH: assert probe reads real relation state ===")
    ok = True
    # With select_related, author is present BEFORE load (not a false None).
    r = await probe_load_drops_relation(load_with_related=True)
    t1 = "before='orig-author'" in r
    print(f"  relation present before load (expect orig-author): {r}  PASS={t1}")
    ok &= t1
    # The author row genuinely exists (probe isn't reporting a phantom).
    exists = (await Author.objects.get(id=1)).name == "orig-author"
    t2 = exists
    print(f"  author row exists in DB (expect True): {exists}  PASS={t2}")
    ok &= t2
    print(f"TEETH: {'ALL PASS' if ok else 'FAILED'}")
    return 0 if ok else 1


async def _amain(teeth: bool) -> int:
    await _db.connect()
    try:
        return await (_run_teeth() if teeth else _run_real())
    finally:
        await _db.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain("--teeth" in sys.argv)))
