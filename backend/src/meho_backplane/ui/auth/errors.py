# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``session_expired`` exception handler -- HTML gets a redirect (G0.25 #1694).

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
guide). The handler intercepts exactly one shape:

* status ``401`` **and** detail
  :data:`~meho_backplane.ui.auth.refresh.SESSION_EXPIRED_DETAIL`
  **and** path under ``/ui/`` **and** the request ``Accept`` header
  names ``text/html``

and answers it with a ``302`` to
``/ui/auth/login?return_to=<original path+query>`` -- the same
redirect contract :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`
uses for missing sessions -- plus a ``meho_session`` cookie clear
(the row is terminally dead once a refresh has failed; RFC 6749 § 6
gives the BFF nothing left to present). Non-HTML callers on the same
401 get the regular JSON body, with the cookie cleared as well.
**Everything else** -- other detail codes, other statuses, other
paths -- delegates verbatim to
:func:`fastapi.exception_handlers.http_exception_handler`, so the
chassis-wide error contract (including ``/api/*``'s structured 401
codes) is bit-for-bit unchanged.

Scoping the interception to the ``session_expired`` detail code --
minted only by :mod:`meho_backplane.ui.auth.refresh` -- rather than
to "any 401 with an HTML Accept" is deliberate: blanket-redirecting
401s would swallow real diagnostics (``signature_verification_failed``,
``invalid_audience``) behind a login bounce loop.

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
from meho_backplane.ui.auth.routes import LOGIN_PATH, clear_session_cookie

__all__ = ["ui_session_expired_exception_handler"]


_UI_PREFIX = "/ui/"


def _is_session_expired_ui_request(request: Request, exc: StarletteHTTPException) -> bool:
    """True when *exc* is the BFF refresh-failure 401 on a ``/ui/*`` path."""
    return (
        exc.status_code == status.HTTP_401_UNAUTHORIZED
        and exc.detail == SESSION_EXPIRED_DETAIL
        and request.url.path.startswith(_UI_PREFIX)
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


async def ui_session_expired_exception_handler(
    request: Request,
    exc: Exception,
) -> Response:
    """App-level handler: map ``session_expired`` 401s to a re-auth flow.

    Registered for :class:`starlette.exceptions.HTTPException` (which
    makes this the app's HTTPException handler, period -- hence the
    explicit delegation to FastAPI's stock
    :func:`~fastapi.exception_handlers.http_exception_handler` for
    every non-matching exception). The parameter is typed
    ``Exception`` to satisfy Starlette's ``ExceptionHandler``
    signature; the isinstance narrow below is the registration
    contract made explicit.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registration contract
        raise exc
    if not _is_session_expired_ui_request(request, exc):
        return await http_exception_handler(request, exc)

    accept = request.headers.get("accept", "")
    log = structlog.get_logger(__name__)
    if "text/html" in accept:
        log.info(
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
