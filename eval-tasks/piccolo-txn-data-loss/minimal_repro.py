#!/usr/bin/env python3
"""Standalone reproducer for the disappearing-record bug.

One third-party dependency (piccolo, with its SQLite driver), no test
framework, a throwaway on-disk database. It models an ordinary flow: we try to
persist a record inside a transaction, the transaction does not go through, and
later we persist the same record again outside any transaction.

    async with DB.transaction():
        await order.save()
        ...            # something fails; the transaction is rolled back
    ...
    await order.save()  # retry, no transaction this time

The retry reports success and the object looks saved, yet afterwards the table
is empty — the record is silently gone, with no error raised anywhere.

Run it and inspect the final row count: it prints how many records ended up in
the table. The record we saved (twice) should be there. It is not.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from piccolo.columns import Varchar
from piccolo.engine.sqlite import SQLiteEngine
from piccolo.table import Table

_DB_PATH = os.path.join(tempfile.gettempdir(), "piccolo_repro.sqlite")
DB = SQLiteEngine(path=_DB_PATH)


class Order(Table, db=DB):
    reference = Varchar()


async def _main() -> None:
    # Fresh table every run.
    if os.path.exists(_DB_PATH):
        os.unlink(_DB_PATH)
    await Order.create_table(if_not_exists=True)

    order = Order(reference="ORD-1")

    # First attempt: inside a transaction that does not complete.
    try:
        async with DB.transaction():
            await order.save()
            raise RuntimeError("upstream step failed; abort this transaction")
    except RuntimeError:
        pass  # the caller handles the failure and moves on

    # Retry outside any transaction. This reports success.
    await order.save()

    # How many orders are actually stored?
    stored = await Order.count()
    print(f"orders stored: {stored}  (expected 1)")


if __name__ == "__main__":
    asyncio.run(_main())
