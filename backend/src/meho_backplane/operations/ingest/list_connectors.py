# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``list_ingested_connectors`` -- aggregate query for the ``GET /api/v1/connectors`` route.

Split out of :mod:`pipeline` to keep both modules under the project's
600-line file budget. The function is conceptually adjacent to the
ingestion pipeline (both surface the operator-facing view of what's
in ``endpoint_descriptor`` + ``operation_group``) but the read path
has its own concerns (visibility filter, status aggregation, dialect-
portable conditional counts) and benefits from a dedicated module.

Visibility filter
-----------------

Built-in connectors (``tenant_id IS NULL``) are visible to every
operator; tenant-curated connectors are visible only when
``tenant_id == operator.tenant_id``. Cross-tenant rows never appear
in the result.

Status filter semantics
-----------------------

* ``"staged"`` — at least one group is staged.
* ``"enabled"`` — every group is enabled.
* ``"disabled"`` — every group is disabled.
* ``"all"`` / ``None`` — no filter.

A connector with a mixed group state (some staged, some enabled)
shows in the ``staged`` filter — the operator-facing question is
"is there anything left to review" and the answer is yes.

Dialect portability
-------------------

The conditional aggregation uses portable ``CASE WHEN ... THEN 1
ELSE 0 END`` SUM expressions rather than dialect-specific ``FILTER``
clauses (PG-only) so the same query runs against SQLite in tests.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.ingest._llm_grouping_internals import build_connector_id
from meho_backplane.operations.ingest.api_schemas import (
    ConnectorListItem,
    ConnectorStatusFilter,
)

__all__ = ["list_ingested_connectors"]


def _matches_status_filter(
    row: dict[str, int],
    status: ConnectorStatusFilter | None,
) -> bool:
    """Apply the ``staged`` / ``enabled`` / ``disabled`` filter to one row."""
    if status is None or status == "all":
        return True
    if status == "staged":
        return row["staged"] > 0
    if status == "enabled":
        return row["total"] > 0 and row["enabled"] == row["total"]
    if status == "disabled":
        return row["total"] > 0 and row["disabled"] == row["total"]
    return True  # pragma: no cover -- Literal exhausts the inputs


def _connector_sort_key(
    item: tuple[tuple[UUID | None, str, str, str], dict[str, int]],
) -> tuple[str, str, str, str]:
    """Stable sort key for connector tuples.

    Direct sorting on the dict's tuple keys would crash when one
    row has ``tenant_id=None`` and another has a UUID -- Python 3
    forbids ``UUID < None``. The synthetic key projects ``None ->
    ""`` only for ordering; the original ``None`` value survives
    into the response.
    """
    (tenant_uuid, product, version, impl_id), _ = item
    tenant_str = "" if tenant_uuid is None else str(tenant_uuid)
    return (product, version, impl_id, tenant_str)


async def _aggregate_groups_by_connector(
    session: AsyncSession,
    *,
    operator_tenant_id: UUID,
) -> dict[tuple[UUID | None, str, str, str], dict[str, int]]:
    """Aggregate :class:`OperationGroup` rows by connector triple.

    Returns a dict keyed on ``(tenant_id, product, version,
    impl_id)`` whose values are
    ``{total, staged, enabled, disabled}`` counts.
    """
    staged_case = func.sum(
        case((OperationGroup.review_status == "staged", 1), else_=0),
    ).label("staged")
    enabled_case = func.sum(
        case((OperationGroup.review_status == "enabled", 1), else_=0),
    ).label("enabled")
    disabled_case = func.sum(
        case((OperationGroup.review_status == "disabled", 1), else_=0),
    ).label("disabled")
    total = func.count(OperationGroup.id).label("total")

    stmt = (
        select(
            OperationGroup.tenant_id,
            OperationGroup.product,
            OperationGroup.version,
            OperationGroup.impl_id,
            total,
            staged_case,
            enabled_case,
            disabled_case,
        )
        .where(
            (OperationGroup.tenant_id.is_(None)) | (OperationGroup.tenant_id == operator_tenant_id),
        )
        .group_by(
            OperationGroup.tenant_id,
            OperationGroup.product,
            OperationGroup.version,
            OperationGroup.impl_id,
        )
    )
    result = await session.execute(stmt)
    aggregated: dict[tuple[UUID | None, str, str, str], dict[str, int]] = {}
    for row in result.all():
        tenant_uuid, product, version, impl_id, total_v, staged_v, enabled_v, disabled_v = row
        aggregated[(tenant_uuid, product, version, impl_id)] = {
            "total": int(total_v or 0),
            "staged": int(staged_v or 0),
            "enabled": int(enabled_v or 0),
            "disabled": int(disabled_v or 0),
        }
    return aggregated


async def _operation_count_by_connector(
    session: AsyncSession,
    *,
    operator_tenant_id: UUID,
) -> dict[tuple[UUID | None, str, str, str], int]:
    """Aggregate :class:`EndpointDescriptor` rows by connector triple.

    Source-kind filter excludes typed-connector rows (G3.x hand-
    coded ops) -- this endpoint lists *ingested* connectors only,
    per the G0.7 review-queue contract; typed connectors live in
    the v2 registry and operators don't drive them through the
    review state machine.
    """
    stmt = (
        select(
            EndpointDescriptor.tenant_id,
            EndpointDescriptor.product,
            EndpointDescriptor.version,
            EndpointDescriptor.impl_id,
            func.count(EndpointDescriptor.id).label("op_count"),
        )
        .where(
            EndpointDescriptor.source_kind == "ingested",
            (EndpointDescriptor.tenant_id.is_(None))
            | (EndpointDescriptor.tenant_id == operator_tenant_id),
        )
        .group_by(
            EndpointDescriptor.tenant_id,
            EndpointDescriptor.product,
            EndpointDescriptor.version,
            EndpointDescriptor.impl_id,
        )
    )
    result = await session.execute(stmt)
    counts: dict[tuple[UUID | None, str, str, str], int] = {}
    for row in result.all():
        tenant_uuid, product, version, impl_id, count = row
        counts[(tenant_uuid, product, version, impl_id)] = int(count or 0)
    return counts


async def list_ingested_connectors(
    *,
    operator: Operator,
    status: ConnectorStatusFilter | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> list[ConnectorListItem]:
    """Return one :class:`ConnectorListItem` per visible connector.

    Visibility: built-in connectors (``tenant_id IS NULL``) are
    visible to every operator; tenant-curated connectors are visible
    only when ``tenant_id == operator.tenant_id``. Cross-tenant rows
    never appear in the result. The operator's role does NOT gate
    visibility -- read access to the registry is operator-level. The
    write paths (:meth:`IngestionPipelineService.ingest`, the
    state-machine methods on :class:`ReviewService`) carry their own
    role gates.

    See the module docstring for *status* semantics. Aggregation
    runs as two SQL queries (groups + operation counts) because
    sub-selects with correlated aggregation are dialect-finicky on
    SQLite; the two queries together stay cheap relative to the
    per-row JSON projection.
    """
    sm = sessionmaker if sessionmaker is not None else get_sessionmaker()
    async with sm() as session:
        groups_by_connector = await _aggregate_groups_by_connector(
            session,
            operator_tenant_id=operator.tenant_id,
        )
        op_counts_by_connector = await _operation_count_by_connector(
            session,
            operator_tenant_id=operator.tenant_id,
        )

    items: list[ConnectorListItem] = []
    for key, row in sorted(groups_by_connector.items(), key=_connector_sort_key):
        tenant_uuid, product, version, impl_id = key
        if not _matches_status_filter(row, status):
            continue
        connector_id = build_connector_id(product, version, impl_id)
        items.append(
            ConnectorListItem(
                connector_id=connector_id,
                product=product,
                version=version,
                impl_id=impl_id,
                tenant_id=tenant_uuid,
                group_count=row["total"],
                staged_group_count=row["staged"],
                enabled_group_count=row["enabled"],
                disabled_group_count=row["disabled"],
                operation_count=op_counts_by_connector.get(key, 0),
            ),
        )
    return items
