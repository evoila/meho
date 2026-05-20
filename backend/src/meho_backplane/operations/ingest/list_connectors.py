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

Class-side registrations from the v2 connector registry (entries
registered via
:func:`~meho_backplane.connectors.registry.register_connector_v2`
that have no rows in ``endpoint_descriptor`` / ``operation_group``
yet) are unioned into the response with ``group_count: 0,
operation_count: 0``. This surfaces "State 0.5" connectors
(harbor, sddc-manager during pre-G3.5 windows) so operators see
``connector registered ⇒ visible in list`` rather than silent
invisibility until the first op lands. v1-compat shim entries
(``(product, "", "")`` rows the v1 :func:`register_connector` writes
into the v2 table) are excluded — they're an internal compat
detail, not separately registered connectors.

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

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import all_connectors_v2
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

    Counts every ``source_kind`` -- ``"ingested"`` (G0.7 spec-driven),
    ``"typed"`` (G3.x hand-coded via :func:`register_typed_operation`),
    and ``"composite"`` (G3.x orchestration via
    :func:`register_composite_operation`). The visibility driver for
    this endpoint is :func:`_aggregate_groups_by_connector`, which has
    never carried a source-kind filter -- a typed connector (e.g.
    ``bind9-ssh-9.x`` with 11 typed ops) surfaces because its
    :class:`OperationGroup` rows are visible. The op-count rollup
    used to filter ``source_kind == "ingested"`` (an artefact of the
    G0.7-only era when this endpoint only listed ingested
    connectors), which left typed-connector rows visible with
    ``operation_count: 0`` -- the asymmetry between the two paired
    queries was the bug (Signal #4 in the v0.3.0 RDC dogfood), fixed
    by dropping the filter so both queries count the same universe
    of rows.
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

    The response is the union of two sources:

    * DB-backed rows from ``operation_group`` / ``endpoint_descriptor``
      — the connectors that have made it through (or are mid-way
      through) the review pipeline. These sort first.
    * Class-side registrations from the v2 connector registry that
      have no DB rows yet ("State 0.5"). These append with
      ``group_count: 0, operation_count: 0`` so an operator sees
      every registered connector. Class-only entries are always
      built-in (``tenant_id IS NULL``); they're filtered out under
      an explicit ``status`` narrowing (no groups ⇒ nothing to
      review).
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
    items.extend(_class_side_only_items(groups_by_connector.keys(), status=status))
    return items


def _class_side_only_items(
    db_keys: Iterable[tuple[UUID | None, str, str, str]],
    *,
    status: ConnectorStatusFilter | None,
) -> list[ConnectorListItem]:
    """Return rows for v2-registered connectors with no DB-side state.

    Walks :func:`all_connectors_v2` and yields a
    ``group_count: 0, operation_count: 0`` row for every triple that
    isn't already represented in *db_keys*. The v1-compat shims the
    registry writes as ``(product, "", "")`` are skipped — they're a
    resolver-internal compatibility detail, not separately registered
    connectors, and surfacing them would double-list every v1
    connector (e.g. ``k8s`` already lands as
    ``("k8s", "1.x", "k8s")`` via its dedicated v2 call).

    Class-side registrations are always built-in (``tenant_id IS NULL``)
    — there is no per-tenant class registration path. Under an explicit
    ``status`` narrowing (``staged`` / ``enabled`` / ``disabled``) the
    rows are excluded: zero groups means nothing to review and would
    otherwise pollute the review-queue view.
    """
    if status not in (None, "all"):
        return []
    db_triples = {(product, version, impl_id) for (_, product, version, impl_id) in db_keys}
    rows: list[ConnectorListItem] = []
    for product, version, impl_id in sorted(all_connectors_v2().keys()):
        if not version or not impl_id:
            # v1-compat shim — see docstring.
            continue
        if (product, version, impl_id) in db_triples:
            continue
        rows.append(
            ConnectorListItem(
                connector_id=build_connector_id(product, version, impl_id),
                product=product,
                version=version,
                impl_id=impl_id,
                tenant_id=None,
                group_count=0,
                staged_group_count=0,
                enabled_group_count=0,
                disabled_group_count=0,
                operation_count=0,
            ),
        )
    return rows
