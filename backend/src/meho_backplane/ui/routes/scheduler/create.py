# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Create-trigger modal render + submit + live-validate helpers (Task #1826).

Initiative #1824 (G10.8 Autonomous execution control plane), Task #1826
(T6). Three concerns live here, all tenant_admin-gated server-side:

* :func:`render_create_modal` -- ``GET /ui/scheduler/create``. Renders the
  HTMX-loaded ``<dialog>`` with the agent-definition dropdown (populated
  from :meth:`~meho_backplane.agents.service.AgentDefinitionService.list_`),
  the Alpine kind-switch (``cron`` / ``one_off`` / ``event``), and the
  shared ``inputs`` / ``in_flight_policy`` / ``work_ref`` fields. Mints a
  fresh CSRF token and re-sets the ``meho_csrf`` cookie on the same
  response so the modal's own ``hx-headers`` echo lines up after the swap
  (the #1693 / #1754 cookie-desync class).

* :func:`validate_cron` -- ``POST /ui/scheduler/validate-cron``. The
  server-side validate-as-you-type endpoint: reuses
  :func:`~meho_backplane.scheduler.cron.is_valid_cron_expr` +
  :func:`~meho_backplane.scheduler.cron.next_fire_after` to render a live
  validity + next-fire preview fragment so the operator never submits a
  free-text cron expression with no feedback. No new dependency -- the
  same croniter seam the wire-schema validator uses.

* :func:`create_trigger` -- ``POST /ui/scheduler/create``. Builds a
  :class:`~meho_backplane.scheduler.schemas.ScheduledTriggerCreate` from
  the form (the discriminated-union validator surfaces a bad kind/field
  combination as a 422 with a clear message), then persists via the
  in-process :class:`~meho_backplane.scheduler.service.SchedulerAdminService`
  -- the same ``create`` the Bearer ``POST /api/v1/scheduler/triggers``
  route calls. Success returns 204 + ``HX-Redirect: /ui/scheduler`` so
  HTMX navigates back to the list with the new trigger visible (newest
  first). A validation / FK failure re-renders the modal with a typed
  banner rather than tearing the operator out of the flow.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import (
    ScheduledTriggerInFlightPolicy,
    ScheduledTriggerKind,
)
from meho_backplane.scheduler.cron import (
    InvalidCronExpressionError,
    InvalidTimezoneError,
    is_valid_cron_expr,
    next_fire_after,
)
from meho_backplane.scheduler.schemas import ScheduledTriggerCreate
from meho_backplane.scheduler.service import (
    AgentDefinitionMissingError,
    SchedulerAdminService,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.templating import get_templates

__all__ = [
    "create_trigger",
    "render_create_modal",
    "validate_cron",
]

_log = structlog.get_logger(__name__)


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Re-set the ``meho_csrf`` cookie on a modal-rendering response.

    A modal render mints a fresh token; the cookie must be rotated in
    lockstep or the double-submit pair desyncs (#1693 / #1754). Same
    attributes the list / connectors / approvals renders use.
    """
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


async def render_create_modal(
    request: Request,
    session_ctx: UISessionContext,
) -> HTMLResponse:
    """Render the HTMX-loaded create-trigger modal fragment.

    The agent dropdown is populated from the tenant's agent definitions;
    when the tenant has none, the modal renders an empty-state hint
    (a trigger needs a definition to dispatch) rather than a dropdown
    with no options. The in_flight_policy radio defaults to
    ``fail_into_audit`` per the consumer doc.
    """
    agents = AgentDefinitionService()
    definitions = await agents.list_(session_ctx.tenant_id, limit=500)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "csrf_token": csrf_token,
        "agent_definitions": [
            {"id": str(d.id), "name": d.name, "enabled": d.enabled} for d in definitions
        ],
        "kind_values": [k.value for k in ScheduledTriggerKind],
        "in_flight_policy_values": [p.value for p in ScheduledTriggerInFlightPolicy],
        "in_flight_policy_default": ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value,
        "error_message": None,
    }
    response = get_templates().TemplateResponse(request, "scheduler/_create_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def validate_cron(
    request: Request,
    *,
    cron_expr: str,
    timezone: str,
) -> HTMLResponse:
    """Render the live cron-validity + next-fire preview fragment.

    Server-side validate-as-you-type: reuses the same croniter-backed
    helpers the wire schema validates with, so the preview is byte-exact
    with what a submit will accept. An empty expression renders a neutral
    "enter a cron expression" hint (the operator is still typing); a valid
    expression renders the computed ``next_fire_at`` (in UTC, the column's
    storage tz); an invalid expression or bad timezone renders the typed
    error so the operator fixes it before submit.
    """
    expr = cron_expr.strip()
    tz = timezone.strip() or "UTC"
    context: dict[str, object] = {
        "cron_expr": expr,
        "timezone": tz,
        "valid": False,
        "next_fire_at": None,
        "error": None,
    }
    if not expr:
        context["error"] = None  # neutral hint, not an error
        return get_templates().TemplateResponse(request, "scheduler/_cron_preview.html", context)
    if not is_valid_cron_expr(expr):
        context["error"] = "Not a valid 5-field cron expression."
        return get_templates().TemplateResponse(request, "scheduler/_cron_preview.html", context)
    try:
        preview = next_fire_after(expr, datetime.now(UTC), tz)
    except InvalidTimezoneError:
        context["error"] = f"Unknown timezone: {tz!r}"
        return get_templates().TemplateResponse(request, "scheduler/_cron_preview.html", context)
    except InvalidCronExpressionError:
        # Defence-in-depth: is_valid_cron_expr already passed, but the
        # arithmetic path validates again; treat a divergence as invalid.
        context["error"] = "Not a valid 5-field cron expression."
        return get_templates().TemplateResponse(request, "scheduler/_cron_preview.html", context)
    context["valid"] = True
    context["next_fire_at"] = preview
    return get_templates().TemplateResponse(request, "scheduler/_cron_preview.html", context)


def _parse_json_field(raw: str | None, *, field_name: str) -> dict[str, object] | None:
    """Parse an optional JSON-object form field, or 422 on malformed input.

    ``inputs`` and ``event_filter`` arrive as JSON text from a textarea.
    Empty / whitespace -> ``None`` (omitted). A non-object or unparseable
    value surfaces as a 422 the caller maps to a re-rendered modal banner.
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be a JSON object",
        )
    return parsed


def _parse_fire_at(raw: str | None) -> datetime | None:
    """Parse the ``one_off`` ``fire_at`` datetime-local field.

    The browser ``<input type="datetime-local">`` posts an ISO-8601
    string without a timezone offset (e.g. ``2026-07-01T09:30``). Treat a
    naive value as UTC wall-clock (the column's storage tz) so the trigger
    fires at the instant the operator intends; an offset-carrying value is
    honoured as-is. ``None`` / empty -> ``None`` (the schema validator then
    rejects a one-off with no fire_at as 422).
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="fire_at must be an ISO-8601 datetime",
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _build_create_payload(
    *,
    kind_raw: str,
    agent_definition_id: str,
    cron_expr: str | None,
    timezone: str,
    fire_at_raw: str | None,
    event_filter_raw: str | None,
    inputs_raw: str | None,
    in_flight_policy: str,
    work_ref: str | None,
) -> ScheduledTriggerCreate:
    """Build + validate a :class:`ScheduledTriggerCreate` from the form fields.

    Maps each form field onto the wire schema, letting the schema's
    discriminated-union + cron + timezone validators do the heavy lifting
    (a bad kind/field combination, an invalid cron expression, or an
    unknown timezone all surface as a :class:`ValidationError` the caller
    maps to a re-rendered modal banner). Per-kind null-ing: only the
    discriminator field for the chosen ``kind`` is populated so the
    "must leave the other fields null" invariant holds even when the
    Alpine kind-switch left stale values in hidden inputs.
    """
    try:
        kind = ScheduledTriggerKind(kind_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown trigger kind: {kind_raw!r}",
        ) from exc
    try:
        agent_id = uuid.UUID(agent_definition_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="agent_definition_id must be a UUID",
        ) from exc
    try:
        policy = ScheduledTriggerInFlightPolicy(in_flight_policy)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown in_flight_policy: {in_flight_policy!r}",
        ) from exc

    inputs = _parse_json_field(inputs_raw, field_name="inputs")
    work_ref_clean = work_ref.strip() if work_ref and work_ref.strip() else None

    # Populate only the discriminator field for the chosen kind so the
    # schema's exactly-one-discriminator invariant holds regardless of any
    # stale hidden-input values the kind-switch left behind.
    cron_value: str | None = None
    fire_at_value: datetime | None = None
    event_filter_value: dict[str, object] | None = None
    if kind == ScheduledTriggerKind.CRON:
        cron_value = cron_expr.strip() if cron_expr and cron_expr.strip() else None
    elif kind == ScheduledTriggerKind.ONE_OFF:
        fire_at_value = _parse_fire_at(fire_at_raw)
    else:  # ScheduledTriggerKind.EVENT
        event_filter_value = _parse_json_field(event_filter_raw, field_name="event_filter")

    try:
        return ScheduledTriggerCreate(
            kind=kind,
            agent_definition_id=agent_id,
            cron_expr=cron_value,
            fire_at=fire_at_value,
            event_filter=event_filter_value,
            timezone=timezone.strip() or "UTC",
            inputs=inputs,
            in_flight_policy=policy,
            work_ref=work_ref_clean,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_first_validation_message(exc),
        ) from exc


def _first_validation_message(exc: ValidationError) -> str:
    """Extract a single human message from a Pydantic ValidationError.

    The discriminated-union + cron validators raise clear ``ValueError``
    messages ("cron triggers require cron_expr", "invalid cron
    expression: ..."); surface the first so the modal banner is
    actionable without dumping the full error chain.
    """
    errors = exc.errors()
    if errors:
        msg = errors[0].get("msg", "")
        # Pydantic prefixes "Value error, " on raised ValueErrors; strip it.
        return str(msg).removeprefix("Value error, ") or "invalid trigger payload"
    return "invalid trigger payload"


async def _rerender_modal_with_error(
    request: Request,
    session_ctx: UISessionContext,
    *,
    error_message: str,
) -> HTMLResponse:
    """Re-render the create modal carrying a typed error banner.

    Re-mints + re-sets the CSRF token (the failed submit consumed the
    prior one) so the operator's next submit attempt lines up.
    """
    agents = AgentDefinitionService()
    definitions = await agents.list_(session_ctx.tenant_id, limit=500)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "csrf_token": csrf_token,
        "agent_definitions": [
            {"id": str(d.id), "name": d.name, "enabled": d.enabled} for d in definitions
        ],
        "kind_values": [k.value for k in ScheduledTriggerKind],
        "in_flight_policy_values": [p.value for p in ScheduledTriggerInFlightPolicy],
        "in_flight_policy_default": ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value,
        "error_message": error_message,
    }
    response = get_templates().TemplateResponse(request, "scheduler/_create_modal.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _bind_audit_for_create(*, session_ctx: UISessionContext, kind: str) -> None:
    """Bind contextvars so the chassis audit + broadcast hooks fire.

    Mirrors the Bearer ``POST /api/v1/scheduler/triggers`` route's binding
    so the UI-driven create commits an audit row under the canonical
    ``scheduler.create`` op id (the UI session middleware does not bind
    these contextvars itself).
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        audit_op_id="scheduler.create",
        audit_op_class="write",
        audit_trigger_kind=kind,
    )


async def create_trigger(
    request: Request,
    session_ctx: UISessionContext,
    operator: Operator,
    *,
    kind: str,
    agent_definition_id: str,
    cron_expr: str | None,
    timezone: str,
    fire_at: str | None,
    event_filter: str | None,
    inputs: str | None,
    in_flight_policy: str,
    work_ref: str | None,
) -> HTMLResponse:
    """Persist a new scheduled trigger and HX-Redirect to the list.

    Submit handler for ``POST /ui/scheduler/create``. Builds the wire
    schema (validation errors re-render the modal with a banner), then
    persists via the in-process service (the same ``create`` the REST
    route calls). Success -> 204 + ``HX-Redirect: /ui/scheduler``.
    """
    try:
        payload = _build_create_payload(
            kind_raw=kind,
            agent_definition_id=agent_definition_id,
            cron_expr=cron_expr,
            timezone=timezone,
            fire_at_raw=fire_at,
            event_filter_raw=event_filter,
            inputs_raw=inputs,
            in_flight_policy=in_flight_policy,
            work_ref=work_ref,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return await _rerender_modal_with_error(request, session_ctx, error_message=detail)

    _bind_audit_for_create(session_ctx=session_ctx, kind=payload.kind.value)

    service = SchedulerAdminService()
    try:
        entry = await service.create(
            tenant_id=operator.tenant_id,
            created_by_sub=operator.sub,
            payload=payload,
        )
    except AgentDefinitionMissingError:
        return await _rerender_modal_with_error(
            request,
            session_ctx,
            error_message="No agent definition with that id exists in this tenant.",
        )

    structlog.contextvars.bind_contextvars(audit_trigger_id=str(entry.id))
    _log.info(
        "ui_scheduler_create",
        tenant_id=str(session_ctx.tenant_id),
        operator_sub=session_ctx.operator_sub,
        trigger_id=str(entry.id),
        kind=payload.kind.value,
    )
    return HTMLResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"HX-Redirect": "/ui/scheduler"},
    )
