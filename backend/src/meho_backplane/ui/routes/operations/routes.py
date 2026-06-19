# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operations launcher UI routes: connector picker + group browse + search + drawer.

Initiative #1835 (G10.9 Operations console), Task #1879 (T1). The
read-only entry surface for the console's first execution path (preview
lands in T2, run in T3). Today the only way to find a runnable operation
is the CLI (``meho operation groups`` / ``meho operation search``); this
adds the cross-connector web launcher.

Why a session BFF and not the Bearer ``/api/v1/operations/*`` routes
-------------------------------------------------------------------

The REST operation routes (``api/v1/operations.py``) are Bearer-gated over
a verified JWT. A browser carrying only the BFF session cookie cannot
authenticate them. So this module adds ``/ui/operations`` sub-routes that
are ``require_ui_session``-gated and call the operation meta-tools
(:func:`~meho_backplane.operations.meta_tools.list_operation_groups` /
:func:`~meho_backplane.operations.meta_tools.search_operations` /
:func:`~meho_backplane.operations.meta_tools.describe_descriptor`) and the
connector listing
(:func:`~meho_backplane.operations.ingest.list_connectors.list_ingested_connectors`)
in-process -- the same console-surface pattern the approvals / corpus / kb
surfaces use. All three meta-tools are complete and tested (Goal #388); no
backend work happens here.

Route inventory
---------------

* ``GET /ui/operations`` -- the full-page launcher (``extends base.html``).
  Renders the connector picker (populated from the connector listing so the
  operator never types a ``connector_id`` by hand), and -- when a
  ``connector_id`` is selected -- the group list + search results region.
  Content-negotiated on ``HX-Request``: a picker change re-fetches THIS
  route with ``HX-Request: true`` and gets only the ``_results.html``
  fragment.
* ``GET /ui/operations/search`` -- the HTMX search/browse partial. Takes
  ``connector_id`` + ``q`` (the canonical free-text param; NOT ``query``).
  Empty ``q`` browses the connector's operation groups; non-empty ``q``
  runs the hybrid BM25 + cosine search. Debounced via
  ``hx-trigger="keyup changed delay:300ms"`` + ``hx-push-url`` so searches
  are shareable.
* ``GET /ui/operations/descriptor/{descriptor_id}`` -- the operation
  detail drawer fragment (read). See the RBAC note below.

The literal ``search`` / ``descriptor`` segments register BEFORE the
``{descriptor_id}`` route is even relevant -- the only ``{param}`` route
sits under the distinct ``/ui/operations/descriptor/`` prefix, so first-
match-wins routing never binds ``search`` as a descriptor id. The router
is registered ahead of the stubs aggregate in
:func:`meho_backplane.ui.routes.build_router`.

connector_id shape (load-bearing)
---------------------------------

The picker emits the ``<impl_id>-<version>`` id (e.g. ``vmware-rest-9.0``,
``vault-1.x``) the meta-tools expect, NOT the bare product slug
(``vault``), which names no connector and raises
:class:`~meho_backplane.operations.meta_tools.UnknownConnectorError`. The
picker is populated from
:func:`~meho_backplane.operations.ingest.list_connectors.list_ingested_connectors`,
whose ``connector_id`` round-trips through the dispatcher resolve path by
construction (#773), so an operator selecting a listed connector never hits
a 404. A ``connector_id`` that is registered-but-not-ingested raises
:class:`~meho_backplane.operations.meta_tools.ConnectorNotIngestedError`;
the search partial renders its typed ``next_step`` ingest hint rather than
an empty result.

RBAC (load-bearing -- the same-template-both-roles trap)
--------------------------------------------------------

* OPERATOR: group browse + hybrid search + the drawer's operator-safe
  fields (``op_id`` / ``summary`` / ``description`` / ``method`` / ``path``
  / ``safety_level`` / ``requires_approval`` / ``parameter_schema``).
* TENANT_ADMIN: additionally the ``llm_instructions`` block in the drawer.

``llm_instructions`` is the per-op agent prompt; leaking it to a read-only
operator is a prompt-injection vector (``api/v1/operations.py:22-25``). The
drawer must NOT render it for a plain operator. This module uses
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_role_probe`
(fails SOFT to no-privileges) to drive the template's ``is_tenant_admin``
conditional, and only threads ``llm_instructions`` into the template
context when the probe says tenant_admin -- so a non-admin render never
carries the prompt text in its body, even though
:func:`~meho_backplane.operations.meta_tools.describe_descriptor` returns
it unconditionally on the model.

Tenant isolation
----------------

Every read derives ``tenant_id`` from the validated
:class:`UISessionContext` only (via the lifted :class:`Operator`); the
meta-tools' tenant-scoped queries make a cross-tenant descriptor id
indistinguishable from a missing one (404).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.operations.ingest.list_connectors import list_ingested_connectors
from meho_backplane.operations.meta_tools import (
    ConnectorNotIngestedError,
    UnknownConnectorError,
    describe_descriptor,
    list_operation_groups,
    search_operations,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_operations_router"]

log = structlog.get_logger(__name__)

#: Maximum free-text query length accepted by the search partial. Matches
#: the kb surface's cap: bounds the URL representation and keeps an
#: operator's verbatim query out of server logs at length.
_MAX_QUERY_LENGTH: Final[int] = 500

#: Maximum ``connector_id`` length accepted on the query string. A
#: ``<impl_id>-<version>`` id is short; bounding the wire shape rejects an
#: oversized paste at the form boundary (422) rather than forwarding it
#: into :func:`parse_connector_id`.
_MAX_CONNECTOR_ID_LENGTH: Final[int] = 256

#: Number of ranked search hits to request from the meta-tool. The hybrid
#: retriever clamps its own ``limit`` to 50; 25 is a comfortable launcher
#: page that keeps the first paint fast.
_SEARCH_LIMIT: Final[int] = 25

#: Page size for the group-browse listing (empty query). Real connectors
#: carry O(10) groups; 100 keeps every one on one page.
_GROUPS_LIMIT: Final[int] = 100

#: Module-level ``Depends`` closures -- built once (rather than inline) to
#: satisfy ruff B008, matching the approvals / kb / connectors routers.
_require_session = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)


async def _resolve_operator(session: UISessionContext) -> Operator:
    """Reconstruct the full :class:`Operator` from the BFF session.

    The meta-tools need a real :class:`Operator` (they read
    ``operator.tenant_id`` for the tenant-scoped queries). Mirrors the
    connectors role-probe lift (these are read-only routes, like the
    connectors detail page): load the decrypted session row and re-verify
    its access token through the chassis JWT chain. The JWT round-trip
    catches a same-session role demotion the next time the operator
    browses; caching a role on the session row would extend the
    demotion's effective window to the session's absolute TTL.

    Raises :class:`fastapi.HTTPException` 401 when the session was revoked
    / expired between the middleware check and here (the BFF error handler
    maps ``ui_session_required`` to a login redirect for HTML requests).
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        decrypted = await load_session(db_session, session.session_id)
    if decrypted is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    settings = get_settings()
    return await verify_jwt_for_audience(
        f"Bearer {decrypted.access_token}",
        expected_audience=settings.keycloak_audience,
    )


def _is_htmx(request: Request) -> bool:
    """Return whether *request* is an HTMX-driven fetch (case-insensitive)."""
    return request.headers.get("hx-request", "").lower() == "true"


async def _list_connectors(operator: Operator) -> list[dict[str, Any]]:
    """Project the visible connectors into the picker's option shape.

    Populates the picker from the same listing
    (:func:`list_ingested_connectors`) ``GET /api/v1/connectors`` serves, so
    every option's ``value`` is a ``<impl_id>-<version>`` id that resolves
    through the dispatcher (#773). The bare product slug is never emitted.
    Only dispatchable (``state == "ingested"``) connectors are selectable --
    a ``state == "registered"`` connector has no operations to browse yet,
    so listing it as a picker option would dead-end on every search.
    """
    items = await list_ingested_connectors(operator=operator)
    return [
        {
            "connector_id": item.connector_id,
            "product": item.product,
            "enabled_operation_count": item.enabled_operation_count,
        }
        for item in items
        if item.state == "ingested"
    ]


async def _descriptor_ids_for_op_ids(
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
    op_ids: list[str],
) -> dict[str, uuid.UUID]:
    """Map each *op_id* to its enabled descriptor's UUID, tenant-scoped.

    Search hits carry only ``op_id`` (a string); the detail-drawer route is
    keyed on the descriptor ``id`` (a UUID). This resolves the link target
    for each hit without a per-hit round-trip: one query over the
    connector's natural key, scoped to built-in (``tenant_id IS NULL``) +
    this tenant's rows -- the same visibility the meta-tools enforce. An
    op_id absent from the map (disabled, cross-tenant) simply renders
    without a drawer link rather than a dead one.
    """
    if not op_ids:
        return {}
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session:
        result = await db_session.execute(
            select(EndpointDescriptor.op_id, EndpointDescriptor.id).where(
                EndpointDescriptor.product == product,
                EndpointDescriptor.version == version,
                EndpointDescriptor.impl_id == impl_id,
                EndpointDescriptor.op_id.in_(op_ids),
                EndpointDescriptor.is_enabled.is_(True),
                (EndpointDescriptor.tenant_id.is_(None))
                | (EndpointDescriptor.tenant_id == tenant_id),
            )
        )
        # First row per op_id wins. Either bucket resolves to a real
        # descriptor the drawer can open; this is only a link target, not a
        # dispatch decision, so the tenant-vs-global precedence the
        # meta-tools enforce on the actual read does not need replicating
        # here. ``setdefault`` keeps it deterministic per query.
        mapping: dict[str, uuid.UUID] = {}
        for op_id, desc_id in result.all():
            mapping.setdefault(op_id, desc_id)
        return mapping


def build_operations_router() -> APIRouter:
    """Construct the ``/ui/operations*`` :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without shared route state -- the convention
    every surface router (approvals / kb / connectors) follows. Registered
    ahead of the stubs aggregate in
    :func:`meho_backplane.ui.routes.build_router`.
    """
    router = APIRouter(tags=["ui-operations"])

    # NOTE: the literal ``search`` segment and the ``descriptor/{id}`` route
    # are registered BEFORE the bare ``/ui/operations`` index so the
    # first-match-wins lookup is unambiguous; the only ``{param}`` route
    # sits under the distinct ``/ui/operations/descriptor/`` prefix, so the
    # literal ``search`` can never bind as a descriptor id (the ordering
    # discipline the approvals / kb routers document).

    @router.get("/ui/operations/search", response_class=HTMLResponse)
    async def operations_search(
        request: Request,
        session: UISessionContext = _require_session,
        connector_id: str = Query(default="", max_length=_MAX_CONNECTOR_ID_LENGTH),
        q: str = Query(default="", max_length=_MAX_QUERY_LENGTH),
    ) -> HTMLResponse:
        """Render the group-browse / hybrid-search results partial.

        Empty ``q`` browses the connector's operation groups; a non-empty
        ``q`` runs the hybrid BM25 + cosine search. An unknown / not-yet-
        ingested ``connector_id`` renders the typed 404 / ingest-hint
        panel, not an empty 200.
        """
        return await _render_results(request, session, connector_id, q)

    @router.get("/ui/operations/descriptor/{descriptor_id}", response_class=HTMLResponse)
    async def operations_descriptor(
        request: Request,
        descriptor_id: uuid.UUID,
        session: UISessionContext = _require_session,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Render the operation detail drawer fragment (read-only).

        ``llm_instructions`` is threaded into the template context ONLY when
        the role probe says tenant_admin -- a plain operator's render never
        carries the per-op agent prompt in its body (the RBAC trap).
        """
        return await _render_drawer(request, session, descriptor_id, role_probe)

    @router.get("/ui/operations", response_class=HTMLResponse)
    async def operations_index(
        request: Request,
        session: UISessionContext = _require_session,
        connector_id: str = Query(default="", max_length=_MAX_CONNECTOR_ID_LENGTH),
        q: str = Query(default="", max_length=_MAX_QUERY_LENGTH),
    ) -> HTMLResponse:
        """Render the operations launcher, content-negotiated by ``HX-Request``.

        A picker change re-fetches this route with ``HX-Request: true`` and
        gets only the ``_results.html`` fragment; a normal navigation
        (sidebar link, bookmark, shared search URL) gets the full page.
        """
        if _is_htmx(request):
            return await _render_results(request, session, connector_id, q)
        return await _render_index(request, session, connector_id, q)

    return router


async def _build_results_context(
    operator: Operator,
    connector_id: str,
    q: str,
) -> dict[str, Any]:
    """Assemble the shared results context for the page + the HTMX partial.

    Resolves groups (always, for the browse rail) and -- when ``q`` is set
    -- the ranked search hits, mapping each hit's ``op_id`` to its
    descriptor UUID so the drawer link target is concrete. The
    connector-existence taxonomy is surfaced as ``error`` /
    ``next_step`` rather than raised, so the partial renders the typed
    ingest hint inline instead of bubbling a 404 into an HTMX swap.
    """
    connector_id = connector_id.strip()
    query = q.strip()
    context: dict[str, Any] = {
        "connector_id": connector_id,
        "query": query,
        "groups": [],
        "hits": [],
        "error": None,
        "next_step": None,
    }
    if not connector_id:
        return context

    product, version, impl_id = parse_connector_id(connector_id)
    try:
        groups_result = await list_operation_groups(
            operator, {"connector_id": connector_id, "limit": _GROUPS_LIMIT}
        )
        context["groups"] = groups_result.get("groups", [])
        if query:
            search_result = await search_operations(
                operator,
                {"connector_id": connector_id, "query": query, "limit": _SEARCH_LIMIT},
            )
            hits = search_result.get("hits", [])
            desc_ids = await _descriptor_ids_for_op_ids(
                tenant_id=operator.tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                op_ids=[h["op_id"] for h in hits],
            )
            for hit in hits:
                resolved = desc_ids.get(hit["op_id"])
                hit["descriptor_id"] = str(resolved) if resolved is not None else None
            context["hits"] = hits
    except ConnectorNotIngestedError as exc:
        # Registered-but-not-ingested: surface the typed ingest hint the
        # operator self-corrects against (mirrors api/v1/operations.py).
        context["error"] = "connector_not_ingested"
        context["next_step"] = exc.next_step
    except UnknownConnectorError:
        context["error"] = "unknown_connector"
    return context


async def _render_results(
    request: Request,
    session: UISessionContext,
    connector_id: str,
    q: str,
) -> HTMLResponse:
    """Render the ``operations/_results.html`` group-browse / search partial."""
    operator = await _resolve_operator(session)
    context = await _build_results_context(operator, connector_id, q)
    return get_templates().TemplateResponse(request, "operations/_results.html", context)


async def _render_index(
    request: Request,
    session: UISessionContext,
    connector_id: str,
    q: str,
) -> HTMLResponse:
    """Render the full-page launcher: picker + group list + search region."""
    operator = await _resolve_operator(session)
    context = await _build_results_context(operator, connector_id, q)
    context["connectors"] = await _list_connectors(operator)
    context["active_surface"] = "operations"
    context["page_title"] = "Operations"
    return get_templates().TemplateResponse(request, "operations/index.html", context)


async def _render_drawer(
    request: Request,
    session: UISessionContext,
    descriptor_id: uuid.UUID,
    role_probe: OperatorRoleProbe,
) -> HTMLResponse:
    """Render the operation detail drawer fragment for *descriptor_id*.

    Maps a missing / cross-tenant descriptor to 404 (the meta-tool returns
    ``None`` for both, so the two are indistinguishable to the caller). The
    ``llm_instructions`` block is threaded into the context ONLY when the
    role probe says tenant_admin -- the operator-render body never carries
    the per-op agent prompt.
    """
    operator = await _resolve_operator(session)
    descriptor = await describe_descriptor(operator, descriptor_id)
    if descriptor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operation_descriptor_not_found",
        )
    context: dict[str, Any] = {
        "descriptor": descriptor,
        "is_tenant_admin": role_probe.is_tenant_admin,
        # Gate the per-op agent prompt server-side: only a tenant_admin
        # render carries it. A plain operator's body never contains the
        # prompt text (prompt-injection vector if leaked).
        "llm_instructions": descriptor.llm_instructions if role_probe.is_tenant_admin else None,
    }
    return get_templates().TemplateResponse(request, "operations/_drawer.html", context)
