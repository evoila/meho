# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable-row operations for ``scheduled_trigger`` (G11.3-T2 #823).

The narrow data-access layer the scheduler loop calls. Three concerns:

1. **Insert** a new trigger with a computed ``next_fire_at`` (the
   admin surface, G11.3-T5, will call this; T2 exposes it because the
   tests construct triggers without going through HTTP).
2. **Claim** due rows replica-safely. On PostgreSQL this uses
   ``SELECT ... FOR UPDATE SKIP LOCKED`` so two concurrent claimers
   never receive the same row; the function returns the claimed rows
   *while the transaction is still open* so the caller can advance /
   mark-fired in the same transaction (the lock holds until commit).
   On SQLite the locking clauses no-op (SQLite has a single writer at a
   time anyway); the test path runs two loop instances in the same
   process and relies on Python-level row uniqueness in the
   ``UPDATE ... RETURNING`` advance step.
3. **Advance** a cron trigger to the next ``next_fire_at`` *before*
   the actual agent fire, and **mark fired** a one-off after its single
   fire. The "advance before fire" discipline is what guarantees a slow
   agent run cannot delay the next tick: even if the run takes 10
   minutes, the cron's next fire is already persisted with the correct
   scheduled instant, and on the next tick the same row's
   ``next_fire_at`` is already in the future.

All functions take an :class:`AsyncSession` they did not open -- the
caller's transaction owns the commit / rollback. The functions flush
so the row state is visible within the transaction but do not commit;
that keeps the claim's row lock live for the full claim-advance-fire
sequence.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import (
    ScheduledTrigger,
    ScheduledTriggerKind,
    ScheduledTriggerStatus,
)
from meho_backplane.scheduler.cron import next_fire_after

__all__ = [
    "advance_cron_trigger",
    "claim_due_triggers",
    "create_cron_trigger",
    "create_one_off_trigger",
    "mark_one_off_fired",
]


async def create_cron_trigger(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_definition_id: uuid.UUID | None,
    cron_expr: str,
    inputs: dict[str, object],
    identity_sub: str,
    created_by_sub: str,
    timezone: str = "UTC",
    base: datetime | None = None,
) -> ScheduledTrigger:
    """Insert a cron trigger with a computed first ``next_fire_at``.

    *base* is the instant from which the first match is computed; the
    default ``datetime.now(UTC)`` is what production wants ("schedule
    relative to wall-clock now"). Tests pass an explicit base to make
    the first-fire instant deterministic.

    Raises:
        InvalidCronExpressionError: *cron_expr* is not a valid 5-field
            cron expression.
    """
    if base is None:
        base = datetime.now(UTC)
    # next_fire_after also validates the expression; the explicit
    # call here is so the validation error fires before the INSERT.
    next_fire = next_fire_after(cron_expr, base, timezone)
    row = ScheduledTrigger(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_definition_id=agent_definition_id,
        kind=ScheduledTriggerKind.CRON.value,
        cron_expr=cron_expr,
        timezone=timezone,
        next_fire_at=next_fire,
        status=ScheduledTriggerStatus.ACTIVE.value,
        inputs=inputs,
        identity_sub=identity_sub,
        created_by_sub=created_by_sub,
    )
    session.add(row)
    await session.flush()
    return row


async def create_one_off_trigger(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_definition_id: uuid.UUID | None,
    run_at: datetime,
    inputs: dict[str, object],
    identity_sub: str,
    created_by_sub: str,
) -> ScheduledTrigger:
    """Insert a one-off trigger that fires once at *run_at*.

    *run_at* is the wall-clock instant the trigger should fire. The
    column is stored UTC-normalised so cross-tz operators see consistent
    timestamps; a naive input is treated as UTC.
    """
    run_at_utc = run_at.replace(tzinfo=UTC) if run_at.tzinfo is None else run_at.astimezone(UTC)
    row = ScheduledTrigger(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_definition_id=agent_definition_id,
        kind=ScheduledTriggerKind.ONE_OFF.value,
        cron_expr=None,
        timezone="UTC",
        next_fire_at=run_at_utc,
        status=ScheduledTriggerStatus.ACTIVE.value,
        inputs=inputs,
        identity_sub=identity_sub,
        created_by_sub=created_by_sub,
    )
    session.add(row)
    await session.flush()
    return row


async def claim_due_triggers(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> Sequence[ScheduledTrigger]:
    """Return up to *limit* active triggers whose ``next_fire_at`` <= *now*.

    On PostgreSQL: ``SELECT ... WHERE ... FOR UPDATE SKIP LOCKED`` so a
    second claimer in another transaction (another replica, another
    process) sees zero rows for any row this transaction has locked.
    The row locks are released on the caller's commit / rollback.

    On SQLite (the test path): the locking clauses no-op; the
    claim-advance-fire sequence in :func:`advance_cron_trigger` and
    :func:`mark_one_off_fired` uses ``UPDATE ... WHERE id = :id AND
    status = 'active'`` to enforce single-fire across two in-process
    loops sharing the same DB connection pool. (The unit-test simulates
    two replicas in one Python process.)

    The query orders by ``next_fire_at`` ASC so the most overdue
    triggers fire first -- a deployment that just restarted after a
    long pause fires its most-overdue rows first, then walks forward.
    """
    conn = await session.connection()
    stmt = (
        select(ScheduledTrigger)
        .where(
            ScheduledTrigger.status == ScheduledTriggerStatus.ACTIVE.value,
            ScheduledTrigger.next_fire_at <= now,
        )
        .order_by(ScheduledTrigger.next_fire_at.asc())
        .limit(limit)
    )
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def advance_cron_trigger(
    session: AsyncSession,
    row: ScheduledTrigger,
    *,
    fire_instant: datetime,
) -> ScheduledTrigger | None:
    """Advance *row* to its next cron match; stamp ``last_fired_at``.

    The "compute next then fire" discipline: this runs *before* the
    actual agent fire so a slow run cannot delay the next tick. The
    update is conditional on ``status='active'`` AND
    ``next_fire_at=row.next_fire_at`` so a concurrent claimer (race
    window between the SELECT and the UPDATE on a dialect without
    SKIP LOCKED) cannot double-advance the same row.

    Returns the row when the advance succeeded, ``None`` when another
    claimer already advanced it (the conditional UPDATE matched zero
    rows). The loop treats ``None`` as "skip the fire; the other claimer
    owns this tick".

    Raises:
        ~meho_backplane.scheduler.cron.InvalidCronExpressionError: the
            persisted ``cron_expr`` is no longer valid (operator edited
            it to garbage between the claim and the advance). The loop
            catches and quarantines such rows by transitioning them to
            ``paused``.
    """
    if row.cron_expr is None:
        # Defensive: a cron row with a NULL expression is corrupt. Do
        # not advance -- park the row (the caller decides whether to
        # park or fail loudly; this function refuses to advance).
        return None
    previous_next = row.next_fire_at
    new_next = next_fire_after(row.cron_expr, fire_instant, row.timezone)
    stmt = (
        update(ScheduledTrigger)
        .where(
            ScheduledTrigger.id == row.id,
            ScheduledTrigger.status == ScheduledTriggerStatus.ACTIVE.value,
            ScheduledTrigger.next_fire_at == previous_next,
        )
        .values(
            next_fire_at=new_next,
            last_fired_at=fire_instant,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        return None
    # Refresh the local row object so the caller sees the new values
    # without re-querying.
    row.next_fire_at = new_next
    row.last_fired_at = fire_instant
    return row


async def mark_one_off_fired(
    session: AsyncSession,
    row: ScheduledTrigger,
    *,
    fire_instant: datetime,
) -> ScheduledTrigger | None:
    """Transition a one-off *row* to ``status='fired'`` exactly once.

    Same conditional-UPDATE shape as :func:`advance_cron_trigger`: the
    ``WHERE status='active' AND next_fire_at=row.next_fire_at`` guard
    is what enforces single-fire across concurrent claimers on a
    dialect without SKIP LOCKED. Returns the row on success, ``None``
    when another claimer already marked it fired.
    """
    previous_next = row.next_fire_at
    stmt = (
        update(ScheduledTrigger)
        .where(
            ScheduledTrigger.id == row.id,
            ScheduledTrigger.status == ScheduledTriggerStatus.ACTIVE.value,
            ScheduledTrigger.next_fire_at == previous_next,
        )
        .values(
            status=ScheduledTriggerStatus.FIRED.value,
            last_fired_at=fire_instant,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        return None
    row.status = ScheduledTriggerStatus.FIRED.value
    row.last_fired_at = fire_instant
    return row
