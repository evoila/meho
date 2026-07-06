# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recoverable-auth-state exception handler -- HTML gets a redirect.

FastAPI's default :class:`HTTPException` handler renders every error
as ``application/json``, regardless of the request's ``Accept``
header. For the chassis ``/api/*`` surface that is exactly right;
for a browser navigating ``/ui/*`` it is the FE-3 dogfood failure --
the operator stares at ``{"detail": "..."}`` where a login page
should be.

This module ships the app-level exception handler
:func:`ui_session_expired_exception_handler`, registered in
:mod:`meho_backplane.main` for
:class:`starlette.exceptions.HTTPException` (FastAPI routes raise
:class:`fastapi.HTTPException`, a subclass, so one registration
covers both -- the override pattern from the FastAPI handling-errors
guide). The handler intercepts exactly two recoverable shapes, both
only when the request ``Accept`` header names ``text/html``:

* **Session expired** (G0.25 #1694) -- status ``401`` **and** detail
  :data:`~meho_backplane.ui.auth.refresh.SESSION_EXPIRED_DETAIL`
  **and** path under ``/ui/``. Answered with a ``302`` to
  ``/ui/auth/login?return_to=<original path+query>`` -- the same
  redirect contract
  :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware` uses
  for missing sessions -- plus a ``meho_session`` cookie clear (the row
  is terminally dead once a refresh has failed; RFC 6749 § 6 gives the
  BFF nothing left to present).

* **Expired callback state** (G0.29 #2089, Leg 1) -- status ``400``
  **and** detail
  :data:`~meho_backplane.ui.auth.routes.AUTHORIZATION_STATE_EXPIRED_DETAIL`
  **and** path ``/ui/auth/callback``. The operator let the login
  window lapse (``STATE_TTL``), so the ``state`` / PKCE verifier
  expired. Answered with a ``303`` to ``/ui/auth/login`` to restart the
  flow on one click. No cookie is touched -- the callback runs
  pre-session. No ``return_to`` is carried: it was stashed with the
  now-expired verifier and cannot be recovered.

Non-HTML callers on either shape get the regular structured JSON body
(the ``session_expired`` case also drops the dead cookie).
**Everything else** -- other detail codes, other statuses, other
paths, the callback's genuine IdP ``authorization_failed`` and its
token-endpoint ``502`` -- delegates verbatim to
:func:`fastapi.exception_handlers.http_exception_handler`, so the
chassis-wide error contract (including ``/api/*``'s structured 401
codes) is bit-for-bit unchanged.

Scoping each interception to a specific detail code -- ``session_expired``
minted only by :mod:`meho_backplane.ui.auth.refresh`,
``authorization_state_expired`` minted only by the recoverable branch
of :mod:`meho_backplane.ui.auth.routes` -- rather than to "any 401/400
with an HTML Accept" is deliberate: blanket-redirecting would swallow
real diagnostics (``signature_verification_failed``, an IdP-declined
``authorization_failed``) behind a login bounce loop.

References
----------

* FastAPI -- override + reuse the default exception handlers:
  https://fastapi.tiangolo.com/tutorial/handling-errors/
"""

from __future__ import annotations

from urllib.parse import quote

import structlog
from fastapi import Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.ui.auth.refresh import SESSION_EXPIRED_DETAIL
from meho_backplane.ui.auth.routes import (
    AUTHORIZATION_STATE_EXPIRED_DETAIL,
    LOGIN_PATH,
    clear_session_cookie,
)

__all__ = ["ui_session_expired_exception_handler"]


_UI_PREFIX = "/ui/"

#: Exact path of the OAuth callback handler. The recoverable
#: callback-state interception below is scoped to this one route so a
#: ``400 authorization_state_expired`` raised from anywhere else (there
#: is nowhere else today, but the scoping is defence-in-depth) is left
#: to FastAPI's stock JSON handler.
_CALLBACK_PATH = "/ui/auth/callback"


def _is_session_expired_ui_request(request: Request, exc: StarletteHTTPException) -> bool:
    """True when *exc* is the BFF refresh-failure 401 on a ``/ui/*`` path."""
    return (
        exc.status_code == status.HTTP_401_UNAUTHORIZED
        and exc.detail == SESSION_EXPIRED_DETAIL
        and request.url.path.startswith(_UI_PREFIX)
    )


def _is_expired_callback_state_request(request: Request, exc: StarletteHTTPException) -> bool:
    """True when *exc* is the recoverable expired-callback-state 400.

    Matches the ``400 authorization_state_expired`` raised by
    :func:`meho_backplane.ui.auth.routes._exchange_or_translate` on
    ``GET /ui/auth/callback`` when the ``state`` / verifier expired or
    mismatched (G0.29 #2089, Leg 1). Scoped to the callback path and
    that exact detail code so the genuine IdP ``?error=`` path (which
    raises ``authorization_failed``) and the token-endpoint-unreachable
    502 are never swept into the login-restart affordance.
    """
    return (
        exc.status_code == status.HTTP_400_BAD_REQUEST
        and exc.detail == AUTHORIZATION_STATE_EXPIRED_DETAIL
        and request.url.path == _CALLBACK_PATH
    )


def _login_redirect(request: Request) -> RedirectResponse:
    """302 to login carrying the original path+query as ``return_to``.

    Mirrors the middleware's redirect shape
    (:func:`meho_backplane.ui.auth.middleware._redirect_to_login`):
    the value is percent-encoded wholesale and re-validated by the
    login route's ``_safe_return_to`` before it lands in any
    follow-up ``Location``, so a crafted path cannot become an open
    redirect here either.
    """
    path = request.url.path
    query = request.url.query
    full_path = f"{path}?{query}" if query else path
    location = f"{LOGIN_PATH}?return_to={quote(full_path, safe='')}"
    response = RedirectResponse(url=location, status_code=status.HTTP_302_FOUND)
    response.headers["cache-control"] = "no-store"
    return response


def _login_restart_redirect() -> RedirectResponse:
    """303 to a fresh login flow after an expired callback state.

    Unlike :func:`_login_redirect`, no ``return_to`` is carried: the
    originally-requested path was stashed in the PKCE verifier store
    keyed on the ``state`` that just expired, so it is unrecoverable by
    the time this handler runs. A bare ``/ui/auth/login`` restarts the
    flow and lands the operator on the ``/ui/`` default -- the same
    outcome a manual "start over" produces, minus the hand-navigation.

    ``303 See Other`` (not 302) is the correct status for "the recovery
    is a *new GET* on the login route": it tells the browser to follow
    with GET regardless of the original method, and side-steps the
    302-method-preservation ambiguity older agents carry.
    """
    response = RedirectResponse(url=LOGIN_PATH, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["cache-control"] = "no-store"
    return response


async def _handle_session_expired(request: Request, exc: StarletteHTTPException) -> Response:
    """Map the refresh-failure 401 to a login bounce (HTML) or JSON body.

    The dead session cookie is dropped in both branches -- the row is
    terminally unloadable once refresh has failed, so the client has
    no further use for the id.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        structlog.get_logger(__name__).info(
            "ui_session_expired_redirect",
            path=request.url.path,
        )
        response: Response = _login_redirect(request)
    else:
        # JSON callers (htmx fragment fetches without an HTML Accept,
        # scripted probes) keep the structured body the AC pins; only
        # the dead cookie is dropped.
        response = await http_exception_handler(request, exc)
    clear_session_cookie(response)
    return response


async def _handle_expired_callback_state(request: Request, exc: StarletteHTTPException) -> Response:
    """Map the recoverable expired-callback-state 400 to a login restart.

    HTML navigations (an operator who let the login window lapse) get a
    ``303`` back to ``/ui/auth/login`` so the flow restarts on one click
    instead of dead-ending on raw JSON (G0.29 #2089, Leg 1). No cookie
    is touched -- the callback runs pre-session, so there is no
    ``meho_session`` to clear. Non-HTML / scripted callers keep the
    structured ``authorization_state_expired`` body.
    """
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        structlog.get_logger(__name__).info(
            "ui_auth_callback_state_expired_restart",
            path=request.url.path,
        )
        return _login_restart_redirect()
    return await http_exception_handler(request, exc)


async def ui_session_expired_exception_handler(
    request: Request,
    exc: Exception,
) -> Response:
    """App-level handler: map recoverable BFF auth states to a re-auth flow.

    Registered for :class:`starlette.exceptions.HTTPException` (which
    makes this the app's HTTPException handler, period -- hence the
    explicit delegation to FastAPI's stock
    :func:`~fastapi.exception_handlers.http_exception_handler` for
    every non-matching exception). The parameter is typed
    ``Exception`` to satisfy Starlette's ``ExceptionHandler``
    signature; the isinstance narrow below is the registration
    contract made explicit.

    Two recoverable shapes are intercepted, both only for HTML
    navigations (JSON / scripted callers keep the structured body):

    * ``401 session_expired`` on ``/ui/*`` (G0.25 #1694) -- refresh
      failed, bounce to login carrying ``return_to``.
    * ``400 authorization_state_expired`` on ``/ui/auth/callback``
      (G0.29 #2089) -- the login window lapsed, restart the flow.

    Everything else -- other detail codes, other statuses, other paths,
    including the ``/api/*`` structured 401 codes and the callback's
    genuine IdP ``authorization_failed`` / token-endpoint ``502`` --
    delegates verbatim to FastAPI's stock handler.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registration contract
        raise exc
    if _is_session_expired_ui_request(request, exc):
        return await _handle_session_expired(request, exc)
    if _is_expired_callback_state_request(request, exc):
        return await _handle_expired_callback_state(request, exc)
    return await http_exception_handler(request, exc)
