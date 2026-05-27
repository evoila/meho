# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background topology-refresh loop.

Initiative #363 (G9.1), Task #450 (T3). :func:`start_topology_refresh_scheduler`
returns an :class:`asyncio.Task` the FastAPI lifespan owns: it sweeps
every tenant's targets on the
:attr:`~meho_backplane.settings.Settings.topology_refresh_interval_seconds`
cadence, calling :func:`~meho_backplane.topology.refresh.refresh_target_topology`
per target.

Why ``asyncio.create_task`` and not APScheduler 4.x
---------------------------------------------------

Task #450's body prefers APScheduler 4.x **but explicitly allows**
reusing an in-lifespan ``asyncio`` loop instead. APScheduler 4.x has
never shipped a stable release — only ``4.0.0aN`` alphas, which the
maintainer documents as "should NOT be used in production". The chassis
ships to a production governance backplane and follows a "no new
substrate / minimal dependencies" discipline (``CLAUDE.md``). A stdlib
``asyncio`` loop registered in the lifespan is zero-dependency, is the
issue's own stated fallback, and is the same shape the lifespan already
uses for other long-lived resources. APScheduler can be revisited once
4.x is stable and a richer scheduling need (cron expressions, persisted
jobs) actually exists.

Stampede protection
-------------------

Two backplane replicas share one Postgres. Without coordination both
would refresh the same target every cadence, doubling discovery load on
the vendor API. Each ``(tenant, target)`` refresh is wrapped in a
session-level PostgreSQL advisory lock keyed on a stable 63-bit hash of
``(tenant_id, target_id)``: ``pg_try_advisory_lock`` (non-blocking — a
replica that loses the race skips that target this sweep rather than
queueing) and ``pg_advisory_unlock`` on the way out. On non-PostgreSQL
dialects (the SQLite unit-test path) the lock is a no-op: the test
process is single-replica so there is nothing to stampede.

Failure isolation + backoff
---------------------------

Every per-target refresh runs inside its own ``try`` / ``except`` so one
bad target (unreachable connector, malformed hints) never stalls the
rest of the sweep. A failing target is put on exponential backoff —
``2 x interval`` per consecutive failure, capped at 4 h — tracked
in-memory; a success clears the backoff. The next sweep skips a target
still inside its backoff window.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target, Tenant
from meho_backplane.metrics import TOPOLOGY_REFRESH_TOTAL
from meho_backplane.settings import get_settings
from meho_backplane.topology.refresh import refresh_target_topology

__all__ = [
    "start_topology_refresh_scheduler",
    "stop_topology_refresh_scheduler",
]

_log = structlog.get_logger(__name__)

#: The acting identity for scheduler-driven refreshes. The refresh
#: service writes one ``audit_log`` row per refresh; a stable synthetic
#: sub makes scheduled refreshes filterable in audit queries and
#: distinct from operator-driven ones (same pattern the connectors use
#: for system-context operators).
_SYSTEM_OPERATOR_SUB = "system:topology-scheduler"

#: Per-target backoff ceiling: a permanently-failing target is retried
#: at most every 4 hours regardless of how small the base interval is.
_MAX_BACKOFF_SECONDS = 4 * 3600


def _advisory_lock_key(tenant_id: uuid.UUID, target_id: uuid.UUID) -> int:
    """Map a ``(tenant, target)`` pair to a stable signed-63-bit lock key.

    ``pg_advisory_lock`` takes a ``bigint`` (signed 64-bit). A blake2b
    digest of the two UUIDs gives a deterministic, well-distributed key;
    masking to 63 bits keeps it non-negative so it round-trips through
    asyncpg's ``bigint`` binding without overflow on the sign bit.
    """
    digest = hashlib.blake2b(tenant_id.bytes + target_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


@dataclass
class _TargetBackoff:
    """In-memory per-target failure state for the backoff ladder."""

    consecutive_failures: int = 0
    #: ``time.monotonic()`` value before which the target is skipped.
    skip_until: float = 0.0


@dataclass
class _SchedulerState:
    """Mutable per-process scheduler state (backoff table)."""

    backoff: dict[uuid.UUID, _TargetBackoff] = field(default_factory=dict)


def _system_operator(tenant_id: uuid.UUID) -> Operator:
    """Build the synthetic per-tenant operator scheduled refreshes run as.

    ``raw_jwt`` is the empty string — scheduled refreshes never forward
    a token to a downstream vendor (the connector uses the target's own
    stored credentials). ``OPERATOR`` role satisfies the model's
    required field; the refresh path does not gate on role.
    """
    return Operator(
        sub=_SYSTEM_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Acquire a session-level PG advisory lock; ``True`` on non-PG.

    Returns ``True`` when the lock is held (or the dialect has no
    advisory locks, i.e. the single-replica SQLite test path) and the
    caller should proceed with the refresh; ``False`` when another
    replica holds it and this target should be skipped this sweep.
    """
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return True
    locked = await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
    return bool(locked)


async def _advisory_unlock(session: AsyncSession, key: int) -> None:
    """Release a session-level PG advisory lock; no-op on non-PG."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def _refresh_one_target(
    target: Target,
    state: _SchedulerState,
) -> None:
    """Refresh a single target under its advisory lock, isolating failure.

    A lock-session is opened solely to host the advisory lock for the
    duration of the refresh (the refresh service opens its own
    transactional session for the reconcile). The lock is released in a
    ``finally`` so a crash mid-refresh never strands it for the rest of
    the connection's life.
    """
    target_id = target.id
    tenant_id = target.tenant_id
    bo = state.backoff.get(target_id)
    if bo is not None and time.monotonic() < bo.skip_until:
        return

    key = _advisory_lock_key(tenant_id, target_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as lock_session:
        if not await _try_advisory_lock(lock_session, key):
            TOPOLOGY_REFRESH_TOTAL.labels(outcome="skipped_locked").inc()
            _log.info(
                "topology_refresh_skipped_locked",
                target_id=str(target_id),
                tenant_id=str(tenant_id),
            )
            return
        try:
            await refresh_target_topology(target, _system_operator(tenant_id))
            TOPOLOGY_REFRESH_TOTAL.labels(outcome="ok").inc()
            state.backoff.pop(target_id, None)
        except Exception:
            TOPOLOGY_REFRESH_TOTAL.labels(outcome="error").inc()
            bo = state.backoff.setdefault(target_id, _TargetBackoff())
            bo.consecutive_failures += 1
            interval = get_settings().topology_refresh_interval_seconds
            delay = min(
                interval * (2**bo.consecutive_failures),
                _MAX_BACKOFF_SECONDS,
            )
            bo.skip_until = time.monotonic() + delay
            _log.exception(
                "topology_refresh_target_failed",
                target_id=str(target_id),
                tenant_id=str(tenant_id),
                consecutive_failures=bo.consecutive_failures,
                backoff_seconds=delay,
            )
        finally:
            await _advisory_unlock(lock_session, key)


async def _run_one_sweep(state: _SchedulerState) -> None:
    """Walk every tenant's live targets once, refreshing each in isolation.

    Targets are enumerated tenant-by-tenant so the per-tenant boundary
    the rest of the graph enforces is visible in the iteration shape
    itself. A failure refreshing any single target is swallowed by
    :func:`_refresh_one_target`; this sweep always completes.

    Soft-deleted targets (``deleted_at IS NOT NULL``, G0.14-T4 #1145)
    are excluded — same filter the resolver, REST list, MCP
    ``list_targets``, and the broadcast feed dropdown apply. Without
    this, a tenant_admin's DELETE would leave the scheduler probing
    the dead row every cadence: connector calls, audit rows, broadcast
    events, and graph_node reconciliation against a retired target,
    partially defeating the credential-hygiene use-case the soft-delete
    surface exists to enable.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())
        targets_by_tenant: dict[uuid.UUID, list[Target]] = {}
        for tid in tenant_ids:
            rows = list(
                (
                    await session.execute(
                        select(Target).where(
                            Target.tenant_id == tid,
                            Target.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            targets_by_tenant[tid] = rows

    for targets in targets_by_tenant.values():
        for target in targets:
            await _refresh_one_target(target, state)


async def _scheduler_loop() -> None:
    """The forever loop: sweep, sleep one cadence, repeat.

    The loop body is fully guarded so a transient failure enumerating
    tenants/targets logs and waits for the next cadence rather than
    killing the background task (which would silently stop all
    scheduled refreshes until the next process restart).
    ``asyncio.CancelledError` propagates so lifespan shutdown can stop
    the task cleanly.
    """
    state = _SchedulerState()
    _log.info(
        "topology_refresh_scheduler_started",
        interval_seconds=get_settings().topology_refresh_interval_seconds,
    )
    while True:
        try:
            await _run_one_sweep(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("topology_refresh_sweep_failed")
        await asyncio.sleep(get_settings().topology_refresh_interval_seconds)


def start_topology_refresh_scheduler() -> asyncio.Task[None]:
    """Start the background refresh loop and return its task.

    Registered in :func:`meho_backplane.main.lifespan`. The returned
    task is cancelled on lifespan shutdown; the caller awaits the
    cancellation so the loop unwinds cleanly. Returning the task (rather
    than fire-and-forgetting) keeps a strong reference alive — an
    un-referenced task can be garbage-collected mid-flight.
    """
    return asyncio.create_task(_scheduler_loop(), name="topology-refresh-scheduler")


async def stop_topology_refresh_scheduler(task: asyncio.Task[None]) -> None:
    """Cancel the scheduler task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception surfaced during unwind propagates so a broken shutdown is
    visible rather than silently swallowed.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
