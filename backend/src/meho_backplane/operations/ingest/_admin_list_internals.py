# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Private query helpers for :meth:`ConnectorAdminService.list_connectors`.

Module-level free functions split out of
:mod:`meho_backplane.operations.ingest.admin_service` so the public
service stays under the project's per-file size budget. Same shape as
:mod:`meho_backplane.operations.ingest._internals` for the review
service.

Three helpers compose the listing query:

* :func:`enumerate_visible_triples` — enumerate every
  ``(product, version, impl_id, tenant_id)`` tuple the operator may
  see (built-in + own tenant).
* :func:`build_connector_summary` — for one triple, run the three
  count queries + max-updated_at scan + status aggregation.
* :func:`tally_group_statuses` / :func:`aggregate_status` — pure
  Python folding from per-group review_status to the connector-
  level aggregate.

All three are non-public (no entry in the package
``__init__.py``); v0.2.next refactors are free to reshape them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EndpointDescriptor, OperationGroup

__all__ = [
    "aggregate_status",
    "build_connector_summary_rows",
    "enumerate_visible_triples",
    "tally_group_statuses",
]


async def enumerate_visible_triples(
    session: AsyncSession,
    *,
    operator_tenant_id: UUID,
) -> Sequence[tuple[str, str, str, UUID | None]]:
    """Return every ``(product, version, impl_id, tenant_id)`` the operator can see.

    Built-in rows (``tenant_id IS NULL``) are always returned.
    Tenant-curated rows are returned only when their ``tenant_id``
    equals the operator's. Other tenants' rows are filtered at the
    SQL boundary so a cross-tenant operator can't enumerate via
    timing or row count.
    """
    stmt = (
        select(
            OperationGroup.product,
            OperationGroup.version,
            OperationGroup.impl_id,
            OperationGroup.tenant_id,
        )
        .where(
            (OperationGroup.tenant_id.is_(None)) | (OperationGroup.tenant_id == operator_tenant_id),
        )
        .distinct()
    )
    result = await session.execute(stmt)
    # `result.all()` returns Row objects; the tuple() coercion produces
    # the (product, version, impl_id, tenant_id) shape callers expect.
    return [tuple(row) for row in result.all()]


async def build_connector_summary_rows(
    session: AsyncSession,
    *,
    triple: tuple[str, str, str, UUID | None],
) -> tuple[dict[str, int], Any, int, int]:
    """Run the four scans needed to summarise one connector triple.

    Returns ``(status_counts, last_updated_at, operation_count,
    enabled_operation_count)``. Three queries instead of one giant
    join keep each statement readable + index-friendly. The total
    per-call cost is small because the
    ``operation_group_global_idx`` / ``operation_group_tenant_idx``
    partial uniques cover both predicates.

    The caller composes a :class:`ConnectorSummary` from the
    returned shape; this helper is split out so the orchestrator
    method stays under the 60-line per-function budget.
    """
    product, version, impl_id, tenant_id = triple

    # Group statuses + max(updated_at).
    group_stmt = select(
        OperationGroup.review_status,
        OperationGroup.updated_at,
    ).where(
        OperationGroup.product == product,
        OperationGroup.version == version,
        OperationGroup.impl_id == impl_id,
    )
    if tenant_id is None:
        group_stmt = group_stmt.where(OperationGroup.tenant_id.is_(None))
    else:
        group_stmt = group_stmt.where(OperationGroup.tenant_id == tenant_id)
    group_rows = (await session.execute(group_stmt)).all()
    status_counts = tally_group_statuses(group_rows)
    last_updated_at = max(
        (row.updated_at for row in group_rows),
        default=None,
    )

    # Op counts (total + enabled). Two `count(*)` queries instead of a
    # single CASE-WHEN aggregate so the SQL stays trivially portable
    # across SQLite (boolean = integer) and Postgres (boolean ≠ integer
    # without an explicit cast) — no per-dialect TypeDecorator gymnastics.
    total_stmt = select(func.count(EndpointDescriptor.id)).where(
        EndpointDescriptor.product == product,
        EndpointDescriptor.version == version,
        EndpointDescriptor.impl_id == impl_id,
    )
    enabled_stmt = total_stmt.where(EndpointDescriptor.is_enabled.is_(True))
    if tenant_id is None:
        total_stmt = total_stmt.where(EndpointDescriptor.tenant_id.is_(None))
        enabled_stmt = enabled_stmt.where(EndpointDescriptor.tenant_id.is_(None))
    else:
        total_stmt = total_stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        enabled_stmt = enabled_stmt.where(EndpointDescriptor.tenant_id == tenant_id)
    operation_count = int((await session.execute(total_stmt)).scalar_one() or 0)
    enabled_operation_count = int(
        (await session.execute(enabled_stmt)).scalar_one() or 0,
    )

    return status_counts, last_updated_at, operation_count, enabled_operation_count


def tally_group_statuses(rows: Sequence[Any]) -> dict[str, int]:
    """Count ``review_status`` values across a row set."""
    out: dict[str, int] = {"staged": 0, "enabled": 0, "disabled": 0}
    for row in rows:
        status = row.review_status
        if status in out:
            out[status] += 1
    return out


def aggregate_status(
    counts: dict[str, int],
) -> Literal["staged", "enabled", "disabled"]:
    """Fold per-group status counts into the connector-level aggregate."""
    if counts["staged"] > 0:
        return "staged"
    if counts["enabled"] > 0:
        return "enabled"
    return "disabled"
