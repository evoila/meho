# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/connectors/overrides`` -- the operation override report (#4711).

The console page for the report "operations running with reduced
approval": every operation a platform admin turned approval off for, with
connector, operation, risk level, who, when, reason and scope.
``?show=all`` lists every operator override of a risk level or approval
flag instead. The rows come from the same loader the REST route
``GET /api/v1/connectors/operation-overrides`` uses, so the two always
agree. Gated to ``tenant_admin``, like the REST route.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.api.v1.operation_overrides import load_override_rows
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.routes.connectors.operator import resolve_operator_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["build_overrides_router"]

_require_admin_dep = Depends(resolve_operator_or_403)
_show_query = Query(default="reduced")


async def _overrides_page(
    request: Request,
    show: Literal["reduced", "all"] = _show_query,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """Render the override report page."""
    rows = await load_override_rows(operator, reduced_approval=show == "reduced")
    return get_templates().TemplateResponse(
        request,
        "connectors/overrides.html",
        {"rows": [row.model_dump() for row in rows], "show": show},
    )


def build_overrides_router() -> APIRouter:
    """Construct the override report router (a factory, like its siblings)."""
    router = APIRouter(tags=["ui-connectors"])
    router.add_api_route(
        "/ui/connectors/overrides",
        _overrides_page,
        methods=["GET"],
        name="ui_connectors_overrides",
        response_class=HTMLResponse,
    )
    return router
