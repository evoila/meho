# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Scheduled background topology-history retention prune (G9.3-T6, #858).

Initiative #365 (G9.3) carries the diff-on-write history tables
:class:`~meho_backplane.db.models.GraphNodeHistory` /
:class:`~meho_backplane.db.models.GraphEdgeHistory`: every refresh-
driven or operator-annotated graph mutation lands one append-only row
per affected table. Without a retention policy these tables grow
unbounded -- a 1-h refresh cadence on a churning tenant adds tens of
thousands of rows per week, indefinitely.

This module ships the corresponding *physical* cleanup: an
``asyncio`` task the FastAPI lifespan owns that ticks on a fixed
cadence (default 7 days / weekly) and deletes rows where
``valid_from < now() - topology_history_retention_days``. The two
history tables are otherwise append-only by contract (see
:class:`~meho_backplane.db.models.GraphNodeHistory` docstring); this
prune task is the **only** place the application issues a DELETE
against them.

Why ``asyncio.create_task`` and not APScheduler 4.x
---------------------------------------------------

Identical reasoning to :mod:`~meho_backplane.topology.scheduler` (the
G9.1-T3 topology-refresh loop) and
:mod:`~meho_backplane.memory.expiry` (the G5.2-T1 memory-expiry
sweeper): APScheduler 4.x has only shipped alphas which the maintainer
documents as "should NOT be used in production". A stdlib ``asyncio``
loop registered in the lifespan is zero-dependency, follows the
chassis's "no new substrate / minimal dependencies" discipline
(``CLAUDE.md``), and is the same shape the lifespan already uses for
every other long-lived background task. Issue #858 references
APScheduler in the task body as the precedent name; the established
chassis pattern is the in-lifespan ``asyncio`` loop both prior G9.x
schedulers chose.

Opt-out via ``TOPOLOGY_HISTORY_RETENTION_DAYS=0``
-------------------------------------------------

The retention setting accepts ``0`` as the "keep forever" sentinel: a
tick still runs, observes the no-op shape (``retention_days == 0``),
logs ``topology_history_retention_disabled``, and returns without
issuing any DELETE or audit row. This is deliberately distinct from
``TOPOLOGY_HISTORY_PRUNE_ENABLED=false`` (which skips starting the
loop entirely in the lifespan): a ``0``-day operator still gets a
heartbeat log every tick that proves the prune surface is alive and
the disk-growth tradeoff is policy-driven, not accidentally-undeployed.

The disk-growth tradeoff is flagged in
``deploy/charts/meho/values.yaml`` (the Helm knob comment), in the
operator-facing ``docs/codebase/topology.md`` runbook, and in the
audit-channel ``docs/architecture/audit.md`` (the ``INTERNAL`` ``path``
registry).

Per-pod leader election
-----------------------

Initiative #374 explicitly defers per-pod leader election to
v0.2.next. Under N replicas the worst case is N identical bounded
DELETE statements in the same second targeting rows that are already
gone from the previous winner -- idempotent and cheap. Adding
advisory locks here would mirror the topology refresh scheduler's
pattern, but the topology refresh substrate's stampede cost is real
(each refresh hits a vendor API). The retention prune's stampede cost
is "an extra DELETE that hits zero rows", which is below the noise
floor of normal DB load -- the same calculus
:mod:`meho_backplane.memory.expiry` documents for its sweeper.

Audit-row shape
---------------

One :class:`~meho_backplane.db.models.AuditLog` row per tick (not per
tenant or per table): the prune is a system-wide operation, not a
tenant-scoped one. The audit row carries
``operator_sub='system:topology-history-retention'``,
``method='INTERNAL'``, ``path='topology.history.prune'``,
``status_code=200`` on success, and
``payload={"dropped_node_rows": N, "dropped_edge_rows": M,
"retention_days": D, "cutoff": <iso-ts>}``. The tenant_id on the row
is the per-tenant sentinel reserved for system-wide ops (see
:func:`_resolve_system_tenant_id`); operators querying audit by tenant
do not see the prune row mixed into their tenant timeline.

Fail-closed contract: the audit writer raises on commit failure and
the per-tick ``try`` / ``except`` block in :func:`_prune_loop` logs and
continues to the next cadence -- one bad audit write must not kill the
prune loop. This is the opposite of the chassis HTTP path (which
fail-closes the *request*); for an internal background task,
loop-survival dominates: a single failed audit row is preferable to
the loop dying and never pruning history again.

Failure isolation + bounded delete
----------------------------------

Each tick runs inside its own ``try`` / ``except`` so one bad tick
(transient DB blip, malformed row) never stalls the loop. The DELETE
is **bounded** by the ``valid_from < cutoff`` predicate -- there is no
unbounded LIMIT-less DELETE that could lock the table for minutes on
a high-churn tenant. Both history tables share the
``(tenant_id, valid_from DESC)`` index (declared in migration
``0012``); the cutoff filter rides the index in reverse, so even a
multi-million-row history table prunes in seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdgeHistory, GraphNodeHistory
from meho_backplane.memory.audit import (
    INTERNAL_METHOD,
    write_internal_audit_row,
)
from meho_backplane.settings import get_settings

__all__ = [
    "SYSTEM_OPERATOR_SUB",
    "TOPOLOGY_HISTORY_PRUNE_PATH",
    "TOPOLOGY_HISTORY_SYSTEM_TENANT_ID",
    "start_topology_history_retention_sweeper",
    "stop_topology_history_retention_sweeper",
]

_log = structlog.get_logger(__name__)

#: Synthetic ``operator_sub`` value for retention-prune audit rows.
#: Distinct from the memory-expiry sweeper's ``"system"`` literal so
#: G8.2 audit-query filters can partition prune rows from sweep rows
#: by ``operator_sub`` alone (rather than parsing ``path``). The
#: ``"system:<job>"`` shape matches the convention
#: :data:`meho_backplane.memory.audit.SYSTEM_OPERATOR_SUB`'s docstring
#: documents as the forward-looking pattern.
SYSTEM_OPERATOR_SUB: str = "system:topology-history-retention"

#: Canonical ``path`` value for the retention prune audit row. Defined
#: here so the audit doc, the prune task, and any future audit-query
#: consumer share one symbol. Registered in
#: ``docs/architecture/audit.md`` under the ``INTERNAL`` ``path``
#: registry.
TOPOLOGY_HISTORY_PRUNE_PATH: str = "topology.history.prune"

#: Sentinel tenant id reserved for system-wide ``INTERNAL`` rows that
#: are not naturally scoped to a real tenant. The retention prune is
#: one such op: the cutoff predicate spans every tenant's rows in one
#: statement, so attributing the audit row to any single real tenant
#: would mislead operators querying their own audit timeline. A
#: deterministic UUID5 namespace-derived value is used so the literal
#: is stable across pod restarts and deploys; the value is reserved by
#: convention (this module is its only writer) rather than enforced by
#: a DB-side row in ``tenant``. The audit_log table's ``tenant_id``
#: column is a soft-FK (see :class:`~meho_backplane.db.models.AuditLog`
#: docstring) so writing a row whose tenant_id has no matching ``tenant``
#: row is the supported shape for system ops.
TOPOLOGY_HISTORY_SYSTEM_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-0000000858a1")


def _resolve_system_tenant_id() -> uuid.UUID:
    """Return the sentinel tenant id for the retention prune audit row.

    Centralised behind a function so a future migration to per-tenant
    retention (Initiative #365 work-item #8 flags this as a v0.3
    direction) can route the call site through a lookup without
    rewriting the prune task. v0.2 returns the fixed sentinel.
    """
    return TOPOLOGY_HISTORY_SYSTEM_TENANT_ID


async def _delete_history_older_than(cutoff: datetime) -> tuple[int, int]:
    """Run the two bounded DELETE statements; return ``(node_rows, edge_rows)``.

    Two separate DELETEs (one per history table) rather than a single
    statement with a UNION-ed predicate: SQLAlchemy 2.x's typed DELETE
    expects one ``__tablename__`` per statement, and the two tables
    share no ORM ancestry beyond :class:`~meho_backplane.db.models.Base`.
    The PG-side cost is identical -- both DELETEs ride the
    ``(tenant_id, valid_from DESC)`` index declared in migration
    ``0012`` -- and the per-table row counts surface cleanly in the
    audit payload for the operator-facing "how much did we prune?"
    question.

    ``rowcount`` is only typed on the concrete ``CursorResult`` subclass
    that ``session.execute(delete(...))`` returns at runtime; the
    abstract ``Result`` superclass mypy infers does not declare the
    attribute (same shape ``kb/service.py`` + ``tenancy/ensure.py``
    document with ``type: ignore[attr-defined]``). The value is
    ``None`` only for ``executemany`` batches the bounded single-
    statement DELETE above never triggers, so the defensive ``or 0``
    collapses the ``int | None`` shape down to the ``int`` the audit
    payload requires.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        node_result = await session.execute(
            delete(GraphNodeHistory).where(GraphNodeHistory.valid_from < cutoff)
        )
        edge_result = await session.execute(
            delete(GraphEdgeHistory).where(GraphEdgeHistory.valid_from < cutoff)
        )
        await session.commit()
        dropped_nodes: int = node_result.rowcount or 0  # type: ignore[attr-defined]
        dropped_edges: int = edge_result.rowcount or 0  # type: ignore[attr-defined]
    return dropped_nodes, dropped_edges


async def _write_prune_audit_row(
    *,
    dropped_nodes: int,
    dropped_edges: int,
    retention_days: int,
    cutoff: datetime,
    duration_ms: float,
) -> None:
    """Write the one summary audit row, swallowing audit-side failures.

    The DELETE has already committed before this helper is called; an
    audit-write failure must not stall the prune loop. Surface the
    failure loud-but-non-fatal so operators see it in logs and a
    flapping audit substrate is visible at the same log-key shape the
    memory-expiry sweeper uses.
    """
    try:
        await write_internal_audit_row(
            operator_sub=SYSTEM_OPERATOR_SUB,
            tenant_id=_resolve_system_tenant_id(),
            method=INTERNAL_METHOD,
            path=TOPOLOGY_HISTORY_PRUNE_PATH,
            status_code=200,
            duration_ms=duration_ms,
            payload={
                "dropped_node_rows": dropped_nodes,
                "dropped_edge_rows": dropped_edges,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
            },
        )
    except Exception:
        _log.exception(
            "topology_history_retention_audit_write_failed",
            dropped_node_rows=dropped_nodes,
            dropped_edge_rows=dropped_edges,
            retention_days=retention_days,
        )


async def _run_one_prune_tick() -> None:
    """One prune sweep: delete rows older than the retention window, audit.

    Reads ``topology_history_retention_days`` and computes a cutoff of
    ``now(UTC) - retention_days``. When ``retention_days == 0`` (the
    "keep forever" sentinel) the function logs and returns without
    issuing any DELETE -- no audit row is written for the no-op case
    because the audit channel would otherwise carry one row per tick
    with ``dropped_*_rows = 0``, which is log-volume waste at weekly
    cadence and indistinguishable from a real "swept clean" outcome.

    The audit row uses the per-process ``perf_counter`` clock for
    ``duration_ms`` to match the memory-expiry sweeper's shape; the
    chassis HTTP path uses the same monotonic clock so dashboards
    grouping by ``method`` see comparable timings.

    Returns no value; the per-tick ``try`` / ``except`` in
    :func:`_prune_loop` catches any exception so a single bad tick
    cannot kill the loop.
    """
    tick_started = time.perf_counter()
    settings = get_settings()
    retention_days = settings.topology_history_retention_days

    if retention_days == 0:
        # Opt-out sentinel: log the heartbeat (proving the prune
        # surface is alive and the operator's "keep forever" choice
        # is policy-driven) and return without DELETE or audit row.
        # No audit row because every weekly tick would otherwise emit
        # one ``dropped_*=0`` row indistinguishable from a real
        # "swept clean" outcome.
        _log.info(
            "topology_history_retention_disabled",
            retention_days=0,
            interval_seconds=settings.topology_history_prune_interval_seconds,
        )
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    dropped_nodes, dropped_edges = await _delete_history_older_than(cutoff)
    duration_ms = (time.perf_counter() - tick_started) * 1000.0
    _log.info(
        "topology_history_retention_tick_done",
        dropped_node_rows=dropped_nodes,
        dropped_edge_rows=dropped_edges,
        retention_days=retention_days,
        cutoff=cutoff.isoformat(),
        duration_ms=duration_ms,
    )
    await _write_prune_audit_row(
        dropped_nodes=dropped_nodes,
        dropped_edges=dropped_edges,
        retention_days=retention_days,
        cutoff=cutoff,
        duration_ms=duration_ms,
    )


async def _prune_loop() -> None:
    """The forever loop: sleep one cadence, prune, repeat.

    Order is sleep-then-prune (rather than prune-then-sleep) so the
    very first tick after process start does not race the rest of the
    startup work (engine init, embedding preload, typed-op
    registration, topology-refresh scheduler boot). A
    ``TOPOLOGY_HISTORY_PRUNE_INTERVAL_SECONDS`` delay after startup is
    the cleanest signal that all eager init has completed -- mirroring
    the memory-expiry sweeper's choice for the same reason.

    Per-tick ``try`` / ``except`` guards mean a transient DB blip is
    logged and the loop continues to the next cadence.
    ``asyncio.CancelledError`` propagates so lifespan shutdown can stop
    the task cleanly.
    """
    interval = get_settings().topology_history_prune_interval_seconds
    _log.info(
        "topology_history_retention_started",
        interval_seconds=interval,
        retention_days=get_settings().topology_history_retention_days,
    )
    while True:
        # Sleep first so the first tick is delayed by one cadence; the
        # ``CancelledError`` here is propagated up to ``stop_*`` cleanly.
        await asyncio.sleep(get_settings().topology_history_prune_interval_seconds)
        try:
            await _run_one_prune_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "topology_history_retention_tick_failed",
                exc_info=True,
            )


def start_topology_history_retention_sweeper() -> asyncio.Task[None]:
    """Start the background retention prune loop and return its task.

    Registered in :func:`meho_backplane.main.lifespan` behind the
    ``TOPOLOGY_HISTORY_PRUNE_ENABLED`` setting. The returned task is
    cancelled on lifespan shutdown; the caller awaits the cancellation
    so the loop unwinds cleanly. Returning the task (rather than
    fire-and-forgetting) keeps a strong reference alive -- an
    un-referenced :class:`asyncio.Task` can be garbage-collected
    mid-flight, producing the "Task was destroyed but it is pending!"
    warnings the pytest-asyncio shutdown rejects.

    Naming mirrors
    :func:`meho_backplane.topology.scheduler.start_topology_refresh_scheduler`
    and
    :func:`meho_backplane.memory.expiry.start_memory_expiry_sweeper`
    so the lifespan-task disposal pattern is uniform across the three
    G9.x / G5.x background loops.
    """
    return asyncio.create_task(
        _prune_loop(),
        name="topology-history-retention-sweeper",
    )


async def stop_topology_history_retention_sweeper(task: asyncio.Task[None]) -> None:
    """Cancel the retention prune task and await its unwind.

    Swallows the expected :class:`asyncio.CancelledError`; any other
    exception surfaced during unwind propagates so a broken shutdown
    is visible rather than silently swallowed. The shape mirrors
    :func:`meho_backplane.topology.scheduler.stop_topology_refresh_scheduler`
    verbatim so future contributors find one disposal pattern across
    the lifespan-owned tasks.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
