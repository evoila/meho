# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/ui/broadcast/overrides`` -- the tenant-admin Overrides tab.

Initiative #1842 (G10.x operator console), Task #1891. Adds a third tab
to the existing ``/ui/broadcast`` Alpine tab strip so a tenant admin can
list, create, and delete :class:`BroadcastOverride` rules without leaving
the console -- the in-console counterpart of
``meho broadcast overrides set ...``.

Three routes, all under ``/ui/broadcast/overrides*``:

* ``GET /ui/broadcast/overrides`` -- the tab's lazy-loaded fragment
  (``broadcast/_overrides.html``, no ``base.html`` chrome). Renders the
  tenant's rules as a table plus the create form + the delete-confirm
  ``<dialog>``. Tenant-admin-gated: a non-admin operator gets the gated
  empty state, never the table.
* ``POST /ui/broadcast/overrides`` -- the create-form submit target.
  Re-renders the refreshed fragment on success; echoes the backend's
  422 (glob-not-regex) / 409 (already-exists) as an inline form error.
* ``DELETE /ui/broadcast/overrides/{override_id}`` -- the delete-confirm
  target. Re-renders the table on a 204; surfaces a cross-tenant 404 as
  "rule already removed".

Why the BFF goes through the impl functions, not the Bearer API
==============================================================

The backend CRUD plane
(:mod:`meho_backplane.api.v1.broadcast_overrides`) is the authority for
the override contract -- tenant-scoping, the glob-not-regex 422, the
composite-unique 409, the cross-tenant 404. This tab must never diverge
from that contract, so it calls the *same* implementation functions
(:func:`list_overrides_impl` / :func:`create_override_impl` /
:func:`delete_override_impl`) in-process with the lifted
:class:`Operator` + a UI session, exactly as the G6.3-T5 admin MCP tools
do. The tab never touches the DB directly and never re-implements the
RBAC / validation rules; it reuses them.

RBAC
====

The list / create / delete routes lift the full :class:`Operator` from
the BFF session via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
-- the same helper the connectors re-probe write surface uses. A
non-admin operator hitting the GET fragment renders the 403-driven
"Overrides require tenant_admin" empty state (a 403 from the dependency
is caught and turned into the gated fragment so the tab degrades
gracefully); a non-admin POST / DELETE 403s outright (the create form +
delete button are not even rendered for them, so a 403 there means a
forged request).

Resolution precedence note
==========================

The override resolver (G6.3-T2) is *most-specific-wins*: a scoped rule
(``scope_field`` / ``scope_value`` set) beats an op-wide rule for the
same pattern. The fragment renders a banner spelling this out so a flat
table is not misread as flat-priority.

Delete re-exposure
==================

Deleting a rule re-exposes the suppressed detail for matching ops on the
live feed and the Slack mirror -- the footgun the Initiative #376 design
calls out. The fragment renders the delete as a two-step
``<dialog>``-gated confirm whose body spells out that consequence in
prose, not a one-click row action.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.broadcast_overrides import (
    BroadcastOverrideCreate,
    create_override_impl,
    delete_override_impl,
    list_overrides_impl,
)
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_session
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["build_overrides_router"]

_log = structlog.get_logger(__name__)

#: The scope-field options the create-form select offers. The empty
#: string is the "op-wide" sentinel (NULL ``scope_field`` / ``scope_value``
#: pair); the other two are the backend's
#: ``Literal["namespace", "target_name"]`` allowlist on
#: :class:`BroadcastOverrideCreate`.
_SCOPE_FIELD_OPTIONS: Final[tuple[str, ...]] = ("namespace", "target_name")

#: The detail-level options the create-form select offers -- the
#: backend's ``Literal["full", "aggregate"]`` on
#: :class:`BroadcastOverrideCreate`.
_DETAIL_OPTIONS: Final[tuple[str, ...]] = ("full", "aggregate")


def _overrides_context(
    *,
    csrf_token: str,
    rows: list[object] | None = None,
    is_tenant_admin: bool,
    create_error: str | None = None,
    op_id_prefill: str = "",
) -> dict[str, object]:
    """Build the template context for ``broadcast/_overrides.html``.

    ``rows`` is the tenant's :class:`BroadcastOverride` list (only loaded
    for the admin path; ``None`` for the gated empty state).
    ``create_error`` carries an inline form-error message echoed from the
    backend's 422 / 409 so a failed create re-renders the form with the
    message instead of silently no-opping. ``op_id_prefill`` pre-fills the
    create form's ``op_id_pattern`` for the "suppress this op" cross-link.
    """
    return {
        "csrf_token": csrf_token,
        "overrides": rows or [],
        "is_tenant_admin": is_tenant_admin,
        "scope_field_options": _SCOPE_FIELD_OPTIONS,
        "detail_options": _DETAIL_OPTIONS,
        "create_error": create_error,
        "op_id_prefill": op_id_prefill,
    }


def _render_overrides(
    request: Request,
    *,
    session_ctx: UISessionContext,
    rows: list[object] | None,
    is_tenant_admin: bool,
    create_error: str | None = None,
    op_id_prefill: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """Render the ``broadcast/_overrides.html`` fragment + refresh the CSRF cookie.

    Minting a fresh token + re-setting the ``meho_csrf`` cookie on every
    render keeps the form's ``hx-headers`` echo and the cookie in lockstep
    (the cookie/header desync class #1693 fixed on the memory create
    modal): the form embeds the token THIS render minted and the response
    sets the matching cookie, so the double-submit pair always lines up.
    """
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    response = get_templates().TemplateResponse(
        request,
        "broadcast/_overrides.html",
        _overrides_context(
            csrf_token=csrf_token,
            rows=rows,
            is_tenant_admin=is_tenant_admin,
            create_error=create_error,
            op_id_prefill=op_id_prefill,
        ),
        status_code=status_code,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


async def _resolve_admin_or_gated(
    request: Request,
    session_ctx: UISessionContext,
) -> Operator | None:
    """Lift the operator + gate to tenant_admin, returning ``None`` on a soft 403.

    The GET fragment must degrade to the gated empty state for a
    non-admin operator rather than 500-ing the tab, so the
    :class:`HTTPException` 403
    :func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
    raises is caught and mapped to ``None`` (render the gated state). Any
    other status (401 identity mismatch, etc.) propagates unchanged.
    """
    try:
        return await resolve_operator_or_403(request, session_ctx)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return None
        raise


def _error_detail(exc: ValidationError) -> str:
    """Project a Pydantic :class:`ValidationError` into one inline message.

    The create path validates the form into
    :class:`BroadcastOverrideCreate` before calling the impl, so the
    glob-not-regex rule (``op_id_pattern must be glob, not regex; ...``)
    and the scope-pair rule both surface as :class:`ValidationError`
    here. The first error's message is the operator-facing string the
    template renders in the form's error banner.
    """
    errors = exc.errors()
    if errors:
        return str(errors[0].get("msg", "invalid override"))
    return "invalid override"


#: Module-level :class:`fastapi.Depends` closures -- ruff B008 guard.
_require_ui_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)


async def _list_overrides_handler(
    request: Request,
    op_id: str = "",
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``GET /ui/broadcast/overrides[?op_id=...]`` -- the tab fragment.

    Tenant-admin-gated: a non-admin operator renders the gated empty
    state, never the rules. ``op_id`` pre-fills the create form's
    ``op_id_pattern`` for the "suppress this op" cross-link. The rules
    come from the backend ``list_overrides_impl`` with the lifted operator
    so the UI never diverges from the API's RBAC contract.
    """
    operator = await _resolve_admin_or_gated(request, session_ctx)
    if operator is None:
        return _render_overrides(
            request,
            session_ctx=session_ctx,
            rows=None,
            is_tenant_admin=False,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    rows = await list_overrides_impl(operator=operator, session=db_session)
    return _render_overrides(
        request,
        session_ctx=session_ctx,
        rows=list(rows),
        is_tenant_admin=True,
        op_id_prefill=op_id,
    )


async def _create_override_handler(
    request: Request,
    op_id_pattern: str = Form(...),
    detail: str = Form(...),
    scope_field: str = Form(default=""),
    scope_value: str = Form(default=""),
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/broadcast/overrides`` -- create a rule, re-render the table.

    tenant_admin-gated (403 outright for a non-admin -- the form is not
    rendered for them). The form fields are validated into
    :class:`BroadcastOverrideCreate`, so the backend's glob-not-regex and
    scope-pair rules surface as a :class:`ValidationError` echoed inline; a
    duplicate ``(tenant_id, op_id_pattern, scope_field, scope_value)``
    surfaces the 409 ``broadcast_override_already_exists`` inline. On
    success the refreshed fragment is swapped back in.
    """
    operator = await resolve_operator_or_403(request, session_ctx)
    # An empty scope_field select means an op-wide rule -- send the NULL
    # pair the backend's scope-pair validator expects (a blank scope_value
    # alongside a set scope_field is the half-set bug the validator
    # rejects, so coalesce both to None together).
    normalised_scope_field = scope_field or None
    normalised_scope_value = scope_value or None
    try:
        payload = BroadcastOverrideCreate(
            op_id_pattern=op_id_pattern,
            scope_field=normalised_scope_field,  # type: ignore[arg-type]
            scope_value=normalised_scope_value,
            detail=detail,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        rows = await list_overrides_impl(operator=operator, session=db_session)
        return _render_overrides(
            request,
            session_ctx=session_ctx,
            rows=list(rows),
            is_tenant_admin=True,
            create_error=_error_detail(exc),
            op_id_prefill=op_id_pattern,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    create_error = await _try_create_in_savepoint(
        payload=payload,
        operator=operator,
        db_session=db_session,
    )
    rows = await list_overrides_impl(operator=operator, session=db_session)
    return _render_overrides(
        request,
        session_ctx=session_ctx,
        rows=list(rows),
        is_tenant_admin=True,
        create_error=create_error,
        op_id_prefill=op_id_pattern if create_error else "",
        status_code=status.HTTP_409_CONFLICT if create_error else 200,
    )


async def _try_create_in_savepoint(
    *,
    payload: BroadcastOverrideCreate,
    operator: Operator,
    db_session: AsyncSession,
) -> str | None:
    """Run the create inside a SAVEPOINT; return a 409 message or ``None``.

    The composite-unique IntegrityError ``create_override_impl`` turns
    into a 409 only rolls back the nested transaction -- the outer
    ``get_session`` transaction stays usable, so the caller's subsequent
    ``list_overrides_impl`` re-render can still query. Without the
    savepoint, the failed flush poisons the whole session ("Can't operate
    on closed transaction") and the error-render query 500s.
    """
    try:
        async with db_session.begin_nested():
            await create_override_impl(
                payload=payload,
                operator=operator,
                session=db_session,
            )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            return "A rule with this pattern and scope already exists."
        raise
    return None


async def _delete_override_handler(
    request: Request,
    override_id: uuid.UUID,
    session_ctx: UISessionContext = _require_ui_session_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``DELETE /ui/broadcast/overrides/{override_id}`` -- delete, re-render.

    tenant_admin-gated. The cross-tenant / unknown-id case surfaces as the
    backend's 404 ``broadcast_override_not_found`` (never 403 -- existence
    is not leaked across tenants), rendered as a "rule already removed"
    notice; the refreshed table re-renders either way so the operator sees
    the current state.
    """
    operator = await resolve_operator_or_403(request, session_ctx)
    already_removed = False
    try:
        await delete_override_impl(
            override_id=override_id,
            operator=operator,
            session=db_session,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            already_removed = True
        else:
            raise
    rows = await list_overrides_impl(operator=operator, session=db_session)
    return _render_overrides(
        request,
        session_ctx=session_ctx,
        rows=list(rows),
        is_tenant_admin=True,
        create_error=("That rule was already removed." if already_removed else None),
        status_code=(status.HTTP_404_NOT_FOUND if already_removed else 200),
    )


def build_overrides_router() -> APIRouter:
    """Construct the broadcast Overrides-tab :class:`APIRouter`.

    Registers the list / create / delete routes. Factory function (not a
    module-level constant) so a test app can construct parallel routers
    without sharing route state -- mirrors the feed / stream / history /
    event routers.
    """
    router = APIRouter(tags=["ui-broadcast"])
    router.add_api_route(
        "/ui/broadcast/overrides",
        _list_overrides_handler,
        methods=["GET"],
        name="ui_broadcast_overrides",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/broadcast/overrides",
        _create_override_handler,
        methods=["POST"],
        name="ui_broadcast_overrides_create",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/broadcast/overrides/{override_id}",
        _delete_override_handler,
        methods=["DELETE"],
        name="ui_broadcast_overrides_delete",
        response_class=HTMLResponse,
    )
    return router
