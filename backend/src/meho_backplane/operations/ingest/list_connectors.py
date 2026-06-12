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
yet) are unioned into the response with every count zeroed
(``group_count: 0, operation_count: 0, enabled_operation_count:
0``). This surfaces "State 0.5" connectors
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

next_step hint (G0.13-T3 / #1133)
---------------------------------

``state="registered"`` rows carry a :class:`NextStep` object pointing
at the verb that closes the workflow gap surfaced by the v0.6.0 RDC
dogfood (consumer's signal 11: half-registered connectors fail
lookup with no in-product hint). ``state="ingested"`` rows omit it
(``next_step=None``) because the dispatcher already resolves them.

The catalog lookup uses the v2-registry's ``(product, version)``,
not the parser-derived shortening, because the catalog stores SDDC
under ``product="sddc-manager"`` while the listing emits
``product="sddc"``. See :func:`_next_step_for_registered`.
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
    NextStep,
)
from meho_backplane.operations.ingest.catalog import (
    CatalogError,
    ConnectorSpecCatalog,
    load_catalog,
)

__all__ = ["list_ingested_connectors", "next_step_for_registered_connector"]

_log = structlog.get_logger(__name__)


def next_step_for_registered_connector(
    *,
    product: str,
    version: str,
    impl_id: str,
) -> NextStep | None:
    """Return the ingest ``next_step`` for a registered-but-not-ingested connector.

    The meta-tools' ``connector_not_ingested`` signal (#1482) reuses the
    exact hint ``GET /api/v1/connectors`` renders on a ``state="registered"``
    row, so the in-product "what do I run next?" answer is identical across
    the listing surface and the discovery meta-tools
    (:func:`~meho_backplane.operations.meta_tools.list_operation_groups` /
    :func:`~meho_backplane.operations.meta_tools.search_operations`).

    *(product, version, impl_id)* is the triple
    :func:`parse_connector_id` derived from the caller's ``connector_id`` —
    the same parsed shape the listing emits. The catalog lookup, however,
    is keyed on the *registry* product (SDDC registers under
    ``"sddc-manager"`` while the listing emits ``"sddc"``), so this helper
    re-finds the matching v2-registry entry by the same lossless
    round-trip the listing uses and feeds the registry triple to
    :func:`_next_step_for_registered`. Returns ``None`` when no registry
    entry round-trips to the parsed triple (the connector is genuinely
    unknown, not merely un-ingested) so the caller can fall through to the
    unknown-connector path.
    """
    catalog = _load_catalog_or_none()
    for reg_product, reg_version, reg_impl_id in sorted(all_connectors_v2().keys()):
        parsed = _resolve_class_only_natural_key(
            registry_product=reg_product,
            registry_version=reg_version,
            registry_impl_id=reg_impl_id,
        )
        if parsed is None:
            continue
        _connector_id, parsed_product, parsed_version, parsed_impl_id = parsed
        if (parsed_product, parsed_version, parsed_impl_id) == (product, version, impl_id):
            return _next_step_for_registered(
                catalog=catalog,
                registry_product=reg_product,
                registry_version=reg_version,
                registry_impl_id=reg_impl_id,
                dispatch_product=parsed_product,
            )
    return None


def _next_step_for_registered(
    *,
    catalog: ConnectorSpecCatalog | None,
    registry_product: str,
    registry_version: str,
    registry_impl_id: str,
    dispatch_product: str,
) -> NextStep:
    """Build the ``next_step`` hint for a ``state="registered"`` row.

    Looks up the registry's ``(product, version)`` in the connector-spec
    catalog (#743) — the right lookup key, since the catalog stores
    ``product="sddc-manager"`` while the listing emits the parser-derived
    ``"sddc"``, and ``("sddc", "9.0")`` would always miss for SDDC.

    *dispatch_product* is the parser-derived product the listing row
    advertises and the dispatch/query surface keys on
    (``parse_connector_id("vrli-rest-9.0") -> "vrli"``) — the
    ``--product`` the **catalog-miss** verb emits so an operator copying
    it lands a *dispatchable* connector (the ingest path persists rows
    under this spelling). Emitting the registry product (``vcf-logs``)
    here was the claude-rdc-hetzner-dc#1136 false-success: the verb said
    ``vcf-logs`` while the row carried ``product="vrli"``, so it never
    round-tripped and the catalog kept reporting ``registered, 0 ops``.
    The catalog-hit branches keep ``entry.product`` (a catalogued
    connector's catalog product already equals the dispatcher's derived
    one; no VCF-family entry is catalogued).

    Three branches: **supported** catalog hit → ``--catalog`` verb;
    **spec-only** catalog hit → manual ``--spec`` verb on the catalog's
    native triple (upstream is HTML-portal / fqdn-templated, #789 N8 /
    #1361); **catalog miss** → manual ``--spec`` verb on
    *dispatch_product* + the hand-authored-spec on-ramp for spec-less
    vendors (#1533 / ci-07, see ``connector-ingestion.md``).

    *catalog* ``None`` (load failed — only in tests / mid-reload, a
    malformed catalog would have crashed the lifespan) degrades to the
    manual-mode rationale rather than raising.
    """
    entry = catalog.get(registry_product, registry_version) if catalog is not None else None
    if entry is not None and entry.catalog_ingest == "supported":
        return NextStep(
            verb=f"meho connector ingest --catalog {entry.product}/{entry.version}",
            rationale="spec available in catalog; run ingest to populate operations",
        )
    if entry is not None and entry.catalog_ingest == "spec-only":
        return NextStep(
            verb=(
                f"meho connector ingest --product {entry.product} "
                f"--version {entry.version} --impl {entry.impl_id} "
                f"--spec <concrete-openapi-uri>"
            ),
            rationale=(
                "catalog upstream is HTML-portal or fqdn-templated and "
                "cannot drive --catalog ingest server-side; fetch the "
                "raw OpenAPI spec from the appliance and pass via --spec"
            ),
        )
    return NextStep(
        verb=(
            f"meho connector ingest --product {dispatch_product} "
            f"--version {registry_version} --impl {registry_impl_id} "
            f"--spec <upstream-openapi-uri>"
        ),
        rationale=(
            "not in catalog; run manual ingest with --spec pointing at the "
            "vendor OpenAPI spec (file:// / https:// / docs:<...>). If the "
            "product publishes no OpenAPI spec at all, author a minimal "
            "OpenAPI 3.x covering just the ops you need and pass it via "
            "--spec file://… (see connector-ingestion.md)"
        ),
    )


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
) -> dict[tuple[UUID | None, str, str, str], dict[str, int]]:
    """Aggregate :class:`EndpointDescriptor` rows by connector triple.

    Returns a dict keyed on ``(tenant_id, product, version,
    impl_id)`` whose values are ``{total, enabled}`` op counts.
    ``enabled`` counts the rows whose per-op ``is_enabled`` flag is
    set -- the dispatchable subset -- via the same portable ``CASE
    WHEN`` SUM technique as :func:`_aggregate_groups_by_connector`,
    so an operator can tell how many of a connector's ops are
    actually callable vs ingested-but-disabled (G0.23-T5 / #1636).

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
    of rows. The ``enabled`` split rides on the same unfiltered
    universe: both numbers count the same rows, one of them narrowed
    by ``is_enabled`` only.
    """
    enabled_case = func.sum(
        case((EndpointDescriptor.is_enabled.is_(True), 1), else_=0),
    ).label("enabled")
    total = func.count(EndpointDescriptor.id).label("total")

    stmt = (
        select(
            EndpointDescriptor.tenant_id,
            EndpointDescriptor.product,
            EndpointDescriptor.version,
            EndpointDescriptor.impl_id,
            total,
            enabled_case,
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
    counts: dict[tuple[UUID | None, str, str, str], dict[str, int]] = {}
    for row in result.all():
        tenant_uuid, product, version, impl_id, total_v, enabled_v = row
        counts[(tenant_uuid, product, version, impl_id)] = {
            "total": int(total_v or 0),
            "enabled": int(enabled_v or 0),
        }
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
      have no DB rows yet ("State 0.5"). These append with every
      count zeroed and ``state="registered"``
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

    items = await _emit_db_backed_rows(
        operator=operator,
        groups_by_connector=groups_by_connector,
        op_counts_by_connector=op_counts_by_connector,
        status=status,
    )
    items.extend(
        _class_side_only_items(
            groups_by_connector.keys(),
            status=status,
            catalog=_load_catalog_or_none(),
        ),
    )
    return items


async def _emit_db_backed_rows(
    *,
    operator: Operator,
    groups_by_connector: dict[tuple[UUID | None, str, str, str], dict[str, int]],
    op_counts_by_connector: dict[tuple[UUID | None, str, str, str], dict[str, int]],
    status: ConnectorStatusFilter | None,
) -> list[ConnectorListItem]:
    """Emit one ``state="ingested"`` :class:`ConnectorListItem` per DB-backed connector.

    Walks the aggregated ``operation_group`` rows in stable sort order,
    applies the :attr:`~ConnectorListItem.status` filter, drops rows
    whose ``connector_id`` would not round-trip through the
    dispatcher's resolve path (per the G0.9.1-T1 / #773 listing-
    integrity contract — see :func:`_resolves_through_dispatcher`), and
    projects each survivor into the wire shape with ``state="ingested"``
    and ``next_step=None`` (the catalog-completion hint only applies
    to the class-side-only path; ingested rows already dispatch).

    Extracted from :func:`list_ingested_connectors` so the DB-side
    emission and the class-side-only emission stay each below the
    code-quality function-size limit.
    """
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
        op_counts = op_counts_by_connector.get(key, {})
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
                operation_count=op_counts.get("total", 0),
                enabled_operation_count=op_counts.get("enabled", 0),
                state="ingested",
            ),
        )
    return items


def _load_catalog_or_none() -> ConnectorSpecCatalog | None:
    """Return the cached catalog, or ``None`` if the load failed.

    Startup parses the catalog inside the lifespan -- a malformed catalog
    would already have crashed the app before any request reached this
    code. The defensive ``except`` here covers two edge cases that
    appear only in test contexts:

    * a unit test that monkeypatches the resource loader to inject a
      malformed YAML and then asserts the listing degrades gracefully;
    * a process mid-reload where :func:`load_catalog`'s lru_cache was
      explicitly cleared.

    Both cases prefer a degraded ``next_step`` hint (manual-mode rationale)
    over a 500 from a route the operator depends on for diagnosability.
    """
    try:
        return load_catalog()
    except CatalogError:
        _log.warning(
            "next_step_catalog_load_failed",
            reason="catalog_error_at_listing_time_fallback_to_manual_hint",
        )
        return None


def _class_side_only_items(
    db_keys: Iterable[tuple[UUID | None, str, str, str]],
    *,
    status: ConnectorStatusFilter | None,
    catalog: ConnectorSpecCatalog | None,
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
        item = _maybe_build_class_only_item(
            registry_product=product,
            registry_version=version,
            registry_impl_id=impl_id,
            db_triples=db_triples,
            catalog=catalog,
        )
        if item is not None:
            rows.append(item)
    return rows


def _maybe_build_class_only_item(
    *,
    registry_product: str,
    registry_version: str,
    registry_impl_id: str,
    db_triples: set[tuple[str, str, str]],
    catalog: ConnectorSpecCatalog | None,
) -> ConnectorListItem | None:
    """Project one v2-registry entry into a ``state="registered"`` row or skip it.

    Returns ``None`` for any of four drop conditions (v1-compat shim,
    parser failure, lossy parse, DB-dedupe); each drop emits a
    structured ``dropped_unresolvable_connector_id`` log via
    :func:`_resolve_class_only_natural_key`. Otherwise builds the
    :class:`ConnectorListItem` with ``state="registered"`` and the
    catalog-driven ``next_step`` hint.

    Extracted from :func:`_class_side_only_items` so the per-entry
    branching stays under the code-quality function-size limit.
    Keyword-only to avoid positional-arg confusion (``product`` and
    ``impl_id`` can both look like an identifier at the call site).
    """
    parsed = _resolve_class_only_natural_key(
        registry_product=registry_product,
        registry_version=registry_version,
        registry_impl_id=registry_impl_id,
    )
    if parsed is None:
        return None
    connector_id, parsed_product, parsed_version, parsed_impl_id = parsed
    if (parsed_product, parsed_version, parsed_impl_id) in db_triples:
        # DB rows already represent this connector under the parser-
        # derived natural key; the DB-loop emitted the ingested row,
        # so skip the class-only ``registered`` row to avoid
        # duplication. Covers both the trivial case (registry product
        # == parsed product) and the SDDC case (registry product
        # "sddc-manager" vs parsed "sddc").
        return None
    return ConnectorListItem(
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
        enabled_operation_count=0,
        state="registered",
        next_step=_next_step_for_registered(
            catalog=catalog,
            # Lookup against the registry triple (the catalog's native
            # key) rather than the parsed one — for SDDC the registry
            # holds ``product="sddc-manager"`` while the listing emits
            # ``product="sddc"``. The catalog is keyed on the registry
            # side; this is the lookup the operator-facing verb
            # resolves against. The manual-mode verb, however, must hand
            # back the parser-derived ``--product`` (the same spelling
            # this row advertises and the ingest path persists rows
            # under) so it round-trips to a dispatchable ingest
            # (claude-rdc-hetzner-dc#1136).
            registry_product=registry_product,
            registry_version=registry_version,
            registry_impl_id=registry_impl_id,
            dispatch_product=parsed_product,
        ),
    )


def _resolve_class_only_natural_key(
    *,
    registry_product: str,
    registry_version: str,
    registry_impl_id: str,
) -> tuple[str, str, str, str] | None:
    """Validate + parse a v2-registry entry's natural key.

    Returns ``(connector_id, parsed_product, parsed_version,
    parsed_impl_id)`` for a survivor, or ``None`` for any of three
    drop reasons (with a structured log per drop):

    * **v1-compat shim** — ``version`` or ``impl_id`` empty
      (``(product, "", "")``); registry-internal shim, not a
      separately registered connector.
    * **Parser failure** — :func:`parse_connector_id` raises on the
      emitted ``connector_id`` (impossible-to-parse ``impl_id`` shape).
    * **Lossy parse** — the parser recovers a different
      ``(version, impl_id)`` than the registry advertised; the
      dispatcher would fail to resolve any DB row eventually written
      under the registry's natural key.

    The SDDC case (registry ``product="sddc-manager"``, parsed
    ``product="sddc"``) survives because the ``(version, impl_id)``
    pair round-trips losslessly even though ``product`` doesn't —
    that's the documented exception the dispatcher's parsed product
    matches what ``SDDC_PRODUCT="sddc"`` already writes into
    ``endpoint_descriptor`` rows.
    """
    if not registry_version or not registry_impl_id:
        return None
    connector_id = build_connector_id(registry_product, registry_version, registry_impl_id)
    try:
        parsed_product, parsed_version, parsed_impl_id = parse_connector_id(connector_id)
    except ValueError:
        _log.warning(
            "dropped_unresolvable_connector_id",
            source="v2_registry",
            connector_id=connector_id,
            registry_product=registry_product,
            registry_version=registry_version,
            registry_impl_id=registry_impl_id,
            reason="parse_connector_id_raised",
        )
        return None
    if (parsed_version, parsed_impl_id) != (registry_version, registry_impl_id):
        _log.warning(
            "dropped_unresolvable_connector_id",
            source="v2_registry",
            connector_id=connector_id,
            registry_product=registry_product,
            registry_version=registry_version,
            registry_impl_id=registry_impl_id,
            parsed_product=parsed_product,
            parsed_version=parsed_version,
            parsed_impl_id=parsed_impl_id,
            reason="impl_id_or_version_not_recoverable_from_connector_id",
        )
        return None
    return connector_id, parsed_product, parsed_version, parsed_impl_id


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
