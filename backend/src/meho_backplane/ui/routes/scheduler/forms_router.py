# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route registration for the create + cancel write surfaces (Task #1826).

Thin wrappers that parse FastAPI params + resolve the tenant_admin-gated
:class:`~meho_backplane.auth.operator.Operator` dependency, then hand off
to the render / submit helpers in
:mod:`~meho_backplane.ui.routes.scheduler.create` and
:mod:`~meho_backplane.ui.routes.scheduler.cancel`. Split from the render
logic so neither module exceeds the chassis ~600-line cap and the helpers
stay unit-testable without a FastAPI :class:`Request` fixture.

Route inventory (all tenant_admin-gated server-side via
:func:`~meho_backplane.ui.routes.scheduler.operator.resolve_operator_or_403`):

* ``GET  /ui/scheduler/create``              -- HTMX-loaded create modal.
* ``POST /ui/scheduler/validate-cron``       -- live cron validate + preview.
* ``POST /ui/scheduler/create``              -- create submit handler.
* ``GET  /ui/scheduler/{id}/cancel``         -- HTMX-loaded confirm modal.
* ``POST /ui/scheduler/{id}/cancel``         -- terminal cancel submit.

Registration order is **load-bearing**: this router is included **before**
the parametrised ``GET /ui/scheduler/{trigger_id}`` detail route (in the
umbrella :func:`~meho_backplane.ui.routes.scheduler.build_router`) so the
literal ``"create"`` / ``"validate-cron"`` tokens are never captured as a
trigger id by the detail handler.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.scheduler.cancel import render_cancel_modal, submit_cancel
from meho_backplane.ui.routes.scheduler.create import (
    create_trigger,
    render_create_modal,
    validate_cron,
)
from meho_backplane.ui.routes.scheduler.operator import resolve_operator_or_403

__all__ = ["build_forms_router"]

#: Module-level ``Depends`` closures -- ruff B008 idiom matching the
#: connectors / memory routes.
_require_session_dep = Depends(require_ui_session)
_require_admin_dep = Depends(resolve_operator_or_403)

#: Form-field length caps mirroring the
#: :class:`~meho_backplane.scheduler.schemas.ScheduledTriggerCreate` field
#: bounds. The server-side Pydantic validation is authoritative; these
#: caps bound the form-body parse against a paste-from-clipboard accident
#: before the bytes reach the schema.
_CRON_MAX = 128
_TIMEZONE_MAX = 64
_WORK_REF_MAX = 256
_FIRE_AT_MAX = 64
_JSON_MAX = 16384


async def _create_modal_handler(
    request: Request,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/scheduler/create`` -- HTMX-loaded create modal fragment."""
    del operator  # gate only; render needs no operator-specific context.
    return await render_create_modal(request, session_ctx)


async def _validate_cron_handler(
    request: Request,
    cron_expr: str = Form(default="", max_length=_CRON_MAX),
    timezone: str = Form(default="UTC", max_length=_TIMEZONE_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/scheduler/validate-cron`` -- live validate + next-fire preview.

    Tenant_admin-gated (it is part of the create flow); the operator
    argument is the gate only. Reuses the same croniter-backed helpers the
    wire schema validates with so the preview is byte-exact with submit.
    """
    del operator  # gate only.
    return await validate_cron(request, cron_expr=cron_expr, timezone=timezone)


async def _create_submit_handler(
    request: Request,
    # ``Form(default=...)`` (not ``Form(...)``) on the optional fields so an
    # omitted submit flows to the schema validation (which re-renders the
    # modal with a typed banner) rather than tripping FastAPI's raw 422.
    kind: str = Form(default="cron"),
    agent_definition_id: str = Form(default=""),
    cron_expr: str | None = Form(default=None, max_length=_CRON_MAX),
    timezone: str = Form(default="UTC", max_length=_TIMEZONE_MAX),
    fire_at: str | None = Form(default=None, max_length=_FIRE_AT_MAX),
    event_filter: str | None = Form(default=None, max_length=_JSON_MAX),
    inputs: str | None = Form(default=None, max_length=_JSON_MAX),
    in_flight_policy: str = Form(default="fail_into_audit"),
    work_ref: str | None = Form(default=None, max_length=_WORK_REF_MAX),
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/scheduler/create`` -- create one trigger via the in-process service."""
    return await create_trigger(
        request,
        session_ctx,
        operator,
        kind=kind,
        agent_definition_id=agent_definition_id,
        cron_expr=cron_expr,
        timezone=timezone,
        fire_at=fire_at,
        event_filter=event_filter,
        inputs=inputs,
        in_flight_policy=in_flight_policy,
        work_ref=work_ref,
    )


async def _cancel_modal_handler(
    request: Request,
    trigger_id: uuid.UUID,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/scheduler/{id}/cancel`` -- HTMX-loaded terminal-confirm modal."""
    del operator  # gate only.
    return await render_cancel_modal(request, session_ctx, trigger_id)


async def _cancel_submit_handler(
    request: Request,
    trigger_id: uuid.UUID,
    session_ctx: UISessionContext = _require_session_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``POST /ui/scheduler/{id}/cancel`` -- cancel one trigger (terminal)."""
    return await submit_cancel(request, session_ctx, operator, trigger_id)


def build_forms_router() -> APIRouter:
    """Construct the scheduler create + cancel forms :class:`APIRouter`.

    Factory function (chassis convention). The literal-prefix routes
    (``/ui/scheduler/create``, ``/ui/scheduler/validate-cron``) must
    register before the parametrised ``GET /ui/scheduler/{trigger_id}``
    detail route -- handled by the include order in
    :func:`~meho_backplane.ui.routes.scheduler.build_router`. The
    ``/ui/scheduler/{id}/cancel`` routes carry an extra literal segment
    so they never collide with the bare detail path.
    """
    router = APIRouter(tags=["ui-scheduler"])
    router.add_api_route(
        "/ui/scheduler/create",
        _create_modal_handler,
        methods=["GET"],
        name="ui_scheduler_create_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/scheduler/validate-cron",
        _validate_cron_handler,
        methods=["POST"],
        name="ui_scheduler_validate_cron",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/scheduler/create",
        _create_submit_handler,
        methods=["POST"],
        name="ui_scheduler_create_submit",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/scheduler/{trigger_id}/cancel",
        _cancel_modal_handler,
        methods=["GET"],
        name="ui_scheduler_cancel_modal",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/scheduler/{trigger_id}/cancel",
        _cancel_submit_handler,
        methods=["POST"],
        name="ui_scheduler_cancel_submit",
        response_class=HTMLResponse,
    )
    return router
