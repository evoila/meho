# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/scheduler`` -- the per-tenant scheduled-trigger list.

Initiative #1824 (G10.8 Autonomous execution control plane), Task #1826
(T6). The route serves two response shapes from one handler (mirroring
:mod:`meho_backplane.ui.routes.connectors.list_view`):

* **Full page** (normal browser navigation) -- the ``scheduler/list.html``
  page extending ``base.html``.
* **HTMX fragment** (``HX-Request: true``) -- the
  ``scheduler/_table_rows.html`` partial so a filter change re-renders
  only the table body.

The list is read at ``operator`` role: it calls the in-process
:class:`~meho_backplane.scheduler.service.SchedulerAdminService` (the same
service the Bearer ``GET /api/v1/scheduler/triggers`` route uses) rather
than the REST surface, because a browser carrying only the BFF session
cookie cannot authenticate the Bearer route. Tenant scoping is
non-overrideable -- the service's first WHERE clause is the session's
``tenant_id``; no query parameter carries a tenant id (cross-tenant
``tenant_filter`` is platform_admin-only and needs the tenant selector,
out of scope for this task).

URL contract::

    GET /ui/scheduler
        [?kind=cron|one_off|event
         &status=active|paused|cancelled|fired
         &work_ref=<exact-match string>]

Out-of-enum ``kind`` / ``status`` -> 422 (the ``StrEnum`` query validator
at the HTTP boundary). The agent-definition names that label each row are
resolved once per render via
:meth:`~meho_backplane.agents.service.AgentDefinitionService.list_` and
mapped by id, so a busy list does not issue one lookup per row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.scheduler.service import SchedulerAdminService
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.scheduler.operator import (
    OperatorRoleProbe,
    resolve_role_probe,
)
from meho_backplane.ui.routes.scheduler.views import (
    KindFilterValue,
    StatusFilterValue,
    build_agent_name_map,
    project_trigger_to_view,
)
from meho_backplane.ui.templating import get_templates

__all__ = ["build_list_router"]

#: Hard cap on the triggers a single list render considers. The scheduler
#: list is a glance + manage surface; a tenant with more than this many
#: triggers has a scheduling-sprawl problem the list view is not the place
#: to page through. Matches the ``SchedulerAdminService.list_`` default
#: list limit posture.
_LIST_LIMIT = 200

#: Module-level ``Depends`` closures -- ruff B008 idiom (no function calls
#: in default argument positions), matching the connectors / memory routes.
_require_session_dep = Depends(require_ui_session)
_role_probe_dep = Depends(resolve_role_probe)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``).

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives drive
    (https://htmx.org/reference/#request_headers). The handler branches on
    this header to decide between the full page (browser nav) and the
    table-rows fragment (filter swap). Case-insensitive per the HTTP spec.
    """
    return request.headers.get("hx-request", "").lower() == "true"


async def _render(
    request: Request,
    *,
    kind: KindFilterValue | None,
    status_filter: StatusFilterValue | None,
    work_ref: str | None,
    session_ctx: UISessionContext,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the list page or the table-rows fragment.

    Both branches receive the same context shape so the fragment template
    and the full-page template stay interchangeable.
    """
    scheduler = SchedulerAdminService()
    triggers = await scheduler.list_(
        session_ctx.tenant_id,
        kind=kind.value if kind is not None else None,
        status=status_filter.value if status_filter is not None else None,
        work_ref=work_ref or None,
        limit=_LIST_LIMIT,
    )
    # Resolve the agent-definition names once for the whole page (not per
    # row) so the rendered table labels each trigger with its agent's name
    # rather than a bare UUID.
    agents = AgentDefinitionService()
    definitions = await agents.list_(session_ctx.tenant_id, limit=_LIST_LIMIT)
    agent_names = build_agent_name_map(definitions)
    rows = [project_trigger_to_view(t, agent_names=agent_names) for t in triggers]

    now = datetime.now(UTC)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Scheduler",
        "active_surface": "scheduler",
        "rows": rows,
        # Shared "now" so the relative-time macro stays consistent across
        # rows within one render (per-row datetime.now would drift).
        "now_utc": now,
        "kind_options": [k.value for k in KindFilterValue],
        "status_options": [s.value for s in StatusFilterValue],
        "kind_filter": kind.value if kind is not None else "",
        "status_filter": status_filter.value if status_filter is not None else "",
        "work_ref_filter": work_ref or "",
        "csrf_token": csrf_token,
        # tenant_admin gate for the "Create trigger" button + per-row
        # "Cancel" affordance. The create / cancel routes re-check the role
        # server-side via ``resolve_operator_or_403``; the template hides
        # the affordance from operators who can't use it. Fails soft to
        # ``False`` (button hidden) on a transient JWT-validation hiccup.
        "is_tenant_admin": is_tenant_admin,
    }
    template_name = (
        "scheduler/_table_rows.html" if _is_htmx_request(request) else "scheduler/list.html"
    )
    response = get_templates().TemplateResponse(request, template_name, context)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )
    return response


def build_list_router() -> APIRouter:
    """Construct the scheduler-list :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can
    construct parallel routers without sharing route state -- the chassis
    convention every surface router follows. Registers the single
    ``GET /ui/scheduler`` route serving both the full page and the HTMX
    fragment from one handler.
    """
    router = APIRouter(tags=["ui-scheduler"])

    async def _handler(
        request: Request,
        kind: KindFilterValue | None = Query(default=None),
        status: StatusFilterValue | None = Query(default=None),
        work_ref: str | None = Query(default=None, max_length=256),
        session_ctx: UISessionContext = _require_session_dep,
        role_probe: OperatorRoleProbe = _role_probe_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/scheduler``. See module docstring for the URL contract."""
        return await _render(
            request,
            kind=kind,
            status_filter=status,
            work_ref=work_ref,
            session_ctx=session_ctx,
            is_tenant_admin=role_probe.is_tenant_admin,
        )

    router.add_api_route(
        "/ui/scheduler",
        _handler,
        methods=["GET"],
        name="ui_scheduler_list",
        response_class=HTMLResponse,
    )
    return router
