# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agents UI route registration -- maps HTTP verbs to the render helpers.

Initiative #1824 (G10.8 Agents console), Task #1825 (T1). The route
handlers are thin wrappers: parse FastAPI params, resolve the session /
role dependency, and hand off to the render helpers in
:mod:`~meho_backplane.ui.routes.agents.views` (read) or
:mod:`~meho_backplane.ui.routes.agents.forms` (write). Splitting
registration from render logic keeps each module under the chassis-wide
size caps and gives the render helpers a unit-testable seam.

Route inventory
---------------

Read (operator-or-above; soft role probe for the affordance hints):

* ``GET /ui/agents`` -- list page or HTMX card-list fragment.
* ``GET /ui/agents/{name}`` -- detail page or HTMX body fragment.

Write (tenant_admin only; server-side 403 via ``resolve_operator_or_403``):

* ``GET  /ui/agents/create`` -- HTMX-loaded create modal.
* ``POST /ui/agents/create`` -- create submit.
* ``GET  /ui/agents/{name}/edit`` -- HTMX-loaded edit modal.
* ``PATCH /ui/agents/{name}`` -- edit submit.
* ``POST /ui/agents/{name}/toggle`` -- enable / disable.
* ``GET  /ui/agents/{name}/delete`` -- HTMX-loaded delete-confirm modal.
* ``POST /ui/agents/{name}/delete`` -- delete submit.

Registration order is **load-bearing** for the static-prefix verbs --
``/ui/agents/create`` MUST register before ``/ui/agents/{name}``
because FastAPI matches the first route whose path template fits, and
``{name}`` would otherwise consume the literal ``"create"`` token. The
parametrised ``/edit`` / ``/toggle`` / ``/delete`` routes carry an
extra literal trailing segment, so their ordering relative to the bare
``/ui/agents/{name}`` is not load-bearing; we still register them after
``create`` for the same readability convention the memory router uses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.agents.forms import (
    IDENTITY_REF_MAX,
    NAME_MAX,
    SYSTEM_PROMPT_MAX,
    TURN_BUDGET_MAX_LEN,
    render_create_modal,
    render_delete_modal,
    render_edit_modal,
    submit_create,
    submit_delete,
    submit_edit,
    submit_toggle,
)
from meho_backplane.ui.routes.agents.operator import (
    OperatorRoleProbe,
    resolve_operator_or_403,
    resolve_role_probe,
    resolve_run_operator_or_403,
)
from meho_backplane.ui.routes.agents.run import (
    INPUT_MAX,
    WORK_REF_MAX,
    render_run_console,
    stream_run_events,
    submit_run,
)
from meho_backplane.ui.routes.agents.principals_forms import (
    NAME_MAX as PRINCIPAL_NAME_MAX,
)
from meho_backplane.ui.routes.agents.principals_forms import (
    OWNER_SUB_MAX,
    render_register_modal,
    render_revoke_modal,
    submit_register,
    submit_revoke,
)
from meho_backplane.ui.routes.agents.principals_views import (
    render_principals_index,
    validate_principal_name,
)
from meho_backplane.ui.routes.agents.views import render_detail, render_index, validate_name

__all__ = ["build_agents_router"]

#: Module-level :class:`Depends` closures -- ruff B008 idiom mirroring
#: the chassis memory / connectors routes (no calls in default argument
#: positions).
_require_session_dep = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)
_require_admin_dep = Depends(resolve_operator_or_403)
_require_run_operator_dep = Depends(resolve_run_operator_or_403)


async def _list_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
) -> HTMLResponse:
    """``GET /ui/agents`` -- list page or HTMX card-list fragment."""
    return await render_index(request, session_ctx, is_tenant_admin=role_probe.is_tenant_admin)


async def _detail_handler(
    request: Request,
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
) -> HTMLResponse:
    """``GET /ui/agents/{name}`` -- detail page or HTMX body fragment."""
    validate_name(name)
    return await render_detail(
        request, session_ctx, name=name, is_tenant_admin=role_probe.is_tenant_admin
    )


async def _run_console_handler(
    request: Request,
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
) -> HTMLResponse:
    """``GET /ui/agents/{name}/run`` -- the run console page (operator read)."""
    del role_probe  # the page is reachable by any authenticated session.
    validate_name(name)
    return await render_run_console(request, session_ctx, name=name)


async def _run_submit_handler(
    request: Request,
    name: str,
    input_: str = Form(default="", alias="input", max_length=INPUT_MAX),
    work_ref: str | None = Form(default=None, max_length=WORK_REF_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_run_operator_dep,
) -> HTMLResponse:
    """``POST /ui/agents/{name}/run`` -- authorise a run (CSRF-gated, operator)."""
    validate_name(name)
    return await submit_run(
        request,
        session_ctx,
        operator,
        name=name,
        input_=input_,
        work_ref=work_ref,
    )


async def _run_stream_handler(
    request: Request,
    name: str,
    token: str = Query(default="", max_length=4096),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_run_operator_dep,
) -> StreamingResponse:
    """``GET /ui/agents/{name}/run/stream`` -- cookie-authed SSE bridge."""
    validate_name(name)
    return await stream_run_events(
        request,
        session_ctx,
        operator,
        name=name,
        token=token,
    )


async def _create_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/create`` -- HTMX-loaded create modal fragment."""
    del operator  # gate only; render needs no operator-specific context.
    return await render_create_modal(request, session_ctx)


async def _create_submit_handler(
    request: Request,
    # ``Form(default="")`` (not ``Form(...)``) so an empty / omitted
    # submit flows to the ``AgentDefinitionCreate`` Pydantic validation
    # (which re-renders the modal with field errors) rather than
    # tripping FastAPI's own raw-422 boundary.
    name: str = Form(default="", max_length=NAME_MAX),
    identity_ref: str = Form(default="", max_length=IDENTITY_REF_MAX),
    model_tier: str = Form(default="standard"),
    system_prompt: str = Form(default="", max_length=SYSTEM_PROMPT_MAX),
    turn_budget: str | None = Form(default=None, max_length=TURN_BUDGET_MAX_LEN),
    enabled: bool = Form(default=False),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/create`` -- create one agent definition.

    An unchecked ``enabled`` checkbox is omitted from the form body
    entirely (HTML checkbox semantics), so ``Form(default=False)`` lands
    the correct ``False``; a checked box posts the value and coerces to
    ``True``.
    """
    return await submit_create(
        request,
        session_ctx,
        operator,
        name=name,
        identity_ref=identity_ref,
        model_tier=model_tier,
        system_prompt=system_prompt,
        turn_budget=turn_budget,
        enabled=enabled,
    )


async def _edit_modal_handler(
    request: Request,
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/{name}/edit`` -- pre-populated edit modal fragment."""
    del operator  # gate only.
    validate_name(name)
    return await render_edit_modal(request, session_ctx, name=name)


async def _edit_submit_handler(
    request: Request,
    name: str,
    identity_ref: str = Form(default="", max_length=IDENTITY_REF_MAX),
    model_tier: str = Form(default="standard"),
    system_prompt: str = Form(default="", max_length=SYSTEM_PROMPT_MAX),
    turn_budget: str | None = Form(default=None, max_length=TURN_BUDGET_MAX_LEN),
    enabled: bool = Form(default=False),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``PATCH /ui/agents/{name}`` -- update one agent definition."""
    validate_name(name)
    return await submit_edit(
        request,
        session_ctx,
        operator,
        name=name,
        identity_ref=identity_ref,
        model_tier=model_tier,
        system_prompt=system_prompt,
        turn_budget=turn_budget,
        enabled=enabled,
    )


async def _toggle_handler(
    name: str,
    enabled: bool = Form(default=False),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/{name}/toggle`` -- enable / disable the agent."""
    validate_name(name)
    return await submit_toggle(session_ctx, operator, name=name, enabled=enabled)


async def _delete_modal_handler(
    request: Request,
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/{name}/delete`` -- HTMX-loaded delete-confirm modal."""
    del operator  # gate only.
    validate_name(name)
    return await render_delete_modal(request, session_ctx, name=name)


async def _delete_submit_handler(
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/{name}/delete`` -- delete one agent definition."""
    validate_name(name)
    return await submit_delete(session_ctx, operator, name=name)


async def _principals_list_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    role_probe: OperatorRoleProbe = _role_probe_dep,
    include_revoked: bool = Query(default=False),
) -> HTMLResponse:
    """``GET /ui/agents/principals`` -- list page or HTMX table fragment."""
    return await render_principals_index(
        request,
        session_ctx,
        is_tenant_admin=role_probe.is_tenant_admin,
        include_revoked=include_revoked,
    )


async def _principals_register_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/principals/register`` -- HTMX-loaded register modal."""
    del operator  # gate only; render needs no operator-specific context.
    return await render_register_modal(request, session_ctx)


async def _principals_register_submit_handler(
    request: Request,
    # ``Form(default="")`` (not ``Form(...)``) so an empty / omitted
    # submit flows to the service-layer name validation (which re-renders
    # the modal with the field error) rather than tripping FastAPI's own
    # raw-422 boundary.
    name: str = Form(default="", max_length=PRINCIPAL_NAME_MAX),
    owner_sub: str | None = Form(default=None, max_length=OWNER_SUB_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/principals/register`` -- register a new principal."""
    return await submit_register(
        request,
        session_ctx,
        operator,
        name=name,
        owner_sub=owner_sub,
    )


async def _principals_revoke_modal_handler(
    request: Request,
    name: str,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/principals/{name}/revoke`` -- kill-switch confirm modal."""
    del operator  # gate only.
    validate_principal_name(name)
    return await render_revoke_modal(request, session_ctx, name=name)


async def _principals_revoke_submit_handler(
    request: Request,
    name: str,
    confirm_name: str = Form(default="", max_length=PRINCIPAL_NAME_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/agents/principals/{name}/revoke`` -- revoke (kill switch)."""
    validate_principal_name(name)
    return await submit_revoke(
        request,
        session_ctx,
        operator,
        name=name,
        confirm_name=confirm_name,
    )


def _register_static_prefix_routes(router: APIRouter) -> None:
    """Wire the literal-prefix routes (``/ui/agents/create``) onto *router*.

    Registration order is **load-bearing**: these must register before
    :func:`_register_parametrized_routes`. A request to
    ``/ui/agents/create`` would otherwise route to ``/ui/agents/{name}``
    with ``name="create"`` -- a 404 from the validate-name check instead
    of the intended modal fragment.
    """
    router.add_api_route(
        "/ui/agents/create",
        _create_modal_handler,
        methods=["GET"],
        name="ui_agents_create_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/create",
        _create_submit_handler,
        methods=["POST"],
        name="ui_agents_create_submit",
        response_class=HTMLResponse,
    )


def _register_read_routes(router: APIRouter) -> None:
    """Wire the parametrised GET routes (list + detail) onto *router*."""
    router.add_api_route(
        "/ui/agents",
        _list_handler,
        methods=["GET"],
        name="ui_agents_list",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}",
        _detail_handler,
        methods=["GET"],
        name="ui_agents_detail",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}/edit",
        _edit_modal_handler,
        methods=["GET"],
        name="ui_agents_edit_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}/delete",
        _delete_modal_handler,
        methods=["GET"],
        name="ui_agents_delete_modal",
        response_class=HTMLResponse,
    )


def _register_run_routes(router: APIRouter) -> None:
    """Wire the run-console routes (T2 #1829) onto *router*.

    Three routes under the per-agent ``/run`` sub-path:

    * ``GET  /ui/agents/{name}/run`` -- the console page (operator read).
    * ``POST /ui/agents/{name}/run`` -- authorise a run (CSRF-gated).
    * ``GET  /ui/agents/{name}/run/stream`` -- the cookie-authed SSE
      bridge that proxies ``invoker.stream_events``.

    The ``/run`` literal segment sits one level below ``{name}`` and the
    ``/run/stream`` segment one below that, so neither collides with the
    bare ``/ui/agents/{name}`` detail route or the ``/edit`` / ``/delete``
    modal routes; ``{name}`` cannot consume the literal ``run`` token.
    """
    router.add_api_route(
        "/ui/agents/{name}/run",
        _run_console_handler,
        methods=["GET"],
        name="ui_agents_run_console",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}/run",
        _run_submit_handler,
        methods=["POST"],
        name="ui_agents_run_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}/run/stream",
        _run_stream_handler,
        methods=["GET"],
        name="ui_agents_run_stream",
        response_class=StreamingResponse,
    )


def _register_write_routes(router: APIRouter) -> None:
    """Wire the PATCH + POST (edit / toggle / delete) routes onto *router*.

    The PATCH route shares ``/ui/agents/{name}`` with the detail GET;
    FastAPI distinguishes by method so registration order is not
    load-bearing there.
    """
    router.add_api_route(
        "/ui/agents/{name}",
        _edit_submit_handler,
        methods=["PATCH"],
        name="ui_agents_edit_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}/toggle",
        _toggle_handler,
        methods=["POST"],
        name="ui_agents_toggle",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/{name}/delete",
        _delete_submit_handler,
        methods=["POST"],
        name="ui_agents_delete_submit",
        response_class=HTMLResponse,
    )


def _register_principals_routes(router: APIRouter) -> None:
    """Wire the agent-principals sub-surface routes (T4 #1831) onto *router*.

    Registration order is **load-bearing**: these literal-prefix routes
    (``/ui/agents/principals`` and ``/ui/agents/principals/register``)
    MUST register before :func:`_register_read_routes`, whose
    ``/ui/agents/{name}`` template would otherwise consume the literal
    ``"principals"`` token and route a principals request to the
    agent-definition detail handler (a 404 from its validate-name check).

    Read (operator-or-above; soft role probe for the affordance hints):

    * ``GET /ui/agents/principals`` -- principals list page / HTMX table.

    Write (tenant_admin only; server-side 403 via
    :func:`resolve_operator_or_403`):

    * ``GET  /ui/agents/principals/register`` -- register modal.
    * ``POST /ui/agents/principals/register`` -- register submit.
    * ``GET  /ui/agents/principals/{name}/revoke`` -- kill-switch modal.
    * ``POST /ui/agents/principals/{name}/revoke`` -- revoke submit.

    The ``/register`` literal registers before the ``{name}/revoke``
    parametrised pair; the trailing ``/revoke`` literal on the latter
    keeps the two from colliding, but the bare-prefix discipline matches
    the agent-definition router's convention.
    """
    router.add_api_route(
        "/ui/agents/principals",
        _principals_list_handler,
        methods=["GET"],
        name="ui_agent_principals_list",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/principals/register",
        _principals_register_modal_handler,
        methods=["GET"],
        name="ui_agent_principals_register_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/principals/register",
        _principals_register_submit_handler,
        methods=["POST"],
        name="ui_agent_principals_register_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/principals/{name}/revoke",
        _principals_revoke_modal_handler,
        methods=["GET"],
        name="ui_agent_principals_revoke_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/agents/principals/{name}/revoke",
        _principals_revoke_submit_handler,
        methods=["POST"],
        name="ui_agent_principals_revoke_submit",
        response_class=HTMLResponse,
    )


def build_agents_router() -> APIRouter:
    """Construct the agents UI :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- mirrors
    the memory / connectors convention. Registration order is
    load-bearing: the static-prefix ``/ui/agents/create`` route and the
    ``/ui/agents/principals*`` sub-surface routes (T4 #1831) both register
    before the parametrised ``/ui/agents/{name}`` read route so the
    literal segments are not bound as a ``{name}``.
    """
    router = APIRouter(tags=["ui-agents"])
    _register_static_prefix_routes(router)
    _register_principals_routes(router)
    _register_read_routes(router)
    _register_run_routes(router)
    _register_write_routes(router)
    return router
