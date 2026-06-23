# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render helpers + projections for the agent-grants console surface.

Initiative #1824 (G10.8 Agents console), Task #1832 (T5). Pulled out of
:mod:`~meho_backplane.ui.routes.agents.grants.routes` so the route
handlers stay thin signature wrappers and the projection logic can be
unit-tested without an HTTP layer -- the same split the agent
definitions surface uses.

Two read surfaces (both tenant_admin -- see
:mod:`~meho_backplane.ui.routes.agents.grants.operator`):

* :func:`render_index` -- ``GET /ui/agents/grants``: the full-page
  grants table (or the HTMX tbody fragment on a filter swap). One row
  per grant: ``principal_sub``, ``op_pattern`` (glob), ``target_scope``
  (UUID / ``*`` / any), a **verdict badge** (``auto-execute`` =
  success, ``needs-approval`` = warning, ``deny`` = error), the expiry
  (permanent vs an elevation that auto-expires at T), ``created_by_sub``,
  and ``created_at``. The ``principal_sub`` + ``include_expired``
  filters drive an HTMX re-render of the tbody.
* :func:`render_detail` -- ``GET /ui/agents/grants/{grant_id}``: the
  full :class:`~meho_backplane.agents.grant_schemas.AgentGrantRead`
  with the revoke affordance. A non-existent / cross-tenant id renders
  the 404 page (the service returns ``None`` for both, mirroring the
  REST surface's existence-leak collapse).

Verdict rendering is **unambiguous by design** (an acceptance criterion
on #1832): a ``deny`` grant renders an error-coloured badge labelled
"deny" and must never read as an allow. The badge colour + label are
both derived from the verdict string in :func:`_verdict_badge` so the
two cannot drift apart.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from meho_backplane.agents.grant_schemas import AgentGrantRead
from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.csrf import (
    CSRF_COOKIE_NAME,
    mint_csrf_token,
    verify_csrf_token,
)
from meho_backplane.ui.routes.agents.views import is_htmx_request
from meho_backplane.ui.templating import get_templates

__all__ = [
    "LIST_LIMIT",
    "parse_grant_id_or_404",
    "render_detail",
    "render_index",
    "verdict_badge_class",
]

#: Maximum number of grants pulled per list fetch. Grant corpora per
#: tenant stay small under the least-privilege model (a handful of
#: explicit grants per principal); the service default cap (100) is
#: plenty, but pin it here so the table render and the service call
#: agree. Capped at the service's hard 500 ceiling.
LIST_LIMIT: Final[int] = 500

#: DaisyUI badge class per verdict. The colour carries the safety
#: semantics: ``auto-execute`` is the permissive end (success/green),
#: ``deny`` is the refusal (error/red), ``needs-approval`` sits between
#: (warning/amber). An unknown verdict (should never reach here -- the
#: backend enum is closed) falls back to a neutral badge rather than
#: rendering as a permissive green.
_VERDICT_BADGE: Final[dict[str, str]] = {
    "auto-execute": "badge-success",
    "needs-approval": "badge-warning",
    "deny": "badge-error",
}


def verdict_badge_class(verdict: str) -> str:
    """Return the DaisyUI badge class for *verdict*.

    Centralised so the badge colour and the verdict label always derive
    from the same string -- a ``deny`` grant can never be coloured like
    an allow (an acceptance criterion on #1832). An unrecognised verdict
    falls back to a neutral ``badge-ghost`` rather than the permissive
    green, so a future verdict value never silently reads as
    auto-execute.
    """
    return _VERDICT_BADGE.get(verdict, "badge-ghost")


def parse_grant_id_or_404(grant_id: str) -> UUID:
    """Parse the ``grant_id`` path parameter to a UUID, 404 on malformed.

    A malformed id surfaces as 404 (info-leak avoidance) rather than 422
    on the read path, mirroring the agent-definition surface's
    :func:`~meho_backplane.ui.routes.agents.views.validate_name` posture
    and the REST surface collapsing absent / cross-tenant into one 404.
    """
    try:
        return UUID(grant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="grant_not_found") from exc


def _target_scope_display(target_scope: str | None) -> str:
    """Human-readable rendering of ``target_scope``.

    ``None`` (any target) and ``"*"`` (explicit any-target) both read as
    "any target" so the table does not show a bare ``None``; a UUID
    renders verbatim (truncation is the template's job).
    """
    if target_scope is None or target_scope == "*":
        return "any target"
    return target_scope


def _row_context(grant: AgentGrantRead) -> dict[str, object]:
    """Project an :class:`AgentGrantRead` into the table-row dict shape."""
    return {
        "id": str(grant.id),
        "principal_sub": grant.principal_sub,
        "op_pattern": grant.op_pattern,
        "target_scope": grant.target_scope,
        "target_scope_display": _target_scope_display(grant.target_scope),
        "verdict": grant.verdict,
        "verdict_badge": verdict_badge_class(grant.verdict),
        "created_by_sub": grant.created_by_sub,
        "created_at": grant.created_at,
        "expires_at": grant.expires_at,
        # A grant with an expiry is a time-bounded elevation; the
        # template branches on this to label it "elevation" + show the
        # auto-expiry plainly rather than "permanent".
        "is_elevation": grant.expires_at is not None,
    }


def _detail_context(grant: AgentGrantRead) -> dict[str, object]:
    """Project an :class:`AgentGrantRead` into the detail-template shape."""
    return {
        **_row_context(grant),
        "tenant_id": str(grant.tenant_id),
        "updated_at": grant.updated_at,
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

    Same rule the agent-definitions + memory list renders follow
    (#1754): a full-page render mints + sets a fresh token, while an
    HTMX fragment render (the filter swap) reuses the request's live
    ``meho_csrf`` cookie -- and does not rotate it -- so an open
    create / elevate modal's echoed token snapshot stays valid. A
    fragment request without a valid cookie falls back to a fresh mint
    so its own forms still validate.
    """
    if not is_htmx:
        return mint_csrf_token(session_id), True
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and verify_csrf_token(session_id, existing):
        return existing, False
    return mint_csrf_token(session_id), True


def _common_context(session_ctx: UISessionContext, csrf_token: str) -> dict[str, object]:
    """Build the dict shared across every grants template render."""
    return {
        "page_title": "Agent grants",
        "active_surface": "agents",
        "operator_sub": session_ctx.operator_sub,
        "csrf_token": csrf_token,
    }


async def render_index(
    request: Request,
    session_ctx: UISessionContext,
    *,
    principal_sub: str | None,
    include_expired: bool,
) -> HTMLResponse:
    """Render the grants table page or the HTMX tbody fragment.

    One handler serves both shapes (branch on ``HX-Request``): the full
    ``agents/grants/index.html`` page on a browser navigation, the
    ``agents/grants/_rows.html`` tbody fragment on a filter swap. The
    ``principal_sub`` (exact match) + ``include_expired`` filters narrow
    the listing. The ``meho_csrf`` cookie is set on the full-page render
    only (#1754).
    """
    cleaned_principal = principal_sub.strip() if principal_sub else None
    service = AgentGrantService()
    grants = await service.list_(
        session_ctx.tenant_id,
        principal_sub=cleaned_principal or None,
        include_expired=include_expired,
        limit=LIST_LIMIT,
    )
    is_htmx = is_htmx_request(request)
    csrf_token, set_csrf = _resolve_list_csrf(request, str(session_ctx.session_id), is_htmx=is_htmx)
    context: dict[str, object] = {
        **_common_context(session_ctx, csrf_token),
        "grants": [_row_context(grant) for grant in grants],
        "grant_count": len(grants),
        "filter_principal_sub": cleaned_principal or "",
        "include_expired": include_expired,
        # The whole surface is tenant_admin (the hard gate already
        # passed to reach this render), so the write affordances always
        # render here. Kept as an explicit flag so the template shares
        # the rest of the console's ``can_write`` convention.
        "can_write": True,
    }
    template_name = "agents/grants/_rows.html" if is_htmx else "agents/grants/index.html"
    response = get_templates().TemplateResponse(request, template_name, context)
    if set_csrf:
        _set_csrf_cookie(response, csrf_token)
    return response


async def _fetch_grant_or_404(
    session_ctx: UISessionContext,
    grant_id: UUID,
) -> AgentGrantRead:
    """Pull one grant by id within the session's tenant. 404 on missing.

    The service returns ``None`` for both an absent id and a
    cross-tenant id (the tenant-scoped WHERE makes the latter
    invisible), so the 404 here collapses "no such grant" and "not
    yours" into one status -- the existence-leak avoidance the REST
    surface holds.
    """
    service = AgentGrantService()
    grant = await service.get(session_ctx.tenant_id, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="grant_not_found")
    return grant


async def render_detail(
    request: Request,
    session_ctx: UISessionContext,
    *,
    grant_id: UUID,
) -> HTMLResponse:
    """Render the grant detail page (or the HTMX body fragment).

    404 for an absent / cross-tenant id. The verdict renders with the
    same colour-coded badge as the list; the expiry renders plainly
    ("permanent" vs "auto-expires at T"). The revoke affordance is the
    only write on the detail view (edit is delete + re-create -- grants
    are immutable rows).
    """
    grant = await _fetch_grant_or_404(session_ctx, grant_id)
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        **_common_context(session_ctx, csrf_token),
        "grant": _detail_context(grant),
        "can_write": True,
    }
    template_name = (
        "agents/grants/_detail_body.html"
        if is_htmx_request(request)
        else "agents/grants/detail.html"
    )
    response = get_templates().TemplateResponse(request, template_name, context)
    _set_csrf_cookie(response, csrf_token)
    return response
