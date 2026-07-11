# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background sweeper for stale ``requires_approval`` parks.

Task #2322 (G0.31 #2364). Wires the long-shipped-but-inert
:func:`~meho_backplane.operations.approval_queue.expire_stale_requests`
into a periodic driver so parked approvals actually age out: without a
caller the TTL lifecycle was half-built (the ``expired`` status + the
``expires_at`` column existed, nothing stamped or swept them), leaving
pending approvals to accumulate forever on a live deploy.

Design
------

Mirrors the sibling governance-surface expiry sweepers
(:mod:`meho_backplane.agents.grant_expiry`, the G11.2-T6 grant sweeper,
and :mod:`meho_backplane.memory.expiry`): an ``asyncio.Task`` the FastAPI
lifespan owns, a fixed tick cadence
(``APPROVAL_EXPIRY_TICK_INTERVAL_SECONDS``, default 300s), sleep-then-
sweep ordering, and a per-tick ``try/except`` so one bad tick never kills
the loop.

Why a dedicated sweeper (not the agent-trigger scheduler tick):
``expire_stale_requests`` is **tenant-scoped** — it takes an
:class:`~meho_backplane.auth.operator.Operator` and filters on
``operator.tenant_id`` — so the sweep has to enumerate tenants and act as
a per-tenant system operator, exactly the shape
:mod:`meho_backplane.topology.scheduler` already uses. Following the
established per-surface-sweeper convention (each with its own
``*_enabled`` opt-out + ``*_tick_interval_seconds`` cadence) keeps the
approval sweep independent of the cron/one-off scheduler's advisory lock
and of ``SCHEDULER_ENABLED``.

Per-tenant isolation
--------------------

Each tenant is swept in its own session/transaction. A failure sweeping
one tenant is logged and swallowed so the remaining tenants still get
swept — the same "loop survival over per-tick completeness" trade-off the
topology scheduler makes.

Audit + broadcast
-----------------

``expire_stale_requests`` writes one ``approval.decision`` audit row per
expired request inside the sweep transaction (the durable truth). After
that transaction commits, one fail-open ``approval.expired`` broadcast
event is published per expired row via
:func:`~meho_backplane.operations.approval_queue.publish_approval_event`
so operator watchers see the transition live; a broadcast outage never
blocks the durable decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations.approval_queue import (
    expire_stale_requests,
    publish_approval_event,
)
from meho_backplane.settings import get_settings

__all__ = [
    "start_approval_expiry_sweeper",
    "stop_approval_expiry_sweeper",
    "sweep_expired_approvals",
]

_log = structlog.get_logger(__name__)

#: The acting identity for sweeper-driven expiries. A stable synthetic
#: sub makes swept expiries filterable in audit queries and distinct from
#: operator-driven decisions — the same pattern the topology scheduler
#: (``system:topology-scheduler``) and connector system contexts use.
_SYSTEM_OPERATOR_SUB = "system:approval-expiry"


def _system_operator(tenant_id: uuid.UUID) -> Operator:
    """Build the synthetic per-tenant operator the sweep expires rows as.

    ``raw_jwt`` is the empty string — the sweep forwards no token
    downstream. ``OPERATOR`` role satisfies the ``>= operator`` floor
    :func:`~meho_backplane.operations.approval_queue.expire_stale_requests`
    enforces on the acting identity.
    """
    return Operator(
        sub=_SYSTEM_OPERATOR_SUB,
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _sweep_one_tenant(
    tenant_id: uuid.UUID,
    *,
    cutoff: datetime,
    default_ttl: timedelta,
) -> int:
    """Expire the past-deadline pending rows for one tenant; return the count.

    Opens a dedicated session so a failure sweeping this tenant rolls back
    only its own work and is isolated from the rest of the sweep. The
    ``approval.expired`` broadcasts are published **after** the sweep
    transaction commits (fail-open) so a phantom event cannot outlive a
    rolled-back expiry.
    """
    operator = _system_operator(tenant_id)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            expired = await expire_stale_requests(
                session,
                operator=operator,
                now=cutoff,
                default_ttl=default_ttl,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            _log.exception(
                "approval_expiry_tenant_failed",
                tenant_id=str(tenant_id),
            )
            return 0

    for request in expired:
        await publish_approval_event(
            tenant_id=tenant_id,
            request=request,
            decision="expired",
            principal_sub=operator.sub,
            audit_id=request._audit_id,  # type: ignore[attr-defined]
        )
    return len(expired)


async def sweep_expired_approvals(*, now: datetime | None = None) -> int:
    """Run one full expiry sweep across every tenant; return rows expired.

    Public so tests can drive a deterministic single sweep without the
    cadence sleep. Enumerates tenants once, then expires each tenant's
    past-deadline pending rows under a per-tenant system operator, passing
    the configured ``APPROVAL_DEFAULT_TTL`` so legacy null-``expires_at``
    rows age out via the sweep-time coalesce.
    """
    default_ttl = timedelta(seconds=get_settings().approval_default_ttl_seconds)
    cutoff = now or datetime.now(UTC)
    tick_started = time.perf_counter()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())

    expired_total = 0
    for tenant_id in tenant_ids:
        expired_total += await _sweep_one_tenant(
            tenant_id,
            cutoff=cutoff,
            default_ttl=default_ttl,
        )

    if expired_total:
        _log.info(
            "approval_expiry_tick_done",
            expired_total=expired_total,
            tenants=len(tenant_ids),
            duration_ms=(time.perf_counter() - tick_started) * 1000.0,
        )
    else:
        _log.debug("approval_expiry_tick_clean")
    return expired_total


async def _sweeper_loop() -> None:
    """Forever loop: sleep one cadence, sweep, repeat.

    Sleep-then-sweep (not sweep-then-sleep) so the first tick is delayed
    by one cadence, giving the rest of lifespan startup time to complete.
    ``CancelledError`` propagates so lifespan shutdown can stop the task
    cleanly; any other per-tick failure is logged and the loop continues.
    """
    interval = get_settings().approval_expiry_tick_interval_seconds
    _log.info("approval_expiry_sweeper_started", interval_seconds=interval)
    while True:
        await asyncio.sleep(get_settings().approval_expiry_tick_interval_seconds)
        try:
            await sweep_expired_approvals()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("approval_expiry_tick_failed", exc_info=True)


def start_approval_expiry_sweeper() -> asyncio.Task[None]:
    """Start the background sweeper loop and return its task handle.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``APPROVAL_EXPIRY_ENABLED`` setting. The returned task is cancelled on
    lifespan shutdown; the caller awaits the cancellation so the loop
    unwinds cleanly. Returning the task (rather than fire-and-forgetting)
    keeps a strong reference alive — an un-referenced ``asyncio.Task`` can
    be garbage-collected mid-flight.
    """
    return asyncio.create_task(_sweeper_loop(), name="approval-expiry-sweeper")


async def stop_approval_expiry_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the sweeper task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception propagates so a broken shutdown is visible. Mirrors
    :func:`~meho_backplane.agents.grant_expiry.stop_grant_expiry_sweeper`.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
