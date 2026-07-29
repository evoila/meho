# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/checks/{dashboard_id}`` -- the per-Dashboard detail surface.

Task #2506 under Initiative #2416 (parent goal #221). The detail page renders
the Dashboard's rolled-up five-state badge plus the member table: each member
Sensor with its raw + effective state, the *pending* marker (a failing state
held by the ``for:`` window), the held-since instant, the severity cap, and
the last observed value / evidence.

Read at ``operator`` role via the in-process
:class:`~meho_backplane.checks.dashboard_service.CheckDashboardAdminService`.
Tenant scoping is non-overrideable: the service's ``get`` returns ``None`` for
a cross-tenant / absent id, which this handler maps to 404
``dashboard_not_found`` -- the same existence-leak collapse the REST surface
relies on. Read-only in v1 (no write affordances).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meho_backplane.checks.dashboard_service import CheckDashboardAdminService
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.checks.views import project_detail
from meho_backplane.ui.templating import get_templates

__all__ = ["build_detail_router"]

_require_session_dep = Depends(require_ui_session)


def build_detail_router() -> APIRouter:
    """Construct the checks-detail :class:`APIRouter`.

    Factory function (chassis convention). The route is included **after** the
    literal ``GET /ui/checks`` list route (in the umbrella
    :func:`~meho_backplane.ui.routes.checks.build_checks_router`); the
    ``uuid.UUID`` path type 422s a non-UUID segment, and the include-order
    discipline keeps the literal list path from being shadowed.
    """
    router = APIRouter(tags=["ui-checks"])

    @router.get("/ui/checks/{dashboard_id}", response_class=HTMLResponse)
    async def checks_detail(
        request: Request,
        dashboard_id: uuid.UUID,
        session_ctx: UISessionContext = _require_session_dep,
    ) -> HTMLResponse:
        """Render the per-Dashboard detail page (rollup badge + member table)."""
        service = CheckDashboardAdminService()
        detail = await service.get(session_ctx.tenant_id, dashboard_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="dashboard_not_found",
            )
        context: dict[str, object] = {
            "page_title": "Dashboard",
            "active_surface": "checks",
            "dashboard": project_detail(detail),
        }
        return get_templates().TemplateResponse(request, "checks/detail.html", context)

    return router
