# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Branded error pages for web addresses; JSON everywhere else.

A browser that reaches a wrong console address should get a page that
looks like the product. A client that reaches a wrong API address must
still get JSON, because something is parsing that answer -- an agent, a
script, a retry loop. Handing HTML to an API caller turns a clean 404
into a parse error, and the caller then reports something unrelated to
what actually happened.

**The split is by route, not by ``Accept``.** A browser sends
``Accept: text/html`` to every address it is pointed at, including an
API one. Deciding on the header alone would hand a browser-shaped
client an HTML body from ``/api/v1/...`` and break exactly the contract
above. So the question this module asks is "is this path a web page?",
and only a path under :data:`WEB_PREFIXES` is.

Three statuses get a page: 404, 403 and 500. Every other status, and
every path outside :data:`WEB_PREFIXES`, is left to the handler that
was already there.

**A console address is not by itself a page.** Many ``/ui`` routes
answer htmx and ``fetch`` callers that read the JSON body -- the run
console's Stop button shows a 403's ``detail`` to say who may stop
another person's run, and a 403 elsewhere names the missing permission.
Replacing those with a generic page would drop text the caller needs,
so the page is rendered only for a *document navigation* and every
other caller keeps the response it had.

Ordering with the session handler
---------------------------------

``meho_backplane.ui.auth.errors.ui_session_expired_exception_handler``
owns the same :class:`starlette.exceptions.HTTPException` key: it maps
a signed-out ``/ui/*`` request to a login redirect. Only one handler
can hold a key, so this module wraps it rather than competing with it
-- the session cases are decided first and keep their behaviour, and
what it would have delegated to FastAPI's stock handler arrives here
instead. A 403 that means "sign in" therefore still redirects; a 403
that means "you may not" renders the page.

The 500 path is separate
------------------------

An unhandled exception never becomes an ``HTTPException``. Starlette's
:class:`~starlette.middleware.errors.ServerErrorMiddleware` catches it,
calls the handler registered for :class:`Exception`, sends that
response -- and then **re-raises**, so the server still logs the
traceback. Two consequences worth knowing: the handler must not raise
(a failure there replaces the page with a bare 500), and a test must
pass ``raise_server_exceptions=False`` to the test client or the
re-raise surfaces in the test instead of the response.

References
----------

* FastAPI -- override + reuse the default exception handlers:
  https://fastapi.tiangolo.com/tutorial/handling-errors/
* Starlette ``ServerErrorMiddleware`` (re-raises after handling):
  https://www.starlette.io/exceptions/
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Final

import structlog
from fastapi import Request, Response, status
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.ui.auth.errors import ui_session_expired_exception_handler
from meho_backplane.ui.fragment_guard import is_document_navigation
from meho_backplane.ui.security_headers import FRAME_ANCESTORS_CSP, X_FRAME_OPTIONS
from meho_backplane.ui.templating import get_templates

__all__ = [
    "WEB_PREFIXES",
    "is_web_path",
    "web_http_exception_handler",
    "web_server_error_handler",
]

_log = structlog.get_logger(__name__)

#: The path prefixes that answer with a page rather than with JSON.
#:
#: Everything else the app serves is a machine surface: ``/api/v1``,
#: ``/mcp``, ``/.well-known``, ``/metrics`` and the health endpoints.
#: Keeping the web list short and explicit, rather than listing the
#: machine prefixes and treating the remainder as web, means a new
#: machine surface added tomorrow keeps its JSON by default.
WEB_PREFIXES: Final[tuple[str, ...]] = ("/ui",)

_TEMPLATE: Final[str] = "errors/web_error.html"

#: What each status says. The text is fixed here and never includes
#: anything from the request, so the page shows only its own words.
_PAGES: Final[dict[int, tuple[str, str]]] = {
    status.HTTP_403_FORBIDDEN: (
        "Not allowed",
        "This account may not open that page. If that looks wrong, ask whoever "
        "manages your access.",
    ),
    status.HTTP_404_NOT_FOUND: (
        "Page not found",
        "That address does not match a page in the console. It may have moved, "
        "or the link may be incomplete.",
    ),
    status.HTTP_500_INTERNAL_SERVER_ERROR: (
        "Something went wrong",
        "The console could not finish that request. The error has been logged.",
    ),
}


def is_web_path(path: str) -> bool:
    """Return whether *path* is a web page address rather than an API one.

    A prefix matches on a segment boundary, so ``/ui`` and ``/ui/agents``
    are web while a hypothetical ``/uitest`` is not.
    """
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in WEB_PREFIXES)


#: Longest reason rendered on the page. A route composing something far
#: larger is not writing for a reader, and an unbounded string on an
#: error page is how an address ends up quoted back at full length.
_REASON_MAX: Final[int] = 200


def _echoes_the_address(text: str, request: Request) -> bool:
    """True when *text* quotes a caller-supplied part of the address back.

    Several routes build a message by interpolating a path parameter --
    ``f"target {name!r} not found"`` -- which puts whatever was in the
    address onto the page. The page is meant to explain the refusal, not
    to repeat what was typed.

    The test is against the matched **path parameters**, not against the
    path as a whole. A message may legitimately contain a word that also
    appears as a fixed segment -- "keycloak authoring requires
    tenant_admin" on ``/ui/keycloak/...`` -- and rejecting those would
    throw away the explanations this page exists to carry. Only the
    variable parts of the route are caller-supplied.
    """
    params = request.scope.get("path_params") or {}
    lowered = text.casefold()
    return any(
        len(value) > 2 and value.casefold() in lowered
        for value in (str(v) for v in params.values())
    )


def _reason(exc: Exception, code: int, request: Request) -> str | None:
    """The route's own explanation, when it added one worth showing.

    A 403 is rarely just "no". The routes say which role is required --
    ``Your role cannot decide approval requests`` -- or which account to
    use, and a generic page throws that away.

    Two shapes carry one, and they are trusted differently.

    A ``dict`` detail with a ``message`` is a structured refusal the
    route composed *for a reader*: the citations 403 names the capability
    the identity is missing. That message is used as given.

    A plain string is a diagnostic that happens to be shown, and several
    are built by interpolating a path segment. Those are dropped rather
    than quoting the address back onto the page.

    Starlette fills ``detail`` with the status phrase when the raiser
    passed none, so "Forbidden" and "Not Found" are dropped too:
    repeating the status as a reason tells the reader nothing.

    Jinja escapes whatever survives, so nothing here can carry markup
    onto the page.
    """
    if not isinstance(exc, StarletteHTTPException):
        return None
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message")
        text = message.strip() if isinstance(message, str) else ""
    elif isinstance(detail, str):
        text = detail.strip()
        if text and _echoes_the_address(text, request):
            return None
    else:
        return None
    if not text or text.casefold() == HTTPStatus(code).phrase.casefold():
        return None
    return text[:_REASON_MAX]


def _render(request: Request, code: int, reason: str | None = None) -> Response:
    """Render the branded page for *code*, anti-framing headers included.

    The headers are stamped here rather than left to
    :class:`~meho_backplane.ui.security_headers.UIFramingHeadersMiddleware`
    because the 500 path does not pass through it:
    ``ServerErrorMiddleware`` sits outside every application middleware,
    so a page rendered from the 500 handler would otherwise ship without
    them and could be framed by another site. Stamping unconditionally
    keeps the two paths identical; the middleware setting the same values
    again on the 404/403 path is a no-op.
    """
    heading, message = _PAGES[code]
    response: Response = get_templates().TemplateResponse(
        request,
        _TEMPLATE,
        {
            "status": code,
            "heading": heading,
            "message": message,
            "reason": reason,
        },
        status_code=code,
    )
    response.headers["Content-Security-Policy"] = FRAME_ANCESTORS_CSP
    response.headers["X-Frame-Options"] = X_FRAME_OPTIONS
    return response


def _wants_a_page(request: Request) -> bool:
    """True when this request would replace the whole page in a browser.

    A console address does not imply a page. Plenty of ``/ui`` routes
    answer htmx and ``fetch`` callers, and those callers read the JSON
    body: the run console's Stop button shows the 403 ``detail`` to say
    who may stop another person's run. Handing them HTML would drop that
    text on the floor, so only a document navigation gets the page and
    every other caller keeps the response it had.

    :func:`~meho_backplane.ui.fragment_guard.is_document_navigation` is
    the console's existing answer to this question -- htmx is never a
    navigation, ``Sec-Fetch-Dest`` decides where it is sent, and an
    ``Accept`` asking for HTML is the fallback.
    """
    return is_document_navigation(request.scope)


#: The statuses that get a page. Deliberately disjoint from the three
#: the session handler claims (401 ``session_expired``, 400
#: ``authorization_state_expired``, 409 ``tenant_removed``), so the two
#: handlers never contend for the same error and the order between them
#: cannot change behaviour.
_PAGE_STATUSES: Final[tuple[int, ...]] = (
    status.HTTP_403_FORBIDDEN,
    status.HTTP_404_NOT_FOUND,
    status.HTTP_500_INTERNAL_SERVER_ERROR,
)


async def web_http_exception_handler(request: Request, exc: Exception) -> Response:
    """Render 404 and 403 as a page on a web address; delegate the rest.

    Wraps the session handler rather than replacing it -- see the module
    docstring on ordering. The two cannot collide: that handler acts
    only on 401, 400 and 409, and this one only on 403, 404 and 500.

    A page is rendered only for a document navigation -- an htmx or
    ``fetch`` caller on the same address keeps the JSON body and its
    ``detail``.
    """
    if (
        isinstance(exc, StarletteHTTPException)
        and exc.status_code in _PAGE_STATUSES
        and is_web_path(request.url.path)
        and _wants_a_page(request)
    ):
        return _render(request, exc.status_code, _reason(exc, exc.status_code, request))
    return await ui_session_expired_exception_handler(request, exc)


async def web_server_error_handler(request: Request, exc: Exception) -> Response:
    """Render 500 as a page on a web address; JSON everywhere else.

    Must not raise: ``ServerErrorMiddleware`` sends whatever comes back
    and then re-raises the original error, so an exception here would
    lose the page and leave the caller with a bare 500.
    """
    if is_web_path(request.url.path) and _wants_a_page(request):
        try:
            return _render(request, status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:  # pragma: no cover - the page itself failing
            _log.exception("web_error_page_render_failed", path=request.url.path)
    # Exactly what ``ServerErrorMiddleware.error_response`` has always
    # sent. Anything parsing a 500 today -- an agent, a script, a retry
    # loop -- is reading this body, so the only safe default is to keep
    # it byte for byte and change the response on web navigations alone.
    return PlainTextResponse(
        "Internal Server Error",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
