# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/connectors*`` -- REST surface for spec-ingestion + review.

G0.7-T6 (#406) of Initiative #389. Seven routes mounted under
``/api/v1/connectors*`` that drive the spec-ingestion pipeline (T1
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
  Returns 204. Role: ``tenant_admin``.
* ``POST /api/v1/connectors/{connector_id}/enable`` — transition all
  groups to ``enabled``; cascade. Returns 204. Idempotent. Role:
  ``tenant_admin``.
* ``POST /api/v1/connectors/{connector_id}/disable`` — transition all
  groups to ``disabled``; cascade. Returns 204. Idempotent. Role:
  ``tenant_admin``.

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
  status code wins.
* :class:`InvalidSpecError` / :class:`UnsupportedSpecError` /
  :class:`InvalidSchemaError` / :class:`OpIdCollision` /
  :class:`LlmOutputInvalid` → 400 Bad Request (with the structured
  detail message from the exception).
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

The ingest route uses a module-level
:data:`_llm_client_factory_dep` FastAPI dependency that resolves the
:class:`LlmClientFactory` for the :class:`IngestionPipelineService`.
Production wiring sets this via :func:`set_llm_client_factory` at
app-startup time (G0.7-T5 will land the Anthropic adapter wire-up);
tests inject a stub. The default factory raises
:class:`LlmClientUnavailable`, which the route maps to 503.
"""

from __future__ import annotations

from json import JSONDecodeError
from typing import Annotated

import httpx
import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.operations.ingest import (
    CatalogListResponse,
    ConnectorNotFoundError,
    ConnectorReviewPayload,
    EditGroupBody,
    EditOpBody,
    IngestionPipelineResult,
    IngestionPipelineService,
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
    UncoveredVersionLabel,
    UnsupportedSpecError,
    VersionMismatchError,
    build_version_mismatch_detail,
    default_llm_client_factory,
    list_ingested_connectors,
    load_catalog,
)
from meho_backplane.operations.ingest.api_schemas import ConnectorStatusFilter

__all__ = [
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
    a test or a feature-flagged deploy. Production wiring at app
    startup (G0.7-T5) calls this once with the Anthropic adapter
    factory; tests call it from their fixture setup.

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


def _get_llm_client_factory() -> LlmClientFactory:
    """FastAPI dependency that returns the active LLM-client factory.

    Built as a dependency so tests can also override via
    :attr:`FastAPI.dependency_overrides` when convenient.
    """
    return _llm_client_factory


@router.post("/ingest", response_model=IngestResponse)
async def ingest_endpoint(
    body: IngestRequest,
    operator: Operator = _require_admin,
    llm_client_factory: Annotated[
        LlmClientFactory,
        Depends(_get_llm_client_factory),
    ] = default_llm_client_factory,
) -> IngestResponse:
    """Run the full ingestion pipeline (T1 → T2 → T3) for one connector.

    ``dry_run=true`` parses every spec but writes nothing; the
    response carries the parser's ``inserted_count`` projection and
    ``grouping=None``. The real path runs the parse → register_
    ingested → run_llm_grouping pipeline serially and returns the
    aggregated counts + grouping result.

    Tenant scoping: the operator's tenant_id from the JWT is used
    as the write scope unless the operator is ``tenant_admin``
    (built-in ingest, ``tenant_id=NULL``). For v0.2 the route always
    writes under the operator's tenant_id; built-in ingest is the
    same call shape from a tenant_admin operator whose tenant_id
    happens to be the "built-in" admin tenant. The CLI / MCP
    siblings can target the built-in scope explicitly when needed
    by hitting :class:`IngestionPipelineService` directly with
    ``tenant_id=None``.
    """
    service = IngestionPipelineService(
        operator=operator,
        llm_client_factory=llm_client_factory,
    )
    result = await _run_ingest_with_http_mapping(service=service, body=body, operator=operator)
    ingestion_model, grouping_model = result.to_api_models()
    return IngestResponse(ingestion=ingestion_model, grouping=grouping_model)


async def _run_ingest_with_http_mapping(
    *,
    service: IngestionPipelineService,
    body: IngestRequest,
    operator: Operator,
) -> IngestionPipelineResult:
    """Drive :meth:`IngestionPipelineService.ingest` and map domain errors to HTTP.

    Extracted from :func:`ingest_endpoint` so the handler body stays
    under the code-quality function-size threshold; the mapping
    table itself is the load-bearing contract documented at the top
    of the module.
    """
    try:
        return await service.ingest(
            product=body.product,
            version=body.version,
            impl_id=body.impl_id,
            specs=body.specs,
            base_url=body.base_url,
            tenant_id=operator.tenant_id,
            dry_run=body.dry_run,
        )
    except LlmClientUnavailable as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        # Defence-in-depth — the route's _require_admin already
        # gates this, but the service-level guard might catch a
        # cross-tenant write that slipped through.
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except VersionMismatchError as exc:
        # G0.9-T8 (#740). 422 (not 400) because the request was
        # syntactically valid but semantically refuses the spec-vs-label
        # cross-check. The structured detail names both versions so the
        # operator's error message tells them exactly what to fix. The
        # detail builder is shared with the MCP path
        # (:mod:`meho_backplane.operations.ingest.error_envelopes`,
        # G0.9.1-T5 #777) so the REST 422 body and the MCP -32602
        # ``data`` member can't drift.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=build_version_mismatch_detail(exc),
        ) from exc
    except UncoveredVersionLabel as exc:
        # G0.9-T9 (#741). The request body parsed fine (Pydantic
        # accepted shape + length bounds), but the operator's
        # ``version`` label is semantically outside every registered
        # connector class's ``supported_version_range`` for the
        # ``(product, impl_id)`` pair — orphan-at-ingest. 422
        # Unprocessable Entity is the right code: structurally valid,
        # semantically rejected. Listed BEFORE the generic ValueError-
        # family catch below so the more-specific exception wins.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except (
        InvalidSpecError,
        UnsupportedSpecError,
        InvalidSchemaError,
        OpIdCollision,
        LlmOutputInvalid,
    ) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except (yaml.YAMLError, JSONDecodeError) as exc:
        # The parser passes malformed YAML / JSON bubble-up by design
        # (per parse_openapi's docstring) so the loader's structured
        # error message survives to the operator. Route maps to 400.
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"could not decode spec: {exc}",
        ) from exc
    except httpx.HTTPError as exc:
        # HTTP(S) fetch failures for URL specs surface as
        # ``httpx.HTTPError`` per :func:`parse_openapi`'s contract.
        # 502 Bad Gateway is the closest semantic fit: the operator's
        # request is fine but an upstream the route had to reach
        # didn't respond cleanly.
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream spec fetch failed: {exc}",
        ) from exc


@router.get("")
async def list_endpoint(
    status: ConnectorStatusFilter | None = Query(default=None),
    operator: Operator = _require_operator,
) -> dict[str, list[dict[str, object]]]:
    """List ingested connectors visible to the operator.

    Visibility scope (per
    :func:`list_ingested_connectors`): operator's-tenant rows +
    built-ins. The optional ``status`` filter narrows by aggregated
    review status; ``all`` (or omission) returns everything.

    The response is wrapped in ``{"connectors": [...]}`` so future
    paging / cursor fields can land non-breakingly. The route builds
    the payload by calling :meth:`ConnectorListItem.model_dump`
    (with ``mode="json"``) on each item returned by
    :func:`list_ingested_connectors` rather than annotating
    ``response_model=ConnectorListResponse`` — per-item ``tenant_id``
    UUIDs need to render as strings in JSON, and the per-item
    ``mode="json"`` dump is the simplest way to get that without
    introducing a custom serializer.
    """
    items = await list_ingested_connectors(
        operator=operator,
        status=status,
    )
    return {"connectors": [item.model_dump(mode="json") for item in items]}


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
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def edit_op_endpoint(
    connector_id: str,
    op_id: str,
    body: EditOpBody,
    operator: Operator = _require_admin,
) -> Response:
    """Edit a per-op operator override.

    At least one of ``custom_description`` / ``safety_level`` /
    ``requires_approval`` / ``is_enabled`` must be set; an empty
    body yields 400. Writes one ``meho.connector.edit_op`` audit
    row. Returns 204 on success.

    The ``op_id`` path parameter uses the ``:path`` converter so
    operations whose natural key contains slashes
    (``"GET:/api/vcenter/cluster"``) round-trip without
    URL-encoding the colon-prefixed path segment.
    """
    service = ReviewService(operator)
    try:
        await service.edit_op(
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
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


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
