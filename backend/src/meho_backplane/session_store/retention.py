# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The cleanup job: remove sessions older than the tenant's retention period.

An ``asyncio`` task owned by the FastAPI lifespan, like the other
retention loops (for example :mod:`meho_backplane.request_usage.retention`):

* it sleeps one interval first (``SESSION_STORE_CLEANUP_INTERVAL_SECONDS``,
  one hour by default), so it does not race the start;
* a session is old when its last upload (``last_seen_at``) is older than
  the tenant's period (``agent_session_retention``) or the default
  (``SESSION_STORE_RETENTION_DAYS_DEFAULT``);
* for each old session it locks the session row (an upload locks it
  too), checks the age again, deletes the files, then the rows. So an
  upload that came in after the search keeps its session. When the
  object storage fails, the rows stay and the next run tries again;
* at most :data:`MAX_PER_RUN` sessions per run, so one run stays short;
* one ``INTERNAL`` audit row per deleted session, in the same commit as
  the deleted rows;
* a failed run is logged and the loop goes on.

The job does nothing while the store is not configured, and the lifespan
does not start it when ``SESSION_STORE_CLEANUP_ENABLED`` is false.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.memory.audit import INTERNAL_METHOD
from meho_backplane.metrics import note_loop_tick
from meho_backplane.session_store.config import load_config
from meho_backplane.session_store.models import AgentSessionRecord, AgentSessionUpload
from meho_backplane.session_store.object_store import ObjectStore, get_object_store
from meho_backplane.session_store.service import delete_record_rows, retention_days_for

__all__ = [
    "CLEANUP_PATH",
    "MAX_PER_RUN",
    "SYSTEM_OPERATOR_SUB",
    "cleanup_once",
    "start_session_store_cleanup",
    "stop_session_store_cleanup",
]

_log = structlog.get_logger(__name__)

SYSTEM_OPERATOR_SUB: Final[str] = "system:session-store-cleanup"
CLEANUP_PATH: Final[str] = "sessions.cleanup"
MAX_PER_RUN: Final[int] = 500


async def _expired(now: datetime) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """Return ``(tenant_id, record_id, cutoff)`` of old sessions, oldest first."""
    config = load_config()
    found: list[tuple[uuid.UUID, uuid.UUID, datetime]] = []
    async with get_sessionmaker()() as session:
        tenants = list(await session.scalars(select(AgentSessionRecord.tenant_id).distinct()))
        for tenant_id in tenants:
            retention = await retention_days_for(session, tenant_id, config)
            cutoff = now - timedelta(days=retention.retention_days)
            rows = await session.execute(
                select(AgentSessionRecord.id)
                .where(
                    AgentSessionRecord.tenant_id == tenant_id,
                    AgentSessionRecord.last_seen_at < cutoff,
                )
                .order_by(AgentSessionRecord.last_seen_at)
                .limit(MAX_PER_RUN - len(found))
            )
            found.extend((tenant_id, record_id, cutoff) for record_id in rows.scalars())
            if len(found) >= MAX_PER_RUN:
                break
    return found


async def _remove(
    store: ObjectStore, tenant_id: uuid.UUID, record_id: uuid.UUID, cutoff: datetime
) -> bool:
    """Delete one old session's files, then its rows with their audit row in one commit.

    Returns ``False`` when the session is gone or had an upload since the search.
    """
    async with get_sessionmaker()() as session:
        session_id = await session.scalar(
            select(AgentSessionRecord.session_id)
            .where(AgentSessionRecord.id == record_id, AgentSessionRecord.last_seen_at < cutoff)
            .with_for_update()
        )
        if session_id is None:
            return False
        keys = list(
            await session.scalars(
                select(AgentSessionUpload.object_key).where(
                    AgentSessionUpload.record_id == record_id
                )
            )
        )
        await store.delete(keys)
        await delete_record_rows(session, record_id)
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                event_class=AuditEventClass.OPERATION_EXECUTION,
                occurred_at=datetime.now(UTC),
                operator_sub=SYSTEM_OPERATOR_SUB,
                tenant_id=tenant_id,
                method=INTERNAL_METHOD,
                path=CLEANUP_PATH,
                status_code=200,
                duration_ms=Decimal("0"),
                payload={"op_id": CLEANUP_PATH, "session_id": session_id, "objects": len(keys)},
            )
        )
        await session.commit()
    return True


async def cleanup_once(*, now: datetime | None = None) -> int | None:
    """Run one cleanup. Returns how many sessions went, or ``None`` when not configured."""
    if not load_config().configured:
        return None
    store = get_object_store()
    deleted = 0
    failed = 0
    for tenant_id, record_id, cutoff in await _expired(now or datetime.now(UTC)):
        try:
            removed = await _remove(store, tenant_id, record_id, cutoff)
        except Exception:
            failed += 1
            _log.warning("session_store_cleanup_failed", record_id=str(record_id), exc_info=True)
            continue
        if removed:
            deleted += 1
    _log.info("session_store_cleanup_done", deleted=deleted, failed=failed)
    return deleted


async def _cleanup_loop(interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("session_store_cleanup_tick_failed", exc_info=True)
        note_loop_tick("session_store_cleanup", interval_seconds)


def start_session_store_cleanup() -> asyncio.Task[None]:
    """Start the cleanup loop and return its task (the caller keeps a reference)."""
    interval = load_config().cleanup_interval_seconds
    return asyncio.create_task(_cleanup_loop(interval), name="session-store-cleanup")


async def stop_session_store_cleanup(task: asyncio.Task[None]) -> None:
    """Cancel the cleanup loop and wait for it to stop."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
