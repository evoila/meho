# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-row registry actions: enable / disable / enable-reads / delete.

Initiative #1839 (G10.13 Connector ingest & curation registry UI),
Task #1885 (T1) work items #2-#4. The confirm-gated write surface the
registry list (``registry_list.py``) rows drive. Each verb is an HTMX
handler under a literal-suffixed BFF route that calls the **shipped REST
handler in-process** (the ``forms_router.py`` pattern) so the UI write
and the Bearer-API write share one validation + state-machine +
audit code path -- never an HTTP call back to ``/api/v1``.

Route inventory (all ``tenant_admin``-gated server-side via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`):

* ``GET  /ui/connectors/registry/{connector_id}/enable``        -- confirm modal.
* ``POST /ui/connectors/registry/{connector_id}/enable``        -- enable submit.
* ``GET  /ui/connectors/registry/{connector_id}/enable-reads``  -- confirm modal.
* ``POST /ui/connectors/registry/{connector_id}/enable-reads``  -- enable-reads submit.
* ``GET  /ui/connectors/registry/{connector_id}/disable``       -- confirm modal.
* ``POST /ui/connectors/registry/{connector_id}/disable``       -- disable submit.
* ``GET  /ui/connectors/registry/{connector_id}/delete``        -- type-to-confirm modal.
* ``DELETE /ui/connectors/registry/{connector_id}``             -- delete submit.

``connector_id`` is the slash-free ``<impl_id>-<version>`` string, so
every route uses a **plain ``{connector_id}`` string path param** -- the
``:path`` converter is NOT needed (matching the shipped REST routes).

Governance footguns
-------------------

``enable`` / ``enable-reads`` (and ``disable`` followed by a re-enable)
**loosen** the safety boundary, so each confirm modal spells out the
projected blast radius (the group / op counts from the row that would
flip). ``delete`` is **type-to-confirm** (the operator retypes the
``connector_id``) and the copy surfaces that enabled operations will be
removed (the ``enabled_operations_deleted`` advisory).

Error panels
------------

The in-process REST handlers raise :class:`fastapi.HTTPException` on the
state-machine / scope failures. The action handlers catch it and render
an actionable inline panel rather than letting a 5xx escape:

* ``409 connector_scope_ambiguous`` -- the ``candidates[]`` envelope
  (a label that maps to both a tenant row and a built-in row); the panel
  lists the candidates so the operator disambiguates.
* ``409 invalid state transition`` -- a forbidden enable/disable move;
  the panel names the rejected transition.
* ``404`` -- an unknown / cross-tenant / already-deleted connector.

(The ``503 LlmClientUnavailable`` panel the initiative calls for is
raised only by the ingest *pipeline* -- the catalog/quadruple grouping
call -- which lands in T2's ingest modal, not these four verbs. The
panel renderer here is shape-generic so T2 reuses it.)

CSRF
----

Every state-changing verb rides the double-submit ``meho_csrf`` cookie
via the confirm element's OWN ``hx-headers`` ``X-CSRF-Token`` (HTMX does
not propagate ``hx-headers`` to a descendant form). Each modal render
mints a token + re-sets the cookie so the double-submit pair lines up.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.api.v1.connectors_ingest import (
    delete_endpoint,
    disable_endpoint,
    enable_endpoint,
    enable_reads_endpoint,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.ingest import (
    ConnectorListItem,
    list_ingested_connectors,
)
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403
from meho_backplane.ui.routes.connectors.registry_list import render_registry_row
from meho_backplane.ui.templating import get_templates

__all__ = ["build_registry_actions_router"]

_log = structlog.get_logger(__name__)

#: ``connector_id`` is ``<impl_id>-<version>``; both halves are bounded
#: in the schema, so a longer path segment cannot name a real row --
#: reject it cheaply with the same 404 the resolver would raise after a
#: round trip (the connectors / corpus detail idiom against fuzzer spam).
_CONNECTOR_ID_MAX = 256

#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom.
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_operator_or_403)


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the ``meho_csrf`` double-submit cookie on *response*.

    Value MUST equal the token the rendered markup echoes via
    ``hx-headers`` or the CSRF middleware rejects the next submit. Same
    posture as :mod:`~meho_backplane.ui.routes.connectors.registry_list`.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _validate_connector_id(connector_id: str) -> None:
    """Reject an over-long path segment with a 404 before any DB work."""
    if not connector_id or len(connector_id) > _CONNECTOR_ID_MAX:
        raise HTTPException(status_code=404, detail=f"connector {connector_id!r} not found")


async def _find_row(operator: Operator, connector_id: str) -> ConnectorListItem | None:
    """Return the operator-visible registry row for *connector_id*, or ``None``.

    Re-reads via the same :func:`list_ingested_connectors` the list view
    calls (operator-scoped visibility), then matches on ``connector_id``.
    Used by the confirm-modal renders (to show the projected blast
    radius) and the success path (to re-render the affected row for the
    OOB swap). A re-read is cheap relative to a per-row JSON projection
    and keeps the modal counts authoritative against the live rows.
    """
    items = await list_ingested_connectors(operator=operator)
    for item in items:
        if item.connector_id == connector_id:
            return item
    return None


def _render_error_panel(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
    candidates: list[dict[str, Any]] | None = None,
) -> HTMLResponse:
    """Render the inline action-error panel fragment.

    Swapped into the row's per-action result slot (the action button's
    ``hx-target``) so a failed verb surfaces an actionable message in
    place of a 5xx / stack trace. ``candidates`` carries the
    ``connector_scope_ambiguous`` ``candidates[]`` list so the panel can
    enumerate the tenant-vs-built-in rows the label mapped to.
    """
    return get_templates().TemplateResponse(
        request,
        "connectors/_registry_error.html",
        {
            "title": title,
            "message": message,
            "candidates": candidates or [],
        },
        status_code=status_code,
    )


def _panel_from_http_exception(
    request: Request,
    exc: HTTPException,
    *,
    connector_id: str,
) -> HTMLResponse:
    """Map an in-process REST :class:`HTTPException` to an inline panel.

    Branches on the structured ``detail`` shape the shipped handlers
    raise: the ``connector_scope_ambiguous`` 409 carries a dict with a
    ``candidates`` list; an ``invalid state transition`` 409 and the
    ``404`` carry a plain string. Anything else re-raises unchanged so an
    unexpected fault still surfaces as a real error rather than a
    mislabelled panel.
    """
    detail = exc.detail
    if (
        exc.status_code == 409
        and isinstance(detail, dict)
        and detail.get("detail") == "connector_scope_ambiguous"
    ):
        candidates = detail.get("candidates")
        return _render_error_panel(
            request,
            title="Connector scope is ambiguous",
            message=(
                f"{connector_id!r} resolves to both a tenant-curated row and a "
                "built-in row, so the action cannot pick one without guessing. "
                "Disambiguate via the MCP sibling (the built-in / global scope is "
                "tenant_admin-only and not writable from this console)."
            ),
            status_code=409,
            candidates=candidates if isinstance(candidates, list) else [],
        )
    if exc.status_code == 409:
        return _render_error_panel(
            request,
            title="State transition not allowed",
            message=(
                f"The requested change to {connector_id!r} is forbidden by the "
                f"review state machine: {detail}."
            ),
            status_code=409,
        )
    if exc.status_code == 404:
        return _render_error_panel(
            request,
            title="Connector not found",
            message=(
                f"{connector_id!r} is unknown, belongs to another tenant, or was "
                "already deleted. Refresh the registry list."
            ),
            status_code=404,
        )
    raise exc


async def _render_row_swap(
    request: Request,
    *,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Re-render the affected registry row for the HTMX OOB swap.

    Re-reads the row post-mutation so the swapped ``<tr>`` shows the new
    counts / state. A ``delete`` removes the row entirely -- the row is
    gone from the service read, so the swap returns an empty
    out-of-band ``<tr>`` that removes the old row from the table.
    """
    item = await _find_row(operator, connector_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = render_registry_row(
        request,
        item=item,
        connector_id=connector_id,
        csrf_token=csrf_token,
        is_tenant_admin=True,
        oob=True,
    )
    _set_csrf_cookie(response, csrf_token)
    return response


# ---------------------------------------------------------------------------
# Confirm-modal renders
# ---------------------------------------------------------------------------


def _render_modal(
    request: Request,
    *,
    template: str,
    item: ConnectorListItem,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render a confirm-modal fragment + re-set the CSRF cookie.

    The modal's confirm button carries the projected blast radius (the
    row's group / op counts) so a loosening action is never one-click
    without the operator seeing what flips. Mints + re-sets the
    ``meho_csrf`` cookie so the modal's own ``hx-headers`` echo lines up.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, Any] = {
        "row": {
            "connector_id": item.connector_id,
            "product": item.product,
            "version": item.version,
            "impl_id": item.impl_id,
            "is_builtin": item.tenant_id is None,
            "state": item.state,
            "group_count": item.group_count,
            "staged_group_count": item.staged_group_count,
            "enabled_group_count": item.enabled_group_count,
            "disabled_group_count": item.disabled_group_count,
            "operation_count": item.operation_count,
            "enabled_operation_count": item.enabled_operation_count,
        },
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, template, context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _modal_for(
    request: Request,
    *,
    template: str,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """Resolve the row, then render the named confirm modal.

    A connector that is not operator-visible 404s before the modal
    renders -- there is nothing to confirm against an unknown row.
    """
    _validate_connector_id(connector_id)
    item = await _find_row(operator, connector_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"connector {connector_id!r} not found")
    return _render_modal(request, template=template, item=item, session_ctx=session_ctx)


# ---------------------------------------------------------------------------
# Verb submits (call the shipped REST handler in-process)
# ---------------------------------------------------------------------------


async def _submit_enable(
    request: Request,
    *,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """``POST .../enable`` -- enable every group via the REST handler."""
    _validate_connector_id(connector_id)
    try:
        await enable_endpoint(connector_id=connector_id, operator=operator)
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)
    _log.info(
        "ui_connector_registry_enabled",
        connector_id=connector_id,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
    )
    return await _render_row_swap(
        request,
        connector_id=connector_id,
        session_ctx=session_ctx,
        operator=operator,
    )


async def _submit_disable(
    request: Request,
    *,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """``POST .../disable`` -- disable every group via the REST handler."""
    _validate_connector_id(connector_id)
    try:
        await disable_endpoint(connector_id=connector_id, operator=operator)
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)
    _log.info(
        "ui_connector_registry_disabled",
        connector_id=connector_id,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
    )
    return await _render_row_swap(
        request,
        connector_id=connector_id,
        session_ctx=session_ctx,
        operator=operator,
    )


async def _submit_enable_reads(
    request: Request,
    *,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """``POST .../enable-reads`` -- bulk-enable read-class ops via the REST handler."""
    _validate_connector_id(connector_id)
    try:
        await enable_reads_endpoint(connector_id=connector_id, operator=operator)
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)
    _log.info(
        "ui_connector_registry_enable_reads",
        connector_id=connector_id,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
    )
    return await _render_row_swap(
        request,
        connector_id=connector_id,
        session_ctx=session_ctx,
        operator=operator,
    )


async def _submit_delete(
    request: Request,
    *,
    connector_id: str,
    session_ctx: UISessionContext,
    operator: Operator,
) -> HTMLResponse:
    """``DELETE .../{connector_id}`` -- delete the connector via the REST handler.

    On success the row is gone from the operator-visible read, so the
    OOB row-swap returns an empty ``<tr>`` that removes the old row from
    the table.
    """
    _validate_connector_id(connector_id)
    try:
        await delete_endpoint(connector_id=connector_id, operator=operator)
    except HTTPException as exc:
        return _panel_from_http_exception(request, exc, connector_id=connector_id)
    _log.info(
        "ui_connector_registry_deleted",
        connector_id=connector_id,
        tenant_id=str(operator.tenant_id),
        operator_sub=operator.sub,
    )
    return await _render_row_swap(
        request,
        connector_id=connector_id,
        session_ctx=session_ctx,
        operator=operator,
    )


def build_registry_actions_router() -> APIRouter:
    """Construct the registry per-row actions :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    build parallel routers without shared route state -- the list /
    detail / corpus router convention. Every route is literal-suffixed
    (``/{connector_id}/enable`` etc.) so it is unambiguous against the
    bare ``GET /ui/connectors/{name}`` detail route, but the include
    order in :func:`~meho_backplane.ui.routes.connectors.build_router`
    still registers this router (and the list router) before the detail
    router so the literal ``registry`` segment wins the first-match-wins
    lookup over ``{name}``.
    """
    router = APIRouter(tags=["ui-connectors"])

    async def _enable_modal_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET .../{connector_id}/enable`` -- enable confirm modal."""
        return await _modal_for(
            request,
            template="connectors/_registry_enable_modal.html",
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _enable_reads_modal_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET .../{connector_id}/enable-reads`` -- enable-reads confirm modal."""
        return await _modal_for(
            request,
            template="connectors/_registry_enable_reads_modal.html",
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _disable_modal_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET .../{connector_id}/disable`` -- disable confirm modal."""
        return await _modal_for(
            request,
            template="connectors/_registry_disable_modal.html",
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _delete_modal_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``GET .../{connector_id}/delete`` -- type-to-confirm delete modal."""
        return await _modal_for(
            request,
            template="connectors/_registry_delete_modal.html",
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _enable_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST .../{connector_id}/enable``."""
        return await _submit_enable(
            request,
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _enable_reads_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST .../{connector_id}/enable-reads``."""
        return await _submit_enable_reads(
            request,
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _disable_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``POST .../{connector_id}/disable``."""
        return await _submit_disable(
            request,
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    async def _delete_handler(
        request: Request,
        connector_id: str,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _require_admin_dep,
    ) -> HTMLResponse:
        """``DELETE .../{connector_id}``."""
        return await _submit_delete(
            request,
            connector_id=connector_id,
            session_ctx=session_ctx,
            operator=operator,
        )

    # GET confirm modals.
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/enable",
        _enable_modal_handler,
        methods=["GET"],
        name="ui_connectors_registry_enable_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/enable-reads",
        _enable_reads_modal_handler,
        methods=["GET"],
        name="ui_connectors_registry_enable_reads_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/disable",
        _disable_modal_handler,
        methods=["GET"],
        name="ui_connectors_registry_disable_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/delete",
        _delete_modal_handler,
        methods=["GET"],
        name="ui_connectors_registry_delete_modal",
        response_class=HTMLResponse,
    )
    # POST / DELETE submits.
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/enable",
        _enable_handler,
        methods=["POST"],
        name="ui_connectors_registry_enable",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/enable-reads",
        _enable_reads_handler,
        methods=["POST"],
        name="ui_connectors_registry_enable_reads",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/disable",
        _disable_handler,
        methods=["POST"],
        name="ui_connectors_registry_disable",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}",
        _delete_handler,
        methods=["DELETE"],
        name="ui_connectors_registry_delete",
        response_class=HTMLResponse,
    )
    return router
