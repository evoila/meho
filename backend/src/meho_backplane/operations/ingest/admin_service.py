# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin service layer for connector ingestion + listing (G0.7-T7).

The three surfaces that drive connector ingest + listing (T5 CLI,
T6 REST, T7 admin MCP tools) all need the same orchestration:

* Parse one or more OpenAPI specs.
* Bulk-upsert them via
  :func:`~meho_backplane.operations.ingest.register_ingested.register_ingested_operations`
  (T2 #403), once per spec under one shared ``connector_id``.
* Optionally run T3's two-pass LLM grouping
  (:func:`~meho_backplane.operations.ingest.llm_groups.run_llm_grouping`)
  so the connector lands with proposed groups + per-op assignments.

Pre-T7 each surface would have wired that orchestration itself —
duplicated dispatch logic, three places to change a default, three
audit paths to keep in sync. Folding it into
:class:`ConnectorAdminService` is the load-bearing factor: T5/T6/T7
all consume the same async method here and stay strictly thin over
it. The complementary :class:`ReviewService` (T4 #402) handles the
state-machine + edit + read surface.

What this service does NOT do
-----------------------------

* Reach out to the network to fetch spec bytes. The parser
  (:func:`~meho_backplane.operations.ingest.openapi.parse_openapi`)
  handles URLs and local paths; this service receives the parsed
  output via the public method or hands the URI to the parser
  directly. A v0.2.next polish is to support uploaded spec bodies
  (multipart), but that's a request-shape concern for T6 to handle
  before calling this service.
* Decide tenant scoping policy beyond the existing RBAC discipline
  in :class:`ReviewService`. Ingesting into a tenant scope (rather
  than built-in) is allowed for ``tenant_admin``; cross-tenant
  ingest is rejected via :class:`ConnectorNotFoundError` semantics
  the way the review service rejects cross-tenant edits.

Public surface (re-exported from
:mod:`meho_backplane.operations.ingest`):

* :class:`IngestRequest` — Pydantic shape carrying everything T6's
  POST body and T7's MCP arguments need.
* :class:`IngestResponse` — Pydantic shape carrying the
  :class:`IngestionResult` per spec and the
  :class:`GroupingResult` (or ``None`` when grouping was skipped /
  dry-run).
* :class:`ConnectorSummary` — One row of the ``list_connectors``
  response.
* :class:`ConnectorListResponse` — The full listing.
* :class:`ConnectorAdminService` — The orchestrator itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.ingest._admin_list_internals import (
    aggregate_status,
    build_connector_summary_rows,
    enumerate_visible_triples,
)
from meho_backplane.operations.ingest.exceptions import (
    ConnectorNotFoundError,
)
from meho_backplane.operations.ingest.llm_groups import (
    DEFAULT_GROUPING_BATCH_SIZE,
    DEFAULT_MAX_GROUPS,
    DEFAULT_MIN_GROUPS,
    GroupingResult,
    LlmClient,
    run_llm_grouping,
)
from meho_backplane.operations.ingest.openapi import parse_openapi
from meho_backplane.operations.ingest.register_ingested import (
    IngestionResult,
    register_ingested_operations,
)

__all__ = [
    "ConnectorAdminService",
    "ConnectorListResponse",
    "ConnectorStatusFilter",
    "ConnectorSummary",
    "IngestRequest",
    "IngestResponse",
    "IngestSpecRef",
    "SpecIngestionOutcome",
]

_log = structlog.get_logger(__name__)


#: Status filter accepted by :meth:`ConnectorAdminService.list_connectors`.
#:
#: Mirrors the T6 REST query parameter and the T5 CLI ``--status`` flag
#: so all three surfaces share the same vocabulary. ``"all"`` returns
#: every status; the others narrow by ``OperationGroup.review_status``.
#:
#: An aggregate ``connector_status`` is computed per row from the set
#: of group statuses (see :meth:`ConnectorAdminService.list_connectors`)
#: — a connector with mixed group statuses is reported as ``"staged"``
#: when any group is still staged, else ``"enabled"`` when at least one
#: is enabled, else ``"disabled"``. The filter applies to the
#: aggregate, not to individual groups.
ConnectorStatusFilter = Literal["staged", "enabled", "disabled", "all"]


class IngestSpecRef(BaseModel):
    """One spec source to ingest under the requested connector triple.

    A single ingest request can carry multiple specs to merge them
    under one ``connector_id`` (e.g. vSphere's ``vcenter.yaml`` +
    ``vi-json.yaml``). Each spec is parsed once, each row is tagged
    with ``f"spec:{source_label or uri}"`` so operators reviewing the
    connector can tell which spec contributed which op.
    """

    model_config = ConfigDict(frozen=True)

    uri: str = Field(min_length=1)
    """File path or ``http(s)://`` URL the parser consumes verbatim."""

    source_label: str | None = None
    """Optional human-friendly identifier injected into the
    per-row ``"spec:..."`` tag. Defaults to *uri* when omitted; the
    full URI is too long for operator-facing renders against multi-
    spec connectors so most callers pass a short label."""


class IngestRequest(BaseModel):
    """Request body shared by T6 REST + T7 MCP ingest.

    The CLI (T5) likewise constructs one of these and forwards it to
    the REST surface. Frozen because the orchestrator threads the
    same instance through ``register_ingested_operations`` +
    ``run_llm_grouping``; an accidental mid-pipeline mutation would
    yield a confused-deputy bug in the audit trail.
    """

    model_config = ConfigDict(frozen=True)

    product: str = Field(min_length=1)
    version: str = Field(min_length=1)
    impl_id: str = Field(min_length=1)
    specs: list[IngestSpecRef] = Field(min_length=1)
    base_url: str | None = None
    """Optional default base URL persisted on the auto-registered
    :class:`~meho_backplane.operations.ingest.GenericRestConnector`
    shim. Only used on first ingestion of a triple; subsequent
    ingestions leave the existing connector's base_url unchanged."""

    dry_run: bool = False
    """When ``True``, parse the specs and return the predicted
    :class:`IngestionResult` shape with all counts zeroed, but skip
    the DB writes + the LLM grouping pass. T5 CLI ``--dry-run``
    routes here."""

    run_grouping: bool = True
    """When ``True`` (default), run the two-pass LLM grouping after
    bulk upsert. When ``False``, leave every row's ``group_id``
    NULL — used by tests and by ``--no-grouping`` operator
    overrides for diagnostic ingests."""

    batch_size: int = DEFAULT_GROUPING_BATCH_SIZE
    min_groups: int = DEFAULT_MIN_GROUPS
    max_groups: int = DEFAULT_MAX_GROUPS

    tenant_id: UUID | None = None
    """Target tenant scope. ``None`` → built-in / global (the only
    scope ``tenant_admin`` callers can target *across* tenants);
    UUID → tenant-curated connector under the operator's own
    tenant. Cross-tenant ingest is rejected via
    :class:`ConnectorNotFoundError`."""


class SpecIngestionOutcome(BaseModel):
    """One :class:`IngestionResult` rolled up with its spec identity.

    Surfaced in operator output (CLI table + REST JSON) so they can
    see per-spec counts when a multi-spec ingest mixes inserts +
    skips across sources.
    """

    model_config = ConfigDict(frozen=True)

    source_label: str
    """The ``source_label`` from the matching :class:`IngestSpecRef`
    (or the URI when the caller omitted the label)."""

    uri: str
    inserted_count: int
    updated_count: int
    skipped_count: int
    connector_registered: bool


class IngestResponse(BaseModel):
    """Aggregate response shape from :meth:`ConnectorAdminService.ingest`."""

    model_config = ConfigDict(frozen=True)

    connector_id: str
    """Echoed back so callers don't need to recompute the triple."""

    product: str
    version: str
    impl_id: str
    tenant_id: UUID | None

    specs: list[SpecIngestionOutcome]
    """One entry per spec in the request. Order matches the request."""

    grouping: GroupingResult | None = None
    """``None`` when grouping was skipped (``dry_run=True`` or
    ``run_grouping=False``) and when there were no unassigned ops
    to group (idempotent re-run path)."""

    dry_run: bool = False
    """Echoed back; on ``True`` every count in *specs* is the
    predicted (parse-only) count, not a true DB outcome."""


class ConnectorSummary(BaseModel):
    """One connector in :class:`ConnectorListResponse`.

    Counts and the aggregate status are computed from the live
    ``operation_group`` + ``endpoint_descriptor`` rows for the
    triple.
    """

    model_config = ConfigDict(frozen=True)

    connector_id: str
    product: str
    version: str
    impl_id: str
    tenant_id: UUID | None
    """``None`` for built-in / global connectors; UUID for
    tenant-curated."""

    group_count: int
    operation_count: int
    enabled_operation_count: int
    connector_status: Literal["staged", "enabled", "disabled"]
    """Aggregate computed from group review statuses. ``"staged"``
    if any group is still staged; else ``"enabled"`` when at least
    one is enabled; else ``"disabled"``."""

    last_updated_at: datetime | None
    """Latest ``updated_at`` across all group rows for the
    connector. ``None`` is impossible in practice (a connector with
    zero groups is filtered out earlier) but typed permissively to
    avoid leaking SQL-level row ordering assumptions to callers."""


class ConnectorListResponse(BaseModel):
    """Response shape from :meth:`ConnectorAdminService.list_connectors`."""

    model_config = ConfigDict(frozen=True)

    connectors: list[ConnectorSummary]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ConnectorAdminService:
    """Orchestrates connector ingest + listing for T5 / T6 / T7.

    Construct one per request with the calling :class:`Operator`.
    The instance is cheap (no eager DB / network IO) and may be
    discarded after the call completes.

    Test seams: pass a ``sessionmaker`` to redirect the DB queries
    to an in-test SQLite engine, and an ``llm_client`` to stub out
    the chassis LLM adapter without monkey-patching.
    """

    def __init__(
        self,
        operator: Operator,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        llm_client: LlmClient | None = None,
    ) -> None:
        self._operator = operator
        # Resolve lazily, same shape as ReviewService — see its
        # docstring for the chassis-engine-reset rationale.
        self._explicit_sessionmaker = sessionmaker
        self._llm_client = llm_client

    def _sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._explicit_sessionmaker is not None:
            return self._explicit_sessionmaker
        return get_sessionmaker()

    # -- authorisation shared with ingestion ------------------------------

    def _authorize_ingest_scope(self, tenant_id: UUID | None) -> None:
        """Reject ingest into a scope the operator can't act on.

        Mirrors :meth:`ReviewService._authorize_scope` exactly:
        built-in (``tenant_id is None``) requires ``TENANT_ADMIN``;
        any other ``tenant_id`` must equal the operator's own
        tenant. Cross-tenant probing collapses into the same
        ``ConnectorNotFoundError`` so unprivileged operators can't
        enumerate scopes by status-code differential.
        """
        if tenant_id is None:
            if self._operator.tenant_role is not TenantRole.TENANT_ADMIN:
                raise ConnectorNotFoundError(
                    connector_id="<ingest>",
                    tenant_id=tenant_id,
                )
            return
        if tenant_id != self._operator.tenant_id:
            raise ConnectorNotFoundError(
                connector_id="<ingest>",
                tenant_id=tenant_id,
            )

    # -- public read API --------------------------------------------------

    async def list_connectors(
        self,
        *,
        status: ConnectorStatusFilter = "all",
    ) -> ConnectorListResponse:
        """List connectors visible to the operator's tenant + built-in.

        The query scans :class:`OperationGroup` for every
        ``(product, version, impl_id, tenant_id)`` tuple the operator
        is allowed to see, then folds in :class:`EndpointDescriptor`
        counts (total + enabled). Built-in rows
        (``tenant_id IS NULL``) are always visible; tenant-curated
        rows are visible only for the operator's own tenant.

        The aggregate ``connector_status`` per row:

        * ``"staged"`` — at least one group is still in
          ``review_status='staged'``. Caller's review queue.
        * ``"enabled"`` — every non-staged group is enabled and at
          least one is enabled. The everyday state.
        * ``"disabled"`` — no group is staged or enabled (every
          group is disabled). The rollback state.

        The ``status`` filter applies to the aggregate. ``"all"``
        returns every row; the three specific values narrow.
        """
        sessionmaker = self._sessionmaker()
        async with sessionmaker() as session:
            triples = await enumerate_visible_triples(
                session,
                operator_tenant_id=self._operator.tenant_id,
            )
            summaries: list[ConnectorSummary] = []
            for triple in triples:
                summary = await self._summarise_triple(session, triple)
                if status == "all" or summary.connector_status == status:
                    summaries.append(summary)
        # Stable display ordering: built-in first, then by connector_id.
        summaries.sort(
            key=lambda summary: (
                summary.tenant_id is not None,
                summary.connector_id,
            ),
        )
        return ConnectorListResponse(connectors=summaries)

    async def _summarise_triple(
        self,
        session: AsyncSession,
        triple: tuple[str, str, str, UUID | None],
    ) -> ConnectorSummary:
        """Project one ``(product, version, impl_id, tenant_id)`` triple
        into a :class:`ConnectorSummary` via the shared scan helpers."""
        product, version, impl_id, tenant_id = triple
        (
            status_counts,
            last_updated_at,
            operation_count,
            enabled_operation_count,
        ) = await build_connector_summary_rows(session, triple=triple)
        return ConnectorSummary(
            connector_id=f"{impl_id}-{version}",
            product=product,
            version=version,
            impl_id=impl_id,
            tenant_id=tenant_id,
            group_count=sum(status_counts.values()),
            operation_count=operation_count,
            enabled_operation_count=enabled_operation_count,
            connector_status=aggregate_status(status_counts),
            last_updated_at=last_updated_at,
        )

    # -- public write API: ingest -----------------------------------------

    async def ingest(self, request: IngestRequest) -> IngestResponse:
        """Run the full ingest pipeline for *request*.

        Parses each spec in order, registers it under the requested
        ``(product, version, impl_id)`` triple, and (unless
        ``dry_run`` or ``run_grouping=False``) runs the two-pass
        LLM grouping at the end.

        ``dry_run=True`` parses the specs and returns the predicted
        outcome with all DB-write counts zeroed. The parsed shape is
        still validated, so a bad spec still raises (the operator
        learns about it before committing).

        Raises :class:`ConnectorNotFoundError` for an out-of-scope
        ingest (built-in by a non-admin, or a foreign tenant).
        Raises :class:`OpIdCollision` /
        :class:`InvalidSpecError` /
        :class:`UnsupportedSpecError` /
        :class:`InvalidSchemaError` from the parser + register
        helpers verbatim.
        """
        self._authorize_ingest_scope(request.tenant_id)

        spec_outcomes: list[SpecIngestionOutcome] = []
        for spec in request.specs:
            outcome = await self._ingest_one_spec(
                request=request,
                spec=spec,
            )
            spec_outcomes.append(outcome)

        grouping: GroupingResult | None = None
        if not request.dry_run and request.run_grouping:
            grouping = await self._maybe_run_grouping(request)

        connector_id = f"{request.impl_id}-{request.version}"
        _log.info(
            "connector_ingest_complete",
            connector_id=connector_id,
            tenant_id=str(request.tenant_id) if request.tenant_id else None,
            spec_count=len(request.specs),
            dry_run=request.dry_run,
            grouping_ran=grouping is not None,
        )
        return IngestResponse(
            connector_id=connector_id,
            product=request.product,
            version=request.version,
            impl_id=request.impl_id,
            tenant_id=request.tenant_id,
            specs=spec_outcomes,
            grouping=grouping,
            dry_run=request.dry_run,
        )

    # -- ingest internals -------------------------------------------------

    async def _ingest_one_spec(
        self,
        *,
        request: IngestRequest,
        spec: IngestSpecRef,
    ) -> SpecIngestionOutcome:
        """Parse + register one spec; honour ``dry_run``."""
        source_label = spec.source_label or spec.uri
        # The parser tags each row with `spec:<source_label>` via the
        # spec_source keyword. Operators reviewing a multi-spec
        # connector see those tags in the review payload.
        proto_ops = parse_openapi(spec.uri, spec_source=source_label)

        if request.dry_run:
            _log.info(
                "connector_ingest_dry_run_parsed",
                product=request.product,
                version=request.version,
                impl_id=request.impl_id,
                source_label=source_label,
                operation_count=len(proto_ops),
            )
            return SpecIngestionOutcome(
                source_label=source_label,
                uri=spec.uri,
                inserted_count=0,
                updated_count=0,
                skipped_count=0,
                connector_registered=False,
            )

        result: IngestionResult = await register_ingested_operations(
            product=request.product,
            version=request.version,
            impl_id=request.impl_id,
            spec_source=source_label,
            operations=proto_ops,
            base_url=request.base_url,
            tenant_id=request.tenant_id,
        )
        return SpecIngestionOutcome(
            source_label=source_label,
            uri=spec.uri,
            inserted_count=result.inserted_count,
            updated_count=result.updated_count,
            skipped_count=result.skipped_count,
            connector_registered=result.connector_registered,
        )

    async def _maybe_run_grouping(
        self,
        request: IngestRequest,
    ) -> GroupingResult | None:
        """Invoke T3 grouping unless the operator opted out / dry-run.

        Returns ``None`` only on the rare path where the LLM client
        is not configured AND grouping is enabled — in production
        that's a config bug (T6 / T7 wire a configured client at
        startup) but the service layer surfaces ``None`` rather than
        crashing so the caller's audit row still commits.
        """
        if self._llm_client is None:
            _log.warning(
                "connector_ingest_grouping_skipped_no_llm_client",
                product=request.product,
                version=request.version,
                impl_id=request.impl_id,
            )
            return None
        return await run_llm_grouping(
            llm_client=self._llm_client,
            operator_sub=self._operator.sub,
            operator_tenant_id=self._operator.tenant_id,
            product=request.product,
            version=request.version,
            impl_id=request.impl_id,
            tenant_id=request.tenant_id,
            batch_size=request.batch_size,
            min_groups=request.min_groups,
            max_groups=request.max_groups,
            sessionmaker=self._explicit_sessionmaker,
        )
