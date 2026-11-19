# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""First-start checks page: ``GET /ui/first-start``.

Shows the same list as ``GET /api/v1/first-start``
(:mod:`meho_backplane.first_start`). Read-only. It needs the ``operator`` role,
like the other console pages that run checks against systems. It names the
systems of the signed-in tenant that did not answer (of all tenants for a
platform admin), and it passes the signed-in person to the checks, so a
system whose login is in Vault is read with that person's token.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.first_start import reachability_scope, run_first_start_checks
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator
from meho_backplane.ui.roles import resolve_ui_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["build_first_start_router"]

_require_ui_operator_dep = Depends(require_ui_operator)


def build_first_start_router() -> APIRouter:
    """Construct the first-start page :class:`APIRouter`."""
    router = APIRouter(tags=["ui-first-start"])

    async def _page(
        request: Request,
        session_ctx: UISessionContext = _require_ui_operator_dep,
    ) -> HTMLResponse:
        """``GET /ui/first-start``."""
        operator = await resolve_ui_operator(session_ctx)
        tenant_id = session_ctx.tenant_id if operator is None else reachability_scope(operator)
        result = await run_first_start_checks(operator, tenant_id=tenant_id)
        context: dict[str, object] = {
            "page_title": "First-start checks",
            "active_surface": "",
            "result": result,
        }
        return get_templates().TemplateResponse(request, "first_start/index.html", context)

    router.add_api_route(
        "/ui/first-start",
        _page,
        methods=["GET"],
        name="ui_first_start",
        response_class=HTMLResponse,
    )
    return router
