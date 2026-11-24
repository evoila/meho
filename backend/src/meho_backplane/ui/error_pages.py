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

from typing import Final

import structlog
from fastapi import Request, Response, status
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.ui.auth.errors import ui_session_expired_exception_handler
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

#: The console's front door, for the link on the page.
_HOME_PATH: Final[str] = "/ui"

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


def _render(request: Request, code: int) -> HTMLResponse:
    """Render the branded page for *code*."""
    heading, message = _PAGES[code]
    return get_templates().TemplateResponse(
        request,
        _TEMPLATE,
        {
            "status": code,
            "heading": heading,
            "message": message,
            "home_path": _HOME_PATH,
        },
        status_code=code,
    )


#: The statuses that get a page. Deliberately disjoint from the three
#: the session handler claims (401 ``session_expired``, 400
#: ``authorization_state_expired``, 409 ``tenant_removed``), so the two
#: handlers never contend for the same error and the order between them
#: cannot change behaviour.
_PAGE_STATUSES: Final[tuple[int, ...]] = (
    status.HTTP_403_FORBIDDEN,
    status.HTTP_404_NOT_FOUND,
)


async def web_http_exception_handler(request: Request, exc: Exception) -> Response:
    """Render 404 and 403 as a page on a web address; delegate the rest.

    Wraps the session handler rather than replacing it -- see the module
    docstring on ordering. The two cannot collide: that handler acts
    only on 401, 400 and 409, and this one only on 403 and 404.
    """
    if (
        isinstance(exc, StarletteHTTPException)
        and exc.status_code in _PAGE_STATUSES
        and is_web_path(request.url.path)
    ):
        return _render(request, exc.status_code)
    return await ui_session_expired_exception_handler(request, exc)


async def web_server_error_handler(request: Request, exc: Exception) -> Response:
    """Render 500 as a page on a web address; JSON everywhere else.

    Must not raise: ``ServerErrorMiddleware`` sends whatever comes back
    and then re-raises the original error, so an exception here would
    lose the page and leave the caller with a bare 500.
    """
    if is_web_path(request.url.path):
        try:
            return _render(request, status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:  # pragma: no cover - the page itself failing
            _log.exception("web_error_page_render_failed", path=request.url.path)
    return Response(
        content='{"detail":"Internal Server Error"}',
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        media_type="application/json",
    )
