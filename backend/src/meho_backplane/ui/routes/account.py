# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Account surface -- real whoami / role / tenant / expiry + session revoke.

Initiative #1842 (G10.11), Task #1892. The operator console's sidebar
account chip was hardcoded to the literal "OP / Operator"; there was no
surface showing who the operator actually is (Keycloak ``sub``, tenant,
role, session expiry) and no way to see or revoke active sessions.

This module adds an operator-tier Account page at ``/ui/account``:

* ``GET /ui/account`` -- the full page. Renders the operator's
  ``operator_sub``, the active tenant (reusing the chassis
  ``session_tenant`` context shape), the **role read from the
  freshly-verified access token** (lifted through the same JWT
  re-verify path the connectors surface uses, so a same-session
  demotion shows immediately rather than being masked by a value
  cached on the session row), the session ``expires_at``, and the
  operator's active-session list with per-row revoke + a
  "revoke all other sessions" action.

* ``POST /ui/account/sessions/revoke-others`` -- revoke every active
  session for this operator **except** the current one. Registered
  **before** the parametrised single-revoke route so the literal
  ``revoke-others`` segment is never captured as a ``{session_id}``.

* ``POST /ui/account/sessions/{session_id}/revoke`` -- revoke one
  session. Ownership (``operator_sub`` AND ``tenant_id``) is enforced
  **server-side** from the validated session context before
  :func:`~meho_backplane.ui.auth.session_store.revoke_session` runs
  (that helper does no ownership check); a non-owned / cross-tenant id
  returns 404 so another operator's session id is never confirmed.
  Revoking the current session ("this device") logs the operator out:
  the response carries an ``HX-Redirect`` to ``/ui/auth/login`` (and the
  next request's ``load_session`` returns ``None`` anyway, so the
  middleware would redirect regardless).

RBAC: this is operator-tier -- any authenticated operator sees their
**own** account. There is no ``tenant_admin`` gate and no "list/revoke
another operator's sessions" surface; the listing and both revoke
actions are scoped to the caller's own ``operator_sub`` from the
session, never a form field.

Out of scope (Keycloak-owned / future): role editing, tenant switching
(a slot is left in the template), password / MFA / token rotation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_session
from meho_backplane.db.models import WebSession
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.auth.routes import LOGIN_PATH
from meho_backplane.ui.auth.session_store import (
    ActiveSessionRow,
    list_active_sessions,
    load_session,
    revoke_other_sessions,
    revoke_session,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, mint_csrf_token
from meho_backplane.ui.routes.connectors.operator import _lift_operator
from meho_backplane.ui.templating import get_templates

__all__ = ["build_account_router"]

_log = structlog.get_logger(__name__)

#: Module-level :class:`fastapi.Depends` closures -- ruff B008 idiom.
_require_ui_session_dep = Depends(require_ui_session)
_get_session_dep = Depends(get_session)

#: Sentinel role label shown when the freshly-verified token can't be
#: lifted (transient JWKS outage, token swapped). The page still renders
#: the operator's identity; only the role degrades to "unknown" rather
#: than 5xx-ing the read surface -- mirroring the connectors role-probe's
#: fail-soft posture.
_ROLE_UNKNOWN: str = "unknown"


@dataclass(frozen=True)
class _SessionRowView:
    """Template-facing projection of one active-session row.

    Carries the pre-formatted relative + absolute timestamps and the
    ``is_current`` flag (the "This device" badge) so the template stays
    logic-light and the formatting is unit-testable on the route side.
    """

    id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool


async def _lift_role_value(session_ctx: UISessionContext) -> str:
    """Return the live ``tenant_role`` value from the freshly-verified token.

    Lifts the full :class:`~meho_backplane.auth.operator.Operator` through
    the shared JWT re-verify path
    (:func:`~meho_backplane.ui.routes.connectors.operator._lift_operator`)
    so a same-session role demotion shows up immediately -- the role is
    never read from a value cached on the session row. Fails **soft**: a
    JWT-validation hiccup (session row gone, JWKS unreachable, token
    swapped) degrades the rendered role to ``"unknown"`` rather than
    5xx-ing the read-only account page. No security gate hangs off this
    value; the page only displays it.
    """
    try:
        operator = await _lift_operator(session_ctx)
    except Exception as exc:
        _log.info(
            "ui_account_role_lift_unavailable",
            session_id=str(session_ctx.session_id),
            reason=type(exc).__name__,
        )
        return _ROLE_UNKNOWN
    role = operator.tenant_role
    return role.value if isinstance(role, TenantRole) else str(role)


def _project_session_row(row: ActiveSessionRow, *, current_id: uuid.UUID) -> _SessionRowView:
    """Project an :class:`ActiveSessionRow` into the template view shape."""
    return _SessionRowView(
        id=str(row.id),
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        is_current=row.id == current_id,
    )


async def _load_expires_at(
    db_session: AsyncSession, session_ctx: UISessionContext
) -> datetime | None:
    """Return the current session's ``expires_at`` (sliding-window value).

    Reads through :func:`load_session` so the displayed expiry reflects
    the same (possibly sliding-extended) value the middleware would honour
    on the next request. ``None`` when the row vanished between the
    middleware check and this read (revoked / expired in-flight) -- the
    template renders an "expired" hint and the next request redirects to
    login anyway.
    """
    decrypted = await load_session(db_session, session_ctx.session_id)
    return decrypted.expires_at if decrypted is not None else None


async def _render_page(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Render ``GET /ui/account`` -- the full account page."""
    role_value = await _lift_role_value(session_ctx)
    expires_at = await _load_expires_at(db_session, session_ctx)
    rows = await list_active_sessions(
        db_session,
        operator_sub=session_ctx.operator_sub,
        tenant_id=session_ctx.tenant_id,
    )
    session_views = [_project_session_row(r, current_id=session_ctx.session_id) for r in rows]
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "page_title": "Account",
        "active_surface": "account",
        "operator_sub": session_ctx.operator_sub,
        "role_value": role_value,
        "expires_at": expires_at,
        "sessions": session_views,
        "current_session_id": str(session_ctx.session_id),
        "now_utc": datetime.now(UTC),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "account/index.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


async def _render_sessions_fragment(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Render the active-sessions list fragment (post-revoke HTMX swap)."""
    rows = await list_active_sessions(
        db_session,
        operator_sub=session_ctx.operator_sub,
        tenant_id=session_ctx.tenant_id,
    )
    session_views = [_project_session_row(r, current_id=session_ctx.session_id) for r in rows]
    csrf_token = mint_csrf_token(str(session_ctx.session_id))
    context: dict[str, object] = {
        "sessions": session_views,
        "current_session_id": str(session_ctx.session_id),
        "now_utc": datetime.now(UTC),
        "csrf_token": csrf_token,
    }
    response = get_templates().TemplateResponse(request, "account/_sessions.html", context)
    _set_csrf_cookie(response, csrf_token)
    return response


def _set_csrf_cookie(response: HTMLResponse, csrf_token: str) -> None:
    """Set the double-submit ``meho_csrf`` cookie -- mirrors the feed surface."""
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/ui",
    )


def _logout_redirect() -> HTMLResponse:
    """Build the self-logout response: an empty 200 carrying ``HX-Redirect``.

    The "This device" revoke invalidates the operator's own session, so
    the browser must navigate to login. HTMX honours ``HX-Redirect`` by
    doing a client-side navigation; the body is empty because the page is
    about to be replaced. The next request would be redirected by the
    session middleware anyway (the row is now revoked) -- this makes the
    sign-out explicit and immediate.
    """
    return HTMLResponse(
        content="",
        status_code=status.HTTP_200_OK,
        headers={"HX-Redirect": LOGIN_PATH},
    )


async def _do_revoke_one(
    request: Request,
    *,
    raw_session_id: str,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Revoke one session after enforcing server-side ownership.

    Ownership is asserted from the validated ``session_ctx`` -- never a
    form field: the target row's ``operator_sub`` AND ``tenant_id`` must
    match the caller's. A malformed id, a missing row, or a
    non-owned / cross-tenant row all return 404 so another operator's
    session id is never confirmed (a 403 would leak existence).
    """
    try:
        session_id = uuid.UUID(raw_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc

    row = await db_session.get(WebSession, session_id)
    if (
        row is None
        or row.operator_sub != session_ctx.operator_sub
        or row.tenant_id != session_ctx.tenant_id
    ):
        # Either no such row, or it belongs to another operator / tenant.
        # Collapse both to 404 so the response cannot be used to probe
        # for the existence of another operator's session id.
        raise HTTPException(status_code=404, detail="session not found")

    await revoke_session(db_session, session_id)
    is_current = session_id == session_ctx.session_id
    _log.info(
        "ui_account_session_revoked",
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        revoked_session_id=str(session_id),
        is_current=is_current,
    )
    if is_current:
        return _logout_redirect()
    return await _render_sessions_fragment(request, session_ctx=session_ctx, db_session=db_session)


async def _do_revoke_others(
    request: Request,
    *,
    session_ctx: UISessionContext,
    db_session: AsyncSession,
) -> HTMLResponse:
    """Revoke every active session for this operator except the current one."""
    revoked = await revoke_other_sessions(
        db_session,
        operator_sub=session_ctx.operator_sub,
        tenant_id=session_ctx.tenant_id,
        keep_session_id=session_ctx.session_id,
    )
    _log.info(
        "ui_account_sessions_revoke_others",
        operator_sub=session_ctx.operator_sub,
        tenant_id=str(session_ctx.tenant_id),
        revoked_count=revoked,
    )
    return await _render_sessions_fragment(request, session_ctx=session_ctx, db_session=db_session)


def build_account_router() -> APIRouter:
    """Construct the Account surface :class:`APIRouter`.

    Registration order is **load-bearing**: the literal
    ``POST /ui/account/sessions/revoke-others`` route is registered
    **before** the parametrised
    ``POST /ui/account/sessions/{session_id}/revoke`` so the
    first-match-wins lookup binds ``revoke-others`` as the literal route
    rather than capturing ``session_id="revoke-others"`` (mirrors the
    connectors surface's literal-before-param discipline).
    """
    router = APIRouter(tags=["ui-account"])

    async def _page(
        request: Request,
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_session_dep,
    ) -> HTMLResponse:
        """``GET /ui/account``."""
        return await _render_page(request, session_ctx=session_ctx, db_session=db_session)

    async def _revoke_others(
        request: Request,
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_session_dep,
    ) -> HTMLResponse:
        """``POST /ui/account/sessions/revoke-others``."""
        return await _do_revoke_others(request, session_ctx=session_ctx, db_session=db_session)

    async def _revoke_one(
        request: Request,
        session_id: str,
        session_ctx: UISessionContext = _require_ui_session_dep,
        db_session: AsyncSession = _get_session_dep,
    ) -> HTMLResponse:
        """``POST /ui/account/sessions/{session_id}/revoke``."""
        return await _do_revoke_one(
            request,
            raw_session_id=session_id,
            session_ctx=session_ctx,
            db_session=db_session,
        )

    router.add_api_route(
        "/ui/account",
        _page,
        methods=["GET"],
        name="ui_account",
        response_class=HTMLResponse,
    )
    # Literal route BEFORE the parametrised one -- first-match-wins.
    router.add_api_route(
        "/ui/account/sessions/revoke-others",
        _revoke_others,
        methods=["POST"],
        name="ui_account_revoke_others",
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/ui/account/sessions/{session_id}/revoke",
        _revoke_one,
        methods=["POST"],
        name="ui_account_revoke_session",
        response_class=HTMLResponse,
        responses={
            404: {
                "description": (
                    "Session id is malformed, does not exist, or belongs to "
                    "another operator / tenant. Collapsed to 404 so another "
                    "operator's session id is never confirmed."
                ),
                "content": {"text/html": {}},
            },
        },
    )
    return router
