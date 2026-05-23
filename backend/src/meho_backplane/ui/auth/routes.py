# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""BFF auth routes -- ``/ui/auth/{login,callback,logout}``.

Initiative #337 (G10.0 Frontend chassis), Task #865 (T4). This module
ships the FastAPI :class:`APIRouter` that handles the three BFF
auth surfaces:

* ``GET /ui/auth/login`` -- builds the PKCE-protected authorization
  URL and 302s the browser to Keycloak. The original
  ``?return_to=<path>`` query parameter (default ``/ui/``) is
  smuggled through the IdP round-trip via the PKCE verifier store
  keyed on ``state`` -- it never appears on the URL the IdP sees, so
  there is no open-redirect surface to harden.

* ``GET /ui/auth/callback`` -- Keycloak's exact-match redirect target.
  Verifies the ``state`` against the PKCE verifier store, exchanges
  ``code`` + ``code_verifier`` for access + refresh tokens at the
  token endpoint, validates the access token through the chassis
  JWT chain (so the BFF inherits the same issuer / audience /
  ``tenant_id`` / ``tenant_role`` defences as ``/api/*``), creates a
  server-side session row, sets the ``meho_session`` cookie
  (``HttpOnly; Secure; SameSite=Strict; Path=/``), and 302s to the
  originally-requested page.

* ``GET /ui/auth/logout`` -- revokes the session row, clears the
  ``meho_session`` cookie, and 302s to Keycloak's end-session
  endpoint. The IdP-side logout is best-effort: if the discovery doc
  does not advertise an ``end_session_endpoint`` (a pre-OIDC-Session-
  Management Keycloak release, hypothetically), the route still
  drops the cookie and redirects to ``/ui/auth/login`` locally.

This module imports but **does not** register itself onto
:func:`meho_backplane.main.app` -- T5 (#866) wires the router onto
the FastAPI app and decides the mount prefix. The route paths below
are relative to ``/ui/auth`` so the mount in T5 lands the public
surface at the documented URLs.

Cookie attributes
-----------------

The session cookie is the only piece of user-controllable state the
browser holds. Its attributes are non-negotiable per the BFF threat
model (decision #11 in :file:`docs/planning/v0.2-decisions.md`):

* ``HttpOnly`` -- JS cannot read the cookie. Defeats every
  XSS-to-token exfiltration vector.
* ``Secure`` -- TLS-only. The backplane terminates TLS at
  ``meho.evba.lab``; a downgrade to HTTP would silently fail because
  the cookie would not be sent.
* ``SameSite=Strict`` -- the cookie is omitted from cross-site
  navigations, blocking the entire CSRF class on state-changing
  routes (T5's CSRF token is belt-and-braces against same-site
  malicious sub-domains).
* ``Path=/`` -- mounted at the root so the cookie covers ``/api``
  fetches issued from the dashboard's HTMX surface as well as
  ``/ui/*`` page navigations.
* No ``Domain`` attribute -- defaults to the host-only cookie,
  which is the safest shape. Setting ``Domain=`` would expand the
  cookie to subdomains, which the deploy does not need.

Security-sensitive log discipline
---------------------------------

Every log line in this module is reviewed for token / secret /
verifier appearance. Permissible: ``state`` (per-flow CSRF token,
not a credential), ``operator_sub`` (already in the JWT chain's log
context), ``return_to`` (operator-supplied path, validated below).
Forbidden: ``code``, ``code_verifier``, ``access_token``,
``refresh_token``, the client secret. The :mod:`backend.tests.conftest`
secret-leak sweep would catch any drift; the explicit care taken
in this module is the first defence.

References
----------

* RFC 6265bis (cookies):
  https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/
* RFC 8707 (Resource Indicators):
  https://www.rfc-editor.org/rfc/rfc8707
* OIDC Session Management 1.0 (end_session_endpoint):
  https://openid.net/specs/openid-connect-session-1_0.html
* OAuth 2.0 for Browser-Based Apps BCP:
  https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated, Final
from urllib.parse import urlencode

import httpx
import structlog
from authlib.integrations.base_client.errors import (
    MismatchingStateError,
    OAuthError,
)
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.settings import Settings, get_settings
from meho_backplane.ui.auth.flow import (
    MISSING_CLIENT_SECRET_DETAIL,
    OAuthFlowConfigurationError,
    OAuthFlowError,
    TokenExchangeResult,
    build_authorization_request,
    exchange_code_for_tokens,
    resolve_oidc_endpoints,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    revoke_session,
)

__all__ = [
    "LOGIN_PATH",
    "SESSION_COOKIE_NAME",
    "build_router",
    "compute_redirect_uri",
    "set_session_cookie",
]


#: Path prefix mounted by T5 (#866) onto the FastAPI app. Kept as a
#: module-level constant so the middleware (which builds the redirect
#: URL when a session is missing) and the routes agree on the URL
#: shape.
LOGIN_PATH: Final[str] = "/ui/auth/login"
_CALLBACK_PATH: Final[str] = "/ui/auth/callback"
_LOGOUT_PATH: Final[str] = "/ui/auth/logout"

#: Name of the BFF session cookie. The browser holds only this opaque
#: value; the real tokens live encrypted in the ``web_session`` table.
SESSION_COOKIE_NAME: Final[str] = "meho_session"

#: Clock-skew margin trimmed off the access-token TTL when computing
#: the session lifetime. Keeps the session from outliving the access
#: token it represents -- if the token expires at T, the session
#: expires no later than T - margin so a refresh round-trip can still
#: succeed.
_SESSION_TTL_MARGIN_SECONDS: Final[int] = 60

#: Default landing path when no ``?return_to`` is supplied on
#: ``/ui/auth/login``. T5 (#866) lands the dashboard at ``/ui/``.
_DEFAULT_RETURN_TO: Final[str] = "/ui/"


def compute_redirect_uri() -> str:
    """Build the callback URL exact-match Keycloak enforces.

    Derived at request time (not cached at import) so a test that
    swaps :attr:`Settings.backplane_url` between cases sees the swap
    take effect without a process restart. Production deploys pin the
    URL via the env var; the redirect URI registered on the
    ``meho-web`` Keycloak client must match this value exactly --
    Keycloak rejects ``invalid_grant`` on the token endpoint
    otherwise.
    """
    base = get_settings().backplane_url.rstrip("/")
    return f"{base}{_CALLBACK_PATH}"


def _safe_return_to(raw: str | None) -> str:
    """Coerce *raw* into a safe local path or fall back to the default.

    The login route accepts a ``?return_to=`` query parameter so an
    unauthenticated request to a deep link can land there after
    auth. The value is operator-supplied via the middleware redirect,
    so it must be sanitised before it lands as a 302 ``Location``:

    * Empty / absent → ``/ui/`` default.
    * Absolute URLs (anything starting with a scheme or ``//``) →
      ``/ui/`` default. Open-redirect guard -- a crafted phishing
      link could otherwise bounce the operator to an attacker URL
      after auth.
    * Anything outside the ``/ui/`` subtree → ``/ui/`` default.
      Keeps the post-login redirect inside the operator-console
      surface; deep links into ``/api/*`` are not the BFF's job.

    The trailing slash on the default matters: ``/ui`` would 308 to
    ``/ui/``, doubling the round-trip.
    """
    if not raw:
        return _DEFAULT_RETURN_TO
    # Strip whitespace -- a stray newline from a misconstructed URL
    # would otherwise smuggle past the prefix check.
    candidate = raw.strip()
    if not candidate:
        return _DEFAULT_RETURN_TO
    if candidate.startswith("//") or "://" in candidate:
        return _DEFAULT_RETURN_TO
    if not candidate.startswith("/ui/") and candidate != "/ui":
        return _DEFAULT_RETURN_TO
    return candidate


def set_session_cookie(response: RedirectResponse, session_id: uuid.UUID) -> None:
    """Attach the ``meho_session`` cookie to *response* with the BFF flags.

    Centralised so every place the BFF sets the cookie (currently the
    callback handler; logout uses :func:`clear_session_cookie`) emits
    the exact same attributes. Drift between them is a security bug.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=str(session_id),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
        # ``max_age`` is deliberately omitted -- the cookie is a
        # session cookie (no persistence across browser quits). The
        # server-side row's ``expires_at`` is the actual TTL; a
        # browser that hangs on to the cookie past expiry simply
        # finds the session unloadable on next request and gets
        # bounced to login.
    )


def _clear_session_cookie(response: RedirectResponse) -> None:
    """Erase the ``meho_session`` cookie via the same attribute set.

    Setting a cookie with the same name + ``Max-Age=0`` + ``Path=/``
    expires it immediately on the browser. The other attributes must
    match the original ``set_cookie`` call (``Secure``, ``HttpOnly``,
    ``SameSite=Strict``) -- some user agents are picky about
    attribute parity on overwrite, so matching them removes the
    ambiguity.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _build_end_session_url(
    end_session_endpoint: str,
    *,
    post_logout_redirect_uri: str,
    client_id: str,
) -> str:
    """Construct the Keycloak end-session URL.

    Keycloak's end-session endpoint accepts ``post_logout_redirect_uri``
    + ``client_id`` to skip the confirmation page and land the
    operator back on the BFF login URL. We deliberately do NOT send
    ``id_token_hint`` -- it carries the operator's identity and would
    require us to keep the ID token alongside the access/refresh
    tokens (the chassis-locked decision #11 keeps only access +
    refresh for refresh-rotation; the ID token has no further use
    after the initial verify, and storing it just to drive a clean
    logout is unwarranted).
    """
    query = urlencode(
        {
            "post_logout_redirect_uri": post_logout_redirect_uri,
            "client_id": client_id,
        }
    )
    separator = "&" if "?" in end_session_endpoint else "?"
    return f"{end_session_endpoint}{separator}{query}"


async def _handle_login(
    return_to: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Mint a PKCE authorization URL and 302 the browser to Keycloak.

    ``?return_to=<path>`` is the originally-requested path the
    operator was bounced from (the middleware writes it). Declared as
    a typed FastAPI ``Query`` parameter so the OpenAPI document
    reflects the real query-string contract; the value is validated
    via :func:`_safe_return_to` to block open-redirect abuse.
    """
    log = structlog.get_logger(__name__)
    return_to = _safe_return_to(return_to)
    try:
        url, state = await build_authorization_request(
            redirect_uri=compute_redirect_uri(),
            return_to=return_to,
        )
    except OAuthFlowConfigurationError:
        log.warning("ui_auth_oauth_not_configured", route="login")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MISSING_CLIENT_SECRET_DETAIL,
        ) from None
    except httpx.HTTPError as exc:
        log.warning(
            "ui_auth_discovery_failed",
            route="login",
            error_class=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_auth_provider_unreachable",
        ) from exc
    log.info("ui_auth_login_redirect", state=state, return_to=return_to)
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


def _raise_idp_error(idp_error: str, idp_error_description: str | None) -> None:
    """Translate an IdP-emitted ``?error=...`` callback into a 400."""
    log = structlog.get_logger(__name__)
    log.warning(
        "ui_auth_callback_idp_error",
        idp_error=idp_error,
        idp_error_description=idp_error_description,
    )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="authorization_failed",
    )


async def _exchange_or_translate(
    *,
    state: str | None,
    authorization_response: str,
) -> TokenExchangeResult:
    """Run the token exchange and map every error class to an HTTPException.

    Encapsulates the chain of authlib / verifier-store / network
    failures the callback can hit so the calling handler stays under
    the 100-line code-quality cap. The token-side log discipline
    (don't surface specific failure causes in the response body, only
    in structlog) lives here.
    """
    log = structlog.get_logger(__name__)
    try:
        return await exchange_code_for_tokens(
            redirect_uri=compute_redirect_uri(),
            authorization_response=authorization_response,
            state=state,
        )
    except OAuthFlowConfigurationError:
        log.warning("ui_auth_oauth_not_configured", route="callback")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MISSING_CLIENT_SECRET_DETAIL,
        ) from None
    except (OAuthFlowError, MismatchingStateError, OAuthError) as exc:
        # State mismatch / verifier-store miss / IdP-side rejection
        # all collapse to one 400. The specific cause goes to
        # structlog only -- telegraphing it in the body helps an
        # attacker probe.
        log.warning(
            "ui_auth_callback_rejected",
            error_class=type(exc).__name__,
            state=state,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="authorization_failed",
        ) from exc
    except httpx.HTTPError as exc:
        log.warning(
            "ui_auth_token_endpoint_unreachable",
            error_class=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream_auth_provider_unreachable",
        ) from exc


async def _persist_session_from_tokens(
    tokens: TokenExchangeResult,
) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Validate the access token + write the encrypted session row.

    Returns ``(session_id, operator_sub, tenant_id)`` -- the values
    the calling handler needs to log + redirect, without holding the
    plaintext tokens any longer than necessary.
    """
    settings = get_settings()
    # Validate the access token through the chassis JWT chain so the
    # BFF inherits the same issuer / audience / sub / tenant_id /
    # tenant_role checks ``/api/*`` already enforces.
    operator = await verify_jwt_for_audience(
        f"Bearer {tokens.access_token}",
        expected_audience=settings.keycloak_audience,
    )
    lifetime = timedelta(seconds=max(tokens.expires_in - _SESSION_TTL_MARGIN_SECONDS, 60))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        decrypted = await create_session(
            session,
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            lifetime=lifetime,
        )
    return decrypted.id, operator.sub, operator.tenant_id


async def _handle_callback(
    request: Request,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Finish the OAuth round-trip, create the session, set the cookie.

    ``code``, ``state``, ``error``, and ``error_description`` are all
    declared as typed FastAPI ``Query`` parameters so the OpenAPI
    document reflects the real callback-URL contract. They are
    optional because the IdP only ever populates one of the
    ``code``/``state`` pair or the ``error``/``error_description``
    pair; absent values in the wrong combination collapse to a 400
    via :func:`_exchange_or_translate`. The full callback URL (which
    authlib re-parses for token exchange) still comes from
    ``request`` -- typed extraction is for OpenAPI fidelity, not a
    behaviour change.
    """
    log = structlog.get_logger(__name__)
    # ``code`` is consumed by ``str(request.url)`` below (authlib
    # re-parses the full URL); the typed extraction here exists purely
    # so the OpenAPI document lists the parameter.
    del code
    if error:
        _raise_idp_error(error, error_description)
    # ``str(request.url)`` carries the full callback URL with query
    # string -- authlib's :meth:`fetch_token` parses ``code`` and
    # ``state`` out of it.
    tokens = await _exchange_or_translate(
        state=state,
        authorization_response=str(request.url),
    )
    session_id, operator_sub, tenant_id = await _persist_session_from_tokens(tokens)
    log.info(
        "ui_auth_session_created",
        operator_sub=operator_sub,
        tenant_id=str(tenant_id),
        return_to=tokens.return_to,
        session_id=str(session_id),
    )
    response = RedirectResponse(
        url=tokens.return_to or _DEFAULT_RETURN_TO,
        status_code=status.HTTP_302_FOUND,
    )
    set_session_cookie(response, session_id)
    return response


async def _revoke_session_if_present(cookie_value: str | None) -> None:
    """Revoke the session row identified by *cookie_value*.

    No-op when the cookie is absent or malformed; the calling handler
    still 302s the operator at Keycloak so a half-stuck client can
    recover.
    """
    if not cookie_value:
        return
    log = structlog.get_logger(__name__)
    try:
        cookie_id = uuid.UUID(cookie_value)
    except ValueError:
        log.info("ui_auth_logout_malformed_cookie")
        return
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        await revoke_session(session, cookie_id)
    log.info("ui_auth_session_revoked", session_id=str(cookie_id))


async def _resolve_end_session_target(settings_: Settings) -> str:
    """Build the post-logout redirect URL.

    Falls back to a local ``/ui/auth/login`` redirect when the
    discovery doc is unreachable or does not advertise
    ``end_session_endpoint`` -- the session is already revoked at
    that point, so the IdP-side logout is best-effort.
    """
    log = structlog.get_logger(__name__)
    try:
        endpoints = await resolve_oidc_endpoints()
    except httpx.HTTPError as exc:
        log.warning(
            "ui_auth_logout_discovery_failed",
            error_class=type(exc).__name__,
        )
        endpoints = None
    post_logout = f"{settings_.backplane_url.rstrip('/')}{LOGIN_PATH}"
    end_session_url = endpoints.end_session_endpoint if endpoints is not None else None
    client_id = settings_.ui_keycloak_client_id
    if end_session_url and client_id:
        return _build_end_session_url(
            end_session_url,
            post_logout_redirect_uri=post_logout,
            client_id=client_id,
        )
    return post_logout if post_logout != LOGIN_PATH else LOGIN_PATH


async def _handle_logout(request: Request) -> RedirectResponse:
    """Revoke the session row, clear the cookie, 302 to the end-session URL."""
    await _revoke_session_if_present(request.cookies.get(SESSION_COOKIE_NAME))
    target = await _resolve_end_session_target(get_settings())
    response = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    _clear_session_cookie(response)
    return response


def build_router() -> APIRouter:
    """Construct the ``/ui/auth/*`` :class:`APIRouter`.

    A factory function (rather than a module-level constant) so T5
    (#866) can mount the router with whatever prefix the FastAPI app
    decides to expose it under. Each invocation produces a fresh
    router -- the routes themselves are pure handlers (defined at
    module level), so multiple routers attached to different apps
    under test do not share mutable state.
    """
    router = APIRouter(prefix="/ui/auth", tags=["ui-auth"])
    # ``response_class`` + ``status_code`` on each route declare the
    # real 302-redirect contract in the OpenAPI document so the CLI's
    # generated Go client at ``cli/internal/api/client.gen.go``
    # reflects the runtime behaviour. Without these, FastAPI defaults
    # the OpenAPI response to ``200 application/json`` -- correct for
    # the typical handler, wrong for a pure-redirect surface.
    router.add_api_route(
        "/login",
        _handle_login,
        methods=["GET"],
        name="ui_auth_login",
        response_class=RedirectResponse,
        status_code=status.HTTP_302_FOUND,
    )
    router.add_api_route(
        "/callback",
        _handle_callback,
        methods=["GET"],
        name="ui_auth_callback",
        response_class=RedirectResponse,
        status_code=status.HTTP_302_FOUND,
    )
    router.add_api_route(
        "/logout",
        _handle_logout,
        methods=["GET"],
        name="ui_auth_logout",
        response_class=RedirectResponse,
        status_code=status.HTTP_302_FOUND,
    )
    return router
