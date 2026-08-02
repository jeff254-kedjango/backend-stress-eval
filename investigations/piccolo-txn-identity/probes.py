#!/usr/bin/env python3
"""piccolo 1.x L3 interaction hunt: transaction-rollback + object identity.

Ascending-maturity sweep, >=1000 star filter. piccolo = 1,934 GitHub stars.
Pivot to L3 (feature-interaction) bugs: two individually-correct operations
that corrupt shared state only in combination. Reuses the bse_hunt Postgres.

The interaction (Rule 9 — reproduced before theorising):
  1. obj.save() INSIDE a transaction assigns obj.id (the INSERT's PK).   [correct]
  2. the transaction ROLLS BACK — the row is gone from the DB.           [correct]
  3. obj STILL holds the phantom id from the rolled-back insert.         [the seam]
  4. obj.save() AGAIN (outside the txn) sees id-is-set -> issues UPDATE
     on a row that never committed -> updates 0 rows -> the object is
     SILENTLY LOST. No exception, no crash, tests green.                 [DATA LOSS]

Each op is correct in isolation; the combination loses data silently — the
defining shape of an L3 differentiating bug (symptom far from cause).

Probes:
  P1  phantom_id_survives_rollback — after rollback, is obj.id still set?
  P2  resave_causes_silent_loss    — does the re-save UPDATE-into-the-void
                                      instead of INSERT, losing the row?

Run:
  python probes.py           # characterise the interaction
  python probes.py --teeth   # assert probes detect loss vs. correct persistence
"""
from __future__ import annotations

import asyncio
import sys

from piccolo.columns import Varchar
from piccolo.engine.postgres import PostgresEngine
from piccolo.table import Table

DB = PostgresEngine(config={"dsn": "postgresql://jeff@/bse_hunt"})


class Widget(Table, db=DB):
    name = Varchar()


async def _fresh() -> None:
    await Widget.raw("DROP TABLE IF EXISTS widget CASCADE")
    await Widget.create_table(if_not_exists=True)


async def probe_phantom_id_survives_rollback() -> bool:
    """Return True iff obj.id is still set after the enclosing txn rolled back."""
    await _fresh()
    w = Widget(name="x")
    try:
        async with DB.transaction():
            await w.save()
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    return w.id is not None


async def probe_resave_causes_silent_loss(*, rollback: bool = True) -> str:
    """Save in a txn (optionally rolled back), then save again outside it.
    Return a verdict string describing what ended up in the DB.

    TEETH lever ``rollback=False``: COMMIT the txn instead. Then the row is
    real, the re-save updates it correctly, and total rows MUST be 1 — proving
    the probe reports genuine persistence, not a fabricated loss.
    """
    await _fresh()
    w = Widget(name="payload")
    if rollback:
        try:
            async with DB.transaction():
                await w.save()
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
    else:
        async with DB.transaction():
            await w.save()  # commits on clean exit
    # Second save OUTSIDE the transaction.
    await w.save()
    total = await Widget.count()
    named = await Widget.count().where(Widget.name == "payload")
    return f"total={total} named_payload={named} obj_id={w.id}"


async def _run_real() -> int:
    print("=== P1: does a phantom id survive transaction rollback? ===")
    phantom = await probe_phantom_id_survives_rollback()
    print(f"  obj.id still set after rollback: {phantom}")

    print("=== P2: does re-saving after rollback silently lose the row? ===")
    verdict = await probe_resave_causes_silent_loss(rollback=True)
    print(f"  after rollback + re-save: {verdict}")
    # Data loss = the object thinks it saved (id set) but 0 rows exist.
    lost = "total=0" in verdict
    print(f"\n  -> SILENT DATA LOSS (L3 interaction bug): {phantom and lost}")
    print("     Each op correct; the combination loses the row with no error.")
    return 0 if (phantom and lost) else 1


async def _run_teeth() -> int:
    print("=== TEETH: probe must report genuine persistence on the commit path ===")
    ok = True
    # Commit path: row is real, re-save updates it, total MUST be 1 (no false loss).
    committed = await probe_resave_causes_silent_loss(rollback=False)
    t1 = "total=1" in committed
    print(f"  commit path persists row (expect total=1): {committed}  PASS={t1}")
    ok &= t1
    # Rollback path IS flagged as loss (positive control).
    rolled = await probe_resave_causes_silent_loss(rollback=True)
    t2 = "total=0" in rolled
    print(f"  rollback path loses row (expect total=0): {rolled}  PASS={t2}")
    ok &= t2
    print(f"TEETH: {'ALL PASS — probe distinguishes loss from persistence' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_teeth() if "--teeth" in sys.argv else _run_real()))
