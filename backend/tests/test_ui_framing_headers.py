# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression tests for the operator-console clickjacking-defence headers.

Issue evoila-bosnia/meho-internal#101 row L12: the ``/ui/*`` operator
console shipped no anti-framing protection -- no ``X-Frame-Options`` and
no CSP ``frame-ancestors`` -- so any site could load it in an
``<iframe>`` and mount a clickjacking attack.
:class:`~meho_backplane.ui.security_headers.UIFramingHeadersMiddleware`
stamps both OWASP-recommended headers on every ``/ui/*`` response.

These tests assert the headers are present and correct on a real
rendered HTML page response (non-vacuous: the dashboard renders 200 with
a session, and the assertions execute against that response), on the
unauthenticated 302-to-login (a framed login page is itself an attack
surface), and that the out-of-prefix ``/api/*`` surface is NOT stamped
(the scoping is deliberate).

Test app construction mirrors :mod:`backend.tests.test_ui_chassis_smoke`:
a minimal :class:`FastAPI` with the three ``/ui/*`` middlewares wired in
the same order as :mod:`meho_backplane.main`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import (
    SESSION_COOKIE_NAME,
    UISessionMiddleware,
)
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRFMiddleware
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.security_headers import (
    FRAME_ANCESTORS_CSP,
    X_FRAME_OPTIONS,
    UIFramingHeadersMiddleware,
)
from meho_backplane.ui.templating import reset_templating_for_testing
from tests.conftest import DEFAULT_AUDIENCE, DEFAULT_ISSUER, DEFAULT_TENANT_ID

_BACKPLANE_URL = "https://meho.test"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars + reset cached singletons per test.

    Mirrors :func:`backend.tests.test_ui_chassis_smoke._bff_env`: the
    chassis Keycloak / Vault / DB settings plus the BFF encryption key
    + confidential client credentials, so ``create_session`` can encrypt
    the seeded row and ``get_settings`` resolves without a ``KeyError``.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", DEFAULT_AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _BACKPLANE_URL)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_ID", "meho-web")
    monkeypatch.setenv("UI_KEYCLOAK_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()
    yield
    get_settings.cache_clear()
    reset_fernet_cache_for_testing()
    reset_verifier_store_for_testing()
    reset_templating_for_testing()
    clear_discovery_cache()
    clear_jwks_cache()
    reset_engine_for_testing()


def _build_app() -> FastAPI:
    """Build a minimal app with the three ``/ui/*`` middlewares wired.

    Registration order matches :mod:`meho_backplane.main`: CSRF then
    UISession then UIFramingHeaders, so ``add_middleware``'s
    last-added-is-outermost rule puts the framing-headers middleware
    outermost (it stamps even the session middleware's 302-to-login).
    A bare ``/api/sentinel`` route exercises the out-of-prefix scoping.
    """
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.add_middleware(UIFramingHeadersMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())

    @app.get("/api/sentinel")
    async def _api_sentinel() -> dict[str, str]:
        return {"ok": "true"}

    return app


def _seed_session_sync(
    *,
    operator_sub: str = "op-42",
    tenant_id: uuid.UUID | None = None,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row directly and return its UUID."""
    tenant = tenant_id or uuid.UUID(DEFAULT_TENANT_ID)

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant,
                access_token="access-token-plaintext",
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def test_authenticated_ui_html_carries_framing_headers() -> None:
    """``GET /ui/`` (authenticated, 200 HTML) carries both anti-framing headers.

    Non-vacuous: the dashboard renders an HTML 200, and the assertions
    run against that real response -- not a skipped/short-circuited one.
    """
    session_id = _seed_session_sync()
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
        response = client.get("/ui/")

    # Guard the non-vacuity: this is a rendered HTML page, not a redirect.
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>MEHO Operator Console" in response.text

    assert response.headers["x-frame-options"] == X_FRAME_OPTIONS
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert csp == FRAME_ANCESTORS_CSP
    assert "frame-ancestors 'none'" in csp


def test_unauthenticated_ui_redirect_carries_framing_headers() -> None:
    """The 302-to-login also carries the headers -- a framed login is an attack surface."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/")

    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_api_surface_not_stamped() -> None:
    """Out-of-prefix ``/api/*`` responses are deliberately NOT stamped."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/api/sentinel")

    assert response.status_code == 200
    assert "x-frame-options" not in response.headers
    assert "content-security-policy" not in response.headers
