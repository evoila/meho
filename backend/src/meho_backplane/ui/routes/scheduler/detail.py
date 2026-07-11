# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/scheduler/{trigger_id}`` -- the per-trigger detail surface.

Initiative #1824 (G10.8 Autonomous execution control plane), Task #1826
(T6). The detail page renders the full
:class:`~meho_backplane.scheduler.schemas.ScheduledTriggerRead` row: the
discriminator (``kind`` + ``cron_expr`` / ``fire_at`` / ``event_filter``),
the lifecycle (``status``, ``next_fire_at``, ``last_fired_at``), the
governance fields (``in_flight_policy``, ``identity_sub``,
``created_by_sub``), the ``inputs`` blob (pretty JSON), and the
``work_ref`` chip.

Read at ``operator`` role via the in-process
:class:`~meho_backplane.scheduler.service.SchedulerAdminService` (the same
``get`` the Bearer ``DELETE`` route pre-flights with). Tenant scoping is
non-overrideable: the service's ``get`` returns ``None`` for a
cross-tenant / absent id, which this handler maps to 404
``trigger_not_found`` -- the same existence-leak collapse the REST surface
relies on, so a trigger id typed into the URL bar that belongs to another
tenant is indistinguishable from one that does not exist.

The cancel button is tenant_admin-gated. The gate runs server-side in the
:mod:`~meho_backplane.ui.routes.scheduler.cancel` handler; the template
hides the button when the session is not a tenant_admin so the affordance
only appears to operators who can use it. The button is additionally
hidden when the trigger is already terminal (``cancelled`` / ``fired``) --
a cancelled or fired trigger cannot be cancelled again.

Recent fires
------------

A trigger carries no ``trigger_id -> run`` back-link in the substrate
(documented gap in the issue body). When the trigger has a ``work_ref``
set, the detail page surfaces a link to the work_ref-correlated agent runs
(``/ui/agents`` runs filtered by ``work_ref``) so an operator can pivot to
the runs the trigger dispatched. Absent a ``work_ref`` the panel is
omitted (no correlation key exists).
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.scheduler.schemas import ScheduledTriggerRead
from meho_backplane.scheduler.service import SchedulerAdminService
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.scheduler.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.routes.scheduler.views import (
    coerce_utc_aware,
    status_badge_class,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_detail_router"]

#: Terminal statuses -- a trigger in one of these can never be cancelled
#: again (the cancel transition matches ``active`` / ``paused`` only).
_TERMINAL_STATUSES = frozenset({"cancelled", "fired"})

_require_session_dep = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)


def _pretty_json(value: dict[str, object] | None) -> str | None:
    """Render a JSON blob as indented text for the detail page (``None`` -> ``None``).

    ``inputs`` and ``event_filter`` are free-shape dicts; rendering them
    sorted + 2-space-indented gives the operator a stable, diff-friendly
    view. ``sort_keys`` keeps the rendering deterministic across renders.
    """
    if value is None:
        return None
    return json.dumps(value, indent=2, sort_keys=True, default=str)


async def _resolve_agent_name(tenant_id: uuid.UUID, agent_definition_id: uuid.UUID) -> str | None:
    """Resolve the agent-definition name for the trigger's ``agent_definition_id``.

    The detail page shows one trigger, so a targeted list-and-match is
    cheaper than loading every definition. ``None`` (definition deleted
    after the trigger was created) lets the template fall back to the raw
    id.
    """
    agents = AgentDefinitionService()
    definitions = await agents.list_(tenant_id, limit=500)
    for definition in definitions:
        if definition.id == agent_definition_id:
            return definition.name
    return None


def _build_context(
    trigger: ScheduledTriggerRead,
    *,
    agent_name: str | None,
    is_tenant_admin: bool,
    csrf_token: str,
) -> dict[str, object]:
    """Assemble the detail-page template context from a trigger row."""
    is_terminal = trigger.status.value in _TERMINAL_STATUSES
    return {
        "page_title": "Scheduled trigger",
        "active_surface": "scheduler",
        "trigger": {
            "id": str(trigger.id),
            "kind": trigger.kind.value,
            "status": trigger.status.value,
            "status_badge": status_badge_class(trigger.status.value),
            "cron_expr": trigger.cron_expr,
            "timezone": trigger.timezone,
            "fire_at": coerce_utc_aware(trigger.fire_at),
            "event_filter_json": _pretty_json(trigger.event_filter),
            "in_flight_policy": trigger.in_flight_policy.value,
            "next_fire_at": coerce_utc_aware(trigger.next_fire_at),
            "last_fired_at": coerce_utc_aware(trigger.last_fired_at),
            # Skip-state projection (#2327) -- surfaces the silent-skip
            # loop on the row so a healthy-looking 'active' trigger that
            # is actually skipping every tick is visible to the operator.
            "last_skip_reason": trigger.last_skip_reason,
            "last_skipped_at": coerce_utc_aware(trigger.last_skipped_at),
            "skip_count": trigger.skip_count,
            "inputs_json": _pretty_json(trigger.inputs),
            "identity_sub": trigger.identity_sub,
            "created_by_sub": trigger.created_by_sub,
            "work_ref": trigger.work_ref,
            "agent_name": agent_name,
            "agent_definition_id": str(trigger.agent_definition_id),
            "created_at": coerce_utc_aware(trigger.created_at),
            "updated_at": coerce_utc_aware(trigger.updated_at),
        },
        # Cancel is tenant_admin-only AND only meaningful on a
        # non-terminal trigger. Both conditions gate the button; the
        # server-side cancel handler re-checks the role regardless.
        "can_cancel": is_tenant_admin and not is_terminal,
        "is_terminal": is_terminal,
        "csrf_token": csrf_token,
    }


def build_detail_router() -> APIRouter:
    """Construct the scheduler-detail :class:`APIRouter`.

    Factory function (chassis convention). The route is included **after**
    the literal ``/ui/scheduler/create`` route (in the umbrella
    :func:`~meho_backplane.ui.routes.scheduler.build_router`) so the
    first-match-wins lookup never binds ``"create"`` as the
    ``{trigger_id}`` path parameter; the ``uuid.UUID`` path type also
    422s a non-UUID segment, but the include-order discipline is the
    primary guard.
    """
    router = APIRouter(tags=["ui-scheduler"])

    @router.get("/ui/scheduler/{trigger_id}", response_class=HTMLResponse)
    async def scheduler_detail(
        request: Request,
        trigger_id: uuid.UUID,
        session_ctx: UISessionContext = _require_session_dep,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Render the per-trigger detail page (full row + cancel affordance)."""
        scheduler = SchedulerAdminService()
        trigger = await scheduler.get(session_ctx.tenant_id, trigger_id)
        if trigger is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="trigger_not_found",
            )
        agent_name = await _resolve_agent_name(session_ctx.tenant_id, trigger.agent_definition_id)
        csrf_token = mint_csrf_token(str(session_ctx.session_id))
        context = _build_context(
            trigger,
            agent_name=agent_name,
            is_tenant_admin=role_probe.is_tenant_admin,
            csrf_token=csrf_token,
        )
        response = get_templates().TemplateResponse(request, "scheduler/detail.html", context)
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,
            secure=True,
            samesite="strict",
            path="/ui",
        )
        return response

    return router
