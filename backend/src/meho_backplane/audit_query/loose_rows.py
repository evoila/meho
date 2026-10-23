# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Loose audit rows: records whose parent row does not exist (#4670).

A multi-step operation saves the records of each step when the step ends,
and its own row last, after the handler returns. When MEHO stops before
that last save (a crash, a restart, a cancelled call), the step rows and
the step's ``approval.request`` row stay, and their ``parent_audit_id``
never gets a row. MEHO writes no repair row later, so these "loose rows"
are permanent (``docs/decisions/composite-audit-when-a-step-waits.md``).

This module is the read-only query that lists them. It never writes to
``audit_log``: the log stays append-only.

* :func:`find_loose_rows` — tenant-scoped list of loose rows, newest
  first, with the total count and the count inside a recent window. The
  typed op ``audit.loose_rows`` (``connectors/audit/``) wraps it so a
  Sensor can pin it.
* :func:`count_loose_rows_in_forest` — the loose rows inside one replay
  forest, so every replay surface can show the count next to the tree.

A parent "exists" when a row with that id exists **in the same tenant**.
That is the same rule the replay closure uses: a cross-tenant parent id
never widens a tenant's view, so a row whose parent sits in another
tenant counts as loose here too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import AuditLog

from .query import _build_audit_entry
from .schemas import ReplayNode

__all__ = [
    "DEFAULT_RECENT_WINDOW",
    "LOOSE_ROWS_MAX_LIMIT",
    "LooseRow",
    "LooseRowsResult",
    "count_loose_rows_in_forest",
    "find_loose_rows",
]

#: Window for ``recent_count`` when the caller sets none: one day. The
#: default sensor template fires ``degraded`` on one loose row inside it.
DEFAULT_RECENT_WINDOW: Final[timedelta] = timedelta(days=1)

#: Upper bound on the rows one call returns. The counts are always exact;
#: only the listed rows are capped.
LOOSE_ROWS_MAX_LIMIT: Final[int] = 1000


class LooseRow(BaseModel):
    """One audit row whose ``parent_audit_id`` names a row that does not exist."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    ts: datetime
    principal_sub: str
    op_id: str
    event_class: str
    path: str
    parent_audit_id: uuid.UUID


class LooseRowsResult(BaseModel):
    """The loose rows of one tenant plus the two counts the check reads.

    ``count`` is the exact total. ``recent_count`` is the exact number with
    ``ts >= recent_since``. ``rows`` holds at most ``limit`` rows, newest
    first; ``truncated`` says whether the list is shorter than ``count``.
    """

    model_config = ConfigDict(frozen=True)

    rows: list[LooseRow]
    count: int
    recent_count: int
    recent_since: datetime
    truncated: bool


def _missing_parent_predicate(tenant_id: uuid.UUID) -> sa.ColumnElement[bool]:
    """``parent_audit_id`` is set and no row in the tenant carries that id."""
    parent = aliased(AuditLog, name="parent")
    parent_exists = sa.exists().where(
        parent.id == AuditLog.parent_audit_id,
        parent.tenant_id == tenant_id,
    )
    return sa.and_(
        AuditLog.tenant_id == tenant_id,
        AuditLog.parent_audit_id.is_not(None),
        sa.not_(parent_exists),
    )


async def find_loose_rows(
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
    limit: int = 200,
    recent_window: timedelta = DEFAULT_RECENT_WINDOW,
    now: datetime | None = None,
) -> LooseRowsResult:
    """List the tenant's loose rows, newest first, with exact counts.

    Read-only: two ``SELECT`` statements, no write. ``tenant_id`` is a
    mandatory keyword argument, never client input. ``limit`` is clamped
    to ``1..LOOSE_ROWS_MAX_LIMIT``.
    """
    limit = max(1, min(limit, LOOSE_ROWS_MAX_LIMIT))
    recent_since = (now or datetime.now(UTC)) - recent_window
    loose = _missing_parent_predicate(tenant_id)

    counts_stmt = sa.select(
        sa.func.count(),
        sa.func.coalesce(
            sa.func.sum(sa.case((AuditLog.occurred_at >= recent_since, 1), else_=0)),
            0,
        ),
    ).where(loose)
    count, recent_count = (await session.execute(counts_stmt)).one()

    rows_stmt = (
        sa.select(AuditLog)
        .where(loose)
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    rows = [_to_loose_row(row) for row in (await session.execute(rows_stmt)).scalars().all()]

    return LooseRowsResult(
        rows=rows,
        count=int(count),
        recent_count=int(recent_count),
        recent_since=recent_since,
        truncated=len(rows) < int(count),
    )


def _to_loose_row(row: AuditLog) -> LooseRow:
    entry = _build_audit_entry(row, None)
    parent_id = row.parent_audit_id
    if parent_id is None:  # the predicate rules this out
        raise ValueError(f"audit row {row.id} has no parent_audit_id")
    return LooseRow(
        id=entry.id,
        ts=entry.ts,
        principal_sub=entry.principal_sub,
        op_id=entry.op_id,
        event_class=entry.event_class,
        path=entry.path,
        parent_audit_id=parent_id,
    )


async def count_loose_rows_in_forest(
    forest: list[ReplayNode],
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> int:
    """Count the nodes of a replay forest whose parent row does not exist.

    Only a root can be loose: a node whose parent is in the forest is
    rendered under it. A root whose parent id is ``None`` is a real start
    of a chain; a root whose parent id names a row in the tenant (another
    session's row, or a parent pruned by the depth cap) is not loose. The
    remaining parent ids are checked with one tenant-scoped ``SELECT``.
    """
    present = _collect_ids(forest)
    candidates: dict[uuid.UUID, int] = {}
    for root in forest:
        parent_id = root.parent_audit_id
        if parent_id is None or parent_id in present:
            continue
        candidates[parent_id] = candidates.get(parent_id, 0) + 1
    if not candidates:
        return 0
    stmt = sa.select(AuditLog.id).where(
        AuditLog.tenant_id == tenant_id,
        AuditLog.id.in_(list(candidates)),
    )
    existing = set((await session.execute(stmt)).scalars().all())
    return sum(n for parent_id, n in candidates.items() if parent_id not in existing)


def _collect_ids(nodes: list[ReplayNode]) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        found.add(node.id)
        stack.extend(node.children)
    return found
