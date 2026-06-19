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
  detail drawer fragment (read). See the RBAC note below. The drawer
  render mints + re-sets the ``meho_csrf`` cookie so the preview form it
  carries (below) has a matching double-submit pair after the swap.
* ``POST /ui/operations/preview`` (Task #1880, T2) -- the read-only
  request **preview** action that lives inside the detail drawer: given
  the chosen op + a target + params, it renders the literal would-be HTTP
  request (``method`` / ``resolved_path`` / ``query`` / ``redacted_body``)
  **without dispatching it**. CSRF-gated (it is a ``POST`` under ``/ui/``)
  and ``require_ui_session``-gated; calls
  :func:`~meho_backplane.operations.meta_tools.preview_operation`
  in-process -- the same in-process BFF pattern the approvals decision
  POSTs use, because the Bearer-gated ``POST /api/v1/operations/preview``
  cannot be authenticated by a session cookie. Preview NEVER sends a
  request and writes NO audit row; dispatch (Run) is T3.

The literal ``search`` / ``descriptor`` / ``preview`` segments register
BEFORE the ``{descriptor_id}`` route is even relevant -- the only
``{param}`` route sits under the distinct ``/ui/operations/descriptor/``
prefix, so first-match-wins routing never binds ``search`` / ``preview``
as a descriptor id (and ``preview`` is a ``POST``, so it could not collide
with the GET slug route regardless). The router is registered ahead of the
stubs aggregate in :func:`meho_backplane.ui.routes.build_router`.

Preview envelope + the one hard 400
-----------------------------------

:func:`preview_operation` returns the structured envelope verbatim:
``status`` of ``"ok"`` (carries ``method`` / ``resolved_path`` / ``query``
/ ``redacted_body``) / ``"error"`` / ``"unavailable"`` (the operator-input
faults -- unknown op, invalid params, unresolvable connector -- come back
INSIDE the envelope with ``extras.error_code``, NOT as HTTP 4xx, the same
contract as ``POST /api/v1/operations/preview``). The one exception that
surfaces as a hard ``400`` is a missing target name (a ``ValueError`` from
the meta-tool's target normalisation); the BFF route replicates the REST
route's ``try/except ValueError`` and renders that ``400`` as an inline
form error rather than tearing the operator out of the drawer.

The ``redacted_body`` is the would-be body run through the SAME
connector-boundary redaction pipeline the response path uses -- it is not
a new raw-secret surface, so it is surfaced verbatim (with a clear
"redacted -- secrets masked" note) and never un-redacted.

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

import json
import uuid
from typing import Any, Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
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
    preview_operation,
    search_operations,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.session_store import load_session
from meho_backplane.ui.csrf import mint_csrf_token
from meho_backplane.ui.routes.approvals.render import set_csrf_cookie
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

#: Maximum ``op_id`` length accepted on the preview form. An op id is the
#: descriptor's natural-key string (``vault.kv.read``,
#: ``POST:/repos/{owner}/{repo}/issues``); bounding the wire shape rejects
#: an oversized paste at the form boundary (422) rather than forwarding it.
_MAX_OP_ID_LENGTH: Final[int] = 512

#: Maximum target-name length accepted on the preview form. A target name
#: is a short slug (``rdc-vcenter``); the bound protects the form parse
#: against a paste-from-clipboard accident.
_MAX_TARGET_LENGTH: Final[int] = 256

#: Maximum raw ``params`` JSON length accepted on the preview form. The
#: params editor is a free-form JSON textarea; 64 KiB is a comfortable
#: ceiling for a real op's arguments while bounding the body parse against
#: an oversized paste. The CSRF middleware already caps the buffered body
#: at 256 KiB; this is the surface-specific, friendlier-error bound.
_MAX_PARAMS_LENGTH: Final[int] = 64 * 1024

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

    # NOTE: the literal ``search`` / ``preview`` segments and the
    # ``descriptor/{id}`` route are registered BEFORE the bare
    # ``/ui/operations`` index so the first-match-wins lookup is
    # unambiguous; the only ``{param}`` route sits under the distinct
    # ``/ui/operations/descriptor/`` prefix, so the literal ``search`` can
    # never bind as a descriptor id (the ordering discipline the approvals /
    # kb routers document). ``preview`` is a ``POST``, so it cannot collide
    # with the GET slug route regardless -- the ordering note is kept for
    # consistency.

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

    @router.post("/ui/operations/preview", response_class=HTMLResponse)
    async def operations_preview(
        request: Request,
        session: UISessionContext = _require_session,
        connector_id: str = Form(max_length=_MAX_CONNECTOR_ID_LENGTH),
        op_id: str = Form(max_length=_MAX_OP_ID_LENGTH),
        target: str = Form(default="", max_length=_MAX_TARGET_LENGTH),
        params: str = Form(default="", max_length=_MAX_PARAMS_LENGTH),
    ) -> HTMLResponse:
        """Render the read-only request **preview** fragment (no dispatch).

        CSRF-gated by the ``ui/csrf.py`` middleware (a ``POST`` under
        ``/ui/``) and ``require_ui_session``-gated. Resolves the same op +
        target + params a real dispatch would and renders the literal
        would-be HTTP request -- ``method`` / ``resolved_path`` / ``query``
        / ``redacted_body`` -- WITHOUT sending it and WITHOUT writing an
        audit row. Operator-input faults land inside the rendered envelope
        (``status="error"`` / ``"unavailable"``); a missing target name
        surfaces as an inline ``400`` form error.
        """
        return await _render_preview(request, session, connector_id, op_id, target, params)

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

    Mints a fresh CSRF token and re-sets the ``meho_csrf`` cookie on the
    drawer response so the preview form the drawer carries (Task #1880) has
    a matching double-submit pair after the HTMX swap rotated it -- the same
    cookie-desync defence the approvals detail modal applies (#1693 /
    #1754). The ``connector_id`` (``<impl_id>-<version>``) is reconstructed
    from the descriptor's natural key so the preview form can post it
    without the operator re-typing it; preview is offered only for an
    ``source_kind="ingested"`` op (a typed / composite op has no single
    literal HTTP request to preview, so its envelope would be
    ``status="unavailable"`` -- the template hides the form for those).
    """
    operator = await _resolve_operator(session)
    descriptor = await describe_descriptor(operator, descriptor_id)
    if descriptor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="operation_descriptor_not_found",
        )
    csrf_token = mint_csrf_token(str(session.session_id))
    context: dict[str, Any] = {
        "descriptor": descriptor,
        "is_tenant_admin": role_probe.is_tenant_admin,
        # Gate the per-op agent prompt server-side: only a tenant_admin
        # render carries it. A plain operator's body never contains the
        # prompt text (prompt-injection vector if leaked).
        "llm_instructions": descriptor.llm_instructions if role_probe.is_tenant_admin else None,
        # The dispatchable connector id the preview form posts back. The
        # descriptor's natural key round-trips ``parse_connector_id``.
        "connector_id": f"{descriptor.impl_id}-{descriptor.version}",
        # Preview is meaningful only for an HTTP-ingested op.
        "previewable": descriptor.source_kind == "ingested",
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "operations/_drawer.html", context)
    set_csrf_cookie(response, csrf_token)
    return response


async def _render_preview(
    request: Request,
    session: UISessionContext,
    connector_id: str,
    op_id: str,
    target: str,
    params: str,
) -> HTMLResponse:
    """Render the read-only request-preview fragment for the drawer.

    Parses the free-form ``params`` JSON textarea server-side (a malformed
    body is a typed inline error, not a 422), forwards the ``target`` name to
    the meta-tool's bare-string shape, then calls :func:`preview_operation`
    in-process. The structured envelope (``status`` of ``"ok"`` / ``"error"``
    / ``"unavailable"``) is surfaced verbatim; the one ``ValueError`` the
    meta-tool raises (a missing target name -- the empty-string / dict-
    without-name case) is mapped to an inline ``400`` form error, replicating
    the REST route's ``try/except ValueError``.

    The target is forwarded **verbatim** (whitespace-stripped, not coerced to
    ``None``) so a blank field on an ingested op -- which always needs a
    target to resolve its connector -- fails loud as that ``400`` ("supply a
    target name") rather than as a confusing in-envelope ``no_connector``
    fault.

    No dispatch, no audit row -- the meta-tool guarantees both.
    """
    operator = await _resolve_operator(session)

    try:
        parsed_params = _parse_params_field(params)
    except ValueError as exc:
        return _render_preview_form_error(request, connector_id, op_id, target, params, str(exc))

    arguments: dict[str, Any] = {
        "connector_id": connector_id,
        "op_id": op_id,
        # Bare-string target shape ``_normalize_target_arg`` accepts. An empty
        # string (blank field) raises the meta-tool's missing-target-name
        # ValueError -- the REST route's hard 400 -- surfaced inline below.
        "target": target.strip(),
        "params": parsed_params,
    }
    try:
        envelope = await preview_operation(operator, arguments)
    except ValueError as exc:
        # The only ValueError the meta-tool raises is a missing target name
        # (the dict-target-without-name case). The REST route maps it to a
        # hard 400; here it is an inline form error so the operator stays in
        # the drawer.
        return _render_preview_form_error(request, connector_id, op_id, target, params, str(exc))

    context: dict[str, Any] = {
        "envelope": envelope,
        "error_message": None,
        "connector_id": connector_id,
        "op_id": op_id,
        "target": target,
        "params": params,
    }
    return get_templates().TemplateResponse(request, "operations/_preview.html", context)


def _parse_params_field(params: str) -> dict[str, Any]:
    """Parse the preview form's free-form ``params`` JSON into a dict.

    A blank field is the empty-params case (``{}``). A non-blank field must
    be a JSON **object**; a malformed body or a non-object JSON value
    (a list, a bare scalar) raises :class:`ValueError` with an operator-
    legible message the caller renders inline -- the meta-tool's
    ``params`` contract is a dict.
    """
    text = params.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"params is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if not isinstance(value, dict):
        raise ValueError('params must be a JSON object (e.g. {"path": "secret/data/app"}).')
    return value


def _render_preview_form_error(
    request: Request,
    connector_id: str,
    op_id: str,
    target: str,
    params: str,
    message: str,
) -> HTMLResponse:
    """Render the preview fragment with an inline form error (HTTP 400).

    Covers the malformed-``params`` case and the missing-target-name
    ``ValueError`` the meta-tool raises (the REST route's hard 400). The
    fragment carries ``error_message`` and no ``envelope`` so the template
    renders the alert instead of a request preview; the ``400`` status lets
    a caller distinguish a form fault from a successful in-envelope render.
    """
    context: dict[str, Any] = {
        "envelope": None,
        "error_message": message,
        "connector_id": connector_id,
        "op_id": op_id,
        "target": target,
        "params": params,
    }
    return get_templates().TemplateResponse(
        request,
        "operations/_preview.html",
        context,
        status_code=status.HTTP_400_BAD_REQUEST,
    )
