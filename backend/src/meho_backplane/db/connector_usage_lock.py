# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One lock that keeps a connector delete apart from new users of a connector (#5027).

A connector delete first checks that nothing uses the connector (sensors,
runbook templates, hosted agents, schedules, waiting approvals, queued
operation runs) and then removes the rows. A sensor, runbook template,
hosted agent or schedule saved between the check and the delete would be
left pointing at a connector that no longer exists.

So the two sides share one PostgreSQL advisory lock, held for the
transaction:

* The delete takes it **exclusive** (:func:`lock_connector_usage_exclusive`)
  as the first statement of its transaction, then checks and deletes.
* Every writer that can save a new user takes it **shared**
  (:func:`lock_connector_usage_shared`) as the first statement of its write
  transaction. Shared locks do not wait for each other, so normal writes
  never queue behind one another. They only wait while a delete runs.

A writer that was first commits before the delete's check runs, so the
check sees its row and refuses. A writer that comes second waits until the
delete commits; the sensor writer then reads the operation again in its own
transaction and fails cleanly because it is gone.

Both locks are transaction-level (``pg_advisory_xact_lock`` and
``pg_advisory_xact_lock_shared``): PostgreSQL releases them on commit or
rollback, and a crash can never leave one behind. On other databases (the
single-writer SQLite test path) both calls do nothing.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CONNECTOR_USAGE_LOCK_KEY",
    "lock_connector_usage_exclusive",
    "lock_connector_usage_shared",
]

#: Fixed advisory-lock key. A blake2b digest of a fixed label, masked to 63
#: bits so it stays a non-negative ``bigint`` (the same shape as the other
#: advisory-lock keys in the code base).
CONNECTOR_USAGE_LOCK_KEY: int = (
    int.from_bytes(hashlib.blake2b(b"meho:connector-usage", digest_size=8).digest(), "big")
    & 0x7FFF_FFFF_FFFF_FFFF
)


_SHARED_LOCK = text("SELECT pg_advisory_xact_lock_shared(:k)")
_EXCLUSIVE_LOCK = text("SELECT pg_advisory_xact_lock(:k)")


async def _lock(session: AsyncSession, statement: TextClause) -> None:
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(statement, {"k": CONNECTOR_USAGE_LOCK_KEY})


async def lock_connector_usage_shared(session: AsyncSession) -> None:
    """Take the shared lock in *session*'s transaction (a writer of a new user)."""
    await _lock(session, _SHARED_LOCK)


async def lock_connector_usage_exclusive(session: AsyncSession) -> None:
    """Take the exclusive lock in *session*'s transaction (a connector delete)."""
    await _lock(session, _EXCLUSIVE_LOCK)
