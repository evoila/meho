# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/doc_collections`` — catalogue list + readiness probe + lifecycle.

``GET /api/v1/doc_collections`` — the catalogue list (G4.6-T4 #1553): the
REST sibling of the ``list_doc_collections`` MCP tool + the ``meho docs
collections list`` CLI verb (REST / CLI / MCP are sibling fronts on one
backplane — the three-front rule). OPERATOR-gated; tenant-scoped (global +
this tenant's rows); filtered to the collections the operator is entitled
to (holds ``meho-docs:<collection_key>`` for); keyset-paginated by
``collection_key``. So an operator (or the CLI it fronts) sees exactly the
collections ``search_docs`` will accept.

Three tenant-admin-gated write routes against a
:class:`~meho_backplane.db.models.DocCollection` row, mirroring the
``probe_target`` + connector enable/disable precedents:

* ``POST /api/v1/doc_collections/{collection_key}/probe`` — probe the
  collection's backend and **persist on success only** its ``readiness``
  / ``doc_count`` / ``last_ingested_at`` + ``status`` transition onto the
  row, then return the typed
  :class:`~meho_backplane.docs_search.backends.base.BackendReadiness`. A
  probe that fails (backend unconfigured / unreachable / non-2xx /
  malformed, or the row routes to no registered backend) maps to **503**
  and leaves the row untouched — the same write-back split
  ``probe_target`` / ``Target.fingerprint`` use (``api/v1/targets.py``).
* ``POST /api/v1/doc_collections/{collection_key}/enable`` — return a
  disabled collection to service (→ ``provisioning``; a follow-up probe
  promotes it to ``ready``). Idempotent; a forbidden source state → 409.
* ``POST /api/v1/doc_collections/{collection_key}/disable`` — hide a
  collection from search (→ ``disabled``). Idempotent; 409 on a forbidden
  move (none, since ``disable`` is reachable from every live state — the
  guard is belt-and-suspenders against an out-of-enum status).

Why a probe **route** rather than an implicit probe-on-search: a probe
talks to the managed-RAG backend (latency + serialized rebuilds), so it
is an explicit operator/ops action that refreshes the cached liveness the
search path then reads cheaply — exactly the ``probe_target`` →
``Target.fingerprint`` → dispatch split. The collection-scoped search path
reads that cached ``status`` in its shared resolve + entitle + readiness
gate (:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`),
which fails typed against a not-``ready`` collection — branching a terminal
``disabled`` collection (403) from the transient ``provisioning`` /
``rebuilding`` states (409). The readiness rejection lives there, not on
these write routes.

RBAC + tenancy
--------------

``tenant_admin`` minimum on every route (these mutate registry state),
matching the connector enable/disable gate. The collection is resolved
tenant-first via
:func:`~meho_backplane.docs_collections.resolve_doc_collection` so an
admin probes / toggles the row visible to their tenant (their own
curated row shadows a global one); an unknown key → 404 with the typed
``known_keys`` hint.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import (
    DocCollectionBackendTypeError,
    DocCollectionConflictError,
    DocCollectionCreate,
    DocCollectionCreateResponse,
    DocCollectionGlobalError,
    DocCollectionNotDisabledError,
    DocCollectionSummary,
    create_doc_collection,
    delete_doc_collection,
    probe_collection,
    project_doc_collection_create_response,
    project_doc_collection_to_summary,
    resolve_doc_collection,
    set_collection_enabled,
)
from meho_backplane.docs_search import collection_capability_key
from meho_backplane.docs_search.backends.base import BackendReadiness

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/doc_collections", tags=["docs"])

#: Module-level ``Depends`` closure for the tenant-admin gate. Built once
#: (rather than inline) to satisfy ruff B008, matching the convention
#: :mod:`meho_backplane.api.v1.targets` established.
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))

#: Module-level ``Depends`` closure for the catalogue-list operator gate.
#: The list is a read; OPERATOR (not tenant_admin) is the floor — every
#: operator entitled to search a collection may discover it. ``read_only``
#: operators still pass (the list is non-mutating); the per-collection
#: entitlement filter, not the role gate, decides what they see.
_require_operator = Depends(require_role(TenantRole.OPERATOR))

#: Canonical audit op_id for the catalogue list — the SAME ``meho.docs.*``
#: family the ``search_docs`` route + the ``list_doc_collections`` MCP tool
#: bind, so a ``query_audit`` filter on ``op_id="meho.docs.*"`` catches the
#: REST catalogue read transport-independently (G4.5-T8 #1549).
_LIST_OP_ID = "meho.docs.collections.list"


def _dedupe_tenant_first(rows: list[DocCollectionORM]) -> list[DocCollectionORM]:
    """Collapse global+tenant rows sharing a ``collection_key``, tenant wins.

    A ``collection_key`` may exist both as a global row
    (``tenant_id IS NULL``) and as a tenant-curated row; the resolver
    prefers the tenant row (it overrides the global backend binding /
    metadata), so the catalogue must too — listing both would show the same
    key twice and surface the shadowed global metadata. The tenant row
    always wins regardless of which arrives first (the check is
    order-independent), so the NULLS-FIRST/LAST dialect difference between
    SQLite and PostgreSQL does not affect the outcome. Mirrors the identical
    dedupe in the ``list_doc_collections`` MCP tool so the two faces never
    disagree about which scope's row a shadowed key resolves to.
    """
    by_key: dict[str, DocCollectionORM] = {}
    for row in rows:
        existing = by_key.get(row.collection_key)
        if existing is None or row.tenant_id is not None:
            by_key[row.collection_key] = row
    return list(by_key.values())


@router.get(
    "",
    response_model=list[DocCollectionSummary],
)
async def list_doc_collections_endpoint(
    vendor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    operator: Operator = _require_operator,
    session: AsyncSession = Depends(get_session),
) -> list[DocCollectionSummary]:
    """List the doc collections the operator is entitled to search.

    The REST sibling of the ``list_doc_collections`` MCP tool: reads
    ``doc_collections`` tenant-scoped (global + this tenant's rows),
    de-duplicates a shadowed global key in favour of the tenant row, and
    filters to the collections the operator holds
    ``meho-docs:<collection_key>`` for — the same per-collection entitlement
    ``search_docs`` enforces, so every listed key is one ``search_docs``
    will accept. An unprovisioned tenant (no ``meho-docs:*`` capabilities)
    gets an empty list, matching the CLI's client-side hidden-when-
    unprovisioned UX.

    Keyset-paginated by ``collection_key`` (lexicographic): pass
    ``cursor=<last-key-seen>`` for the next page. ``vendor`` is exact-match.
    The entitlement filter runs in Python (it lives in the operator's
    capability set, not a joinable column); the catalogue is small (one row
    per corpus) so the over-read is negligible.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_LIST_OP_ID,
        audit_op_class="read",
    )

    stmt = select(DocCollectionORM).where(
        (DocCollectionORM.tenant_id == operator.tenant_id) | (DocCollectionORM.tenant_id.is_(None)),
    )
    if cursor is not None:
        stmt = stmt.where(DocCollectionORM.collection_key > cursor)
    stmt = stmt.order_by(
        DocCollectionORM.collection_key,
        DocCollectionORM.tenant_id,
    )

    result = await session.execute(stmt)
    rows = _dedupe_tenant_first(list(result.scalars().all()))
    # Apply ``vendor`` AFTER the tenant-first dedupe (Python-side, over the
    # post-dedupe tenant-wins rows), never in the pre-dedupe SQL WHERE: a
    # tenant row may shadow a global key under a *different* vendor, and
    # filtering in SQL would drop the tenant row before it could win and
    # surface the shadowed global row's vendor. Mirrors the identical reorder
    # in the ``list_doc_collections`` MCP tool so the two faces agree.
    if vendor is not None:
        rows = [row for row in rows if row.vendor == vendor]
    entitled = [
        row
        for row in rows
        if collection_capability_key(row.collection_key) in operator.capabilities
    ]
    return [project_doc_collection_to_summary(row) for row in entitled[:limit]]


@router.post(
    "",
    response_model=DocCollectionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "description": (
                "Collection registered. It starts at ``status=provisioning`` "
                "— the **create → probe → ready** flow: ``search_docs`` only "
                "accepts a ``ready`` collection, so call "
                "``POST /api/v1/doc_collections/{collection_key}/probe`` to "
                "promote it before searching. The response carries that "
                "instruction inline as ``next_step``."
            ),
        },
        409: {
            "description": (
                "A collection with this ``collection_key`` already exists "
                "in the tenant's scope (global or per-tenant)."
            ),
        },
        422: {
            "description": (
                "The ``backend.type`` is not a registered search backend; "
                "the detail enumerates the registered types."
            ),
        },
    },
)
async def create_doc_collection_endpoint(
    body: DocCollectionCreate,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> DocCollectionCreateResponse:
    """Register a new doc collection in the requesting tenant.

    The create sibling of the lifecycle write routes — ``tenant_admin``-
    gated, tenant-scoped (``tenant_id`` from the JWT, never the body), with
    the validation + audit every other registry write gets. Mirrors
    ``POST /api/v1/targets``: ``id`` / timestamps are generated server-side,
    ``status`` defaults to ``provisioning`` (a follow-up ``probe`` promotes
    it to ``ready``), the ``backend.type`` is validated against the
    search-backend registry (an unregistered type → 422 listing the valid
    set, not a deferred 503), and a cross-scope ``collection_key`` collision
    → 409. The create binds ``op_id="meho.docs.collections.create"`` so the
    registration joins the ``op_id="meho.docs.*"`` who-touched trail.

    Because a created collection is ``provisioning`` and ``search_docs``
    rejects a non-``ready`` collection, the response carries a ``next_step``
    hint pointing at the probe route (#1756) so the ``create → probe →
    ready`` flow is discoverable from the create reply rather than only from
    a confusing not-ready error on the first search.
    """
    try:
        row = await create_doc_collection(session, operator, body)
    except DocCollectionBackendTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.detail,
        ) from exc
    except DocCollectionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return project_doc_collection_create_response(row)


@router.post(
    "/{collection_key}/probe",
    response_model=BackendReadiness,
    responses={
        404: {"description": "No collection with this key is visible to the tenant."},
        409: {
            "description": (
                "The probe's readiness implies a status the lifecycle "
                "forbids from the collection's current state (e.g. probing "
                "a disabled collection)."
            ),
        },
        503: {
            "description": (
                "The collection's backend is unavailable — unconfigured, "
                "unreachable, non-2xx / malformed, or routes to no "
                "registered backend. Fail-closed; the row's cached "
                "liveness is left untouched (success-only write-back)."
            ),
        },
    },
)
async def probe_collection_endpoint(
    collection_key: str,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> BackendReadiness:
    """Probe a collection's backend and persist its liveness on success.

    Resolves the collection tenant-first, reads its backend's typed
    readiness (forwarding the operator JWT under the operator identity),
    and on success persists ``readiness`` / ``doc_count`` /
    ``last_ingested_at`` + the ``status`` transition onto the row. A
    failed probe is mapped to 503 and leaves the row unchanged: the
    ``get_session`` transaction rolls back on the raise, so nothing the
    service flushed survives.
    """
    collection = await resolve_doc_collection(session, collection_key, operator.tenant_id)
    try:
        return await probe_collection(session, operator, collection)
    except CorpusUnavailable as exc:
        # Fail-closed: an unconfigured / unreachable / non-2xx / unroutable
        # backend is 503. The transport never attaches the raw body, so
        # nothing leaks through the detail. The row stays at its
        # previously-cached liveness (the begin() block rolls back).
        _log.warning(
            "doc_collection_probe_unavailable",
            collection_key=collection_key,
            tenant_id=str(operator.tenant_id),
            backend_status=exc.status,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post(
    "/{collection_key}/enable",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        404: {"description": "No collection with this key is visible to the tenant."},
        409: {"description": "Enable is forbidden from the collection's current state."},
    },
)
async def enable_collection_endpoint(
    collection_key: str,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return a disabled collection to service (→ ``provisioning``).

    Idempotent: re-enabling an already-live collection writes nothing and
    returns 204. A forbidden source state raises 409 via the lifecycle
    guard. A follow-up probe promotes the re-enabled collection to
    ``ready`` once its index confirms.
    """
    collection = await resolve_doc_collection(session, collection_key, operator.tenant_id)
    await set_collection_enabled(session, collection, enabled=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{collection_key}/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        404: {"description": "No collection with this key is visible to the tenant."},
        409: {"description": "Disable is forbidden from the collection's current state."},
    },
)
async def disable_collection_endpoint(
    collection_key: str,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Hide a collection from search (→ ``disabled``).

    Idempotent and lifecycle-guarded — same shape as the enable handler.
    A disabled collection fails ``search_docs`` typed with a **terminal**
    rejection (REST 403 ``detail.error='collection_disabled'`` / MCP
    ``-32602``), distinct from the retryable 409 a ``provisioning`` /
    ``rebuilding`` collection returns, via the shared
    :func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`
    gate — never an empty result.
    """
    collection = await resolve_doc_collection(session, collection_key, operator.tenant_id)
    await set_collection_enabled(session, collection, enabled=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{collection_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        403: {
            "description": (
                "The resolved collection is a global (``tenant_id IS "
                "NULL``) platform-catalogue row — a tenant admin cannot "
                "delete it (structured ``detail.error='global_collection'``)."
            ),
        },
        404: {"description": "No collection with this key is visible to the tenant."},
        409: {
            "description": (
                "The collection is not ``disabled`` — disable it first "
                "(structured ``detail.error='collection_not_disabled'`` + "
                "the current ``status``)."
            ),
        },
    },
)
async def delete_collection_endpoint(
    collection_key: str,
    operator: Operator = _require_admin,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Deregister a disabled, tenant-owned collection, freeing its key (#2487).

    The delete half of the registry — the REST counterpart to
    ``POST /api/v1/doc_collections``. Resolves the collection tenant-first
    (an unknown key → 404 with the typed ``known_keys`` hint), then
    hard-deletes it behind two guards owned by the service:

    * **Tenant-owned only** — a global row (``tenant_id IS NULL``) is
      refused with a structured 403 ``detail.error='global_collection'``;
      removing a platform-catalogue row every tenant sees is an ops action,
      out of the tenant API.
    * **Disabled-first** — a collection whose ``status`` is not
      ``disabled`` is refused with a structured 409
      ``detail.error='collection_not_disabled'`` naming the current status.

    On success the row is gone and its ``collection_key`` is freed: a
    re-``POST`` of the same key then succeeds 201 (the recovery loop that
    motivated the issue). Deleting a tenant row that shadowed a global key
    un-shadows the global row (the resolver is tenant-first). One
    ``audit_log`` row per call, bound ``op_id="meho.docs.collections.delete"``.
    """
    collection = await resolve_doc_collection(session, collection_key, operator.tenant_id)
    try:
        await delete_doc_collection(session, operator, collection)
    except DocCollectionGlobalError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=exc.detail,
        ) from exc
    except DocCollectionNotDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.detail,
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
