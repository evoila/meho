# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""BFF session middleware -- load operator identity from the cookie.

Initiative #337 (G10.0 Frontend chassis), Task #865 (T4). This module
ships :class:`UISessionMiddleware` -- the pure-ASGI middleware T5
(#866) registers on the FastAPI app **before** the existing
JWT/audit middlewares for ``/ui/*`` paths.

Responsibilities
----------------

For every request whose path starts with ``/ui/`` (except the BFF
auth surfaces themselves -- those routes set or clear the cookie and
must be reachable unauthenticated):

1. Read ``meho_session`` cookie.
2. Parse it as a UUID -- malformed values are treated as "no
   session" (no exception leaks; the operator just gets bounced to
   login).
3. Call :func:`meho_backplane.ui.auth.session_store.load_session` to
   load + decrypt the row. ``None`` means missing / revoked /
   expired -- all three collapse to "no session".
4. On hit, bind ``operator_sub`` / ``tenant_id`` into the
   :class:`UISessionContext` attached to ``request.state``. Route
   handlers (T5 surfaces) read it via the :func:`require_ui_session`
   dependency to render operator-scoped views.
5. On miss, 302-redirect to ``/ui/auth/login?return_to=<original
   path>`` so the operator lands back on the deep link after auth.

Static assets bypass
--------------------

The chassis (#863) mounts ``/ui/static/*`` for vendored JS + the
compiled Tailwind CSS. Those resources must NOT redirect to login
because (a) the browser would follow the redirect and load the login
page HTML in place of a CSS / JS file, breaking the rendered page,
and (b) the asset paths are deliberately reachable unauthenticated
so the operator sees a styled login surface rather than an unstyled
default-browser-render. The middleware short-circuits on the
``/ui/static/`` prefix.

Pure ASGI, not :class:`BaseHTTPMiddleware`
------------------------------------------

Mirrors the pattern in :mod:`meho_backplane.middleware`'s
:class:`RequestContextMiddleware`: Starlette 1.0+ docs recommend
pure ASGI for any middleware that needs deterministic contextvar
lifetimes or that touches the response start (the cookie set, or
the 302 location). BaseHTTPMiddleware spawns an anyio task wrapper
that has complicated streaming + contextvar interactions; the
ASGI-level pattern below is the canonical "wrap send" recipe.

Why no session refresh here
---------------------------

The RFC-9700-compliant refresh lives in
:mod:`meho_backplane.ui.auth.refresh` (G0.25 #1694) and hooks in at
:func:`require_ui_admin` -- the dependency that actually presents
the access token to the JWT chain -- not in this middleware. Doing
the refresh inside the middleware would require the middleware to
know which surface needs the access token (most ``/ui/*`` page
renders do not), and would inflate the hot path's DB cost. The
middleware only loads + validates the existing session row;
token-consuming dependencies refresh proactively (row near
``expires_at``) and reactively (JWT chain reports
``token_expired``) through the refresh module's seams.

References
----------

* OAuth 2.0 for Browser-Based Apps BCP § 6.1 (BFF pattern):
  https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
* OWASP ASVS v4 §3.3.1 (session-cookie attributes):
  https://owasp.org/www-project-application-security-verification-standard/
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.ui.audit import bind_ui_view_audit
from meho_backplane.ui.auth.routes import LOGIN_PATH, SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import load_session

if TYPE_CHECKING:
    from meho_backplane.auth.operator import TenantRole

__all__ = [
    "AUTH_PREFIX",
    "STATIC_PREFIX",
    "UISessionContext",
    "UISessionMiddleware",
    "require_ui_admin",
    "require_ui_session",
]


#: Prefix every UI route lives under. Requests outside the prefix
#: pass through untouched; requests inside are subject to the
#: session check unless they match :data:`AUTH_PREFIX` or
#: :data:`STATIC_PREFIX`.
_UI_PREFIX: Final[str] = "/ui/"
#: Prefix the BFF auth surfaces live under (login, callback, logout).
#: The middleware MUST let unauthenticated requests reach these so
#: the operator can complete the round-trip. Exact-match on ``/ui/auth``
#: is too narrow (``/ui/auth/login`` is the actual route).
AUTH_PREFIX: Final[str] = "/ui/auth/"
#: Prefix the chassis static-asset mount lives under (#863). The
#: middleware short-circuits on this prefix so vendored JS / compiled
#: CSS load without authentication -- otherwise the operator would
#: see an unstyled login page when their session expires.
STATIC_PREFIX: Final[str] = "/ui/static/"


@dataclass(frozen=True)
class UISessionContext:
    """Per-request session identity exposed on ``request.state``.

    Frozen so a route handler that stashes the context on a logger
    or forwards it to a service layer cannot accidentally mutate
    fields downstream. The shape mirrors :class:`Operator` for the
    fields T5 (#866) needs to render an authenticated page header;
    ``raw_jwt`` / ``tenant_role`` are intentionally absent because
    the session-cookie path does not load them today (the encrypted
    row carries only the access token, not the decoded claims).

    ``tenant_slug`` / ``tenant_name`` are populated by the middleware
    from a same-request lookup against the ``tenant`` table (keyed on
    :attr:`tenant_id`). The fields are surfaced into every UI template
    by the chassis context processor so the page header's tenant chip
    renders the operator-readable name without each route having to
    re-fetch the row (G0.15-T9 #1217). Both are ``None`` only when the
    tenant row was deleted between session-creation and the request
    (an ops anomaly; the operator still authenticates fine, the chip
    just falls back to the tenant UUID).
    """

    session_id: uuid.UUID
    operator_sub: str
    tenant_id: uuid.UUID
    tenant_slug: str | None = None
    tenant_name: str | None = None


def _select_path(scope: Scope) -> str:
    """Return the ASGI scope's path, defaulting to ``/`` on missing values."""
    raw = scope.get("path")
    return raw if isinstance(raw, str) and raw else "/"


def _extract_session_cookie(scope: Scope) -> str | None:
    """Pull the ``meho_session`` cookie value out of the raw headers.

    Avoids the cost of constructing a :class:`Request` object for the
    happy-path of "no cookie, redirect to login". The cookie header
    is iterated once; allocation stays in raw bytes until the cookie
    is found.
    """
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return None
    cookie_name_bytes = SESSION_COOKIE_NAME.encode("ascii")
    for name, value in headers:
        if not isinstance(name, (bytes, bytearray)) or name != b"cookie":
            continue
        if not isinstance(value, (bytes, bytearray)):
            continue
        # The cookie header is ``name1=value1; name2=value2; ...``.
        # We decode lazily on the match -- non-meho cookies stay as
        # bytes.
        value_bytes = bytes(value)
        for chunk in value_bytes.split(b";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if b"=" not in chunk:
                continue
            cookie_name, _, cookie_value = chunk.partition(b"=")
            if cookie_name.strip() == cookie_name_bytes:
                try:
                    return cookie_value.decode("ascii")
                except UnicodeDecodeError:
                    return None
    return None


def _redirect_to_login(original_path_with_query: str) -> tuple[int, list[tuple[bytes, bytes]]]:
    """Build the ASGI ``http.response.start`` shape for the login redirect.

    Returns the status code + the encoded headers list. Constructing
    the response by hand (rather than letting Starlette synthesise a
    :class:`RedirectResponse`) keeps the middleware single-allocation
    on the redirect path -- the body is empty.

    The ``return_to`` query value is the original path with its query
    string, URL-encoded so an operator-supplied target with reserved
    characters round-trips cleanly. The login route's
    :func:`_safe_return_to` validates the decoded value before it
    lands in any subsequent redirect.
    """
    encoded = quote(original_path_with_query, safe="")
    location = f"{LOGIN_PATH}?return_to={encoded}"
    return status.HTTP_302_FOUND, [
        (b"location", location.encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"content-length", b"0"),
    ]


class UISessionMiddleware:
    """Pure-ASGI middleware: load operator identity from the session cookie.

    Constructed once at app startup and bound onto the FastAPI app
    via :meth:`FastAPI.add_middleware` in T5 (#866). Per-request
    state is held in the call's local scope; the class itself is
    stateless.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP scopes (websocket, lifespan) pass through. The BFF
        # has no websocket surface in v0.2 and the lifespan must not
        # be intercepted.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = _select_path(scope)
        # Out-of-prefix paths pass through unchanged -- the middleware
        # is /ui/-only by deliberate scoping. The chassis JWT
        # middleware handles /api/* and /mcp.
        if not path.startswith(_UI_PREFIX):
            await self.app(scope, receive, send)
            return

        # Static asset paths bypass auth -- the operator sees a
        # styled login page rather than an unstyled default render.
        if path.startswith(STATIC_PREFIX):
            await self.app(scope, receive, send)
            return

        # The BFF auth surfaces themselves -- login, callback,
        # logout -- must be reachable without a session.
        if path.startswith(AUTH_PREFIX):
            await self.app(scope, receive, send)
            return

        # Look the session up. Any failure mode (no cookie, malformed
        # UUID, missing row, revoked, expired) collapses to "redirect
        # to login".
        log = structlog.get_logger(__name__)
        cookie_value = _extract_session_cookie(scope)
        session_context: UISessionContext | None = None
        if cookie_value is not None:
            try:
                cookie_id = uuid.UUID(cookie_value)
            except ValueError:
                log.info("ui_session_malformed_cookie", path=path)
                cookie_id = None
            if cookie_id is not None:
                sessionmaker = get_sessionmaker()
                # One transaction loads the session row AND the tenant
                # row -- the tenant lookup is a PK probe on a tiny
                # write-mostly table, so paying it inside the
                # already-running ``load_session`` transaction is
                # microseconds and keeps the page header from needing a
                # separate DB round-trip per request (G0.15-T9 #1217).
                async with sessionmaker() as session, session.begin():
                    decrypted = await load_session(session, cookie_id)
                    if decrypted is not None:
                        tenant_row = (
                            await session.execute(
                                select(Tenant.slug, Tenant.name).where(
                                    Tenant.id == decrypted.tenant_id,
                                ),
                            )
                        ).one_or_none()
                if decrypted is not None:
                    tenant_slug = tenant_row[0] if tenant_row is not None else None
                    tenant_name = tenant_row[1] if tenant_row is not None else None
                    if tenant_row is None:
                        # Session row references a tenant_id with no
                        # tenant row -- the tenant was deleted out from
                        # under the operator's session. Surface the
                        # anomaly so on-call sees the broken FK while
                        # still letting the operator's request proceed
                        # (the page header falls back to the UUID).
                        log.warning(
                            "ui_session_tenant_row_missing",
                            session_id=str(decrypted.id),
                            tenant_id=str(decrypted.tenant_id),
                        )
                    session_context = UISessionContext(
                        session_id=decrypted.id,
                        operator_sub=decrypted.operator_sub,
                        tenant_id=decrypted.tenant_id,
                        tenant_slug=tenant_slug,
                        tenant_name=tenant_name,
                    )

        if session_context is None:
            # Build the original-path-with-query for the post-login
            # round-trip. ``raw_path`` is bytes; ``query_string`` is
            # also bytes. Decode both as ASCII -- URLs are ASCII by
            # protocol; non-ASCII bytes would have been percent-encoded
            # already.
            query_string_raw = scope.get("query_string")
            query_string = (
                query_string_raw.decode("ascii")
                if isinstance(query_string_raw, (bytes, bytearray))
                else ""
            )
            full_path = f"{path}?{query_string}" if query_string else path
            status_code, headers = _redirect_to_login(full_path)
            log.info(
                "ui_session_missing_or_invalid",
                path=path,
                had_cookie=cookie_value is not None,
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": headers,
                }
            )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        # Happy path -- stash the session context on the scope so
        # downstream consumers (FastAPI dependency wrappers) can
        # reach it via ``request.state.ui_session``. ``scope["state"]``
        # is a Starlette convention for per-request state passed
        # between middlewares.
        scope_state = scope.setdefault("state", {})
        if isinstance(scope_state, dict):
            scope_state["ui_session"] = session_context

        # G0.15-T7 (#1216): bind audit contextvars per-request happens
        # later, inside :func:`require_ui_session` (the FastAPI
        # dependency) -- not here. The inner
        # :class:`~meho_backplane.middleware.RequestContextMiddleware`
        # calls :func:`structlog.contextvars.clear_contextvars` at
        # request entry, which would wipe any binding made here before
        # the audit middleware sees it. The dependency runs after the
        # chassis middlewares but before the route handler, which is
        # the load-bearing time window: the audit middleware reads the
        # contextvars on the *response* side, after the handler returns
        # but before forwarding the buffered response.

        await self.app(scope, receive, send)


async def require_ui_session(request: Request) -> UISessionContext:
    """FastAPI dependency: surface the loaded :class:`UISessionContext`.

    Route handlers under ``/ui/*`` (T5 #866) declare
    ``Depends(require_ui_session)`` instead of reaching into
    ``request.state`` directly. The middleware enforces the redirect
    on missing sessions; this dependency is the guarded read.

    Audit binding (G0.15-T7 #1216)
    ------------------------------

    For HTTP GET / HEAD requests the dependency binds the audit
    contextvars the chassis :class:`~meho_backplane.audit.AuditMiddleware`
    consumes -- ``operator_sub``, ``tenant_id``, ``audit_op_id``,
    ``audit_op_class``. This is the per-request choke-point for the
    BFF audit-thread: every ``/ui/<surface>`` GET handler declares
    this dependency (directly or transitively via
    :func:`require_ui_admin`), so binding here guarantees the audit
    middleware writes one row per page view.

    The binding cannot live in :class:`UISessionMiddleware` because
    the inner :class:`~meho_backplane.middleware.RequestContextMiddleware`
    calls :func:`structlog.contextvars.clear_contextvars` at request
    entry -- any binding made in the outer middleware would be wiped
    before the audit middleware reads it. The dependency runs after
    the chassis middlewares but before the route handler, which is
    the load-bearing time window: the audit middleware reads the
    contextvars on the response side, after the handler returns but
    before forwarding the buffered response.

    POST / PATCH / DELETE requests on ``/ui/*`` skip the ``ui_view``
    binding -- those go through service-layer functions (``create_target``,
    ``update_target``, ``forget_memory``, etc.) that audit under
    their own ``op_id`` / ``op_class`` discipline. Binding the
    ``ui_view`` op_class here would produce a duplicate audit row
    per write. ``operator_sub`` and ``tenant_id`` are still bound on
    non-GET requests so a write-path route that bypasses the
    service-layer audit writer still produces a row attributed to
    the operator (under the default ``http.<method>:<path>`` op_id),
    rather than disappearing silently.

    Returns
    -------
    UISessionContext
        The validated identity bound by the middleware.

    Raises
    ------
    HTTPException(401)
        The middleware short-circuited the request *before* this
        dependency could run -- which means a route handler escaped
        the redirect logic (a misconfiguration). The 401 is a hard
        bug signal, not a normal flow.
    """
    context = getattr(request.state, "ui_session", None)
    if context is None:
        # The middleware guarantees a session is bound before any
        # /ui/ route runs; reaching this branch means a route was
        # registered without the middleware in front. Surface as a
        # 401 so the operator's browser does not silently render a
        # half-broken page.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )
    # The middleware only binds ``UISessionContext`` instances onto
    # ``scope["state"]["ui_session"]``; the ``cast`` narrows the
    # ``getattr`` return so the dependency's typed return survives
    # the dynamic attribute access.
    session_context = cast(UISessionContext, context)

    # G0.15-T7 (#1216): bind audit contextvars for the chassis
    # AuditMiddleware. GET / HEAD get the full ``ui_view`` op_id +
    # op_class binding; other methods get only operator + tenant
    # identity (the route's service-layer write owns the op_id).
    method = request.method.upper() if isinstance(request.method, str) else ""
    if method in {"GET", "HEAD"}:
        bind_ui_view_audit(
            operator_sub=session_context.operator_sub,
            tenant_id=str(session_context.tenant_id),
            path=request.url.path,
        )
    else:
        structlog.contextvars.bind_contextvars(
            operator_sub=session_context.operator_sub,
            tenant_id=str(session_context.tenant_id),
        )
    return session_context


async def require_ui_admin(
    request: Request,
    session: UISessionContext = Depends(require_ui_session),
) -> UISessionContext:
    """FastAPI dependency: assert ``tenant_admin`` role for the BFF session.

    Loads the :class:`~meho_backplane.ui.auth.session_store.DecryptedSession`
    to read the stored access token, then validates it via
    :func:`~meho_backplane.auth.jwt.verify_jwt_for_audience` to extract the
    :class:`~meho_backplane.auth.operator.TenantRole`. Raises ``403
    Forbidden`` when the role is below ``tenant_admin`` (i.e. ``operator``
    or ``read_only``). Raises ``401`` when the session is gone or the
    token fails JWT validation for any non-expiry reason.

    Token-expiry handling (G0.25 #1694): the load + verify steps run
    through :mod:`meho_backplane.ui.auth.refresh` -- the session is
    refreshed proactively when the row is within 60 s of
    ``expires_at``, and reactively when the JWT chain reports
    ``token_expired`` (the common case under the default sliding
    extension, where the row deliberately outlives the ~5-minute
    access token). A terminal refresh failure surfaces as ``401
    session_expired``, which the app-level handler in
    :mod:`meho_backplane.ui.auth.errors` maps to a login redirect for
    HTML requests instead of raw JSON.

    This is the T2 upload-RBAC gate: the :class:`UISessionContext` returned
    by :func:`require_ui_session` deliberately omits the role to keep the
    read-only surfaces free of JWT-decode overhead. State-changing upload
    routes add this dependency on top of ``require_ui_session`` to enforce
    ``tenant_admin``.

    Returns the same ``UISessionContext`` so callers can use it for
    ``tenant_id`` / ``operator_sub`` without a second dependency.
    """
    # Deferred import avoids a circular dependency: auth.middleware →
    # auth.jwt (ok) but auth.jwt → settings → (no ui module import).
    # The lazy import is inside the async function body (not module-level)
    # so it runs once per request; the overhead is negligible compared to
    # a DB round-trip + JWT decode. The refresh-module import follows the
    # same discipline for consistency within this function.
    from meho_backplane.settings import get_settings
    from meho_backplane.ui.auth.refresh import (
        load_fresh_session,
        verify_access_token_with_refresh,
    )

    log = structlog.get_logger(__name__)
    settings = get_settings()
    decrypted = await load_fresh_session(session.session_id)

    if decrypted is None:
        # Session disappeared between middleware check and here (revoked /
        # expired in the gap). Treat as unauthenticated.
        log.info(
            "ui_admin_gate_session_gone",
            session_id=str(session.session_id),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ui_session_required",
        )

    try:
        _refreshed, operator = await verify_access_token_with_refresh(
            decrypted,
            expected_audience=settings.keycloak_audience,
        )
    except HTTPException as exc:
        log.info(
            "ui_admin_gate_jwt_invalid",
            session_id=str(session.session_id),
            status_code=exc.status_code,
        )
        raise

    _assert_tenant_admin_role(operator.tenant_role, session_id=session.session_id)

    # Bind operator identity into structlog contextvars so AuditMiddleware
    # can attribute the write operation (reads operator_sub + tenant_id from
    # contextvars to decide whether to write an audit row).
    structlog.contextvars.bind_contextvars(
        operator_sub=session.operator_sub,
        tenant_id=str(session.tenant_id),
    )

    return session


def _assert_tenant_admin_role(tenant_role: TenantRole, *, session_id: uuid.UUID) -> None:
    """Raise 403 unless *tenant_role* ranks at least ``tenant_admin``.

    Extracted from :func:`require_ui_admin` (the gate's only caller)
    purely to keep the dependency under the chassis function-size
    budget; the rank semantics are unchanged from the T2 upload-RBAC
    landing.
    """
    from meho_backplane.auth.operator import TenantRole

    _role_order: tuple[TenantRole, ...] = (
        TenantRole.READ_ONLY,
        TenantRole.OPERATOR,
        TenantRole.TENANT_ADMIN,
    )
    try:
        actual_rank = _role_order.index(tenant_role)
        required_rank = _role_order.index(TenantRole.TENANT_ADMIN)
    except ValueError:
        actual_rank = -1
        required_rank = len(_role_order)

    if actual_rank < required_rank:
        log = structlog.get_logger(__name__)
        log.info(
            "ui_admin_gate_forbidden",
            session_id=str(session_id),
            actual_role=tenant_role.value,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant_admin_required",
        )
