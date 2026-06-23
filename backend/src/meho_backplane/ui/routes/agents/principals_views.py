# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers + projections for the agent-principals console surface.

Initiative #1824 (G10.8 Agents console), Task #1831 (T4). The agent
principals surface is the agent-identity inventory and the Keycloak
kill switch: operators view the tenant's registered principals;
tenant_admins register new ones (creating a Keycloak client + Vault
credential) and revoke them (disabling the Keycloak client, which blocks
all new token grants for that identity).

This module owns the **read** surface (the list page + its row
projection); the **write** surface (register / revoke modals + submit
handlers) lives in
:mod:`~meho_backplane.ui.routes.agents.principals_forms`. The split
mirrors the agent-definition views / forms split the T1 scaffold
(#1825) introduced so each module stays unit-testable without an HTTP
layer and under the chassis size caps.

Read surface
------------

:func:`render_principals_index` -- ``GET /ui/agents/principals``: the
full-page list (or the HTMX table fragment on an ``include_revoked``
toggle swap). One table row per principal: ``name``,
``keycloak_client_id``, a revoked pill, ``owner_sub``,
``created_by_sub``, ``created_at``. An ``include_revoked`` toggle flips
the service query so revoked principals join the inventory for audit
inspection.

RBAC posture: the list is operator-or-above (the route dep already
required an authenticated session; the service read path is not
role-gated). The ``can_write`` flag projected into the template is the
tenant_admin UX hint that reveals the Register / Revoke affordances; the
write routes re-check it server-side via
:func:`~meho_backplane.ui.routes.agents.operator.resolve_operator_or_403`.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.auth.agent_principals import (
    AgentPrincipalRead,
    AgentPrincipalService,
)
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    mint_csrf_token,
    verify_csrf_token,
)
from meho_backplane.ui.routes.agents.views import is_htmx_request
from meho_backplane.ui.templating import get_templates

__all__ = [
    "PRINCIPAL_NAME_MAX_LENGTH",
    "fetch_principal_or_404",
    "render_principals_index",
    "validate_principal_name",
]

#: Maximum length of the ``name`` path parameter the revoke routes
#: accept. Mirrors
#: :data:`meho_backplane.api.v1.agent_principals._NAME_MAX_LENGTH` so a
#: name that passes the REST surface also passes here; defence-in-depth
#: on top of the name-pattern check.
PRINCIPAL_NAME_MAX_LENGTH: Final[int] = 128

#: The agent-principal-name safe alphabet, mirroring
#: :data:`meho_backplane.auth.agent_principals._NAME_PATTERN`. A
#: malformed name in the URL path surfaces as 404 (info-leak avoidance)
#: rather than reaching the service, matching the agent-definition
#: surface's posture.
_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-\.]+$")

#: Maximum number of principals pulled per list fetch. Agent-principal
#: corpora per tenant are small (a handful of named identities); pin the
#: cap here so the list render and the service call agree.
LIST_LIMIT: Final[int] = 200


def validate_principal_name(name: str) -> None:
    """Translate a malformed principal name into 404 at the path-param stage.

    Defence-in-depth before the service-layer query. A malformed name
    surfaces as 404 (info-leak avoidance), not 422, on the path -- the
    same posture the agent-definition surface holds.
    """
    if len(name) > PRINCIPAL_NAME_MAX_LENGTH or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="agent_principal_not_found")


def _row_context(principal: AgentPrincipalRead) -> dict[str, object]:
    """Project an :class:`AgentPrincipalRead` into the list-row dict shape.

    ``keycloak_internal_id`` is intentionally **not** surfaced -- it is an
    internal Keycloak handle with no operator value, and keeping it out of
    the rendered HTML matches the audit trail's posture of not echoing
    upstream-internal identifiers.
    """
    return {
        "name": principal.name,
        "keycloak_client_id": principal.keycloak_client_id,
        "owner_sub": principal.owner_sub,
        "revoked": principal.revoked,
        "created_by_sub": principal.created_by_sub,
        "created_at": principal.created_at,
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

    Same rule the agent-definition + memory list renders follow (#1754):
    a full-page render mints + sets a fresh token, while an HTMX fragment
    render reuses the request's live ``meho_csrf`` cookie (and does not
    rotate it) so an open register / revoke modal's echoed token snapshot
    stays valid. A fragment request without a valid cookie falls back to a
    fresh mint so its own forms still validate.
    """
    if not is_htmx:
        return mint_csrf_token(session_id), True
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and verify_csrf_token(session_id, existing):
        return existing, False
    return mint_csrf_token(session_id), True


def _common_context(session_ctx: UISessionContext, csrf_token: str) -> dict[str, object]:
    """Build the dict shared across every principals template render."""
    return {
        "page_title": "Agent principals",
        "active_surface": "agents",
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
    }


async def render_principals_index(
    request: Request,
    session_ctx: UISessionContext,
    *,
    is_tenant_admin: bool,
    include_revoked: bool,
) -> HTMLResponse:
    """Render the principals list page or the HTMX table fragment.

    One handler serves both shapes (branch on ``HX-Request``): the full
    ``agents/principals/index.html`` page on a browser navigation, the
    ``agents/principals/_table.html`` fragment on the ``include_revoked``
    toggle swap. The ``meho_csrf`` cookie is set on the full-page render
    only (#1754); the fragment reuses the live cookie so an open modal's
    echoed token stays valid.
    """
    service = AgentPrincipalService()
    principals = await service.list_(
        session_ctx.tenant_id,
        include_revoked=include_revoked,
        limit=LIST_LIMIT,
    )
    is_htmx = is_htmx_request(request)
    csrf_token, set_csrf = _resolve_list_csrf(request, str(session_ctx.session_id), is_htmx=is_htmx)
    context: dict[str, object] = {
        **_common_context(session_ctx, csrf_token),
        "principals": [_row_context(principal) for principal in principals],
        "principal_count": len(principals),
        "include_revoked": include_revoked,
        "can_write": is_tenant_admin,
    }
    template_name = "agents/principals/_table.html" if is_htmx else "agents/principals/index.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def fetch_principal_or_404(
    session_ctx: UISessionContext,
    name: str,
) -> AgentPrincipalRead:
    """Pull one principal by name within the session's tenant. 404 on missing.

    The service returns ``None`` for both an absent name and a
    cross-tenant name (the tenant-scoped WHERE makes the latter
    invisible), so the 404 here collapses "no such principal" and "not
    yours" into one status -- the existence-leak avoidance the REST
    surface holds.
    """
    service = AgentPrincipalService()
    principal = await service.get(session_ctx.tenant_id, name)
    if principal is None:
        raise HTTPException(status_code=404, detail="agent_principal_not_found")
    return principal
