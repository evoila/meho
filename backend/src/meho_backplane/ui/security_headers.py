# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Clickjacking-defence response headers for the operator console.

Ships :class:`UIFramingHeadersMiddleware` -- a pure-ASGI middleware that
stamps every ``/ui/*`` response with the two OWASP-recommended
anti-framing headers:

* ``Content-Security-Policy: frame-ancestors 'none'`` -- the modern
  (CSP Level 2) control that forbids the page being rendered inside any
  ``<frame>`` / ``<iframe>`` / ``<object>`` / ``<embed>``.
* ``X-Frame-Options: DENY`` -- the legacy equivalent, sent alongside CSP
  for the older browsers that predate ``frame-ancestors`` support.

Sending both is the cheat-sheet's defence-in-depth guidance: the headers
are independent, so emitting more than one is the recommended posture.

Scoping
-------

Only ``/ui/*`` responses are stamped. The ``/api/*`` and ``/mcp`` JSON
surfaces are not browser-framed UI and adding a CSP there would be noise
at best and a future false-signal at worst; the middleware short-circuits
on out-of-prefix paths, mirroring :class:`UISessionMiddleware`'s
``/ui/``-only scoping. The CSP is deliberately ``frame-ancestors``-only:
this is the clickjacking control, not a full content-security policy --
broadening it to ``script-src`` / ``style-src`` would risk breaking the
console's HTMX + Alpine + inline-script render and is a separate concern.

Pure ASGI, not :class:`BaseHTTPMiddleware`
------------------------------------------

Mirrors :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`
and :class:`~meho_backplane.middleware.RequestContextMiddleware`:
Starlette's own header-stamping middlewares (e.g. ``GZipMiddleware``)
wrap ``send`` and mutate the ``http.response.start`` message in place via
:class:`~starlette.datastructures.MutableHeaders`. That avoids the anyio
task wrapper ``BaseHTTPMiddleware`` spawns and keeps the header injection
on the response-start path for redirects (302 to login) and streaming
(SSE) responses alike. ``setdefault`` is used so a route that ever sets
its own ``frame-ancestors`` CSP keeps its value rather than being
clobbered by a blanket default.

References
----------

* OWASP Clickjacking Defense Cheat Sheet:
  https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html
"""

from __future__ import annotations

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "FRAME_ANCESTORS_CSP",
    "X_FRAME_OPTIONS",
    "UIFramingHeadersMiddleware",
]

#: Prefix every UI route lives under. Matches
#: :data:`~meho_backplane.ui.auth.middleware._UI_PREFIX`; out-of-prefix
#: paths pass through untouched.
_UI_PREFIX: Final[str] = "/ui/"
#: The bare console root WITHOUT the trailing slash. FastAPI answers a
#: ``GET /ui`` with a ``307`` redirect to the canonical ``/ui/``; that
#: redirect would otherwise slip past a ``startswith("/ui/")`` guard, so
#: the canonical entrypoint is matched exactly to keep the headers on
#: every console response (defence-in-depth over a redirect that itself
#: cannot be meaningfully framed).
_UI_ROOT: Final[str] = "/ui"
#: CSP value denying all framing (CSP Level 2 ``frame-ancestors``).
FRAME_ANCESTORS_CSP: Final[str] = "frame-ancestors 'none'"
#: Legacy anti-framing header value for pre-CSP browsers.
X_FRAME_OPTIONS: Final[str] = "DENY"


class UIFramingHeadersMiddleware:
    """Pure-ASGI middleware: stamp anti-clickjacking headers on ``/ui/*``.

    Constructed once at app startup and bound onto the FastAPI app via
    :meth:`FastAPI.add_middleware` in :mod:`meho_backplane.main`. The
    class is stateless; per-response work happens in the wrapped
    ``send`` closure.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP scopes (websocket, lifespan) pass through untouched.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path")
        # Out-of-prefix paths pass through unchanged -- the headers only
        # protect the browser-rendered ``/ui/*`` console, not the JSON
        # API / MCP surfaces. The bare ``/ui`` (no trailing slash) is
        # matched exactly alongside the ``/ui/`` prefix so the canonical
        # redirect to ``/ui/`` is stamped too.
        if not (isinstance(path, str) and (path == _UI_ROOT or path.startswith(_UI_PREFIX))):
            await self.app(scope, receive, send)
            return

        async def send_with_framing_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # ``setdefault`` so a route that deliberately sets its
                # own ``frame-ancestors`` policy is not clobbered.
                headers.setdefault("content-security-policy", FRAME_ANCESTORS_CSP)
                headers.setdefault("x-frame-options", X_FRAME_OPTIONS)
            await send(message)

        await self.app(scope, receive, send_with_framing_headers)
