# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The live grants of an agent, next to the starting grant pick of the agent form.

``GET /ui/agents/principals/live-grants?identity_ref=<agent>`` answers an
HTML fragment. The create and edit forms of an agent definition load it
when they open and each time the person changes the identity reference.
When that agent already has live grants, the fragment lists them with the
sentence "This agent already has these grants; no starting grant is
needed." Else it is empty. The data comes from
:func:`~meho_backplane.agents.live_grants.load_live_grants`, the same
list the REST, MCP and CLI answers show.

The route is for a tenant admin, the same gate as the forms. It reads
only the caller's tenant.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from meho_backplane.agents.live_grants import load_live_grants
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_operator
from meho_backplane.ui.routes.agents.operator import resolve_operator_or_403
from meho_backplane.ui.templating import get_templates

__all__ = ["PATH", "register_live_grants_route"]

#: Two segments below ``/ui/agents``, so no agent name can shadow it.
PATH: Final[str] = "/ui/agents/principals/live-grants"

#: The same cap as the identity reference field of the forms.
_IDENTITY_REF_MAX: Final[int] = 256

_require_operator_dep = Depends(require_ui_operator)
_require_admin_dep = Depends(resolve_operator_or_403)


async def _live_grants_handler(
    request: Request,
    identity_ref: str = Query(default="", max_length=_IDENTITY_REF_MAX),
    session_ctx: UISessionContext = _require_operator_dep,
    operator: Operator = _require_admin_dep,
) -> HTMLResponse:
    """``GET /ui/agents/principals/live-grants`` -- the agent's live grants as a fragment."""
    del operator  # gate only.
    agent = identity_ref.strip()
    live = None
    if agent:
        live = await load_live_grants(session_ctx.tenant_id, agent)
    return get_templates().TemplateResponse(
        request,
        "agents/_live_grants.html",
        {"live": live, "agent": agent},
    )


def register_live_grants_route(router: APIRouter) -> None:
    """Add the live grants fragment route to *router*."""
    router.add_api_route(
        PATH,
        _live_grants_handler,
        methods=["GET"],
        name="ui_agents_live_grants",
        response_class=HTMLResponse,
    )
