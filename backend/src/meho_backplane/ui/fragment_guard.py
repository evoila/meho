# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stop a console fragment from being served as a standalone page.

Most ``/ui/*`` routes answer with a *fragment* -- a scrap of HTML htmx
swaps into an element of a page that is already open. ``GET
/ui/approvals/badge`` is the smallest example: it renders the pending
count and nothing else, so a browser pointed straight at that URL shows
a bare number on a blank page.

That is reachable. The login bounce carries the path the person came
from as ``return_to``; when a background htmx poll is what first met the
dead session, the path it carries is the *fragment's*. #5518 fixed the
common case by preferring htmx's ``HX-Current-URL`` over the request
path, so the poll no longer parks the browser on the fragment. What it
could not fix is the browser that is *already* there -- a tab left on
``/ui/approvals/badge``, a bookmark, a history entry. Reloading any of
those renders the partial as a whole document again.

This middleware closes that off from the other end: a fragment never
renders as a page, whatever asked for it.

The discriminator
-----------------

Two things have to be true before the guard acts.

1. **The request is a top-level navigation.** A browser says so with
   ``Sec-Fetch-Dest: document`` (Chrome 80+, Firefox 90+, Safari
   16.4+); older browsers are read from an ``Accept`` header that asks
   for ``text/html``. An htmx request is excluded outright -- it sends
   ``HX-Request: true`` and *wants* the fragment.

2. **The response is not a whole document.** Decided by looking at the
   first bytes of the body, not at the route or the template.

The second point is the load-bearing one, and it is why this is a
response-body guard rather than a list of fragment routes. Neither of
the cheaper signals holds:

* **Route paths.** There are 131 ``GET /ui`` routes and the route table
  carries no page-vs-fragment mark. Adding one means marking ~60 routes
  across a dozen modules, and every route added later has to remember.
* **Template naming.** The ``_name.html`` convention is real but leaky:
  ``auth/session_ended.html``, ``tenant/choose.html`` and
  ``tenant/refused.html`` carry no underscore and are whole documents,
  while ``_stub.html`` and ``topology/_graph_overlay_error.html`` carry
  one and extend a layout.
* **``{% extends %}``.** Same three templates emit ``<!doctype html>``
  without extending anything, so "does not extend a layout" reads them
  as fragments.

What the rendered bytes say is true by construction, and it also covers
the routes that build an ``HTMLResponse`` by hand instead of going
through the template helper.

Where it sends them
-------------------

To the nearest ancestor that is actually routed --
``/ui/approvals/badge`` becomes ``/ui/approvals``. Stopping at the
*immediate* parent is not enough: of the 34 fragments the console
serves at a fixed path, 7 have a parent that is not a route at all
(``/ui/vault/delete/confirm`` sits under ``/ui/vault/delete``, which
nothing serves), so the redirect would land on a 404. The walk climbs
until it finds a routed ancestor and bottoms out at ``/ui/``.

A routed ancestor may itself be a fragment. That is fine and needs no
special handling: the redirect is a fresh navigation, so the guard runs
again and climbs one more rung. Path depth bounds the chain well inside
the browser's redirect cap.

A ``303`` (not ``302``) because the result is a different resource than
the one asked for, and ``303`` is the status that says so without
inviting the browser to repeat the original request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Only console paths are guarded.
_UI_PREFIX: Final[str] = "/ui/"

#: Static assets are not HTML, and the auth surfaces (login, callback,
#: the session-ended page) render whole documents that must be reachable
#: by navigation -- guarding them would redirect the login page itself.
_EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/ui/static/", "/ui/auth/")

#: A response whose body opens with one of these is a whole document.
#: Compared against a lower-cased prefix of the first body bytes.
_DOCUMENT_PREFIXES: Final[tuple[bytes, ...]] = (b"<!doctype html", b"<html")

#: How many leading bytes are enough to recognise a document. The
#: longest prefix above is 14 bytes; the slack absorbs a leading BOM or
#: stray whitespace without buffering a whole page.
_SNIFF_BYTES: Final[int] = 64

#: Where the walk bottoms out when a fragment has no usable parent.
_DEFAULT_PAGE: Final[str] = "/ui/"


def _header(scope: Scope, name: bytes) -> bytes | None:
    """Return the first value of header *name* from the raw ASGI scope.

    Read off the raw headers (ASGI lower-cases the names per spec) so
    the hot path never builds a :class:`~starlette.requests.Request`.
    """
    headers = scope.get("headers")
    if not isinstance(headers, list):
        return None
    for header_name, value in headers:
        if header_name != name or not isinstance(value, (bytes, bytearray)):
            continue
        return bytes(value)
    return None


def _is_document_navigation(scope: Scope) -> bool:
    """True when the browser is navigating to this URL, not fetching part of a page.

    An htmx request is never a navigation: it carries ``HX-Request:
    true`` and the fragment is exactly what it asked for.

    ``Sec-Fetch-Dest`` answers the question directly where it is sent.
    Where it is absent -- an older browser, ``curl``, the test client --
    an ``Accept`` that asks for ``text/html`` is the fallback signal. A
    caller that sends neither (``curl`` defaults to ``Accept: */*``) is
    read as "not a navigation" and gets today's behaviour untouched.
    """
    hx_request = _header(scope, b"hx-request")
    if hx_request is not None and hx_request.strip().lower() == b"true":
        return False
    fetch_dest = _header(scope, b"sec-fetch-dest")
    if fetch_dest is not None:
        return fetch_dest.strip().lower() == b"document"
    accept = _header(scope, b"accept")
    return accept is not None and b"text/html" in accept.lower()


def _route_templates(app: object) -> frozenset[str]:
    """Collect every routed ``GET`` path template under ``/ui``.

    FastAPI 0.146 nests an included router behind an ``_IncludedRouter``
    placeholder whose ``path`` is absent, so a flat read of
    ``app.routes`` finds nothing. The walk below descends through
    ``original_router`` to reach the real routes.

    That is a private shape, so every access is a defaulted
    :func:`getattr` and a miss degrades to an empty set rather than
    raising -- with which :func:`_ancestor_page` simply falls back to
    ``/ui/``. ``test_route_templates_sees_the_console_routes`` fails
    loudly if a FastAPI upgrade changes the shape, so the degradation
    cannot pass unnoticed.
    """
    collected: set[str] = set()
    seen: set[int] = set()

    def walk(routes: object, prefix: str) -> None:
        if not isinstance(routes, list):
            return
        for route in routes:
            if id(route) in seen:
                continue
            seen.add(id(route))
            nested = getattr(route, "original_router", None)
            if nested is not None:
                context = getattr(route, "include_context", None)
                walk(
                    getattr(nested, "routes", None), prefix + (getattr(context, "prefix", "") or "")
                )
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or ()
            if isinstance(path, str) and "GET" in methods:
                full = prefix + path
                if full.startswith(_UI_PREFIX):
                    collected.add(full)

    walk(getattr(app, "routes", None), "")
    return frozenset(collected)


def _known_route_templates(scope: Scope) -> frozenset[str]:
    """Return the cached ``GET /ui`` templates for the app serving *scope*."""
    app = scope.get("app")
    if app is None:
        return frozenset()
    state = getattr(app, "state", None)
    cached = getattr(state, "ui_route_templates", None)
    if isinstance(cached, frozenset):
        return cached
    templates = _route_templates(app)
    if state is not None:
        state.ui_route_templates = templates
    return templates


def _matches(candidate: str, template: str) -> bool:
    """True when *candidate* is served by the path *template*.

    Segment-wise so a parameterised page counts: ``/ui/memory/user/x``
    matches ``/ui/memory/{scope}/{slug}``, which keeps the walk from
    skipping past a real detail page on its way up.
    """
    candidate_parts = candidate.strip("/").split("/")
    template_parts = template.strip("/").split("/")
    if len(candidate_parts) != len(template_parts):
        return False
    return all(
        expected.startswith("{") or expected == actual
        for expected, actual in zip(template_parts, candidate_parts, strict=True)
    )


def _ancestor_page(path: str, known: frozenset[str]) -> str:
    """Return the nearest routed ancestor of *path*, or :data:`_DEFAULT_PAGE`.

    ``/ui/approvals/badge`` → ``/ui/approvals``.
    ``/ui/vault/delete/confirm`` → ``/ui/vault``, skipping the unrouted
    ``/ui/vault/delete`` that a naive one-segment hop would 404 on.
    """
    current = path.rstrip("/")
    while True:
        parent, separator, _ = current.rpartition("/")
        if not separator or not parent.startswith("/ui") or parent in ("/ui", ""):
            return _DEFAULT_PAGE
        if any(_matches(parent, template) for template in known):
            return parent
        current = parent


def _looks_like_document(body: bytes) -> bool:
    """True when *body* opens like a whole HTML document."""
    head = body[:_SNIFF_BYTES].lstrip().lower()
    return head.startswith(_DOCUMENT_PREFIXES)


def _is_html_ok(message: Message) -> bool:
    """True when the response start says ``200`` with an HTML body.

    Everything else is left alone: a redirect (the login bounce), an
    error page, and the ``text/event-stream`` of the broadcast feed all
    fall out here, so the guard never buffers a stream.
    """
    if message.get("status") != 200:
        return False
    headers = message.get("headers")
    if not isinstance(headers, list):
        return False
    for name, value in headers:
        if name.lower() != b"content-type":
            continue
        return b"text/html" in bytes(value).lower()
    return False


class FragmentNavigationMiddleware:
    """Redirect a navigation that lands on a fragment to its parent page.

    Pure ASGI, like
    :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`, and
    mounted just outside it so it sees the response a console route
    actually produced -- including the ``302`` to login, which it
    ignores because the guard only looks at ``200 text/html``.

    The response start is held back until the first body bytes arrive,
    because the decision to redirect changes the status line and headers
    can no longer be altered once they are sent. Only the first chunk is
    buffered; a page streams on from there untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        if not isinstance(path, str) or not path.startswith(_UI_PREFIX):
            await self.app(scope, receive, send)
            return
        if path.startswith(_EXEMPT_PREFIXES) or not _is_document_navigation(scope):
            await self.app(scope, receive, send)
            return

        held_start: Message | None = None
        buffered = bytearray()
        # None until the first body chunk settles it; then True (stream
        # the real response) or False (the redirect was sent instead).
        passing_through: bool | None = None

        async def guarded_send(message: Message) -> None:
            nonlocal held_start, passing_through

            message_type = message.get("type")
            if message_type == "http.response.start":
                if not _is_html_ok(message):
                    passing_through = True
                    await send(message)
                    return
                held_start = message
                return

            if message_type != "http.response.body" or passing_through is True:
                if passing_through is False:
                    # The redirect already went out; drop the rest of
                    # the fragment body rather than append it to it.
                    return
                await send(message)
                return

            if passing_through is False:
                return

            body = message.get("body", b"") or b""
            buffered.extend(body)
            more_body = bool(message.get("more_body", False))
            if len(buffered) < _SNIFF_BYTES and more_body:
                # Not enough bytes to judge yet and more are coming.
                return

            if _looks_like_document(bytes(buffered)):
                passing_through = True
                if held_start is not None:
                    await send(held_start)
                    held_start = None
                await send(
                    {
                        "type": "http.response.body",
                        "body": bytes(buffered),
                        "more_body": more_body,
                    }
                )
                return

            passing_through = False
            target = _ancestor_page(path, _known_route_templates(scope))
            structlog.get_logger(__name__).info(
                "ui_fragment_navigation_redirected",
                path=path,
                target=target,
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 303,
                    "headers": [
                        (b"location", target.encode("ascii")),
                        (b"content-length", b"0"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app(scope, receive, guarded_send)
