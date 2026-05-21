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

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._lookup import connector_exists, parse_connector_id
from meho_backplane.operations.ingest._llm_grouping_internals import build_connector_id
from meho_backplane.operations.ingest.api_schemas import (
    ConnectorListItem,
    ConnectorStatusFilter,
)

__all__ = ["list_ingested_connectors"]

_log = structlog.get_logger(__name__)


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
      through) the review pipeline. These sort first and carry
      ``state="ingested"``.
    * Class-side registrations from the v2 connector registry that
      have no DB rows yet ("State 0.5"). These append with
      ``group_count: 0, operation_count: 0`` and ``state="registered"``
      so an operator sees every registered connector but knows the
      dispatcher won't resolve a call against it until ingestion /
      typed-op registration lands rows. Class-only entries are always
      built-in (``tenant_id IS NULL``); they're filtered out under
      an explicit ``status`` narrowing (no groups ⇒ nothing to
      review).

    G0.9.1-T1 (#773) listing-integrity contract
    -------------------------------------------

    Every ``connector_id`` this function returns is guaranteed to
    round-trip through the dispatcher resolve path: for ``state ==
    "ingested"`` rows,
    :func:`~meho_backplane.operations._lookup.connector_exists` returns
    ``True`` for
    :func:`~meho_backplane.operations._lookup.parse_connector_id` of
    the emitted ``connector_id``. Rows whose emitted ``connector_id``
    would not round-trip (stale-rename DB rows whose ``impl_id`` no
    longer appears under any registered class, v2-registry entries
    whose registry ``product`` disagrees with what the parser derives
    from ``impl_id``) are dropped before the response is built and a
    structured ``dropped_unresolvable_connector_id`` log event is
    emitted per drop. This closes Signal #6 from the 2026-05-21 RDC
    v0.3.1 dogfood: an LLM browsing the catalog can no longer pick a
    listed ``connector_id`` only to have every downstream call return
    ``HTTP 404`` / ``-32603 UnknownConnectorError``.
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
        if not await _resolves_through_dispatcher(
            operator=operator,
            connector_id=connector_id,
            source_product=product,
            source_version=version,
            source_impl_id=impl_id,
            tenant_uuid=tenant_uuid,
            source="db",
        ):
            continue
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
                state="ingested",
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

    G0.9.1-T1 (#773): the emitted ``product`` / ``connector_id`` are
    derived from the parser's interpretation of the v2-registry
    ``impl_id``, not the registry's ``product`` field, so the
    ``connector_id`` round-trips losslessly through
    :func:`parse_connector_id`. For SDDC the v2 registry stores
    ``product="sddc-manager"`` but the listing emits ``product="sddc"``
    (consistent with what the dispatcher will derive from
    ``parse_connector_id("sddc-rest-9.0")`` and what
    ``SDDC_PRODUCT="sddc"`` already writes into ``endpoint_descriptor``
    rows). When DB rows eventually land under the same row-product the
    parser derives, the listing transitions cleanly from
    ``state="registered"`` to ``state="ingested"`` without a
    ``connector_id`` change. Entries whose ``connector_id`` cannot
    round-trip at all (parser-incompatible ``impl_id`` shape) are
    dropped with a structured log line.
    """
    if status not in (None, "all"):
        return []
    # Dedupe against the DB-emitted set using the row-side natural key
    # (the dispatcher's parsed triple). This keeps the SDDC case clean:
    # the v2 registry holds ``("sddc-manager", "9.0", "sddc-rest")`` but
    # DB rows live under ``("sddc", ...)``; matching on the registry
    # triple would miss the dedupe and emit two rows for the same
    # connector (one ``ingested`` from the DB loop, one ``registered``
    # here). Comparing against the parser-derived triple — the same
    # triple the DB loop emits — drops the duplicate cleanly.
    db_triples = {(product, version, impl_id) for (_, product, version, impl_id) in db_keys}
    rows: list[ConnectorListItem] = []
    for product, version, impl_id in sorted(all_connectors_v2().keys()):
        if not version or not impl_id:
            # v1-compat shim — see docstring.
            continue
        connector_id = build_connector_id(product, version, impl_id)
        try:
            parsed_product, parsed_version, parsed_impl_id = parse_connector_id(connector_id)
        except ValueError:
            _log.warning(
                "dropped_unresolvable_connector_id",
                source="v2_registry",
                connector_id=connector_id,
                registry_product=product,
                registry_version=version,
                registry_impl_id=impl_id,
                reason="parse_connector_id_raised",
            )
            continue
        if (parsed_version, parsed_impl_id) != (version, impl_id):
            # The parser cannot recover the (version, impl_id) the
            # registry advertises; even if a DB row eventually lands
            # under the registry's natural key, the dispatcher will
            # parse this connector_id to a different triple and fail
            # to resolve. Drop and log — there is no clean
            # remediation from inside the listing.
            _log.warning(
                "dropped_unresolvable_connector_id",
                source="v2_registry",
                connector_id=connector_id,
                registry_product=product,
                registry_version=version,
                registry_impl_id=impl_id,
                parsed_product=parsed_product,
                parsed_version=parsed_version,
                parsed_impl_id=parsed_impl_id,
                reason="impl_id_or_version_not_recoverable_from_connector_id",
            )
            continue
        if (parsed_product, parsed_version, parsed_impl_id) in db_triples:
            # DB rows already represent this connector under the
            # parser-derived natural key; the DB-loop emitted the
            # ingested row, so skip the class-only ``registered`` row
            # to avoid duplication. Covers both the trivial case
            # (registry product == parsed product) and the SDDC case
            # (registry product "sddc-manager" vs parsed "sddc").
            continue
        rows.append(
            ConnectorListItem(
                connector_id=connector_id,
                product=parsed_product,
                version=parsed_version,
                impl_id=parsed_impl_id,
                tenant_id=None,
                group_count=0,
                staged_group_count=0,
                enabled_group_count=0,
                disabled_group_count=0,
                operation_count=0,
                state="registered",
            ),
        )
    return rows


async def _resolves_through_dispatcher(
    *,
    operator: Operator,
    connector_id: str,
    source_product: str,
    source_version: str,
    source_impl_id: str,
    tenant_uuid: UUID | None,
    source: str,
) -> bool:
    """Return whether *connector_id* round-trips through the dispatcher resolve path.

    Parses ``connector_id`` the same way the dispatcher's
    :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
    /
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    /
    :func:`~meho_backplane.operations.meta_tools.call_operation`
    would, then probes existence with the same tenant-visibility
    filter
    :func:`~meho_backplane.operations._lookup.connector_exists`
    enforces. Returns ``True`` only when the dispatcher would also
    return ``True``.

    Drops + structured-logs every miss so operators / observability
    surface every stale-rename row and every v2-registry
    misalignment in the audit trail. The log line carries both the
    source-row triple and the parsed triple so the
    remediation is obvious from a single log fetch (delete the
    stale row, or correct the v2 registration's ``product``).
    """
    try:
        parsed_product, parsed_version, parsed_impl_id = parse_connector_id(connector_id)
    except ValueError:
        _log.warning(
            "dropped_unresolvable_connector_id",
            source=source,
            connector_id=connector_id,
            row_product=source_product,
            row_version=source_version,
            row_impl_id=source_impl_id,
            row_tenant_id=str(tenant_uuid) if tenant_uuid is not None else None,
            reason="parse_connector_id_raised",
        )
        return False
    # Visibility-scope probe via the same helper the dispatcher
    # uses. The tenant scope is the operator's; the source row's
    # tenant_id is logged for the audit trail but not used to
    # widen the visibility filter (cross-tenant stale rows
    # should not surface either way).
    exists = await connector_exists(
        tenant_id=operator.tenant_id,
        product=parsed_product,
        version=parsed_version,
        impl_id=parsed_impl_id,
    )
    if not exists:
        _log.warning(
            "dropped_unresolvable_connector_id",
            source=source,
            connector_id=connector_id,
            row_product=source_product,
            row_version=source_version,
            row_impl_id=source_impl_id,
            row_tenant_id=str(tenant_uuid) if tenant_uuid is not None else None,
            parsed_product=parsed_product,
            parsed_version=parsed_version,
            parsed_impl_id=parsed_impl_id,
            reason="dispatcher_resolve_path_missed_parsed_triple",
        )
        return False
    return True
