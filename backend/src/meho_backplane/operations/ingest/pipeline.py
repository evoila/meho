# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end ingestion-pipeline service + connector-list query helper.

This module bundles the three T1 / T2 / T3 stages into one
service-layer entry point the REST router (T6,
:mod:`meho_backplane.api.v1.connectors_ingest`), CLI verbs (T5), and
admin MCP tools (T7) all consume:

* :class:`IngestionPipelineService` — orchestrates parse →
  ``register_ingested_operations`` → ``run_llm_grouping`` for a
  ``(product, version, impl_id)`` connector triple ingesting one or
  more OpenAPI specs.
* :func:`list_ingested_connectors` — aggregates
  :class:`~meho_backplane.db.models.OperationGroup` rows into one
  :class:`~meho_backplane.operations.ingest.api_schemas.ConnectorListItem`
  per visible connector.

Both surfaces are tenant-scoped at construction time: the service is
built from an :class:`Operator` and every operation acts on either
``tenant_id == operator.tenant_id`` or ``tenant_id is None`` (built-in
scope, gated on :class:`TenantRole.TENANT_ADMIN`). Cross-tenant probes
raise :class:`ConnectorNotFoundError` — the same conflation
:class:`ReviewService` uses to keep the operator-facing failure
surface uniform.

LLM-client injection
--------------------

The grouping pass requires an :class:`LlmClient` Protocol
implementation. The chassis does not yet ship a production adapter
(T5 of #389 lands the Anthropic Messages-API binding); to keep T6's
REST surface workable both in tests and once the production adapter
is wired, the pipeline accepts an injectable factory at construction
time. The default factory raises :class:`LlmClientUnavailable` so
calling ``POST /api/v1/connectors/ingest`` against a backplane that
hasn't configured an LLM client returns 503 rather than crashing.
Sibling tests / the CLI / the MCP tools each inject their own
client.

Multi-spec merge
----------------

A single :meth:`IngestionPipelineService.ingest` call processes a
list of :class:`SpecSource` entries. Each spec is parsed and upserted
under the same connector triple with the spec's ``uri`` as the
``spec_source`` tag (see
:func:`register_ingested_operations`'s docstring for the tagging
contract). Counts in the returned :class:`IngestionResult` aggregate
across all specs in the request.

Dry run
-------

``dry_run=True`` short-circuits both the DB writes and the LLM call:
the pipeline parses every spec and counts how many operations would
land but does not touch ``endpoint_descriptor`` or
``operation_group``. The response carries ``grouping=None`` and
``ingestion.connector_registered=False`` because neither stage was
exercised. Useful for operators validating a spec before committing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal
from uuid import UUID

import structlog
from packaging.version import InvalidVersion, Version
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations.ingest._llm_grouping_internals import (
    DEFAULT_GROUPING_BATCH_SIZE,
    DEFAULT_MAX_GROUPS,
    DEFAULT_MIN_GROUPS,
    LlmClient,
    build_connector_id,
)
from meho_backplane.operations.ingest.api_schemas import (
    GroupingResultModel,
    IngestionResultModel,
    SpecSource,
)
from meho_backplane.operations.ingest.connector_registration import (
    check_version_covered_by_registered_class,
)
from meho_backplane.operations.ingest.exceptions import VersionMismatchError
from meho_backplane.operations.ingest.llm_groups import (
    GroupingResult,
    run_llm_grouping,
)
from meho_backplane.operations.ingest.openapi import (
    parse_openapi,
    read_spec_info_version,
)
from meho_backplane.operations.ingest.register_ingested import (
    IngestionResult,
    register_ingested_operations,
)
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "IngestionPipelineResult",
    "IngestionPipelineService",
    "LlmClientFactory",
    "LlmClientUnavailable",
    "default_llm_client_factory",
]

_log = structlog.get_logger(__name__)


class LlmClientUnavailable(RuntimeError):  # noqa: N818 -- "Unavailable" reads better than "Error" in the 503 detail message
    """Raised when an LLM client is required but no factory is configured.

    The chassis ships :class:`IngestionPipelineService` with a
    default-fail factory so a misconfigured deployment surfaces here
    rather than crashing partway through the grouping pass. The REST
    layer maps this onto HTTP 503 (Service Unavailable); the CLI / MCP
    sibling tasks render their own operator-facing error.
    """


#: Type alias for the LLM-client factory the pipeline uses.
#:
#: A factory rather than a singleton instance so the chassis can lazy-
#: build the client (e.g. after :func:`Settings.cache_clear` in tests)
#: and so each ingest call can get a fresh client when the
#: implementation pins per-call retry state. The default factory
#: raises :class:`LlmClientUnavailable`.
LlmClientFactory = Callable[[], LlmClient]


def default_llm_client_factory() -> LlmClient:
    """Fail-closed default — no production LLM adapter is wired yet.

    Public so that sibling consumers (REST routes at T6, CLI verbs at
    T5, admin MCP tools at T7) can import the same fallback factory
    from :mod:`meho_backplane.operations.ingest` without reaching
    across the underscore boundary.
    """
    raise LlmClientUnavailable(
        "no LLM client configured for spec-ingestion grouping; "
        "the production Anthropic adapter lands with G0.7-T5 (#405). "
        "Tests inject a deterministic stub via "
        "IngestionPipelineService(..., llm_client_factory=...).",
    )


#: Outcome of one :func:`_classify_version_match` call.
#:
#: * ``"exact"`` — the operator's label is the spec's ``info.version``
#:   verbatim, normalises to the same PEP 440 :class:`Version`, or is a
#:   release-tuple prefix of the spec's version (``spec=9.0.3``,
#:   ``label=9.0`` / ``9`` all qualify). Proceed without comment.
#: * ``"compatible"`` — the two share a major version but differ
#:   inside it (``spec=9.0.3``, ``label=9.1``). The resolver's tie-
#:   break math (``>=N.0,<N+1.0`` per major) treats both as the same
#:   compatibility band; ingest proceeds and emits a structured
#:   ``connector_ingest_version_drift`` log event.
#: * ``"incompatible"`` — different major versions (``spec=7.0.x``,
#:   ``label=9.0``), or one of the two strings is non-PEP-440 and
#:   doesn't match verbatim. Fail-closed with
#:   :class:`VersionMismatchError`.
_VersionMatch = Literal["exact", "compatible", "incompatible"]


def _classify_version_match(spec_version: str, label_version: str) -> _VersionMatch:
    """Compare a spec's ``info.version`` against an operator-supplied label.

    The semantics mirror the runtime resolver in
    :mod:`meho_backplane.connectors.resolver` — a spec's ``9.0.3``
    belongs to the ``>=9.0,<10.0`` band, so any label that
    parses to a :class:`packaging.version.Version` in that band is
    "compatible" enough to proceed; mismatched major versions never
    are. Verbatim string equality is checked first so non-PEP-440
    strings (vendor product codenames like ``"acme-2024Q3"``) still
    pass when the operator types them identically.

    Args:
        spec_version: ``info.version`` from the parsed OpenAPI spec.
        label_version: The operator-supplied
            :attr:`IngestRequest.version` label.

    Returns:
        ``"exact"`` / ``"compatible"`` / ``"incompatible"`` per the
        :class:`_VersionMatch` table.
    """
    if spec_version == label_version:
        return "exact"
    try:
        spec_v = Version(spec_version)
        label_v = Version(label_version)
    except InvalidVersion:
        # At least one side is non-PEP-440. We already ruled out a
        # verbatim string match above, so the label cannot be
        # confidently classified as compatible — fail-closed and let
        # the operator either correct the label or downgrade the spec.
        return "incompatible"
    if spec_v == label_v:
        # PEP 440 normalisation: e.g. Version("1") == Version("1.0").
        return "exact"
    # Release-tuple prefix match: spec=9.0.3, label=9.0 → ("9", "0")
    # is a prefix of ("9", "0", "3"). Order matters — the label is
    # the operator's coarser label, so its release tuple must be a
    # prefix of the spec's. The reverse (label=9.0.3, spec=9.0)
    # falls through to the compatible / incompatible bands.
    if spec_v.release[: len(label_v.release)] == label_v.release:
        return "exact"
    # Same major version → compatible band.
    if spec_v.release and label_v.release and spec_v.release[0] == label_v.release[0]:
        return "compatible"
    return "incompatible"


def _build_spec_label_mismatch(
    *,
    requested_version: str,
    mismatches: list[tuple[str, str | None]],
) -> VersionMismatchError:
    """Construct the ``spec_label_mismatch`` exception with an operator-facing suggestion.

    Picks the first mismatching spec's ``info.version`` as the
    suggested correction — for the common single-spec case it's the
    exact value the operator should re-ingest under; for the multi-
    spec case it nudges them toward inspection of the listed bundle.
    """
    _, primary_spec_version = mismatches[0]
    suggestion = (
        f"either re-ingest under version={primary_spec_version!r} "
        f"to match the spec, or supply specs whose info.version "
        f"matches version={requested_version!r}"
        if primary_spec_version is not None
        else None
    )
    return VersionMismatchError(
        kind="spec_label_mismatch",
        requested_version=requested_version,
        spec_info_versions=mismatches,
        suggestion=suggestion,
    )


def _check_multi_spec_consistency(
    *,
    per_spec: list[tuple[str, str | None]],
    requested_version: str,
) -> None:
    """Verify every supplied spec declares a compatible major version.

    Only meaningful when at least two specs declare an
    ``info.version``; single-spec ingests and ingests where most
    specs omit ``info.version`` slip past without comment.

    Specs whose ``info.version`` isn't PEP 440 parseable contribute
    a ``hash(version)`` sentinel to the majors set so two identical
    vendor codenames cluster together; the rare hash collision is
    harmless because the operator-facing message lists both raw
    values for diagnosis regardless of which branch fired.
    """
    declared = [(uri, version) for uri, version in per_spec if version is not None]
    if len(declared) < 2:
        return
    majors: set[int] = set()
    for _, version in declared:
        try:
            parsed = Version(version)
        except InvalidVersion:
            majors.add(hash(version))
            continue
        if parsed.release:
            majors.add(parsed.release[0])
    if len(majors) > 1:
        raise VersionMismatchError(
            kind="multi_spec_inconsistent",
            requested_version=requested_version,
            spec_info_versions=declared,
        )


class IngestionPipelineResult:
    """Bundled result of one :meth:`IngestionPipelineService.ingest` call.

    Carries the aggregated :class:`IngestionResult` (counts summed
    across every spec in the request) and the
    :class:`GroupingResult` for the single grouping pass that runs
    after all specs are upserted. ``grouping`` is ``None`` for the
    dry-run path.

    Not a frozen :class:`dataclass` because the route layer projects
    it into :class:`IngestionResultModel` / :class:`GroupingResultModel`
    before returning to the client; the intermediate shape is
    internal.
    """

    __slots__ = ("connector_id", "grouping", "ingestion")

    def __init__(
        self,
        *,
        connector_id: str,
        ingestion: IngestionResult,
        grouping: GroupingResult | None,
    ) -> None:
        self.connector_id = connector_id
        self.ingestion = ingestion
        self.grouping = grouping

    def to_api_models(self) -> tuple[IngestionResultModel, GroupingResultModel | None]:
        """Project to the Pydantic models the REST routes return.

        Returns ``(ingestion_model, grouping_model_or_none)``. The
        connector_id is echoed onto both inner models so callers
        round-tripping the response see the same identifier they
        sent in.
        """
        ingestion_model = IngestionResultModel(
            connector_id=self.connector_id,
            inserted_count=self.ingestion.inserted_count,
            updated_count=self.ingestion.updated_count,
            skipped_count=self.ingestion.skipped_count,
            connector_registered=self.ingestion.connector_registered,
            operations_grouped=self.ingestion.operations_grouped,
        )
        grouping_model: GroupingResultModel | None = None
        if self.grouping is not None:
            grouping_model = GroupingResultModel(
                connector_id=self.grouping.connector_id,
                groups_created=self.grouping.groups_created,
                operations_assigned=self.grouping.operations_assigned,
                operations_unassigned=self.grouping.operations_unassigned,
                llm_call_count=self.grouping.llm_call_count,
                llm_duration_ms=self.grouping.llm_duration_ms,
            )
        return ingestion_model, grouping_model


class IngestionPipelineService:
    """Orchestrates the T1 → T2 → T3 ingestion pipeline for one connector.

    Built per-request from the route's
    :class:`~meho_backplane.auth.operator.Operator` so the service-
    level audit rows the T2 / T3 helpers write carry the originating
    operator's identity. The same instance can also be re-used by the
    CLI verbs (T5) and admin MCP tools (T7); both pass their own
    :class:`Operator` at construction.

    Tenant scoping: the constructor optionally takes an explicit
    ``tenant_id`` (``None`` → built-in scope). Built-in ingests are
    gated on :class:`TenantRole.TENANT_ADMIN`; tenant-curated ingests
    are gated on a match between the requested ``tenant_id`` and the
    operator's tenant. Mismatches raise :class:`PermissionError`,
    which the REST router maps onto HTTP 403.

    LLM-client factory: pass a custom factory to inject a test stub or
    a production adapter once one lands. The default fail-closed
    factory keeps misconfigured deployments loud.
    """

    def __init__(
        self,
        operator: Operator,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        llm_client_factory: LlmClientFactory = default_llm_client_factory,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self._operator = operator
        self._explicit_sessionmaker = sessionmaker
        self._llm_client_factory = llm_client_factory
        self._embedding_service = embedding_service

    def _sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """Resolve the sessionmaker lazily — see
        :meth:`ReviewService._sessionmaker` for the same shape.
        """
        if self._explicit_sessionmaker is not None:
            return self._explicit_sessionmaker
        return get_sessionmaker()

    def _authorize(self, tenant_id: UUID | None) -> None:
        """Mirror :meth:`ReviewService._authorize_scope` for ingest paths.

        Built-in ingests require ``tenant_admin``; tenant-curated
        ingests require the operator's tenant_id to match. The route
        layer enforces ``tenant_admin`` minimum on the ingest endpoint
        already, but the service-layer guard is defence-in-depth so
        the CLI / MCP siblings get the same isolation even if they
        skip the route decorator.
        """
        if tenant_id is None:
            if self._operator.tenant_role is not TenantRole.TENANT_ADMIN:
                raise PermissionError(
                    "built-in connector ingest requires tenant_admin",
                )
            return
        if tenant_id != self._operator.tenant_id:
            raise PermissionError(
                f"operator tenant_id={self._operator.tenant_id} cannot ingest into "
                f"tenant_id={tenant_id}",
            )

    def _validate_spec_versions(
        self,
        *,
        specs: Sequence[SpecSource],
        requested_version: str,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Cross-check operator-supplied ``version`` against every spec's ``info.version``.

        Three guards in one pass:

        1. Each spec's ``info.version`` is parsed via the lightweight
           :func:`read_spec_info_version` helper (no DB hits, no full
           parse — just enough to read the ``info`` block).
        2. Each parsed ``info.version`` is classified against the
           operator's label via :func:`_classify_version_match`. An
           ``incompatible`` outcome raises
           :class:`VersionMismatchError` with ``kind="spec_label_mismatch"``;
           a ``compatible`` outcome emits a structured
           ``connector_ingest_version_drift`` event and proceeds.
        3. After collecting every spec's ``info.version``, the bundle
           is checked for internal consistency: two specs sharing a
           major version are fine (same connector triple); two specs
           disagreeing on the major version are not, and raise
           :class:`VersionMismatchError` with
           ``kind="multi_spec_inconsistent"``.

        Specs missing ``info.version`` entirely contribute ``None`` to
        the collected list; they're skipped for the cross-check
        (older specs without ``info.version`` can still be ingested
        under whatever label).

        The cross-check runs early — before the parser does the full
        operation walk — so an obviously-misclassified ingest fails
        in milliseconds rather than after we've spent CPU parsing a
        2,000-op spec.
        """
        per_spec, mismatches = self._classify_per_spec(
            specs=specs, requested_version=requested_version, log=log
        )
        if mismatches:
            raise _build_spec_label_mismatch(
                requested_version=requested_version, mismatches=mismatches
            )
        _check_multi_spec_consistency(per_spec=per_spec, requested_version=requested_version)

    @staticmethod
    def _classify_per_spec(
        *,
        specs: Sequence[SpecSource],
        requested_version: str,
        log: structlog.stdlib.BoundLogger,
    ) -> tuple[list[tuple[str, str | None]], list[tuple[str, str | None]]]:
        """Read each spec's ``info.version`` and bucket the outcome.

        Returns ``(per_spec, mismatches)``: every spec contributes one
        ``(uri, info_version_or_none)`` row to ``per_spec`` (used by
        the multi-spec consistency pass); ``mismatches`` lists only
        the rows whose ``info.version`` was incompatible with the
        operator's label.
        """
        per_spec: list[tuple[str, str | None]] = []
        mismatches: list[tuple[str, str | None]] = []
        for spec in specs:
            info_version = read_spec_info_version(spec.uri)
            per_spec.append((spec.uri, info_version))
            if info_version is None:
                # No info.version → no cross-check possible. Operators
                # ingesting older specs without ``info.version`` keep
                # working; document this loudly so the audit trail
                # shows we skipped a check rather than passed one.
                log.info(
                    "connector_ingest_version_check_skipped",
                    spec_uri=spec.uri,
                    reason="spec_info_version_missing",
                )
                continue
            match = _classify_version_match(info_version, requested_version)
            if match == "incompatible":
                mismatches.append((spec.uri, info_version))
            elif match == "compatible":
                # Same major, different minor — warn and proceed per the
                # G0.9-T8 contract. The structured event names both
                # values so the operator can decide whether to re-ingest
                # under the corrected label after the fact.
                log.warning(
                    "connector_ingest_version_drift",
                    spec_uri=spec.uri,
                    spec_info_version=info_version,
                    requested_version=requested_version,
                    match="compatible",
                )
        return per_spec, mismatches

    async def ingest(
        self,
        *,
        product: str,
        version: str,
        impl_id: str,
        specs: Sequence[SpecSource],
        base_url: str | None = None,
        tenant_id: UUID | None = None,
        dry_run: bool = False,
    ) -> IngestionPipelineResult:
        """Run the full pipeline (parse → register → group) for one connector.

        See the module docstring for the multi-spec merge,
        dry-run, and LLM-client contracts. The return value bundles
        the aggregated :class:`IngestionResult` + the single
        :class:`GroupingResult` for downstream projection into the
        REST response.

        Raises :class:`PermissionError` when the operator's tenancy
        doesn't permit writes to *tenant_id*. The parser, registrar,
        and grouping pass propagate their own domain exceptions
        verbatim — the router catches them at the HTTP boundary.
        """
        self._authorize(tenant_id)
        connector_id = build_connector_id(product, version, impl_id)
        log = _log.bind(
            connector_id=connector_id,
            spec_count=len(specs),
            dry_run=dry_run,
            tenant_id=str(tenant_id) if tenant_id is not None else None,
            operator_sub=self._operator.sub,
        )

        # Spec-vs-label cross-check runs before either path: even the
        # dry-run path benefits from catching the mistake at the validate
        # boundary rather than letting an operator dry-run a spec that
        # belongs under a different version label and convince themselves
        # the real ingest will work.
        self._validate_spec_versions(specs=specs, requested_version=version, log=log)

        if dry_run:
            log.info("ingestion_pipeline_dry_run_start")
            # G0.9-T9 (#741) — pre-flight runs in dry-run too so the
            # operator validating a spec sees the same 422 they would
            # see on the real path; the check is cheap (one v2-registry
            # snapshot walk) and parallels the dispatcher's resolver
            # contract. ``register_ingested_operations`` (the real-path
            # caller) re-invokes the same helper before the auto-shim
            # is synthesised; the duplicate call is idempotent.
            check_version_covered_by_registered_class(
                product=product,
                version=version,
                impl_id=impl_id,
            )
            return await self._run_dry_run(
                product=product,
                version=version,
                impl_id=impl_id,
                specs=specs,
                connector_id=connector_id,
            )

        log.info("ingestion_pipeline_start")
        sessionmaker = self._sessionmaker()
        aggregated = await self._run_register_phase(
            product=product,
            version=version,
            impl_id=impl_id,
            specs=specs,
            base_url=base_url,
            tenant_id=tenant_id,
            sessionmaker=sessionmaker,
        )
        log.info(
            "ingestion_pipeline_register_complete",
            inserted_count=aggregated.inserted_count,
            updated_count=aggregated.updated_count,
            skipped_count=aggregated.skipped_count,
            connector_registered=aggregated.connector_registered,
        )

        grouping_result = await self._run_grouping_phase(
            product=product,
            version=version,
            impl_id=impl_id,
            tenant_id=tenant_id,
            sessionmaker=sessionmaker,
        )
        log.info(
            "ingestion_pipeline_grouping_complete",
            groups_created=grouping_result.groups_created,
            operations_assigned=grouping_result.operations_assigned,
            operations_unassigned=grouping_result.operations_unassigned,
        )
        return IngestionPipelineResult(
            connector_id=connector_id,
            ingestion=aggregated,
            grouping=grouping_result,
        )

    # ----- private helpers ------------------------------------------------

    async def _run_dry_run(
        self,
        *,
        product: str,
        version: str,
        impl_id: str,
        specs: Sequence[SpecSource],
        connector_id: str,
    ) -> IngestionPipelineResult:
        """Parse every spec and project the parse counts into a result.

        No DB writes, no LLM call. ``inserted_count`` reports how many
        operations would be inserted on a real run; the other counts
        stay at zero because the body-hash skip / update branches only
        apply once we read existing rows. Operators use the dry-run
        path to verify a spec parses before they commit.
        """
        total_ops = 0
        for spec in specs:
            parsed = parse_openapi(spec.uri, spec_source=spec.uri)
            total_ops += len(parsed)
        ingestion = IngestionResult(
            inserted_count=total_ops,
            updated_count=0,
            skipped_count=0,
            connector_registered=False,
            operations_grouped=False,
        )
        _log.info(
            "ingestion_pipeline_dry_run_complete",
            connector_id=connector_id,
            operation_count=total_ops,
        )
        return IngestionPipelineResult(
            connector_id=connector_id,
            ingestion=ingestion,
            grouping=None,
        )

    async def _run_register_phase(
        self,
        *,
        product: str,
        version: str,
        impl_id: str,
        specs: Sequence[SpecSource],
        base_url: str | None,
        tenant_id: UUID | None,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> IngestionResult:
        """Parse + register every spec under the same connector triple.

        Each spec is parsed and registered separately so the per-spec
        :func:`register_ingested_operations` call attaches its own
        ``spec:<uri>`` tag to the row. The aggregated counts roll up
        every spec's individual result. ``connector_registered`` is
        ``True`` when ANY spec triggered the auto-shim registration —
        on a fresh connector the first spec flips it, subsequent
        specs see it already there.
        """
        aggregated_inserted = 0
        aggregated_updated = 0
        aggregated_skipped = 0
        connector_registered = False

        for spec in specs:
            protos = parse_openapi(spec.uri, spec_source=spec.uri)
            partial = await register_ingested_operations(
                product=product,
                version=version,
                impl_id=impl_id,
                spec_source=spec.uri,
                operations=protos,
                base_url=base_url,
                tenant_id=tenant_id,
                embedding_service=self._embedding_service,
            )
            aggregated_inserted += partial.inserted_count
            aggregated_updated += partial.updated_count
            aggregated_skipped += partial.skipped_count
            connector_registered = connector_registered or partial.connector_registered

        return IngestionResult(
            inserted_count=aggregated_inserted,
            updated_count=aggregated_updated,
            skipped_count=aggregated_skipped,
            connector_registered=connector_registered,
            operations_grouped=False,
        )

    async def _run_grouping_phase(
        self,
        *,
        product: str,
        version: str,
        impl_id: str,
        tenant_id: UUID | None,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> GroupingResult:
        """Resolve the LLM client and run :func:`run_llm_grouping`."""
        llm_client = self._llm_client_factory()
        return await run_llm_grouping(
            llm_client=llm_client,
            operator_sub=self._operator.sub,
            operator_tenant_id=self._operator.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            tenant_id=tenant_id,
            batch_size=DEFAULT_GROUPING_BATCH_SIZE,
            min_groups=DEFAULT_MIN_GROUPS,
            max_groups=DEFAULT_MAX_GROUPS,
            sessionmaker=sessionmaker,
        )
