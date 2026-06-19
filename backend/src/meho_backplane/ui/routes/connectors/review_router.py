# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route registration for the connector **review drawer** (Task #1887).

Thin wrappers that parse FastAPI params + resolve the role dependency,
then hand off to the render / submit helpers in
:mod:`~meho_backplane.ui.routes.connectors.review_drawer`. Split from the
render logic (the ``forms.py`` / ``forms_router.py`` precedent) so neither
module exceeds the chassis ~600-line cap and the helpers stay
unit-testable without a FastAPI :class:`Request` fixture.

Route inventory::

    GET   /ui/connectors/registry/{connector_id}/review
    GET   /ui/connectors/registry/{connector_id}/review/groups/{group_key}
    PATCH /ui/connectors/registry/{connector_id}/operations/{op_id:path}

The two ``GET`` reads are **operator-level** via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_role_probe`
(soft-hide of the edit controls -- the read drawer renders for any
operator, with the per-op edit affordances hidden from non-admins). The
``PATCH`` per-op edit is **tenant_admin-gated** via
:func:`~meho_backplane.ui.routes.connectors.operator.resolve_operator_or_403`
(403 for an operator), mirroring the REST ``edit_op_endpoint``'s
``_require_admin`` gate.

Route-ordering contract (first-match-wins)
------------------------------------------

``connector_id`` (``<impl_id>-<version>``) and ``group_key`` (snake_case,
max 64) are slash-free, so they use plain string path params -- NOT the
``:path`` converter. ``op_id`` is the natural key ``f"{method}:{path}"``
(e.g. ``GET:/api/vcenter/cluster``) which CONTAINS slashes, so the per-op
PATCH route uses ``{op_id:path}`` -- the only param here that needs it --
so the key round-trips intact without URL-encoding breakage.

The umbrella :func:`~meho_backplane.ui.routes.connectors.build_router`
includes this router **before** the ``GET /ui/connectors/{name}`` detail
catch-all (so the literal ``registry`` segment is never captured as a
target ``name``). The ``.../review`` and ``.../operations/...`` routes
carry literal segments after ``{connector_id}``, so they are unambiguous
against the registry-actions router's ``.../{connector_id}/enable`` etc.
siblings -- but ``review`` is registered alongside them under the same
``/ui/connectors/registry/{connector_id}/`` prefix, so the include order
keeps the whole family ahead of the bare detail route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_operator_or_403,
    resolve_role_probe,
)
from meho_backplane.ui.routes.connectors.review_drawer import (
    render_group_body,
    render_review_drawer,
    submit_op_edit,
)
from meho_backplane.ui.routes.corpus.routes import _resolve_operator

__all__ = ["build_review_router"]

#: ``custom_description`` max mirrors ``EditOpBody.custom_description``
#: (4096) so an over-long field is rejected at the schema boundary, not a
#: framework 422 on the raw form body.
_CUSTOM_DESCRIPTION_MAX = 4096

#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom.
_require_session_dep = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)
_require_admin_dep = Depends(resolve_operator_or_403)


async def _drawer_handler(
    request: Request,
    connector_id: str,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
) -> HTMLResponse:
    """``GET .../{connector_id}/review`` -- the drawer shell (lazy accordion)."""
    operator = await _resolve_operator(session_ctx)
    return await render_review_drawer(
        request,
        connector_id=connector_id,
        session_ctx=session_ctx,
        operator=operator,
        is_tenant_admin=role_probe.is_tenant_admin,
    )


async def _group_body_handler(
    request: Request,
    connector_id: str,
    group_key: str,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
) -> HTMLResponse:
    """``GET .../{connector_id}/review/groups/{group_key}`` -- one group's ops."""
    operator = await _resolve_operator(session_ctx)
    return await render_group_body(
        request,
        connector_id=connector_id,
        group_key=group_key,
        session_ctx=session_ctx,
        operator=operator,
        is_tenant_admin=role_probe.is_tenant_admin,
    )


async def _edit_handler(
    request: Request,
    connector_id: str,
    op_id: str,
    # ``Form(default=None)`` (not ``Form(...)``) so an omitted field flows
    # to the handler as "no edit to this field" rather than tripping
    # FastAPI's own raw-body 422 -- the review drawer owns the per-field
    # semantics (an empty body is rejected by the REST handler's own "at
    # least one field" rule, surfaced as a panel).
    custom_description: str | None = Form(default=None, max_length=_CUSTOM_DESCRIPTION_MAX),
    safety_level: str | None = Form(default=None),
    requires_approval: bool | None = Form(default=None),
    is_enabled: bool | None = Form(default=None),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``PATCH .../{connector_id}/operations/{op_id:path}`` -- one per-op edit."""
    return await submit_op_edit(
        request,
        connector_id=connector_id,
        op_id=op_id,
        custom_description=custom_description,
        safety_level=safety_level,
        requires_approval=requires_approval,
        is_enabled=is_enabled,
        session_ctx=session_ctx,
        operator=operator,
    )


def build_review_router() -> APIRouter:
    """Construct the review-drawer :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can build
    parallel routers without shared route state -- the list / detail /
    corpus / registry router convention. The two reads gate on
    ``operator`` (soft-hide); the PATCH gates on ``tenant_admin``. The
    ``{op_id:path}`` converter on the PATCH route is load-bearing: it lets
    the slash-containing natural key round-trip. The include order in
    :func:`~meho_backplane.ui.routes.connectors.build_router` registers
    this router before the ``GET /ui/connectors/{name}`` detail catch-all
    (first-match-wins) so the literal ``registry`` segment is never
    captured as a target ``name``.
    """
    router = APIRouter(tags=["ui-connectors"])
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/review",
        _drawer_handler,
        methods=["GET"],
        name="ui_connectors_registry_review",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/review/groups/{group_key}",
        _group_body_handler,
        methods=["GET"],
        name="ui_connectors_registry_review_group",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/connectors/registry/{connector_id}/operations/{op_id:path}",
        _edit_handler,
        methods=["PATCH"],
        name="ui_connectors_registry_op_edit",
        response_class=HTMLResponse,
    )
    return router
