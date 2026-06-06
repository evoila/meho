# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/doc_collections`` — readiness probe + lifecycle (G4.6-T6 #1555).

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
``Target.fingerprint`` → dispatch split. Collection-scoped search wiring
(the ``status != 'ready'`` typed failure) lives in T3 (#1552), which
calls the
:func:`~meho_backplane.docs_collections.lifecycle.ensure_collection_searchable`
guard this Task ships.

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
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.engine import get_session
from meho_backplane.docs_collections import (
    probe_collection,
    resolve_doc_collection,
    set_collection_enabled,
)
from meho_backplane.docs_search.backends.base import BackendReadiness

__all__ = ["router"]

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/doc_collections", tags=["docs"])

#: Module-level ``Depends`` closure for the tenant-admin gate. Built once
#: (rather than inline) to satisfy ruff B008, matching the convention
#: :mod:`meho_backplane.api.v1.targets` established.
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))


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
    A disabled collection fails ``search_docs`` typed (403) via the T3
    search-time guard rather than returning an empty result.
    """
    collection = await resolve_doc_collection(session, collection_key, operator.tenant_id)
    await set_collection_enabled(session, collection, enabled=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
