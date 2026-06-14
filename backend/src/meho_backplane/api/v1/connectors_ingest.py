# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/connectors*`` -- REST surface for spec-ingestion + review.

G0.7-T6 (#406) of Initiative #389. The routes mounted under
``/api/v1/connectors*`` drive the spec-ingestion pipeline (T1
parser + T2 register_ingested + T3 LLM grouping) and the
review-queue state machine (T4 :class:`ReviewService`).

Route inventory
---------------

* ``POST /api/v1/connectors/ingest`` — run the full pipeline. Body:
  :class:`IngestRequest`. Returns :class:`IngestResponse`. Role:
  ``tenant_admin``.
* ``GET /api/v1/connectors[?status=...]`` — list ingested connectors
  visible to the operator's tenant + built-ins. Returns
  :class:`ConnectorListResponse`. Role: ``operator``.
* ``GET /api/v1/connectors/{connector_id}/review`` — return the full
  review payload (groups + per-group ops + flags). Returns
  :class:`ConnectorReviewPayload`. Role: ``operator``.
* ``PATCH /api/v1/connectors/{connector_id}/groups/{group_key}`` —
  edit a group's ``when_to_use`` / ``name``. Body:
  :class:`EditGroupBody`. Returns 204. Role: ``tenant_admin``.
* ``PATCH /api/v1/connectors/{connector_id}/operations/{op_id}`` —
  edit a per-op override (``safety_level``, ``requires_approval``,
  ``custom_description``, ``is_enabled``). Body: :class:`EditOpBody`.
  Returns 200 with :class:`EditOpResponse` (enable-time advisories
  in ``warnings``, G0.23-T4 #1630). Role: ``tenant_admin``.
* ``POST /api/v1/connectors/{connector_id}/enable`` — transition all
  groups to ``enabled``; cascade. Returns 204. Idempotent. Role:
  ``tenant_admin``.
* ``POST /api/v1/connectors/{connector_id}/enable-reads`` — bulk-enable
  every read-class (GET/HEAD) ingested op; writes stay default-deny.
  Returns 200 with :class:`EnableReadsResponse` (``ops_enabled``
  count). Idempotent (G0.25-T7 #1749). Role: ``tenant_admin``.
* ``POST /api/v1/connectors/{connector_id}/disable`` — transition all
  groups to ``disabled``; cascade. Returns 204. Idempotent. Role:
  ``tenant_admin``.
* ``DELETE /api/v1/connectors/{connector_id}`` — delete the connector:
  remove its rows under the operator's tenant scope and, when no rows
  remain for the triple anywhere, deregister the auto-registered
  ingest shim (G0.25-T2 #1700). Returns 204. Role: ``tenant_admin``.

Tenant scoping
--------------

Every route derives ``tenant_id`` from the JWT-validated
:class:`Operator`. There is no surface that accepts a tenant id from
the body or query string — cross-tenant probes are impossible by
construction. The one nuance: the ``operator`` role sees only their
own tenant's connectors **and** built-ins (``tenant_id IS NULL``);
the ``tenant_admin`` role additionally has write access to built-in
ingests / edits / state transitions. The service layer
(:class:`ReviewService`, :class:`IngestionPipelineService`) carries
matching tenant guards as defence-in-depth for sibling consumers
(CLI verbs T5, admin MCP tools T7) that hit the service layer
directly.

Cross-tenant probes for a connector that belongs to another tenant
surface as 404 :class:`ConnectorNotFoundError`, not 403. Same
conflation :class:`ReviewService` uses: an operator must not be
able to enumerate other tenants by inspecting status-code
differential.

Error mapping
-------------

The service-layer exceptions map to HTTP status codes uniformly:

* :class:`ConnectorNotFoundError` → 404.
* :class:`InvalidStateTransitionError` → 409 Conflict.
* :class:`UncoveredVersionLabel` (G0.9-T9 #741 ingest pre-flight:
  the ``version`` label is outside every registered class's
  ``supported_version_range``) → 422 Unprocessable Entity. Caught
  before the generic ValueError-family below so the more-specific
  status code wins. The ``detail`` body is the structured envelope
  from :func:`build_uncovered_version_label_detail` (``product`` /
  ``version`` / ``impl_id`` / ``registered_classes[]`` of each
  class's ``supported_version_range`` + the rendered ``message``) —
  the same dict the MCP ingest tool ships on the JSON-RPC
  ``error.data`` member, so the two transports can't drift (#1624
  wired the REST half of the parity to the shared #777 builder; the
  MCP half has shipped it since #777/#1534).
* :class:`InvalidSpecError` / :class:`UnsupportedSpecError` /
  :class:`InvalidSchemaError` / :class:`OpIdCollision` /
  :class:`LlmOutputInvalid` → 400 Bad Request. The ``detail`` body
  is the structured envelope from the shared per-class builders in
  :mod:`~meho_backplane.operations.ingest.error_envelopes` — a
  stable snake_case ``detail`` classifier + the rendered ``message``
  + the class's machine-resolvable fields — the same dict the MCP
  ingest tool ships on the JSON-RPC ``error.data`` member (#1610
  closed the REST half of the parity; #1534 closed the MCP half).
* :class:`VersionMismatchError` → 422 Unprocessable Entity. The
  request is syntactically valid but the spec's ``info.version``
  disagrees with the operator-supplied ``version`` label, or two
  specs in the same bundle disagree on the major version. The
  structured detail names both versions so the operator's error
  message tells them exactly what to fix.
* :class:`PermissionError` (raised by
  :meth:`IngestionPipelineService._authorize` when a service-layer
  caller bypasses the route gate) → 403.
* :class:`LlmClientUnavailable` → 503 Service Unavailable.
* :class:`ValueError` (the catch-all the edit verbs raise on
  empty-body / out-of-enum input) → 400.

Audit
-----

Every route writes the AuditMiddleware row tagged with the route
path; the service layer writes its own per-action row under
``meho.connector.*`` op_ids (see
:mod:`~meho_backplane.operations.ingest._internals`). The two-row
shape lets G8 dashboards split "operator called the route" from
"the state actually changed" — useful when an idempotent re-run
writes an HTTP row but no service-level row.

LLM-client injection
--------------------

The ingest route resolves the :class:`LlmClientFactory` for the
:class:`IngestionPipelineService` via the :func:`get_llm_client_factory`
dependency, which returns the active module-level factory. FastAPI
lifespan startup installs the production factory
(:func:`meho_backplane.operations.ingest.build_anthropic_ingest_llm_client`)
via :func:`set_llm_client_factory`, so a deploy with
``ANTHROPIC_API_KEY`` set runs ``--catalog`` ingest grouping for real
(the grouping pass reuses the agent runtime's Anthropic key). Tests
inject a deterministic stub via the same hook (or via
:class:`IngestionPipelineService`'s ``llm_client_factory`` constructor
argument). When no key is configured the production factory still
raises :class:`LlmClientUnavailable`, which the route maps to 503 — so
a misconfigured deploy fails closed rather than 401-ing mid-grouping.
See #1386 for the lifespan wire-up and G0.18-T7 (#1360) for the prior
build-time-only framing this replaces.
"""

from __future__ import annotations

import asyncio
from json import JSONDecodeError
from typing import Annotated, NoReturn
from uuid import UUID

import httpx
import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import JSONResponse, Response

from meho_backplane.api.v1._envelope import (
    ENVELOPE_QUERY,
    EnvelopeVersion,
    wrap_v2_envelope,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.operations._lookup import connector_exists, parse_connector_id
from meho_backplane.operations.ingest import (
    CatalogListResponse,
    ConnectorNotFoundError,
    ConnectorReviewPayload,
    ConnectorSpecEntry,
    EditGroupBody,
    EditOpBody,
    EditOpResponse,
    EnableReadsResponse,
    IngestionPipelineResult,
    IngestionPipelineService,
    IngestJob,
    IngestJobHandle,
    IngestJobNotFoundError,
    IngestJobStatusResponse,
    IngestRequest,
    IngestResponse,
    InvalidSchemaError,
    InvalidSpecError,
    InvalidStateTransitionError,
    LlmClientFactory,
    LlmClientUnavailable,
    LlmOutputInvalid,
    OpIdCollision,
    ReviewService,
    SpecSource,
    UncoveredVersionLabel,
    UnsupportedSpecError,
    UpstreamNotSpecError,
    VersionMismatchError,
    build_catalog_entry_malformed_detail,
    build_catalog_entry_not_found_detail,
    build_catalog_entry_typed_connector_detail,
    build_catalog_entry_upstream_not_spec_detail,
    build_invalid_schema_detail,
    build_invalid_spec_detail,
    build_llm_output_invalid_detail,
    build_op_id_collision_detail,
    build_uncovered_version_label_detail,
    build_unsupported_spec_detail,
    build_upstream_not_spec_detail,
    build_version_mismatch_detail,
    default_llm_client_factory,
    get_job_registry,
    list_ingested_connectors,
    load_catalog,
    run_ingest_job,
)
from meho_backplane.operations.ingest.api_schemas import (
    ConnectorStatusFilter,
    GroupingResultModel,
    IngestionResultModel,
)

__all__ = [
    "get_llm_client_factory",
    "router",
    "set_llm_client_factory",
]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

#: Module-level Depends closures — required to satisfy ruff B008
#: (calls in default argument positions are disallowed). Same shape as
#: :mod:`meho_backplane.api.v1.retrieve` and
#: :mod:`meho_backplane.api.v1.operations`.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


#: Mutable module-level holder for the LLM-client factory used by the
#: ingest pipeline. Default fails closed; tests + production app-
#: bootstrap mutate via :func:`set_llm_client_factory`. A module-level
#: holder beats a FastAPI dependency override here because the
#: factory must be reachable from any test that boots the app via
#: :class:`TestClient` without the test having to know the override
#: surface.
_llm_client_factory: LlmClientFactory = default_llm_client_factory


def set_llm_client_factory(factory: LlmClientFactory) -> LlmClientFactory:
    """Install a new LLM-client factory; return the previous one.

    The previous factory is returned so callers can restore it after
    a test or a feature-flagged deploy. FastAPI lifespan startup is the
    production caller (#1386): it installs
    :func:`meho_backplane.operations.ingest.build_anthropic_ingest_llm_client`,
    which reuses ``settings.anthropic_api_key`` (the same key the agent
    runtime reads) so ``--catalog`` ingest grouping works on deployed
    backplanes. Tests call this from their fixture setup with a
    deterministic stub; a deploy that never set a key keeps the
    fail-closed posture — the production factory raises
    :class:`LlmClientUnavailable` → HTTP 503 when invoked without a key,
    same observable behaviour as the chassis default
    (:func:`default_llm_client_factory`).

    The mutation is intentional rather than a FastAPI ``dependency_
    overrides`` entry: this factory needs to be reachable both from
    the route handler (HTTP path) and from the CLI / MCP siblings
    (which construct :class:`IngestionPipelineService` directly), so
    a module-level holder keeps the two consumers in sync.
    """
    global _llm_client_factory
    previous = _llm_client_factory
    _llm_client_factory = factory
    return previous


def get_llm_client_factory() -> LlmClientFactory:
    """Return the active LLM-client factory.

    Public read-side counterpart to :func:`set_llm_client_factory`.
    The HTTP route consumes it as a FastAPI dependency
    (``Depends(get_llm_client_factory)``) so tests can also override via
    :attr:`FastAPI.dependency_overrides` when convenient; the non-HTTP
    ingest siblings (the ``meho.connector.ingest`` admin MCP tool) call
    it directly so they share the same lifespan-wired factory the route
    sees, rather than pinning the fail-closed default.
    """
    return _llm_client_factory


@router.post(
    "/ingest",
    # Both response shapes declared explicitly so FastAPI's autogen
    # OpenAPI surfaces them and the regen'd Go CLI sees typed
    # ``api.IngestResponse`` (200, sync legacy) and
    # ``api.IngestJobHandle`` (202, async default). Without this map
    # the route returns a bare ``JSONResponse`` and the spec emits a
    # generic ``application/json`` body, which causes oapi-codegen to
    # drop ``IngestResponse`` entirely -- the failure mode that
    # surfaced as ``undefined: api.IngestResponse`` golangci-lint
    # errors in ``cli/internal/cmd/connector/{ingest.go,connector_test.go}``
    # after the async/job-handle shape landed. Mirrors the
    # ``DELETE /api/v1/targets/{name}`` 409 declaration in
    # ``api/v1/targets.py`` -- explicit ``responses`` map even when
    # the handler returns ``JSONResponse`` directly.
    responses={
        200: {
            "model": IngestResponse,
            "description": (
                "Sync legacy path (``async=false`` or ``dry_run=true``)"
                " -- pipeline ran inline; body is the full"
                " :class:`IngestResponse` (``ingestion`` + optional"
                " ``grouping``)."
            ),
        },
        202: {
            "model": IngestJobHandle,
            "description": (
                "Async default (``async=true``) -- pipeline running"
                " off the request thread; body is an"
                " :class:`IngestJobHandle` with the ``job_id`` to"
                " poll via ``GET /api/v1/connectors/ingest/jobs/{job_id}``."
            ),
        },
    },
)
async def ingest_endpoint(
    body: IngestRequest,
    operator: Operator = _require_admin,
    llm_client_factory: Annotated[
        LlmClientFactory,
        Depends(get_llm_client_factory),
    ] = default_llm_client_factory,
) -> JSONResponse:
    """Run the full ingestion pipeline (T1 → T2 → T3) for one connector.

    Async (default; ``async=true``) fires the pipeline off the request
    thread via :func:`asyncio.create_task` and returns
    :class:`IngestJobHandle` at HTTP 202; the operator polls
    ``GET /api/v1/connectors/ingest/jobs/{job_id}`` for completion.
    This is the only mode that survives real-world vendor specs
    without tripping the kubelet liveness probe -- the
    ``vmware/9.0.0.0`` spec at 7.5 MB / 1275 ops blocks the event
    loop for ~30 s in the register + LLM-grouping phases, past
    the 25 s liveness deadline. The 202 + job-id shape is the
    "escape hatch must not crash the pod" answer G0.16-T1 closes.

    Sync (legacy; ``async=false``) runs the pipeline inline and
    returns :class:`IngestResponse` at HTTP 200. Kept for small-spec
    callers (CI tests with ≤ 100-op fixtures, ad-hoc shell scripts,
    the v0.8.x clients that pre-date the async shape). Will not
    survive real-world specs and is documented as such in
    ``docs/codebase/spec-ingestion.md`` and
    ``docs/codebase/api-shape-conventions.md`` §1.

    ``dry_run=true`` parses every spec but writes nothing and is
    always synchronous (the parse-only path is the fast leg per
    RDC #771 Finding 21 -- 30 s for the same spec, but with no DB
    or LLM hops and steady event-loop yields, well clear of the
    probe deadline in practice). ``async`` is ignored on the dry-run
    path; the response carries the parser's ``inserted_count``
    projection and ``grouping=None``.

    The body accepts two mutually-exclusive request shapes (see
    :class:`IngestRequest`):

    * **Catalog-driven shape** (G0.14-T9 / #1150) — ``catalog_entry``
      is a ``"<product>/<version>"`` reference; this handler resolves
      it against the packaged catalog (:func:`load_catalog`) and
      dispatches through the same ingest path as if the caller had
      supplied the resolved quadruple.
    * **Explicit-quadruple shape** — ``product`` + ``version`` +
      ``impl_id`` + ``specs[]`` carry the resolved triple plus the
      spec sources. The MCP admin tool and historical clients use
      this shape.

    Tenant scoping (#1699): this route exposes **no** ``tenant_id``
    parameter — the write scope is always the calling operator's
    ``tenant_id`` from the JWT (the CLI verb drives this route and
    inherits the same scope). The MCP sibling
    ``meho.connector.ingest`` accepts an optional ``tenant_id`` and
    targets the built-in / global scope (``tenant_id=NULL``) when it
    is omitted (tenant_admin only). The dedup lookup
    (``operations/ingest/_upsert``) is scope-aware, so re-ingesting
    the same spec under the other scope re-inserts every op as a
    shadow copy there — verify the scope matches your intent.
    """
    # Remember the original ``catalog_entry`` (pre-resolution) so the
    # ``UpstreamNotSpecError`` path -- raised deep inside the parser
    # when an HTML developer-portal page comes back instead of an
    # OpenAPI spec -- can include the catalog reference in its 422
    # envelope. ``_resolve_catalog_entry_if_set`` returns an explicit-
    # quadruple body with ``catalog_entry=None``, so we have to snapshot
    # before resolution.
    catalog_entry = body.catalog_entry
    resolved, catalog_compatible = _resolve_catalog_entry_if_set(body)
    # The catalog-driven shape resolves its opt-in band from the catalog
    # row (``catalog_compatible``); the explicit-quadruple shape carries
    # the operator-supplied band on the body itself (T1 #1646). The two
    # shapes are mutually exclusive (the ``IngestRequest`` validator
    # rejects a body that sets both), so exactly one of these is ever
    # non-None — fold them into one value the pipeline cross-check honours.
    spec_info_versions_compatible = catalog_compatible or (
        tuple(resolved.spec_info_versions_compatible)
        if resolved.spec_info_versions_compatible is not None
        else None
    )
    service = IngestionPipelineService(
        operator=operator,
        llm_client_factory=llm_client_factory,
    )
    if body.dry_run or not body.async_:
        return await _run_sync_ingest(
            service=service,
            resolved=resolved,
            operator=operator,
            catalog_entry=catalog_entry,
            spec_info_versions_compatible=spec_info_versions_compatible,
        )
    return await _spawn_async_ingest(
        service=service,
        resolved=resolved,
        operator=operator,
        spec_info_versions_compatible=spec_info_versions_compatible,
    )


async def _run_sync_ingest(
    *,
    service: IngestionPipelineService,
    resolved: IngestRequest,
    operator: Operator,
    catalog_entry: str | None,
    spec_info_versions_compatible: tuple[str, ...] | None,
) -> JSONResponse:
    """Run the pipeline inline and return the legacy 200 + IngestResponse.

    Used for ``dry_run=true`` (parse-only fast path) and for explicit
    ``async=false`` requests (small-spec callers that want the
    blocking shape). The dry-run + sync paths share the inline
    response: the route maps domain errors onto HTTPException at
    the request boundary so the v0.8.x error contract carries
    forward to clients that haven't migrated to the async polling
    shape. ``dry_run`` ignores ``async`` because the parse-only leg
    is the fast path that never trips the liveness deadline;
    honouring async there would force a polling round-trip on
    operators who just want a quick spec validation.
    """
    result = await _run_ingest_with_http_mapping(
        service=service,
        body=resolved,
        operator=operator,
        catalog_entry=catalog_entry,
        spec_info_versions_compatible=spec_info_versions_compatible,
    )
    ingestion_model, grouping_model = result.to_api_models()
    response_body = IngestResponse(ingestion=ingestion_model, grouping=grouping_model)
    return JSONResponse(
        content=response_body.model_dump(mode="json"),
        status_code=http_status.HTTP_200_OK,
    )


async def _spawn_async_ingest(
    *,
    service: IngestionPipelineService,
    resolved: IngestRequest,
    operator: Operator,
    spec_info_versions_compatible: tuple[str, ...] | None,
) -> JSONResponse:
    """Create a job row, kick the pipeline off the request thread, return 202.

    Extracted from :func:`ingest_endpoint` so the body of the route
    stays focused on the dry-run vs sync vs async dispatch. The
    helper is also load-bearing for tests that want to drive the
    async path without re-implementing the 202 envelope.

    The closure passed to :func:`run_ingest_job` re-asserts the
    post-resolution invariants the synchronous path's
    ``_run_ingest_with_http_mapping`` asserts -- the resolved body
    is already in explicit-quadruple shape, but the asserts inside
    the closure guard against a future refactor that bypasses
    :func:`_resolve_catalog_entry_if_set`.
    """
    assert resolved.product is not None, "post-resolution invariant: product must be set"
    assert resolved.version is not None, "post-resolution invariant: version must be set"
    assert resolved.impl_id is not None, "post-resolution invariant: impl_id must be set"
    registry = get_job_registry()
    job = await registry.create(
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        catalog_entry=None,  # resolved body always has catalog_entry=None
        product=resolved.product,
        version=resolved.version,
        impl_id=resolved.impl_id,
        spec_uris=[spec.uri for spec in resolved.specs],
    )

    async def _pipeline_call() -> IngestionPipelineResult:
        # Re-bind locals so the closure doesn't depend on the
        # enclosing scope's mutable references after Phase 10 of
        # /auto-implement-issue cleans up.
        return await service.ingest(
            product=resolved.product,  # type: ignore[arg-type]
            version=resolved.version,  # type: ignore[arg-type]
            impl_id=resolved.impl_id,  # type: ignore[arg-type]
            specs=resolved.specs,
            base_url=resolved.base_url,
            tenant_id=operator.tenant_id,
            dry_run=False,
            spec_info_versions_compatible=spec_info_versions_compatible,
        )

    async def _dispatchability_check(result: IngestionPipelineResult) -> bool:
        # Post-run honesty gate (claude-rdc-hetzner-dc#1136): resolve the
        # connector exactly the way the dispatch/query meta-tools do —
        # parse the connector_id into its natural-key triple and probe
        # connector_exists under the operator's tenant scope. A run that
        # returned without raising but leaves this False persisted
        # nothing the dispatcher can route, so the job ends ``degraded``
        # rather than lying with ``succeeded``.
        probe_product, probe_version, probe_impl_id = parse_connector_id(result.connector_id)
        return await connector_exists(
            tenant_id=operator.tenant_id,
            product=probe_product,
            version=probe_version,
            impl_id=probe_impl_id,
        )

    # ``asyncio.create_task`` (not ``BackgroundTasks``) so the work
    # survives the response close -- the FastAPI ``BackgroundTasks``
    # path runs *after* response send but still inside the request
    # lifecycle, which would mean operators with slow clients pay
    # for the long-running pass on the upstream HTTP connection.
    # The bare task is intentional and parallels every other
    # background-worker spawn in the backplane (memory expiry,
    # agent reaper, scheduler loop -- see
    # ``meho_backplane.main._BackgroundTasks``).
    task = asyncio.create_task(
        run_ingest_job(
            job.job_id,
            pipeline_call=_pipeline_call,
            dispatchability_check=_dispatchability_check,
        ),
        name=f"ingest-job-{job.job_id}",
    )
    # Stash a strong reference inside the registry so a future
    # cancel-this-job verb can find the task. Eviction frees the
    # reference along with the IngestJob row.
    _track_background_task(job.job_id, task)

    handle = IngestJobHandle(
        job_id=job.job_id,
        status=job.status,
        poll_url=f"/api/v1/connectors/ingest/jobs/{job.job_id}",
    )
    return JSONResponse(
        content=handle.model_dump(mode="json"),
        status_code=http_status.HTTP_202_ACCEPTED,
    )


#: Strong references to in-flight background tasks. Python's
#: :func:`asyncio.create_task` does not retain its tasks; without an
#: external reference the garbage collector can drop the task
#: mid-execution. Indexed by job id so callers can locate the task
#: for cancellation. Cleared by :func:`_track_background_task`'s
#: completion callback so a steady stream of ingests doesn't grow
#: the map unbounded.
_background_tasks: dict[UUID, asyncio.Task[None]] = {}


def _track_background_task(job_id: UUID, task: asyncio.Task[None]) -> None:
    """Hold a strong reference to *task* until it completes.

    See :data:`_background_tasks` for the rationale -- bare
    :func:`asyncio.create_task` tasks can be GC'd mid-execution if no
    code retains a reference. The completion callback runs on the
    same loop the task ran on and removes the reference; missing
    keys (eviction race, double-completion) are tolerated.
    """
    _background_tasks[job_id] = task
    task.add_done_callback(lambda _t: _background_tasks.pop(job_id, None))


@router.get(
    "/ingest/jobs/{job_id}",
    response_model=IngestJobStatusResponse,
)
async def get_ingest_job_endpoint(
    job_id: UUID,
    operator: Operator = _require_admin,
) -> IngestJobStatusResponse:
    """Poll the durable status of an async ingest job.

    Companion to the async path of :func:`ingest_endpoint`. Reads
    the in-memory :class:`~meho_backplane.operations.ingest.IngestJob`
    row by id, projects it into :class:`IngestJobStatusResponse`,
    and returns it.

    Tenant-isolation gate: a non-admin operator probing a built-in
    (``tenant_id is None``) job, or an operator probing another
    tenant's job, sees the same 404 a missing id returns. The
    ``tenant_admin`` role lifts the built-in gate (so admins can
    inspect global ingests they kicked off).

    Process-local storage: jobs evaporate on pod restart, and a
    polling client that hits a freshly-restarted pod will see 404
    for a job_id the prior pod was running. The operator-facing
    workflow accepts that trade -- a job whose pod died had its
    pipeline interrupted and would not have completed regardless.
    Durable cross-restart jobs are tracked under v0.9.
    """
    registry = get_job_registry()
    try:
        job = await registry.get(
            job_id,
            tenant_id=operator.tenant_id,
            is_tenant_admin=operator.tenant_role is TenantRole.TENANT_ADMIN,
        )
    except IngestJobNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="ingest_job_not_found",
        ) from exc
    return _job_to_response(job)


def _job_to_response(job: IngestJob) -> IngestJobStatusResponse:
    """Project an :class:`IngestJob` into its Pydantic response shape.

    Extracted so the route handler stays narrow and the projection
    can be unit-tested without spinning up an HTTP client. The
    branches mirror the lifecycle of the dataclass: a terminal
    success populates ``ingestion`` (+ optional ``grouping``);
    a ``degraded`` terminal populates both the ``ingestion`` counts
    *and* ``error`` / ``error_class`` (the pipeline returned but its
    output was non-dispatchable); a terminal failure populates
    ``error`` + ``error_class`` only (the pipeline raised, no result);
    ``running`` leaves the result cluster ``None`` so clients branch
    on ``status`` instead of presence-checking.
    """
    ingestion_model: IngestionResultModel | None = None
    grouping_model: GroupingResultModel | None = None
    # ``degraded`` carries a populated ``result`` too (the counts that
    # landed before the dispatchability postcondition failed), so it
    # projects the ingestion/grouping cluster alongside its error.
    if job.status in ("succeeded", "degraded") and job.result is not None:
        ingestion_model, grouping_model = job.result.to_api_models()
    return IngestJobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        catalog_entry=job.catalog_entry,
        product=job.product,
        version=job.version,
        impl_id=job.impl_id,
        spec_uris=list(job.spec_uris),
        started_at=job.started_at,
        ended_at=job.ended_at,
        ingestion=ingestion_model,
        grouping=grouping_model,
        error=job.error,
        error_class=job.error_class,
    )


def _resolve_catalog_entry_if_set(
    body: IngestRequest,
) -> tuple[IngestRequest, tuple[str, ...] | None]:
    """Resolve ``body.catalog_entry`` against the packaged catalog.

    Returns a tuple of (a) a new :class:`IngestRequest` in the
    explicit-quadruple shape so the rest of the ingest pipeline
    doesn't need to branch on which shape the caller used and (b) the
    catalog row's
    :attr:`~meho_backplane.operations.ingest.catalog.ConnectorSpecEntry.spec_info_versions_compatible`
    list, or ``None`` when the row has no opt-in / the caller used
    the explicit-quadruple shape. The catalog-driven shape is
    G0.14-T9 (#1150): REST-native clients ship
    ``{"catalog_entry": "vmware/9.0"}`` and the server resolves the
    triple + spec sources from the package-data catalog.

    The compatibility list flows back as a separate return rather
    than being inlined onto the body so the explicit-quadruple shape
    (which the validator on :class:`IngestRequest` rejects mixed
    bodies for) stays a pure ``(product, version, impl_id, specs)``
    object. The route hands it directly to the pipeline service.

    Raises :class:`HTTPException` (422) with structured detail bodies
    per the T11 error-shape convention
    (:doc:`docs/codebase/error-message-shape.md`) for the four
    catalog-side validation outcomes:

    * Malformed reference (no slash, blank halves) →
      ``catalog_entry_malformed``.
    * Reference well-formed but not in catalog →
      ``catalog_entry_not_found`` carrying ``available_entries[]``.
    * Reference resolves to a typed connector (``upstream is None``)
      with no ingestable spec → ``catalog_entry_typed_connector``.
    * Reference resolves to a fqdn-templated upstream
      (placeholder ``<...>`` in the URL — appliance-served NSX) →
      ``catalog_entry_templated_upstream``. The operator must supply
      the concrete spec via the explicit-quadruple shape.

    A body with ``catalog_entry=None`` is returned verbatim with a
    ``None`` compatibility list — the explicit-quadruple shape
    doesn't carry the catalog row.
    """
    if body.catalog_entry is None:
        return body, None
    catalog_entry = body.catalog_entry
    product, version = _parse_catalog_entry(catalog_entry)
    catalog = load_catalog()
    entry = catalog.get(product, version)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=build_catalog_entry_not_found_detail(
                catalog_entry=catalog_entry,
                available_entries=[f"{e.product}/{e.version}" for e in catalog.entries],
            ),
        )
    _reject_unusable_entry(catalog_entry=catalog_entry, entry=entry)
    # The catalog entry is good — round-trip through the explicit
    # shape so the downstream pipeline can stay unchanged. The
    # validator on IngestRequest still passes because catalog_entry
    # is unset in the round-tripped object.
    return (
        body.model_copy(
            update={
                "catalog_entry": None,
                "product": entry.product,
                "version": entry.version,
                "impl_id": entry.impl_id,
                "specs": [SpecSource(uri=uri) for uri in (entry.upstream or ())],
            },
        ),
        entry.spec_info_versions_compatible,
    )


def _parse_catalog_entry(catalog_entry: str) -> tuple[str, str]:
    """Split a ``"<product>/<version>"`` reference; raise 422 on a
    malformed shape.

    Mirrors the CLI's ``parseCatalogRef`` validation contract but
    runs server-side so REST-native clients get the same diagnostic
    without round-tripping through the CLI. Per T11 convention.
    """
    if "/" not in catalog_entry:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=build_catalog_entry_malformed_detail(catalog_entry=catalog_entry),
        )
    product_raw, _, version_raw = catalog_entry.partition("/")
    product = product_raw.strip()
    version = version_raw.strip()
    if not product or not version or "/" in version:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=build_catalog_entry_malformed_detail(catalog_entry=catalog_entry),
        )
    return product, version


def _reject_unusable_entry(
    *,
    catalog_entry: str,
    entry: ConnectorSpecEntry,
) -> None:
    """Reject typed-connector and fqdn-templated entries with
    structured 422s per T11 convention.

    A typed connector (``upstream is None``) has no ingestable spec;
    a fqdn-templated upstream (placeholder ``<...>`` characters) is
    appliance-served (NSX manager URL, vCenter FQDN) and the catalog
    can't dereference it server-side. Both surfaces refuse the catalog-
    driven shape and point the operator at the explicit-quadruple
    fallback documented in ``docs/cross-repo/connector-catalog.md``.
    """
    if entry.upstream is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=build_catalog_entry_typed_connector_detail(
                catalog_entry=catalog_entry,
                product=entry.product,
                version=entry.version,
                impl_id=entry.impl_id,
            ),
        )
    templated = [url for url in entry.upstream if "<" in url or ">" in url]
    if templated:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "detail": "catalog_entry_templated_upstream",
                "catalog_entry": catalog_entry,
                "product": entry.product,
                "version": entry.version,
                "impl_id": entry.impl_id,
                "templated_upstream": templated,
                "message": (
                    f"catalog_entry_templated_upstream: {catalog_entry!r} "
                    f"upstream {templated!r} is fqdn-templated and cannot "
                    "be resolved server-side. Supply the concrete spec via "
                    "the explicit-quadruple shape "
                    "(product/version/impl_id/specs). "
                    "See docs/cross-repo/connector-catalog.md. "
                    "See docs/codebase/error-message-shape.md."
                ),
            },
        )


def _upstream_not_spec_http_exception(
    exc: UpstreamNotSpecError,
    *,
    catalog_entry: str | None,
) -> HTTPException:
    """Map :exc:`UpstreamNotSpecError` onto the 422 envelope shape.

    G0.15-T2 (#1211). The HTTP fetch succeeded (2xx) but the upstream
    URL returned non-spec content -- typically the Broadcom Developer
    Portal landing page for ``vmware/9.0`` and ``sddc-manager/9.0``,
    which serves HTML rather than OpenAPI YAML/JSON. Before this
    branch, the bytes fell through to the YAML decoder and surfaced
    as an opaque ``could not decode spec: ... line 33`` 400. The
    structured 422 carries the catalog_entry (when the request
    started as catalog-driven), the upstream URL, and the
    Content-Type so an agent / operator can branch on the diagnostic
    and switch to the explicit-quadruple shape with a local spec
    file. Extracted from the mapping helper below so its body stays
    inside the code-quality function-size budget.
    """
    if catalog_entry is not None:
        detail: dict[str, object] = build_catalog_entry_upstream_not_spec_detail(
            catalog_entry=catalog_entry,
            upstream_url=exc.upstream_url,
            content_type=exc.content_type,
        )
    else:
        detail = build_upstream_not_spec_detail(
            upstream_url=exc.upstream_url,
            content_type=exc.content_type,
        )
    return HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=detail,
    )


def _spec_error_http_exception(
    exc: InvalidSpecError
    | UnsupportedSpecError
    | InvalidSchemaError
    | OpIdCollision
    | LlmOutputInvalid,
) -> HTTPException:
    """Map a parser-family ``SpecError`` onto the structured 400 envelope.

    #1610 — REST half of the MCP parity #1534 established. Every
    sibling is a caller-input mistake (wrong OpenAPI flavour, a
    structurally invalid document, a broken ``$ref``, colliding
    op-ids, a bad grouping-LLM response), so the 400 ``detail`` body
    carries the shared structured envelope from
    :mod:`~meho_backplane.operations.ingest.error_envelopes` instead
    of the bare ``str(exc)`` the route shipped before — a stable
    snake_case ``detail`` classifier the caller branches on without
    re-parsing prose, the rendered ``message``, and the per-class
    machine-resolvable fields (``op_ids`` + spec sources for
    :class:`OpIdCollision`, ``pass_name`` for
    :class:`LlmOutputInvalid`). The builders are the single source of
    truth shared with the MCP dispatch table
    (``raise_invalid_params_for_spec_error`` in
    ``mcp/tools/_connector_shared.py``), so the REST 400 ``detail``
    and the MCP ``-32602`` ``error.data`` member can't drift; the
    per-surface dispatch stays local because each surface funnels a
    different exception subset (REST intercepts the 422-mapped
    siblings earlier in the ``except`` chain).
    """
    if isinstance(exc, InvalidSchemaError):
        # InvalidSchemaError before InvalidSpecError — a broken $ref is the
        # narrower domain than a structurally invalid root document (same
        # ordering as the MCP dispatch table).
        detail = build_invalid_schema_detail(exc)
    elif isinstance(exc, InvalidSpecError):
        detail = build_invalid_spec_detail(exc)
    elif isinstance(exc, UnsupportedSpecError):
        detail = build_unsupported_spec_detail(exc)
    elif isinstance(exc, OpIdCollision):
        detail = build_op_id_collision_detail(exc)
    else:
        detail = build_llm_output_invalid_detail(exc)
    return HTTPException(http_status.HTTP_400_BAD_REQUEST, detail)


async def _run_ingest_with_http_mapping(
    *,
    service: IngestionPipelineService,
    body: IngestRequest,
    operator: Operator,
    catalog_entry: str | None = None,
    spec_info_versions_compatible: tuple[str, ...] | None = None,
) -> IngestionPipelineResult:
    """Drive :meth:`IngestionPipelineService.ingest` and map domain errors to HTTP.

    Pre-condition: ``body`` is in the explicit-quadruple shape
    (``product`` / ``version`` / ``impl_id`` / ``specs`` populated).
    The catalog-driven shape is resolved upstream by
    :func:`_resolve_catalog_entry_if_set`; the asserts below pin the
    invariant so a future refactor that bypasses that helper trips
    at the boundary rather than landing a half-populated ingest.

    ``catalog_entry`` carries the operator's original
    ``"<product>/<version>"`` reference when the request started as
    the catalog-driven shape; ``None`` for explicit-quadruple
    requests. Used only on the :exc:`UpstreamNotSpecError` path so
    the 422 envelope can include the catalog reference
    (G0.15-T2 / #1211). The full exception-to-status table is the
    load-bearing contract documented at the top of the module.

    ``spec_info_versions_compatible`` is the catalog row's opt-in
    label-vs-spec compatibility range (G0.16-T5 #1307); ``None`` when
    the row has no opt-in or the caller used the explicit-quadruple
    shape. Forwarded verbatim to
    :meth:`IngestionPipelineService.ingest` so the cross-check inside
    ``_validate_spec_versions`` can widen the verbatim/major-band
    comparison.
    """
    assert body.product is not None, "post-resolution invariant: product must be set"
    assert body.version is not None, "post-resolution invariant: version must be set"
    assert body.impl_id is not None, "post-resolution invariant: impl_id must be set"
    try:
        return await service.ingest(
            product=body.product,
            version=body.version,
            impl_id=body.impl_id,
            specs=body.specs,
            base_url=body.base_url,
            tenant_id=operator.tenant_id,
            dry_run=body.dry_run,
            spec_info_versions_compatible=spec_info_versions_compatible,
        )
    except Exception as exc:
        _raise_ingest_http_error(exc, catalog_entry=catalog_entry)


def _raise_ingest_http_error(
    exc: Exception,
    *,
    catalog_entry: str | None,
) -> NoReturn:
    """Map an ``IngestionPipelineService.ingest`` failure onto an HTTP error.

    The single grep-friendly home for the ingest route's domain-error →
    status-code table (the full contract is documented at the top of the
    module). Each arm re-raises an :class:`HTTPException`; an exception that
    matches no arm is re-raised unchanged so it surfaces as a 500 rather
    than being silently swallowed.

    ``catalog_entry`` carries the operator's original ``"<product>/<version>"``
    reference when the request started as the catalog-driven shape; ``None``
    for explicit-quadruple requests. Used only on the
    :exc:`UpstreamNotSpecError` path so the 422 envelope can include the
    catalog reference (G0.15-T2 / #1211).
    """
    if isinstance(exc, LlmClientUnavailable):
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if isinstance(exc, PermissionError):
        # Defence-in-depth — the route's _require_admin already gates this,
        # but the service-level guard might catch a cross-tenant write that
        # slipped through.
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if isinstance(exc, VersionMismatchError):
        # G0.9-T8 (#740). 422 (not 400) because the request was syntactically
        # valid but semantically refuses the spec-vs-label cross-check.
        # Detail builder is shared with the MCP path
        # (operations/ingest/error_envelopes.py, G0.9.1-T5 #777) so the REST
        # 422 body and the MCP -32602 ``data`` member can't drift.
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            build_version_mismatch_detail(exc),
        ) from exc
    if isinstance(exc, UncoveredVersionLabel):
        # G0.9-T9 (#741). Checked BEFORE the generic ValueError-family arm
        # below so the more-specific exception wins. Shared builder (same
        # parity rationale as the VersionMismatchError sibling above; #1624
        # closed this last bare arm).
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            build_uncovered_version_label_detail(exc),
        ) from exc
    if isinstance(exc, UpstreamNotSpecError):
        raise _upstream_not_spec_http_exception(exc, catalog_entry=catalog_entry) from exc
    if isinstance(
        exc,
        (
            InvalidSpecError,
            UnsupportedSpecError,
            InvalidSchemaError,
            OpIdCollision,
            LlmOutputInvalid,
        ),
    ):
        # #1610 — structured envelope (shared builders), not bare str(exc);
        # see _spec_error_http_exception for the parity rationale.
        raise _spec_error_http_exception(exc) from exc
    if isinstance(exc, (yaml.YAMLError, JSONDecodeError)):
        # The parser passes malformed YAML / JSON bubble-up by design (per
        # parse_openapi's docstring) so the loader's structured error message
        # survives to the operator. Route maps to 400.
        raise HTTPException(
            http_status.HTTP_400_BAD_REQUEST,
            f"could not decode spec: {exc}",
        ) from exc
    if isinstance(exc, httpx.HTTPError):
        # HTTP(S) fetch failures for URL specs surface as ``httpx.HTTPError``
        # per :func:`parse_openapi`'s contract. 502 Bad Gateway is the closest
        # semantic fit: the operator's request is fine but an upstream the
        # route had to reach didn't respond cleanly.
        raise HTTPException(
            http_status.HTTP_502_BAD_GATEWAY,
            f"upstream spec fetch failed: {exc}",
        ) from exc
    # No arm matched — re-raise unchanged so an unexpected failure surfaces
    # as a 500 rather than being swallowed by the broad ``except`` above.
    raise exc


@router.get("")
async def list_endpoint(
    status: ConnectorStatusFilter | None = Query(default=None),
    operator: Operator = _require_operator,
    envelope: EnvelopeVersion | None = ENVELOPE_QUERY,
) -> dict[str, list[dict[str, object]]] | dict[str, object]:
    """List ingested connectors visible to the operator.

    Visibility scope (per
    :func:`list_ingested_connectors`): operator's-tenant rows +
    built-ins. The optional ``status`` filter narrows by aggregated
    review status; ``all`` (or omission) returns everything.

    The default response is wrapped in ``{"connectors": [...]}`` so
    future paging / cursor fields can land non-breakingly. The route
    builds the payload by calling :meth:`ConnectorListItem.model_dump`
    (with ``mode="json"``) on each item returned by
    :func:`list_ingested_connectors` rather than annotating
    ``response_model=ConnectorListResponse`` — per-item ``tenant_id``
    UUIDs need to render as strings in JSON, and the per-item
    ``mode="json"`` dump is the simplest way to get that without
    introducing a custom serializer.

    Passing ``?envelope=v2`` returns the unified ``{"items": [...],
    "next_cursor": ...}`` shape per
    ``docs/codebase/api-shape-conventions.md`` §2. This listing is
    not cursor-paginated, so ``next_cursor`` is always ``None`` under
    the opt-in. Omitting the param keeps the v0.8.0 default shape so
    no client breaks (G0.18-T3 #1356, completing #1312 acceptance A).
    """
    items = await list_ingested_connectors(
        operator=operator,
        status=status,
    )
    serialised = [item.model_dump(mode="json") for item in items]
    if envelope == "v2":
        return wrap_v2_envelope(serialised, next_cursor=None)
    return {"connectors": serialised}


@router.get("/catalog", response_model=CatalogListResponse)
async def catalog_endpoint(
    operator: Operator = _require_operator,
) -> CatalogListResponse:
    """Return the curated connector-spec catalog (Goal #214 on-ramp; #743).

    The catalog maps ``(product, version)`` to the recommended OpenAPI
    spec source(s) + the registered connector class that covers the
    version label. It is global, built-in reference data (not tenant-
    scoped), so operator role is the only gate; the read carries no
    tenant filter. The ``meho connector catalog list`` / ``ingest
    --catalog`` verbs (#915) consume this route — the CLI ships as a
    separate binary and cannot read the server's packaged catalog file.

    Declared before the ``/{connector_id}/...`` routes so the literal
    ``/catalog`` segment is matched ahead of the path-parameter
    patterns. Wrapped in ``{"catalog": [...]}`` for non-breaking paging
    room, mirroring the ``GET /`` list shape. Typed via
    ``response_model=CatalogListResponse`` so the OpenAPI contract for
    this public route declares the envelope + entry fields explicitly;
    unlike the ``GET /`` list route it has no per-row UUID-serialisation
    reason to stay an untyped object map.
    """
    return CatalogListResponse(catalog=load_catalog().entries)


@router.get("/{connector_id}/review", response_model=ConnectorReviewPayload)
async def get_review_endpoint(
    connector_id: str,
    operator: Operator = _require_operator,
) -> ConnectorReviewPayload:
    """Return the full review payload for *connector_id*.

    Operator-level read: any operator can inspect a connector their
    tenant owns plus built-ins. Editing the payload requires
    ``tenant_admin`` via the PATCH routes. Cross-tenant /
    non-existent connector → 404 (the deliberate conflation).
    """
    service = ReviewService(operator)
    try:
        return await service.get_review_payload(connector_id, operator.tenant_id)
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.patch(
    "/{connector_id}/groups/{group_key}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def edit_group_endpoint(
    connector_id: str,
    group_key: str,
    body: EditGroupBody,
    operator: Operator = _require_admin,
) -> Response:
    """Edit a group's ``when_to_use`` / ``name`` overrides.

    At least one of the two body fields must be set; an empty body
    yields 400. Writes one ``meho.connector.edit_group`` audit row
    via the service layer. Returns 204 on success.
    """
    service = ReviewService(operator)
    try:
        await service.edit_group(
            connector_id,
            group_key,
            tenant_id=operator.tenant_id,
            when_to_use=body.when_to_use,
            name=body.name,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{connector_id}/operations/{op_id:path}",
    response_model=EditOpResponse,
)
async def edit_op_endpoint(
    connector_id: str,
    op_id: str,
    body: EditOpBody,
    operator: Operator = _require_admin,
) -> EditOpResponse:
    """Edit a per-op operator override.

    At least one of ``custom_description`` / ``safety_level`` /
    ``requires_approval`` / ``is_enabled`` must be set; an empty
    body yields 400. Writes one ``meho.connector.edit_op`` audit
    row. Returns 200 with an :class:`EditOpResponse` on success.

    G0.23-T4 (#1630) promoted the route from 204 No Content to 200
    so enable-time advisories have a structured wire home:
    ``is_enabled=true`` on an op whose resolved connector is the
    unconfigured ingest auto-shim returns
    ``warnings=[{code='unreplaced_auto_shim', ...}]`` — the edit
    still lands (warnings never block the write), but the operator
    learns about the guaranteed dispatch dead end here instead of
    one ``call_operation`` later. The sibling ``edit_group`` route
    stays at 204 — it has no advisory to carry.

    The ``op_id`` path parameter uses the ``:path`` converter so
    operations whose natural key contains slashes
    (``"GET:/api/vcenter/cluster"``) round-trip without
    URL-encoding the colon-prefixed path segment.
    """
    service = ReviewService(operator)
    try:
        warnings = await service.edit_op(
            connector_id,
            op_id,
            tenant_id=operator.tenant_id,
            custom_description=body.custom_description,
            safety_level=body.safety_level,
            requires_approval=body.requires_approval,
            is_enabled=body.is_enabled,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return EditOpResponse(warnings=warnings)


@router.post(
    "/{connector_id}/enable",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def enable_endpoint(
    connector_id: str,
    operator: Operator = _require_admin,
) -> Response:
    """Transition every group in *connector_id* to ``enabled``.

    Idempotent: a re-call against a fully-enabled connector writes
    no audit row and returns 204. State-machine guards apply (see
    :meth:`ReviewService.enable_connector`); a forbidden source
    state yields 409.
    """
    service = ReviewService(operator)
    try:
        await service.enable_connector(
            connector_id,
            tenant_id=operator.tenant_id,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post(
    "/{connector_id}/enable-reads",
    response_model=EnableReadsResponse,
)
async def enable_reads_endpoint(
    connector_id: str,
    operator: Operator = _require_admin,
) -> EnableReadsResponse:
    """Bulk-enable every read-class (GET/HEAD) ingested op (G0.25-T7 #1749).

    Flips ``is_enabled=true`` on every ingested operation whose HTTP
    method is GET or HEAD in one pass, leaving every write-shaped verb
    (POST / PUT / PATCH / DELETE) and every typed / composite op
    default-deny — writes keep their per-op / composite curation by
    design. The point is broad governed *read* coverage on big
    ingested surfaces without a per-op death-march; the governance
    boundary (writes route through a hand-authored composite or
    command-template) is untouched.

    Returns 200 with an :class:`EnableReadsResponse` carrying the
    ``ops_enabled`` count (not the 204 the enable / disable
    transitions return) so the operator and the generated Go client
    see how many ops flipped. Idempotent: a re-call once the reads are
    enabled flips nothing, writes no audit row, and returns
    ``ops_enabled=0``. Unlike ``enable``, this does not move any
    group's ``review_status`` — it is a per-op flip, so there is no
    state-machine guard and no 409 path. Unknown / cross-tenant
    connector → 404 (the deliberate conflation). One
    ``meho.connector.enable_reads`` audit row is written when at least
    one op flips.

    Tenant scoping (#1699 contract): no ``tenant_id`` parameter — the
    scope is always the calling operator's tenant from the JWT. The
    MCP sibling ``meho.connector.enable_reads`` accepts an optional
    ``tenant_id`` for the built-in / global scope (tenant_admin only).
    """
    service = ReviewService(operator)
    try:
        ops_enabled = await service.enable_reads(
            connector_id,
            tenant_id=operator.tenant_id,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return EnableReadsResponse(connector_id=connector_id, ops_enabled=ops_enabled)


@router.post(
    "/{connector_id}/disable",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def disable_endpoint(
    connector_id: str,
    operator: Operator = _require_admin,
) -> Response:
    """Transition every group in *connector_id* to ``disabled``.

    Idempotent and state-machine-guarded — same shape as the enable
    handler.
    """
    service = ReviewService(operator)
    try:
        await service.disable_connector(
            connector_id,
            tenant_id=operator.tenant_id,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{connector_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_endpoint(
    connector_id: str,
    operator: Operator = _require_admin,
) -> Response:
    """Delete *connector_id* — rows under the operator's tenant + the ingest shim.

    G0.25-T2 (#1700): clean-up surface for the zero-op registry stubs
    aborted ingests leave behind, and for removing an unwanted
    ingested connector outright. Removes the connector's
    ``endpoint_descriptor`` + ``operation_group`` rows and writes one
    ``meho.connector.delete`` audit row in the same transaction; when
    no rows remain for the triple under any scope, the
    auto-registered ``GenericRestConnector`` shim is also popped from
    the v2 registry (hand-coded connector classes are never
    deregistered). A zero-op stub — registered class, no rows — is
    deletable here too: that delete is registry-only.

    Tenant scoping (#1699 contract): this route exposes **no**
    ``tenant_id`` parameter — the delete scope is always the calling
    operator's tenant from the JWT. Built-in / global connectors
    (``tenant_id IS NULL`` rows) are deleted via the MCP sibling
    ``meho.connector.delete`` with ``tenant_id`` omitted
    (tenant_admin only). Unknown ids, cross-tenant probes, and rows
    visible only under a scope this route cannot name all collapse
    into the same 404 the other connector routes use; a repeat
    DELETE therefore returns 404 once the first one landed.

    A connector that still has enabled operations is deleted anyway —
    the advisory is deliberate, not an error: it surfaces as the
    ``connector_delete_enabled_ops`` structured log event plus the
    audit payload's ``enabled_operations_deleted`` count. The wire
    response stays ``204 No Content`` per the task contract, so the
    structured warning body lives on the MCP sibling (the
    ``edit_op`` 200-promotion precedent from #1630 remains available
    if a REST wire home is ever needed). Re-ingesting the same triple
    afterwards re-registers the connector from scratch.
    """
    service = ReviewService(operator)
    try:
        await service.delete_connector(
            connector_id,
            tenant_id=operator.tenant_id,
        )
    except ConnectorNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
