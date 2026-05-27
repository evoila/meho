# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route registration for the target create / edit forms (Task #874).

Thin wrappers that parse FastAPI params + resolve the
tenant_admin-gated :class:`~meho_backplane.auth.operator.Operator`
dependency, then hand off to the render / submit helpers in
:mod:`~meho_backplane.ui.routes.connectors.forms`. Split from the
render logic so neither module exceeds the chassis-wide ~600-line
cap and the helpers stay unit-testable without a FastAPI
:class:`Request` fixture for the projection paths.

Route inventory (all tenant_admin-gated server-side via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`):

* ``GET   /ui/connectors/create``        -- HTMX-loaded create modal.
* ``POST  /ui/connectors/create``        -- create submit handler.
* ``GET   /ui/connectors/{name}/edit``   -- HTMX-loaded edit modal.
* ``PATCH /ui/connectors/{name}``        -- edit submit handler.

Registration order is **load-bearing**: the literal-prefix routes
(``/ui/connectors/create``, ``/ui/connectors/{name}/edit``) are
mounted on their own router and the umbrella
:func:`~meho_backplane.ui.routes.connectors.build_router` includes
this router **before** the parametrised ``GET /ui/connectors/{name}``
detail route so the literal ``"create"`` token is never captured as a
target ``name`` by the detail handler.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_session
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.forms import (
    render_create_modal,
    render_edit_modal,
    submit_create,
    submit_edit,
)
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403

__all__ = ["build_forms_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom matching
#: the chassis dashboard / topology / memory routes.
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_operator_or_403)
_get_session_dep = Depends(get_session)

#: Form-field length caps mirroring the
#: :class:`~meho_backplane.targets.schemas.TargetCreate` field bounds.
#: The server-side Pydantic validation is authoritative; these caps
#: bound the form-body parse against a paste-from-clipboard accident
#: before the bytes reach the schema.
_NAME_MAX = 200
_PRODUCT_MAX = 100
_HOST_MAX = 512
_SECRET_REF_MAX = 1024
_ALIASES_MAX = 2048
_NOTES_MAX = 8192
_PORT_MAX = 16


async def _create_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/connectors/create`` -- HTMX-loaded create modal fragment."""
    del operator  # gate only; render needs no operator-specific context.
    return await render_create_modal(request, session_ctx)


async def _create_submit_handler(
    request: Request,
    # ``Form(default="")`` (not ``Form(...)``) on the text fields so an
    # empty / omitted submit flows to the ``TargetCreate`` Pydantic
    # validation rather than tripping FastAPI's own raw-JSON 422 -- the
    # #874 contract is "invalid input re-renders the *form* with field
    # errors", which means the modal-rendering handler must own the
    # validation, not the framework boundary.
    name: str = Form(default="", max_length=_NAME_MAX),
    product: str = Form(default="", max_length=_PRODUCT_MAX),
    host: str = Form(default="", max_length=_HOST_MAX),
    port: str | None = Form(default=None, max_length=_PORT_MAX),
    auth_model: str = Form(default="shared_service_account"),
    secret_ref: str | None = Form(default=None, max_length=_SECRET_REF_MAX),
    vpn_required: bool = Form(default=False),
    aliases: str | None = Form(default=None, max_length=_ALIASES_MAX),
    notes: str | None = Form(default=None, max_length=_NOTES_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``POST /ui/connectors/create`` -- create one target via the REST handler.

    An unchecked ``vpn_required`` checkbox is omitted from the form
    body entirely (HTML checkbox semantics), so FastAPI's
    ``Form(default=False)`` lands the correct ``False``; a checked box
    posts the literal value the template sets and coerces to ``True``.
    """
    return await submit_create(
        request,
        session_ctx,
        operator,
        db_session,
        name=name,
        product=product,
        host=host,
        port=port,
        auth_model=auth_model,
        secret_ref=secret_ref,
        vpn_required=vpn_required,
        aliases_raw=aliases,
        notes=notes,
    )


async def _edit_modal_handler(
    request: Request,
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``GET /ui/connectors/{name}/edit`` -- pre-populated edit modal fragment."""
    del operator  # gate only.
    return await render_edit_modal(request, session_ctx, db_session, target_name=name)


async def _edit_submit_handler(
    request: Request,
    name: str,
    # ``Form(default=...)`` for the same reason as the create handler:
    # invalid input must re-render the modal with field errors, so the
    # ``TargetUpdate`` Pydantic pass owns validation rather than the
    # framework boundary.
    product: str = Form(default="", max_length=_PRODUCT_MAX),
    host: str = Form(default="", max_length=_HOST_MAX),
    port: str | None = Form(default=None, max_length=_PORT_MAX),
    auth_model: str = Form(default="shared_service_account"),
    secret_ref: str | None = Form(default=None, max_length=_SECRET_REF_MAX),
    vpn_required: bool = Form(default=False),
    aliases: str | None = Form(default=None, max_length=_ALIASES_MAX),
    notes: str | None = Form(default=None, max_length=_NOTES_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
    db_session: AsyncSession = _get_session_dep,
) -> HTMLResponse:
    """``PATCH /ui/connectors/{name}`` -- update one target via the REST handler."""
    return await submit_edit(
        request,
        session_ctx,
        operator,
        db_session,
        target_name=name,
        product=product,
        host=host,
        port=port,
        auth_model=auth_model,
        secret_ref=secret_ref,
        vpn_required=vpn_required,
        aliases_raw=aliases,
        notes=notes,
    )


def build_forms_router() -> APIRouter:
    """Construct the target create / edit forms :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the list / detail / probe router convention. The literal-prefix
    ``/ui/connectors/create`` and ``/ui/connectors/{name}/edit`` routes
    must register before the parametrised ``GET /ui/connectors/{name}``
    detail route (handled by the include order in
    :func:`~meho_backplane.ui.routes.connectors.build_router`).
    """
    router = APIRouter(tags=["ui-connectors"])
    router.add_api_route(
        "/ui/connectors/create",
        _create_modal_handler,
        methods=["GET"],
        name="ui_connectors_create_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/create",
        _create_submit_handler,
        methods=["POST"],
        name="ui_connectors_create_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/{name}/edit",
        _edit_modal_handler,
        methods=["GET"],
        name="ui_connectors_edit_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/{name}",
        _edit_submit_handler,
        methods=["PATCH"],
        name="ui_connectors_edit_submit",
        response_class=HTMLResponse,
    )
    return router
