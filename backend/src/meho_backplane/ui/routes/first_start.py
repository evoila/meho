# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""First-start checks page: ``GET /ui/first-start``.

Shows the same list as the public ``GET /first-start`` endpoint
(:mod:`meho_backplane.first_start`). Read-only. Any signed-in operator can
open it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from meho_backplane.first_start import run_first_start_checks
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.templating import get_templates

__all__ = ["build_first_start_router"]

_require_ui_session_dep = Depends(require_ui_session)


def build_first_start_router() -> APIRouter:
    """Construct the first-start page :class:`APIRouter`."""
    router = APIRouter(tags=["ui-first-start"])

    async def _page(
        request: Request,
        _session_ctx: UISessionContext = _require_ui_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/first-start``."""
        result = await run_first_start_checks()
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
