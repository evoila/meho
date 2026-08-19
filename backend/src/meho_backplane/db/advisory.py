# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Session-level PG advisory locks pinned to one connection (#3010).

Invariant: **the advisory lock and unlock must run on the same DBAPI
connection.** ``pg_try_advisory_lock`` takes a *session-level* lock owned
by the connection it executes on; only ``pg_advisory_unlock`` issued on
that same connection (or the connection actually closing) releases it.
A transaction commit or rollback does not.

Why a dedicated connection
==========================

An :class:`~sqlalchemy.ext.asyncio.AsyncSession` releases its pooled
connection on every ``commit()``; the next statement autobegins on
whatever connection the pool hands out next. A tick body that locks on
its work session and then commits mid-lock therefore unlocks on a
*different* connection: PostgreSQL answers ``WARNING: you don't own a
lock of type ExclusiveLock``, returns ``false``, nothing raises, and the
lock strands on the original connection — now idle in the pool. Every
later tick that draws any other connection reads the key as held and
skips silently. That was #3010: the sensor runner delivered ~35-50 % of
its expected evaluations while ``pg_locks`` showed an *idle* holder of
the runner key, and the #2763 watchdog stamped every lock-miss tick as
healthy.

:func:`advisory_lock` closes the hole by hosting the lock on a dedicated
:meth:`~sqlalchemy.ext.asyncio.AsyncEngine.connect` connection that
carries **only** the lock/unlock statements. The caller's own sessions
commit freely inside the block without ever migrating the lock's
ownership, and the ``finally`` unlock provably lands on the connection
that acquired the key. The same shape has run correctly in
:mod:`meho_backplane.topology.scheduler` (its ``lock_session`` never
commits) since G9.1.

Why not ``pg_try_advisory_xact_lock``
=====================================

A transaction-scoped lock dies at the first ``COMMIT``. Every consumer
of this helper must commit *inside* the locked region (the
claim/advance-before-dispatch durability discipline), so an xact lock
would silently evaporate mid-batch. The session-level lock on a pinned
connection keeps the whole tick body covered.

Cost: one extra pooled connection is checked out per subsystem for the
duration of its tick. With five lock-holding loops and the default pool
size of 10 that is well inside budget; the lock connection runs two
trivial ``SELECT``\\ s and is returned on block exit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import structlog
from sqlalchemy import text

from meho_backplane.db.engine import get_engine
from meho_backplane.metrics import ADVISORY_LOCK_BUSY_TOTAL

__all__ = ["advisory_lock"]


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (not a module-level proxy).

    Same discipline as :mod:`meho_backplane.checks.runner`: a module-level
    proxy caches its bound methods on first use under the production
    ``cache_logger_on_first_use=True`` config, orphaning them from a later
    :func:`structlog.testing.capture_logs`.
    """
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(__name__))


@asynccontextmanager
async def advisory_lock(key: int, *, subsystem: str) -> AsyncIterator[bool]:
    """Hold the session-level PG advisory lock *key* on a pinned connection.

    Yields ``True`` when the lock was acquired (or the engine dialect has
    no advisory locks — the SQLite single-replica test path) and the
    caller should run its tick body; yields ``False`` when another holder
    owns *key* and the caller should skip. On acquisition the lock is
    hosted on a dedicated connection checked out for the whole ``async
    with`` block, so the caller's work sessions may commit freely without
    ever moving the lock, and the unlock in the ``finally`` runs on the
    very connection that acquired it (the #3010 invariant).

    *subsystem* labels the ``advisory_lock_busy_total`` counter
    incremented on a miss and the ``advisory_unlock_failed`` tripwire
    log. Callers add their own subsystem-vocabulary skip log next to the
    ``False`` branch; the counter here is the uniform cross-subsystem
    surface.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return
    async with engine.connect() as conn:
        locked = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}))
        if not locked:
            ADVISORY_LOCK_BUSY_TOTAL.labels(subsystem=subsystem).inc()
            yield False
            return
        try:
            yield True
        finally:
            try:
                released = bool(
                    await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                )
            except BaseException:
                # The unlock could not run (cancellation delivered during
                # cleanup, connection died mid-tick). Invalidate so the raw
                # connection is closed instead of returning to the pool
                # still owning the lock — closing the connection is what
                # releases a session-level advisory lock server-side.
                await conn.invalidate()
                raise
            if not released:
                # Tripwire: with lock + unlock pinned to one connection
                # this cannot fire; if it does, the #3010 invariant has
                # regressed somewhere between acquire and here.
                _log().warning("advisory_unlock_failed", subsystem=subsystem, key=key)
