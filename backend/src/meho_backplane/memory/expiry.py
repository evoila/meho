# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background memory-expiry sweeper (G5.2-T1, #623).

Consumer-needs.md §G5 specifies that session-scoped memory entries
expire by default ("session-scoped hints expire after 7 days unless
re-pinned"). G5.1 (#332) stores the per-row ``expires_at`` in
``doc_metadata`` and filters expired rows out of *read* paths
(:meth:`~meho_backplane.memory.service.MemoryService.recall` /
:meth:`~meho_backplane.memory.service.MemoryService.list_memories` /
:meth:`~meho_backplane.memory.service.MemoryService.search_memories`).
This module ships the corresponding *physical* cleanup: an
``asyncio`` task the FastAPI lifespan owns that ticks on a fixed
cadence (default 24 h) and removes the expired rows so the
``documents`` table does not accumulate undeleted soft-hidden memory.

Why ``asyncio.create_task`` and not APScheduler 4.x
---------------------------------------------------

Identical reasoning to the G9.1-T3 topology scheduler (#450):
APScheduler 4.x has only shipped alphas which the maintainer documents
as "should NOT be used in production". A stdlib ``asyncio`` loop in
the lifespan is zero-dependency, follows the chassis's "no new
substrate / minimal dependencies" discipline (``CLAUDE.md``), and is
the same shape the lifespan already uses for long-lived resources.

Per-pod leader election
-----------------------

Initiative #374 explicitly defers per-pod leader election to v0.2.next.
Under N replicas the worst case is N identical ``DELETE`` statements
in the same second targeting rows that are already gone from the
previous winner -- idempotent and cheap. Adding advisory locks here
would mirror the topology scheduler's pattern, but the topology
substrate's stampede cost is real (each refresh hits a vendor API);
expiry's stampede cost is "an extra DELETE that hits zero rows", which
is below the noise floor of normal DB load.

Failure isolation
-----------------

Each tick runs inside its own ``try`` / ``except`` so one bad tick
(connector down, transient DB blip, malformed metadata row) never
stalls the loop. The exception is logged with ``structlog.warning``
under ``memory_expiry_tick_failed`` and the loop waits for the next
cadence. This is the inverse of the chassis HTTP path (which
fail-closes the *request*); for a background task, loop survival
dominates: a single missed tick is recoverable on the next cadence,
but a crashed loop silently stops cleanup until the next process
restart.

Cross-dialect ``expires_at`` extraction
---------------------------------------

The ``expires_at`` value lives in ``doc_metadata`` (JSONB on PG, JSON
on SQLite) under the canonical ``expires_at`` key. PostgreSQL exposes
the value via ``jsonb_extract_path_text(metadata, 'expires_at')``;
SQLite exposes it via ``json_extract(metadata, '$.expires_at')``.
:func:`_run_one_tick` picks the dialect-appropriate expression at the
session boundary so the same handler runs on both PG (production) and
SQLite (the test path). Values are stored as ISO 8601 strings with a
``+00:00`` / ``Z`` offset (see
:func:`~meho_backplane.memory._internal.build_metadata`), so the
comparison is a stable lexicographic ``<`` against ``now(UTC)`` in the
same format -- ISO 8601 strings sort identically to their datetime
values when the offset is normalised.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.memory._internal import MEMORY_SOURCE
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    MEMORY_EXPIRE_PATH,
    SYSTEM_OPERATOR_SUB,
    write_internal_audit_row,
)
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    import uuid

__all__ = [
    "start_memory_expiry_sweeper",
    "stop_memory_expiry_sweeper",
]

_log = structlog.get_logger(__name__)


def _expires_at_expression(session: AsyncSession) -> sa.ColumnElement[str | None]:
    """Build a dialect-appropriate SQL expression for ``metadata.expires_at``.

    PostgreSQL: ``jsonb_extract_path_text(metadata, 'expires_at')``
    returns ``NULL`` for absent keys and the text value for present
    ones -- matching SQLite's ``json_extract`` shape closely enough
    that the same ``< now_iso`` predicate works on both dialects
    without a per-row branch.

    SQLite (test path): ``json_extract(metadata, '$.expires_at')``
    follows the JSON path grammar; the leading ``$`` and the key are
    quoted because the column is :class:`JSON`, not native ``JSONB``.

    Returning a typed :class:`sa.ColumnElement` keeps mypy happy at the
    call site -- the expression is used in a ``where`` predicate
    comparing the extracted string to a Python string, which the type
    checker can resolve.
    """
    bind = session.bind
    # ``bind`` is ``None`` only when the session is constructed without
    # an engine bound, which never happens for sessions built by
    # ``get_sessionmaker()``. Defensive cast for mypy + clarity.
    if bind is None or bind.dialect.name != "postgresql":
        return sa.func.json_extract(Document.doc_metadata, "$.expires_at")
    return sa.func.jsonb_extract_path_text(Document.doc_metadata, "expires_at")


async def _run_one_tick() -> None:
    """One sweep: find expired ``source='memory'`` rows, delete, audit.

    Two-step query (SELECT then DELETE) instead of ``DELETE ...
    RETURNING`` for portability: SQLite under the version shipped with
    the CI Python image lacks ``RETURNING`` support, and the cost of
    the extra round-trip is invisible at sweeper rates (one tick every
    24 hours in production). The select pulls the ``(tenant_id, kind,
    id)`` tuples; the delete reaps the rows by primary key in one
    statement; per-tenant aggregation then drives one audit row per
    affected tenant.

    The ``now_iso`` comparison uses the canonical ISO 8601 format with
    a ``+00:00`` offset to match the storage format
    :func:`~meho_backplane.memory._internal.build_metadata` writes. The
    string ordering is identical to the underlying datetime ordering
    under a fixed UTC offset, which is what every memory row carries
    (the build_metadata path normalises to UTC before serialising).
    """
    tick_started = time.perf_counter()
    now_iso = datetime.now(UTC).isoformat()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        expires_expr = _expires_at_expression(session)
        # NULL-safe filter: rows with no ``expires_at`` key (persistent
        # memories the operator opted out of TTL on) are skipped via
        # ``expires_expr IS NOT NULL`` -- ``NULL < anything`` is NULL
        # under SQL, but being explicit makes the intent visible at the
        # query layer rather than relying on three-valued logic.
        candidate_query = select(
            Document.id,
            Document.tenant_id,
            Document.kind,
        ).where(
            Document.source == MEMORY_SOURCE,
            expires_expr.is_not(None),
            expires_expr < now_iso,
        )
        result = await session.execute(candidate_query)
        candidates = list(result.all())
        if not candidates:
            _log.debug("memory_expiry_tick_clean")
            return

        per_tenant: dict[uuid.UUID, list[str]] = defaultdict(list)
        ids_to_delete: list[uuid.UUID] = []
        for doc_id, tenant_id, kind in candidates:
            ids_to_delete.append(doc_id)
            per_tenant[tenant_id].append(kind)

        await session.execute(delete(Document).where(Document.id.in_(ids_to_delete)))
        await session.commit()

    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    for tenant_id, kinds in per_tenant.items():
        # Deduplicated, sorted scope list so the audit payload is stable
        # under reordering of the candidate query. Operators querying
        # the payload by scope can rely on a canonical shape.
        unique_scopes = sorted(set(kinds))
        try:
            await write_internal_audit_row(
                operator_sub=SYSTEM_OPERATOR_SUB,
                tenant_id=tenant_id,
                method=INTERNAL_METHOD,
                path=MEMORY_EXPIRE_PATH,
                status_code=200,
                duration_ms=duration_ms,
                payload={
                    "expired_count": len(kinds),
                    "scopes": unique_scopes,
                },
            )
        except Exception:
            # An audit-write failure must not stall the tick (the rows
            # are already deleted; we cannot roll that back from here).
            # Surface the failure loud so operators see it in logs.
            _log.exception(
                "memory_expiry_audit_write_failed",
                tenant_id=str(tenant_id),
                expired_count=len(kinds),
            )
    _log.info(
        "memory_expiry_tick_done",
        affected_tenants=len(per_tenant),
        expired_total=sum(len(k) for k in per_tenant.values()),
        duration_ms=duration_ms,
    )


async def _sweeper_loop() -> None:
    """The forever loop: sleep one cadence, sweep, repeat.

    Order is sleep-then-sweep (rather than sweep-then-sleep) so the
    very first tick after process start does not race the rest of the
    startup work (eager engine init, embedding preload, typed-op
    registration). A ``MEMORY_EXPIRY_TICK_INTERVAL_SECONDS`` delay
    after startup is the cleanest signal that all eager init has
    completed.

    Per-tick ``try`` / ``except`` guards mean a transient DB blip is
    logged and the loop continues to the next cadence. ``CancelledError``
    propagates so lifespan shutdown can stop the task cleanly.
    """
    interval = get_settings().memory_expiry_tick_interval_seconds
    _log.info(
        "memory_expiry_sweeper_started",
        interval_seconds=interval,
    )
    while True:
        # Sleep first so the first tick is delayed by one cadence; the
        # ``CancelledError` here is propagated up to ``stop_*`` cleanly.
        await asyncio.sleep(get_settings().memory_expiry_tick_interval_seconds)
        try:
            await _run_one_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "memory_expiry_tick_failed",
                exc_info=True,
            )


def start_memory_expiry_sweeper() -> asyncio.Task[None]:
    """Start the background sweeper loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``MEMORY_EXPIRY_ENABLED`` setting. The returned task is cancelled
    on lifespan shutdown; the caller awaits the cancellation so the
    loop unwinds cleanly. Returning the task (rather than fire-and-
    forgetting) keeps a strong reference alive -- an un-referenced
    ``asyncio.Task`` can be garbage-collected mid-flight, producing
    the "Task was destroyed but it is pending!" warnings the AC
    explicitly bars under pytest-asyncio shutdown.
    """
    return asyncio.create_task(_sweeper_loop(), name="memory-expiry-sweeper")


async def stop_memory_expiry_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweeper task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception surfaced during unwind propagates so a broken shutdown
    is visible rather than silently swallowed. The shape mirrors
    :func:`~meho_backplane.topology.scheduler.stop_topology_refresh_scheduler`
    verbatim so future contributors find one disposal pattern across
    the lifespan-owned tasks.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
