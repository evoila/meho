# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-grants UI route registration -- maps HTTP verbs to render helpers.

Initiative #1824 (G10.8 Agents console), Task #1832 (T5). The route
handlers are thin wrappers: parse FastAPI params, resolve the
tenant_admin gate dependency, and hand off to the render helpers in
:mod:`~meho_backplane.ui.routes.agents.grants.views` (read) or
:mod:`~meho_backplane.ui.routes.agents.grants.forms` (write).

Route inventory
---------------

Every route is **tenant_admin** -- reads included -- via
:func:`~meho_backplane.ui.routes.agents.grants.operator.resolve_grants_admin_or_403`.
Grant listings reveal the tenant's least-privilege posture, so the
read paths are gated the same as the writes (mirroring the REST surface
``api/v1/agent_grants.py``):

* ``GET  /ui/agents/grants`` -- table page or HTMX tbody fragment.
* ``GET  /ui/agents/grants/create`` -- HTMX-loaded create modal.
* ``POST /ui/agents/grants/create`` -- create submit.
* ``GET  /ui/agents/grants/elevate`` -- HTMX-loaded elevate modal.
* ``POST /ui/agents/grants/elevate`` -- elevate submit.
* ``GET  /ui/agents/grants/{grant_id}`` -- per-grant detail.
* ``GET  /ui/agents/grants/{grant_id}/revoke`` -- revoke-confirm modal.
* ``POST /ui/agents/grants/{grant_id}/revoke`` -- revoke submit.

Registration order is **load-bearing** for the static-prefix verbs:
``/ui/agents/grants/create`` and ``/ui/agents/grants/elevate`` MUST
register before ``/ui/agents/grants/{grant_id}`` because FastAPI
matches the first route whose path template fits, and ``{grant_id}``
would otherwise consume the literal ``"create"`` / ``"elevate"`` token
(surfacing as a 404 from the UUID-parse check). The ``/revoke`` route
carries an extra literal trailing segment, so its ordering relative to
the bare ``/ui/agents/grants/{grant_id}`` is not load-bearing.

The umbrella :func:`build_agent_grants_router` is mounted **before**
:func:`~meho_backplane.ui.routes.agents.build_agents_router` in
:func:`~meho_backplane.ui.routes.build_router` so ``/ui/agents/grants``
wins the first-match-wins lookup against the agents surface's
``/ui/agents/{name}`` (which would otherwise bind ``name="grants"``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.agents.grants.forms import (
    EXPIRES_AT_MAX,
    OP_PATTERN_MAX,
    PRINCIPAL_SUB_MAX,
    TARGET_SCOPE_MAX,
    render_create_modal,
    render_elevate_modal,
    render_revoke_modal,
    submit_create,
    submit_elevate,
    submit_revoke,
)
from meho_backplane.ui.routes.agents.grants.operator import resolve_grants_admin_or_403
from meho_backplane.ui.routes.agents.grants.views import (
    parse_grant_id_or_404,
    render_detail,
    render_index,
)

__all__ = ["build_agent_grants_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom mirroring
#: the agents / connectors routes (no calls in default argument
#: positions). The grants surface gates **every** route (reads
#: included) at tenant_admin, so there is one gate dep, not two.
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_grants_admin_or_403)

#: Filter-field length cap so the ``principal_sub`` query filter cannot
#: be used to push an unbounded string through the form-body parse.
_PRINCIPAL_FILTER_MAX: int = PRINCIPAL_SUB_MAX


async def _list_handler(
    request: Request,
    principal_sub: str | None = Query(default=None, max_length=_PRINCIPAL_FILTER_MAX),
    include_expired: bool = Query(default=False),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/grants`` -- table page or HTMX tbody fragment."""
    del operator  # gate only; the service read is tenant-scoped by session.
    return await render_index(
        request,
        session_ctx,
        principal_sub=principal_sub,
        include_expired=include_expired,
    )


async def _create_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/grants/create`` -- HTMX-loaded create modal fragment."""
    del operator  # gate only; render needs no operator-specific context.
    return await render_create_modal(request, session_ctx)


async def _create_submit_handler(
    request: Request,
    # ``Form(default="")`` (not ``Form(...)``) so an empty / omitted
    # submit flows to the ``AgentGrantCreate`` Pydantic validation
    # (which re-renders the modal with field errors) rather than
    # tripping FastAPI's own raw-422 boundary.
    principal_sub: str = Form(default="", max_length=PRINCIPAL_SUB_MAX),
    op_pattern: str = Form(default="", max_length=OP_PATTERN_MAX),
    target_scope: str | None = Form(default=None, max_length=TARGET_SCOPE_MAX),
    verdict: str = Form(default=""),
    expires_at: str | None = Form(default=None, max_length=EXPIRES_AT_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/grants/create`` -- create one permission grant."""
    return await submit_create(
        request,
        session_ctx,
        operator,
        principal_sub=principal_sub,
        op_pattern=op_pattern,
        target_scope=target_scope,
        verdict=verdict,
        expires_at=expires_at,
    )


async def _elevate_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/grants/elevate`` -- HTMX-loaded elevate modal fragment."""
    del operator  # gate only.
    return await render_elevate_modal(request, session_ctx)


async def _elevate_submit_handler(
    request: Request,
    principal_sub: str = Form(default="", max_length=PRINCIPAL_SUB_MAX),
    op_pattern: str = Form(default="", max_length=OP_PATTERN_MAX),
    target_scope: str | None = Form(default=None, max_length=TARGET_SCOPE_MAX),
    verdict: str = Form(default=""),
    expires_at: str | None = Form(default=None, max_length=EXPIRES_AT_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/grants/elevate`` -- create one time-bounded elevation."""
    return await submit_elevate(
        request,
        session_ctx,
        operator,
        principal_sub=principal_sub,
        op_pattern=op_pattern,
        target_scope=target_scope,
        verdict=verdict,
        expires_at=expires_at,
    )


async def _detail_handler(
    request: Request,
    grant_id: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/grants/{grant_id}`` -- per-grant detail."""
    del operator  # gate only.
    parsed = parse_grant_id_or_404(grant_id)
    return await render_detail(request, session_ctx, grant_id=parsed)


async def _revoke_modal_handler(
    request: Request,
    grant_id: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/grants/{grant_id}/revoke`` -- revoke-confirm modal."""
    del operator  # gate only.
    parsed = parse_grant_id_or_404(grant_id)
    return await render_revoke_modal(request, session_ctx, grant_id=parsed)


async def _revoke_submit_handler(
    grant_id: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/grants/{grant_id}/revoke`` -- revoke one grant."""
    parsed = parse_grant_id_or_404(grant_id)
    return await submit_revoke(session_ctx, operator, grant_id=parsed)


def _register_static_prefix_routes(router: APIRouter) -> None:
    """Wire the literal-prefix routes (create / elevate) onto *router*.

    Registration order is **load-bearing**: these must register before
    :func:`_register_parametrized_routes`. A request to
    ``/ui/agents/grants/create`` would otherwise route to
    ``/ui/agents/grants/{grant_id}`` with ``grant_id="create"`` -- a 404
    from the UUID-parse check instead of the intended modal fragment.
    """
    router.add_api_route(
        "/ui/agents/grants",
        _list_handler,
        methods=["GET"],
        name="ui_agent_grants_list",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/grants/create",
        _create_modal_handler,
        methods=["GET"],
        name="ui_agent_grants_create_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/grants/create",
        _create_submit_handler,
        methods=["POST"],
        name="ui_agent_grants_create_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/grants/elevate",
        _elevate_modal_handler,
        methods=["GET"],
        name="ui_agent_grants_elevate_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/grants/elevate",
        _elevate_submit_handler,
        methods=["POST"],
        name="ui_agent_grants_elevate_submit",
        response_class=HTMLResponse,
    )


def _register_parametrized_routes(router: APIRouter) -> None:
    """Wire the ``{grant_id}`` detail + revoke routes onto *router*.

    Registered after the static-prefix routes so the literal ``create``
    / ``elevate`` tokens are not consumed by ``{grant_id}``.
    """
    router.add_api_route(
        "/ui/agents/grants/{grant_id}",
        _detail_handler,
        methods=["GET"],
        name="ui_agent_grants_detail",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/grants/{grant_id}/revoke",
        _revoke_modal_handler,
        methods=["GET"],
        name="ui_agent_grants_revoke_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/grants/{grant_id}/revoke",
        _revoke_submit_handler,
        methods=["POST"],
        name="ui_agent_grants_revoke_submit",
        response_class=HTMLResponse,
    )


def build_agent_grants_router() -> APIRouter:
    """Construct the agent-grants UI :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the agents / memory / connectors convention. Registration order is
    load-bearing for the static-prefix ``create`` / ``elevate`` routes.
    """
    router = APIRouter(tags=["ui-agent-grants"])
    _register_static_prefix_routes(router)
    _register_parametrized_routes(router)
    return router
