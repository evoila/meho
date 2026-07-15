# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DB access for the gateway assignment + result-ingest tables (#2499).

Thin persistence layer over ``runner_assignments`` and
``runner_check_results``. Holds no business logic — target resolution,
op-descriptor materialisation, and the content digest live in
:mod:`meho_backplane.gateway.assignment_service`. Every function takes an
open :class:`AsyncSession` the caller owns (the caller commits).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import RunnerAssignmentRow, RunnerCheckResult
from meho_backplane.runner.wire import RunnerResult

__all__ = [
    "get_assignment_row",
    "ingest_results",
    "upsert_assignment_row",
]


async def get_assignment_row(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    runner_name: str,
) -> RunnerAssignmentRow | None:
    """Return the single assignment row for ``(tenant_id, runner_name)`` or ``None``."""
    result = await session.execute(
        select(RunnerAssignmentRow).where(
            RunnerAssignmentRow.tenant_id == tenant_id,
            RunnerAssignmentRow.runner_name == runner_name,
        )
    )
    return result.scalar_one_or_none()


async def upsert_assignment_row(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    runner_name: str,
    items: list[dict[str, Any]],
) -> RunnerAssignmentRow:
    """Replace the runner's assignment document wholesale (create or update).

    One row per ``(tenant_id, runner_name)`` (unique index). An existing
    row's ``items`` are overwritten and ``updated_at`` refreshed; otherwise
    a fresh row is inserted. Does not commit — the caller owns the
    transaction.
    """
    row = await get_assignment_row(session, tenant_id=tenant_id, runner_name=runner_name)
    if row is None:
        row = RunnerAssignmentRow(
            tenant_id=tenant_id,
            runner_name=runner_name,
            items=items,
        )
        session.add(row)
    else:
        row.items = items
        row.updated_at = datetime.now(UTC)
    return row


async def ingest_results(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    runner_name: str,
    results: list[RunnerResult],
) -> tuple[int, int]:
    """Persist a runner's result batch idempotently; return ``(accepted, duplicates)``.

    Idempotency key is ``(tenant_id, runner_name, result_uid)``. Portable
    across PostgreSQL and SQLite: dedupe the batch by ``result_uid``, read
    back which of those uids already exist, then insert only the new ones.
    A re-posted spool batch therefore inserts nothing and is reported
    entirely as duplicates. ``received_at`` is left to the ORM default
    (central clock) — never taken from the client. Does not commit.

    ``accepted + duplicates == len(results)``: a within-batch repeat of a
    ``result_uid`` counts once as accepted and the rest as duplicates.
    """
    total = len(results)
    if total == 0:
        return 0, 0

    # Dedupe within the batch, preserving first-seen order.
    seen: set[str] = set()
    unique: list[RunnerResult] = []
    for item in results:
        if item.result_uid in seen:
            continue
        seen.add(item.result_uid)
        unique.append(item)

    existing_result = await session.execute(
        select(RunnerCheckResult.result_uid).where(
            RunnerCheckResult.tenant_id == tenant_id,
            RunnerCheckResult.runner_name == runner_name,
            RunnerCheckResult.result_uid.in_(seen),
        )
    )
    already: set[str] = set(existing_result.scalars().all())

    accepted = 0
    for item in unique:
        if item.result_uid in already:
            continue
        session.add(
            RunnerCheckResult(
                tenant_id=tenant_id,
                runner_name=runner_name,
                result_uid=item.result_uid,
                check_ref=item.check_ref,
                op_id=item.op_id,
                status=item.status,
                result_payload=item.result,
                error=item.error,
            )
        )
        accepted += 1

    return accepted, total - accepted
