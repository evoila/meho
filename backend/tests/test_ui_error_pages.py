# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A wrong web address gets a page; a wrong API address still gets JSON.

The second half is the one worth guarding. An error handler that answers
HTML to an API caller turns a clean 404 into a parse error, and whatever
was parsing -- an agent, a script, a retry loop -- then reports something
unrelated to what happened. That break is silent: no test fails, no log
says anything, and the caller's bug report points somewhere else.

So every JSON case here sends ``Accept: text/html``. A browser sends that
header to any address it is pointed at, including an API one, which is
exactly why the split cannot be made on the header alone.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session, reset_fernet_cache_for_testing
from meho_backplane.ui.error_pages import (
    is_web_path,
    web_http_exception_handler,
    web_server_error_handler,
)
from meho_backplane.ui.templating import reset_templating_for_testing


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Chassis + BFF env, as every other console suite pins it."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://kc.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_templating_for_testing()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_templating_for_testing()
    reset_engine_for_testing()


#: A browser asks for HTML everywhere, including on API addresses.
_BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.fixture
def client() -> TestClient:
    """A minimal app wired exactly as ``main.py`` wires the handlers."""
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)
    app.add_exception_handler(Exception, web_server_error_handler)

    @app.get("/ui/boom")
    async def ui_boom() -> None:
        raise RuntimeError("unhandled")

    @app.get("/ui/forbidden")
    async def ui_forbidden() -> None:
        raise HTTPException(status_code=403)

    @app.get("/api/v1/boom")
    async def api_boom() -> None:
        raise RuntimeError("unhandled")

    @app.get("/api/v1/forbidden")
    async def api_forbidden() -> None:
        raise HTTPException(status_code=403)

    # raise_server_exceptions=False: ServerErrorMiddleware re-raises after
    # calling the 500 handler, so the default client would surface the
    # RuntimeError instead of the response under test.
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------
# the web half
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/ui/nope", 404), ("/ui/forbidden", 403), ("/ui/boom", 500)],
)
def test_a_wrong_web_address_renders_the_branded_page(
    client: TestClient, path: str, expected: int
) -> None:
    response = client.get(path, headers=_BROWSER)
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert f'data-error-page="{expected}"' in body
    # The brand, not a bare Starlette page.
    assert 'data-theme="evoila-system"' in body
    assert "MEHO Operator Console" in body


def test_the_page_does_not_echo_the_address(client: TestClient) -> None:
    """The page shows only its own words, never anything from the address."""
    response = client.get("/ui/<script>alert(1)</script>", headers=_BROWSER)
    assert response.status_code == 404
    assert "<script>alert(1)</script>" not in response.text
    assert "alert(1)" not in response.text


# --------------------------------------------------------------------
# the API half -- the one a future change breaks silently
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/api/v1/nope", 404), ("/api/v1/forbidden", 403)],
)
def test_a_wrong_api_address_still_returns_json_to_a_browser(
    client: TestClient, path: str, expected: int
) -> None:
    """Even with a browser's ``Accept`` header, an API address answers JSON."""
    response = client.get(path, headers=_BROWSER)
    assert response.status_code == expected
    assert response.headers["content-type"].startswith("application/json")
    assert "<!doctype html>" not in response.text.lower()
    # Parsable, which is the whole point.
    assert isinstance(response.json(), dict)


def test_an_api_500_keeps_starlettes_plain_text_body(client: TestClient) -> None:
    """A 500 off ``/ui`` is plain text, not JSON -- the released shape.

    Listed apart from the 404 and 403 above because the body differs:
    FastAPI renders a raised ``HTTPException`` as JSON, while an
    unhandled exception has always come back as
    ``PlainTextResponse("Internal Server Error")``. Asserting JSON here
    would pin a change to that contract rather than the contract.
    """
    response = client.get("/api/v1/boom", headers=_BROWSER)
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Internal Server Error"
    assert "<!doctype html>" not in response.text.lower()


@pytest.mark.parametrize("path", ["/mcp/anything", "/.well-known/anything", "/metrics"])
def test_other_machine_surfaces_are_not_web_paths(path: str) -> None:
    """Only ``/ui`` is a page surface; a new machine prefix keeps JSON."""
    assert is_web_path(path) is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/ui", True), ("/ui/", True), ("/ui/agents", True), ("/uitest", False), ("/", False)],
)
def test_web_prefix_matches_on_a_segment_boundary(path: str, expected: bool) -> None:
    """``/uitest`` is not ``/ui`` -- a prefix match alone would claim it."""
    assert is_web_path(path) is expected


# --------------------------------------------------------------------
# with the real middleware in front
# --------------------------------------------------------------------


def _real_ui_app() -> FastAPI:
    """The console's middleware and routers, wired as ``main.py`` wires them."""
    from meho_backplane.ui.auth import UISessionMiddleware
    from meho_backplane.ui.auth import build_router as build_ui_auth_router
    from meho_backplane.ui.csrf import CSRFMiddleware
    from meho_backplane.ui.routes import build_router as build_ui_router

    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)
    app.add_exception_handler(Exception, web_server_error_handler)
    return app


def test_a_wrong_ui_address_renders_the_page_for_a_signed_in_operator() -> None:
    """The page is reached with a session, which is the only way to reach it.

    A signed-out visitor never sees it: the session middleware runs in
    front of routing and bounces every ``/ui/*`` request to the login
    before a 404 can be raised (asserted separately below). So the
    branded page serves someone already in the console who followed a
    dead link or a renamed one.
    """
    tenant_id = uuid.uuid4()
    _seed_tenant_row(tenant_id)

    async def _mint() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub="operator-error-page",
                tenant_id=tenant_id,
                access_token="unused-on-the-read-path",
                refresh_token="refresh-token-plaintext",
                lifetime=timedelta(hours=1),
            )
            return decrypted.id

    session_id = asyncio.run(_mint())

    client = TestClient(_real_ui_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    response = client.get("/ui/definitely-not-a-page", headers=_BROWSER)

    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert 'data-error-page="404"' in response.text
    assert 'data-theme="evoila-system"' in response.text


def test_a_signed_out_visitor_still_bounces_to_login_not_to_the_page() -> None:
    """Unchanged behaviour, pinned so the new handler cannot swallow it.

    Registering a 404 handler on the app is exactly the kind of change
    that could turn the login bounce into a dead-end error page.
    """
    client = TestClient(_real_ui_app(), follow_redirects=False)
    response = client.get("/ui/definitely-not-a-page", headers=_BROWSER)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def _seed_tenant_row(tenant_id: uuid.UUID) -> None:
    """Insert the tenant the session row points at."""
    from meho_backplane.db.engine import get_sessionmaker

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug="error-page-tenant", name="Error page"))

    asyncio.run(_do())


# --------------------------------------------------------------------
# review findings: what a console address does *not* change
# --------------------------------------------------------------------

#: A top-level browser navigation. ``Sec-Fetch-Dest: document`` is what
#: a browser sends when it is replacing the whole page.
_NAVIGATION = {**_BROWSER, "Sec-Fetch-Dest": "document"}


def test_an_unhandled_error_outside_the_console_keeps_the_released_body() -> None:
    """A 500 off ``/ui`` stays ``Internal Server Error`` as plain text.

    That is what ``ServerErrorMiddleware.error_response`` has always
    sent. Whatever reads a 500 today -- an agent, a script, a retry loop
    -- is reading this body, so changing it is a change to a released
    contract, not a presentation detail.
    """
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)
    app.add_exception_handler(Exception, web_server_error_handler)

    @app.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError("unhandled")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/boom", headers=_NAVIGATION)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Internal Server Error"


def test_an_htmx_caller_on_a_console_address_keeps_its_json_detail() -> None:
    """htmx and ``fetch`` callers read the body; they must still get it.

    The run console's Stop button shows a 403's ``detail`` to explain who
    may stop another person's run. A generic page would drop that text.
    """
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)

    @app.get("/ui/agents/runs/stop")
    async def stop() -> None:
        raise HTTPException(status_code=403, detail="only_the_owner_may_stop_this_run")

    with TestClient(app) as client:
        response = client.get("/ui/agents/runs/stop", headers={**_BROWSER, "HX-Request": "true"})

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "only_the_owner_may_stop_this_run"


def test_a_fetch_caller_on_a_console_address_keeps_its_json_detail() -> None:
    """Same for a plain ``fetch``: ``Sec-Fetch-Dest`` is not ``document``."""
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)

    @app.get("/ui/memory/thing")
    async def thing() -> None:
        raise HTTPException(status_code=403, detail="permission_denied: not an admin")

    with TestClient(app) as client:
        response = client.get("/ui/memory/thing", headers={**_BROWSER, "Sec-Fetch-Dest": "empty"})

    assert response.status_code == 403
    assert response.json()["detail"] == "permission_denied: not an admin"


def test_the_branded_page_carries_the_anti_framing_headers() -> None:
    """The 500 page is sent outside the middleware that would add them.

    ``ServerErrorMiddleware`` sits outside every application middleware,
    so ``UIFramingHeadersMiddleware`` never sees this response. Without
    the headers stamped in the handler another site could frame it.
    """
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)
    app.add_exception_handler(Exception, web_server_error_handler)

    @app.get("/ui/boom")
    async def boom() -> None:
        raise RuntimeError("unhandled")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/ui/boom", headers=_NAVIGATION)

    assert response.status_code == 500
    assert 'data-error-page="500"' in response.text
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_an_explicitly_raised_500_also_gets_the_page() -> None:
    """A route raising ``HTTPException(500)`` is a 500 like any other.

    It arrives as an ``HTTPException`` rather than through
    ``ServerErrorMiddleware``, so without 500 on the page list the two
    ways of producing the same status answered differently.
    """
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, web_http_exception_handler)

    @app.get("/ui/explicit")
    async def explicit() -> None:
        raise HTTPException(status_code=500)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/ui/explicit", headers=_NAVIGATION)

    assert response.status_code == 500
    assert 'data-error-page="500"' in response.text
