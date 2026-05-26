# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background sweeper for time-bounded permission elevations.

G11.2-T6 (#819) under Initiative #803 (the P3 agent identity + RBAC +
approval gate). Automatically removes ``agent_permission`` rows whose
``expires_at < now(UTC)`` — reverting agents to their baseline
permissions after a change window ends, without any operator action.

Design mirrors :mod:`meho_backplane.memory.expiry` (the G5.2-T1 memory-
expiry sweeper): an ``asyncio.Task`` the FastAPI lifespan owns, a fixed
tick cadence (default 5 minutes), sleep-then-sweep ordering, per-tick
``try/except`` for failure isolation, and one audit row per affected
tenant per tick.

Why not the memory sweeper
--------------------------

Memory expiry reads ``doc_metadata.expires_at`` (a JSONB column). Grant
expiry reads ``agent_permission.expires_at`` (a first-class
``timestamptz`` column with a b-tree index). The two sweepers share the
lifecycle pattern but the DB queries are unrelated; combining them would
add cross-module coupling for no benefit.

Failure isolation
-----------------

Each tick runs inside its own ``try/except``. One bad tick (transient DB
blip, malformed timestamp row) is logged as a warning and the loop
continues to the next cadence — the same "loop survival over per-tick
correctness" trade-off the memory sweeper and topology scheduler make.

Audit rows
----------

Each tick that deletes at least one row writes one ``agent_permission``
audit row per affected tenant via the standard
:func:`~meho_backplane.memory.audit.write_internal_audit_row` helper,
carrying the count of expired grants per tenant and the global tick
duration.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    SYSTEM_OPERATOR_SUB,
    write_internal_audit_row,
)
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    import uuid

__all__ = [
    "start_grant_expiry_sweeper",
    "stop_grant_expiry_sweeper",
]

_log = structlog.get_logger(__name__)

#: Synthetic audit path for grant-expiry rows, mirroring the
#: ``MEMORY_EXPIRE_PATH`` convention in :mod:`meho_backplane.memory.audit`.
_GRANT_EXPIRE_PATH = "/internal/agent-permission/expire"


async def _run_one_tick() -> None:
    """One sweep: find expired grant rows, delete them, audit per tenant.

    Two-step query (SELECT then DELETE) for dialect portability — SQLite
    under the version shipped with the CI Python image lacks
    ``DELETE ... RETURNING`` support for complex WHERE clauses. The
    select pulls ``(tenant_id, id)`` tuples; the DELETE reaps by
    primary key in one statement; per-tenant aggregation drives the
    audit rows.

    The ``expires_at < now_utc`` predicate uses a Python-side
    ``datetime.now(UTC)`` bound parameter so the query is
    dialect-independent (PG's ``timestamptz`` and SQLite's naive
    ``DateTime`` both compare correctly to a bound ``datetime``).
    """
    tick_started = time.perf_counter()
    now_utc = datetime.now(UTC)
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        candidate_query = select(
            AgentPermission.id,
            AgentPermission.tenant_id,
        ).where(
            AgentPermission.expires_at.is_not(None),
            AgentPermission.expires_at < now_utc,
        )
        result = await session.execute(candidate_query)
        candidates = list(result.all())

        if not candidates:
            _log.debug("grant_expiry_tick_clean")
            return

        per_tenant: dict[uuid.UUID, int] = defaultdict(int)
        ids_to_delete: list[uuid.UUID] = []
        for grant_id, tenant_id in candidates:
            ids_to_delete.append(grant_id)
            per_tenant[tenant_id] += 1

        await session.execute(delete(AgentPermission).where(AgentPermission.id.in_(ids_to_delete)))
        await session.commit()

    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    for tenant_id, expired_count in per_tenant.items():
        try:
            await write_internal_audit_row(
                operator_sub=SYSTEM_OPERATOR_SUB,
                tenant_id=tenant_id,
                method=INTERNAL_METHOD,
                path=_GRANT_EXPIRE_PATH,
                status_code=200,
                duration_ms=duration_ms,
                payload={
                    "expired_count": expired_count,
                },
            )
        except Exception:
            _log.exception(
                "grant_expiry_audit_write_failed",
                tenant_id=str(tenant_id),
                expired_count=expired_count,
            )
    _log.info(
        "grant_expiry_tick_done",
        affected_tenants=len(per_tenant),
        expired_total=sum(per_tenant.values()),
        duration_ms=duration_ms,
    )


async def _sweeper_loop() -> None:
    """Forever loop: sleep one cadence, sweep, repeat.

    Sleep-then-sweep (not sweep-then-sleep) so the first tick is
    delayed by one cadence, giving the rest of lifespan startup time to
    complete. ``CancelledError`` propagates so lifespan shutdown can
    stop the task cleanly.
    """
    interval = get_settings().grant_expiry_tick_interval_seconds
    _log.info(
        "grant_expiry_sweeper_started",
        interval_seconds=interval,
    )
    while True:
        await asyncio.sleep(get_settings().grant_expiry_tick_interval_seconds)
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "grant_expiry_tick_failed",
                exc_info=True,
            )


def start_grant_expiry_sweeper() -> asyncio.Task[None]:
    """Start the background sweeper loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``GRANT_EXPIRY_ENABLED`` setting. The returned task is cancelled on
    lifespan shutdown; the caller awaits the cancellation so the loop
    unwinds cleanly. Returning the task (rather than fire-and-forgetting)
    keeps a strong reference alive — an un-referenced ``asyncio.Task``
    can be garbage-collected mid-flight.
    """
    return asyncio.create_task(_sweeper_loop(), name="grant-expiry-sweeper")


async def stop_grant_expiry_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweeper task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception propagates so a broken shutdown is visible.  Mirrors
    :func:`~meho_backplane.memory.expiry.stop_memory_expiry_sweeper`.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
