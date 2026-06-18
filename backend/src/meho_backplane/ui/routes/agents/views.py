# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers + projections for the agents console surface.

Initiative #1824 (G10.8 Agents console), Task #1825 (T1). Pulled out of
:mod:`~meho_backplane.ui.routes.agents.routes` so the route handlers
stay thin signature wrappers and the projection logic can be
unit-tested without an HTTP layer -- the same split the memory +
connectors surfaces use.

Two read surfaces:

* :func:`render_index` -- ``GET /ui/agents``: the full-page list (or
  the HTMX card-list fragment on a poll / filter swap). One DaisyUI
  card per agent definition: name, ``model_tier`` badge, ``enabled``
  pill, ``identity_ref``, ``turn_budget``, ``created_by_sub``,
  ``updated_at``. The sensitive ``system_prompt`` is summarised to its
  first line and ``toolset`` to a tool count -- neither is dumped into
  the list (the audit trail keeps them out by design,
  :mod:`meho_backplane.api.v1.agents`; the UI mirrors that posture).
* :func:`render_detail` -- ``GET /ui/agents/{name}``: the full
  :class:`~meho_backplane.agents.schemas.AgentDefinitionRead`. The
  ``system_prompt`` renders read-only in a monospace block and the
  ``toolset`` / ``output_schema`` render as collapsible
  pretty-printed JSON. A non-existent / cross-tenant name renders the
  404 page (the service returns ``None`` for both, mirroring the REST
  surface's existence-leak collapse).

RBAC posture: reads are operator-or-above (the service read path is
not role-gated, and the route deps already required an authenticated
session). The ``can_write`` flag projected into the template is the
tenant_admin UX hint; the write routes re-check it server-side via
:func:`~meho_backplane.ui.routes.agents.operator.resolve_operator_or_403`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.agents.schemas import AgentDefinitionRead
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    mint_csrf_token,
    verify_csrf_token,
)
from meho_backplane.ui.templating import get_templates

__all__ = [
    "NAME_MAX_LENGTH",
    "is_htmx_request",
    "render_detail",
    "render_index",
    "validate_name",
]

#: Maximum length of the ``name`` path parameter the detail / write
#: routes accept. Mirrors
#: :data:`meho_backplane.api.v1.agents._NAME_MAX_LENGTH` so a name that
#: passes the REST surface also passes here; defence-in-depth on top of
#: the name-pattern check.
NAME_MAX_LENGTH: Final[int] = 128

#: The agent-name safe alphabet, mirroring
#: :data:`meho_backplane.agents.schemas.NAME_PATTERN`. A malformed name
#: in the URL path surfaces as 404 (info-leak avoidance) rather than
#: reaching the service, matching the memory surface's slug posture.
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-\.]+$")

#: Maximum number of agent definitions pulled per list fetch. Agent
#: corpora per tenant are small (a handful of named agents); the
#: service default cap (100) is plenty, but pin it here so the list
#: render and the service call agree.
LIST_LIMIT: Final[int] = 200


def is_htmx_request(request: Request) -> bool:
    """Return ``True`` when the request was issued by HTMX.

    HTMX 2 sets ``HX-Request: true`` on every fetch its directives
    drive (https://htmx.org/reference/#request_headers).
    """
    return request.headers.get("hx-request", "").lower() == "true"


def validate_name(name: str) -> None:
    """Translate a malformed agent name into 404 at the path-param stage.

    Defence-in-depth before the service-layer query. Mirrors the memory
    surface's :func:`~meho_backplane.ui.routes.memory.views.validate_slug`
    posture: a malformed name surfaces as 404 (info-leak avoidance), not
    422, on the read path.
    """
    if len(name) > NAME_MAX_LENGTH or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="agent_not_found")


def _system_prompt_summary(system_prompt: str) -> str:
    """Return the first non-empty line of the system prompt, bounded.

    The system prompt is sensitive (kept out of the audit trail); the
    list card shows only its first line so an operator can recognise the
    agent without the full content being dumped into the scannable list.
    """
    for line in system_prompt.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def _toolset_summary(toolset: dict[str, Any]) -> int:
    """Return the tool count for the list card.

    The toolset is a free-shaped JSON object; T3 (#810) owns its
    resolution contract. For the scannable list we only surface the
    number of top-level keys (a proxy for "how many tools / groups this
    agent is wired to") rather than the structure itself.
    """
    return len(toolset)


def _pretty_json(value: Any) -> str:
    """Pretty-print a JSON-able value for the detail view's collapsible block.

    ``None`` renders as an empty string so the template can branch on
    "no structured-output schema" without a literal ``null`` showing.
    """
    if value is None:
        return ""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _card_context(agent: AgentDefinitionRead) -> dict[str, object]:
    """Project an :class:`AgentDefinitionRead` into the list-card dict shape."""
    return {
        "name": agent.name,
        "model_tier": agent.model_tier,
        "enabled": agent.enabled,
        "identity_ref": agent.identity_ref,
        "turn_budget": agent.turn_budget,
        "created_by_sub": agent.created_by_sub,
        "updated_at": agent.updated_at,
        "system_prompt_summary": _system_prompt_summary(agent.system_prompt),
        "tool_count": _toolset_summary(agent.toolset),
    }


def _detail_context(agent: AgentDefinitionRead, *, can_write: bool) -> dict[str, object]:
    """Project an :class:`AgentDefinitionRead` into the detail-template shape."""
    return {
        "name": agent.name,
        "model_tier": agent.model_tier,
        "enabled": agent.enabled,
        "identity_ref": agent.identity_ref,
        "turn_budget": agent.turn_budget,
        "created_by_sub": agent.created_by_sub,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "system_prompt": agent.system_prompt,
        "toolset_json": _pretty_json(agent.toolset),
        "output_schema_json": _pretty_json(agent.output_schema),
        "tool_count": _toolset_summary(agent.toolset),
        "can_write": can_write,
    }


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Mirror the chassis CSRF cookie posture for state-changing pages."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _resolve_list_csrf(request: Request, session_id: str, *, is_htmx: bool) -> tuple[str, bool]:
    """Pick the CSRF token the list render echoes + whether to set the cookie.

    Same rule the memory list render follows (#1754): a full-page render
    mints + sets a fresh token, while an HTMX fragment render reuses the
    request's live ``meho_csrf`` cookie (and does not rotate it) so an
    open create modal's echoed token snapshot stays valid. A fragment
    request without a valid cookie falls back to a fresh mint so its own
    forms still validate.
    """
    if not is_htmx:
        return mint_csrf_token(session_id), True
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and verify_csrf_token(session_id, existing):
        return existing, False
    return mint_csrf_token(session_id), True


def _common_context(session_ctx: UISessionContext, csrf_token: str) -> dict[str, object]:
    """Build the dict shared across every agents template render."""
    return {
        "page_title": "Agents",
        "active_surface": "agents",
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
    }


async def render_index(
    request: Request,
    session_ctx: UISessionContext,
    *,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the agents list page or the HTMX card-list fragment.

    One handler serves both shapes (branch on ``HX-Request``): the
    full ``agents/index.html`` page on a browser navigation, the
    ``agents/_cards.html`` fragment on a poll / re-render swap. The
    ``meho_csrf`` cookie is set on the full-page render only (#1754).
    """
    service = AgentDefinitionService()
    agents = await service.list_(session_ctx.tenant_id, limit=LIST_LIMIT)
    is_htmx = is_htmx_request(request)
    csrf_token, set_csrf = _resolve_list_csrf(request, str(session_ctx.session_id), is_htmx=is_htmx)
    context: dict[str, object] = {
        **_common_context(session_ctx, csrf_token),
        "agents": [_card_context(agent) for agent in agents],
        "agent_count": len(agents),
        "can_write": is_tenant_admin,
    }
    template_name = "agents/_cards.html" if is_htmx else "agents/index.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def _fetch_agent_or_404(
    session_ctx: UISessionContext,
    name: str,
) -> AgentDefinitionRead:
    """Pull one agent by name within the session's tenant. 404 on missing.

    The service returns ``None`` for both an absent name and a
    cross-tenant name (the tenant-scoped WHERE makes the latter
    invisible), so the 404 here collapses "no such agent" and "not
    yours" into one status -- the existence-leak avoidance the REST
    surface holds.
    """
    service = AgentDefinitionService()
    agent = await service.get(session_ctx.tenant_id, name)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return agent


async def render_detail(
    request: Request,
    session_ctx: UISessionContext,
    *,
    name: str,
    is_tenant_admin: bool,
) -> HTMLResponse:
    """Render the agent detail page (or the HTMX body fragment).

    404 for an absent / cross-tenant name. The ``system_prompt`` renders
    read-only; ``toolset`` / ``output_schema`` render as collapsible
    pretty-printed JSON. The write affordances (edit / toggle / delete)
    render only when ``can_write`` (tenant_admin); the write routes
    re-check server-side.
    """
    agent = await _fetch_agent_or_404(session_ctx, name)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **_common_context(session_ctx, csrf_token),
        "agent": _detail_context(agent, can_write=is_tenant_admin),
    }
    template_name = "agents/_detail_body.html" if is_htmx_request(request) else "agents/detail.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response
