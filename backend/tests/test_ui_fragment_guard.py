# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A console fragment never renders as a standalone page (#769).

``GET /ui/approvals/badge`` answers with the pending count and nothing
else -- htmx swaps it into a page that is already open. Pointed at
directly, it used to render that scrap as a whole document: a bare
number on a blank page. #5518 stopped the login bounce from *creating*
such a URL; this guard stops the URL from rendering when a browser is
already on one (a parked tab, a bookmark, a history entry).

The guard is exercised through a miniature app rather than the console,
so each case pins one branch of the real middleware with no session,
template or database in the way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient

from meho_backplane.ui.fragment_guard import FragmentNavigationMiddleware

#: What a browser sends on a top-level navigation.
_NAVIGATION = {"sec-fetch-dest": "document"}

#: What htmx sends for a background fragment swap.
_HTMX = {"hx-request": "true", "sec-fetch-dest": "empty"}

_PAGE_BODY = "<!doctype html>\n<html lang='en'><body>a whole page</body></html>"
_FRAGMENT_BODY = "<span>7</span>"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A minimal app behind the guard, with one route per response shape."""
    app = FastAPI()

    @app.get("/ui/approvals", response_class=HTMLResponse)
    async def page() -> HTMLResponse:
        return HTMLResponse(_PAGE_BODY)

    @app.get("/ui/approvals/badge", response_class=HTMLResponse)
    async def fragment() -> HTMLResponse:
        return HTMLResponse(_FRAGMENT_BODY)

    @app.get("/ui/vault", response_class=HTMLResponse)
    async def vault_page() -> HTMLResponse:
        return HTMLResponse(_PAGE_BODY)

    # Mirrors the real console: the parent ``/ui/vault/delete`` is not
    # routed, so a one-segment hop would land on a 404.
    @app.get("/ui/vault/delete/confirm", response_class=HTMLResponse)
    async def vault_confirm() -> HTMLResponse:
        return HTMLResponse(_FRAGMENT_BODY)

    @app.get("/ui/memory/{scope}/{slug}", response_class=HTMLResponse)
    async def memory_detail(scope: str, slug: str) -> HTMLResponse:
        return HTMLResponse(_PAGE_BODY)

    @app.get("/ui/memory/{scope}/{slug}/promote", response_class=HTMLResponse)
    async def memory_promote(scope: str, slug: str) -> HTMLResponse:
        return HTMLResponse(_FRAGMENT_BODY)

    @app.get("/ui/orphan", response_class=HTMLResponse)
    async def orphan_fragment() -> HTMLResponse:
        return HTMLResponse(_FRAGMENT_BODY)

    @app.get("/ui/auth/login", response_class=HTMLResponse)
    async def auth_surface() -> HTMLResponse:
        # Deliberately fragment-shaped: the exemption, not the body, is
        # what must keep the login surface reachable.
        return HTMLResponse(_FRAGMENT_BODY)

    @app.get("/ui/elsewhere")
    async def redirecting() -> RedirectResponse:
        return RedirectResponse("/ui/approvals", status_code=302)

    @app.get("/ui/broadcast/stream")
    async def stream() -> StreamingResponse:
        async def frames() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/ui/chunked", response_class=HTMLResponse)
    async def chunked_page() -> StreamingResponse:
        async def pieces() -> AsyncIterator[bytes]:
            # Split so the document prefix spans two chunks -- the guard
            # must wait for enough bytes before judging.
            yield b"<!doc"
            yield b"type html>\n<html><body>streamed</body></html>"

        return StreamingResponse(pieces(), media_type="text/html")

    app.add_middleware(FragmentNavigationMiddleware)
    with TestClient(app) as test_client:
        yield test_client


def test_navigation_to_fragment_redirects_to_parent(client: TestClient) -> None:
    """The reported bug: a parked tab on the badge URL lands on the page."""
    response = client.get("/ui/approvals/badge", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/approvals"


def test_htmx_still_gets_the_fragment(client: TestClient) -> None:
    """The swap htmx asked for must be untouched -- this is the normal path."""
    response = client.get("/ui/approvals/badge", headers=_HTMX, follow_redirects=False)
    assert response.status_code == 200
    assert response.text == _FRAGMENT_BODY


def test_navigation_to_a_page_is_untouched(client: TestClient) -> None:
    """A real page renders as itself, body byte-for-byte."""
    response = client.get("/ui/approvals", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 200
    assert response.text == _PAGE_BODY


def test_accept_header_stands_in_for_sec_fetch_dest(client: TestClient) -> None:
    """Browsers without ``Sec-Fetch-Dest`` are read from ``Accept``."""
    response = client.get(
        "/ui/approvals/badge",
        headers={"accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/approvals"


def test_non_browser_caller_keeps_todays_behaviour(client: TestClient) -> None:
    """``curl`` (``Accept: */*``, no fetch metadata) is not a navigation."""
    response = client.get(
        "/ui/approvals/badge",
        headers={"accept": "*/*"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.text == _FRAGMENT_BODY


def test_auth_surface_is_exempt(client: TestClient) -> None:
    """Guarding ``/ui/auth/`` would redirect the login page itself."""
    response = client.get("/ui/auth/login", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 200
    assert response.text == _FRAGMENT_BODY


def test_redirect_response_passes_through(client: TestClient) -> None:
    """The session middleware's 302-to-login must not be rewritten."""
    response = client.get("/ui/elsewhere", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/approvals"


def test_event_stream_passes_through(client: TestClient) -> None:
    """A feed is never buffered or judged -- it is not ``text/html``."""
    response = client.get("/ui/broadcast/stream", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 200
    assert "data: one" in response.text


def test_document_split_across_chunks_is_recognised(client: TestClient) -> None:
    """A page whose doctype spans two chunks is still a page."""
    response = client.get("/ui/chunked", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 200
    assert response.text.startswith("<!doctype html>")
    assert response.text.endswith("</html>")


def test_walk_skips_an_unrouted_parent(client: TestClient) -> None:
    """``/ui/vault/delete`` is not routed, so the walk climbs to ``/ui/vault``.

    Stopping at the immediate parent would 303 the person onto a 404.
    """
    response = client.get("/ui/vault/delete/confirm", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/vault"


def test_walk_stops_at_a_parameterised_page(client: TestClient) -> None:
    """A detail page counts as an ancestor even though its path is a template."""
    response = client.get(
        "/ui/memory/user/some-note/promote", headers=_NAVIGATION, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/memory/user/some-note"


def test_top_level_fragment_falls_back_to_the_console_root(client: TestClient) -> None:
    """A fragment with no usable parent bottoms out at ``/ui/``."""
    response = client.get("/ui/orphan", headers=_NAVIGATION, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"


def test_route_templates_sees_the_console_routes() -> None:
    """The private-shape walk must keep finding real routes across upgrades.

    :func:`_route_templates` descends through FastAPI 0.146's
    ``_IncludedRouter`` placeholder. If a future version changes that
    shape the walk returns nothing and every fragment would redirect to
    ``/ui/`` instead of its page -- a silent downgrade. This test is
    what makes that loud.
    """
    from meho_backplane.main import app
    from meho_backplane.ui.fragment_guard import _route_templates

    templates = _route_templates(app)
    assert "/ui/approvals" in templates
    assert "/ui/approvals/badge" in templates
    assert len(templates) > 100
