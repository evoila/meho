# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/sensors`` -- the per-tenant Sensor registry ("which sensors exist?").

Task #2591, a follow-up to Initiative #2416 (the deterministic check layer).
The check layer shipped the Sensor registry (#2503), the latest-result
projection on the row, and the ``/ui/checks`` Dashboard rollup (#2506), but a
Sensor was visible in the console **only** if some Dashboard composed it -- a
registered-but-uncomposed Sensor was invisible. This surface adds the registry
read page: every Sensor in the tenant, each with its latest-result projection
(``last_state`` / ``last_value`` / ``last_evaluated_at`` / ``state_since``),
plus the same ``status`` + ``cadence_kind`` filters the Bearer
``GET /api/v1/sensors`` route (#2503) exposes.

The route serves two response shapes from one handler (the checks / runners
mould):

* **Full page** (normal browser navigation) -- the ``sensors/list.html`` page
  extending ``base.html``, including the filter bar.
* **HTMX fragment** (``HX-Request: true``) -- the ``sensors/_table_rows.html``
  partial. The full page's filter selects ``hx-get`` this route and swap only
  the table body, and the table arms an ``hx-trigger="every 30s"`` poll so the
  latest-result projection stays live. Filter state round-trips via ``status``
  / ``cadence_kind`` query params (``hx-push-url``) so the URL is shareable.

Reads at ``operator`` role via the in-process
:class:`~meho_backplane.checks.service.SensorAdminService` (the same ``list_``
the Bearer ``GET /api/v1/sensors`` route uses) rather than the REST surface,
because a browser carrying only the BFF session cookie cannot authenticate the
Bearer route. Tenant scoping is non-overrideable -- the service's first WHERE
clause is the session's ``tenant_id``; no query parameter carries a tenant id,
so a foreign tenant's Sensors never render. Read-only -- create / delete stay
on ``meho sensor`` (#2503); "edit" is delete + recreate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, get_args

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.checks.schemas import SensorCadenceFilter, SensorStatusFilter
from meho_backplane.checks.service import SensorAdminService
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.sensors.views import project_sensor_to_row
from meho_backplane.ui.templating import get_templates

__all__ = ["build_list_router"]

#: Hard cap on the Sensors a single list render considers. The registry page
#: is a glance surface; a tenant with more than this many Sensors has a
#: registration-sprawl problem the list view is not the place to page through.
_LIST_LIMIT = 200

#: Closed filter vocabularies, derived from the REST route's Literal filters so
#: the console and the Bearer surface stay lock-step. Rendered as the ``<option>``
#: sets in the filter bar and used to clamp a stale / hand-typed query param.
_STATUS_OPTIONS: Final[tuple[str, ...]] = get_args(SensorStatusFilter)
_CADENCE_OPTIONS: Final[tuple[str, ...]] = get_args(SensorCadenceFilter)

#: Module-level ``Depends`` closure -- ruff B008 idiom (no function calls in
#: default argument positions), matching the checks / runners routes.
_require_session_dep = Depends(require_ui_session)


def _is_htmx_request(request: Request) -> bool:
    """Return ``True`` when HTMX issued the request (``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def _clamp(value: str | None, allowed: tuple[str, ...]) -> str | None:
    """Clamp a raw filter query param to a known value (blank / unknown -> ``None``).

    The filter ``<select>``s only ever emit a known value or the empty "All"
    option, but a stale bookmark or hand-edited URL can carry anything; an
    unrecognised value renders unfiltered rather than 422-ing the page.
    """
    if value and value in allowed:
        return value
    return None


async def _render(
    request: Request,
    *,
    session_ctx: UISessionContext,
    status_filter: str | None,
    cadence_filter: str | None,
) -> HTMLResponse:
    """Render the registry page or the table-rows fragment.

    One tenant-scoped read (``SensorAdminService.list_``), narrowed by the
    clamped ``status`` / ``cadence_kind`` filters. Both branches receive the
    same context shape so the fragment template and the full-page template stay
    interchangeable; the full page additionally reads the filter option sets +
    the selected values to render the filter bar.
    """
    sensors = await SensorAdminService().list_(
        session_ctx.tenant_id,
        status=status_filter,
        cadence_kind=cadence_filter,
        limit=_LIST_LIMIT,
    )
    rows = [project_sensor_to_row(s) for s in sensors]
    context: dict[str, object] = {
        "page_title": "Sensors",
        "active_surface": "sensors",
        "rows": rows,
        "status_options": _STATUS_OPTIONS,
        "cadence_options": _CADENCE_OPTIONS,
        "status_filter": status_filter or "",
        "cadence_filter": cadence_filter or "",
        # Shared "now" so the relative-time macro stays consistent across rows
        # within one render.
        "now_utc": datetime.now(UTC),
    }
    template_name = "sensors/_table_rows.html" if _is_htmx_request(request) else "sensors/list.html"
    return get_templates().TemplateResponse(request, template_name, context)


def build_list_router() -> APIRouter:
    """Construct the sensors-registry :class:`APIRouter`.

    Factory function (not a module-level constant) so a test app can construct
    parallel routers without sharing route state -- the chassis convention
    every surface router follows. Registers the single ``GET /ui/sensors``
    route serving both the full page and the HTMX fragment from one handler.
    """
    router = APIRouter(tags=["ui-sensors"])

    async def _handler(
        request: Request,
        status: str | None = Query(default=None, max_length=64),
        cadence_kind: str | None = Query(default=None, max_length=64),
        session_ctx: UISessionContext = _require_session_dep,
    ) -> HTMLResponse:
        """Serve ``GET /ui/sensors[?status=&cadence_kind=]``. See module docstring."""
        return await _render(
            request,
            session_ctx=session_ctx,
            status_filter=_clamp(status, _STATUS_OPTIONS),
            cadence_filter=_clamp(cadence_kind, _CADENCE_OPTIONS),
        )

    router.add_api_route(
        "/ui/sensors",
        _handler,
        methods=["GET"],
        name="ui_sensors_list",
        response_class=HTMLResponse,
    )
    return router
