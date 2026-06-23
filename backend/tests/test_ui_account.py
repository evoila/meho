# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Account UI surface.

Initiative #1842 (G10.11), Task #1892. Acceptance criteria from #1892:

1. ``GET /ui/account`` renders the operator's real ``operator_sub``,
   tenant, role from the freshly-verified token, and session
   ``expires_at`` -- a lifted ``Operator`` with ``tenant_role=TENANT_ADMIN``
   renders ``tenant_admin`` (not the literal "Operator").
2. The active-sessions list shows only the calling operator's own
   active rows (own ``operator_sub`` + ``tenant_id``, ``revoked_at IS NULL``,
   not expired); a second operator's / a revoked / an expired row never
   appears. The current row carries the "This device" marker.
3. ``POST /ui/account/sessions/{session_id}/revoke`` with a foreign /
   cross-tenant session id returns 404 and does NOT revoke it.
4. ``POST /ui/account/sessions/revoke-others`` revokes all of this
   operator's *other* active sessions but leaves the current one active.
5. Revoking the current session signs the operator out (self-logout
   warning string in the modal; response redirects to login). Missing
   CSRF token on a revoke gets ``csrf_token_invalid`` 403.
6. ``revoke-others`` resolves as a literal route, not as
   ``{session_id}="revoke-others"``.

Harness mirrors :mod:`backend.tests.test_ui_connectors_view`: a minimal
FastAPI app with the UI session + CSRF middlewares, a ``web_session``
row seeded with a real JWKS-signed access token so the role lift can
re-verify it, and a respx-mocked discovery + JWKS endpoint.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import Tenant, WebSession
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME, UISessionMiddleware
from meho_backplane.ui.auth import build_router as build_ui_auth_router
from meho_backplane.ui.auth.flow import (
    clear_discovery_cache,
    reset_verifier_store_for_testing,
)
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.templating import reset_templating_for_testing
from tests._oidc_jwt_helpers import (
    AUDIENCE as _DEFAULT_AUDIENCE,
)
from tests._oidc_jwt_helpers import (
    ISSUER as _DEFAULT_ISSUER,
)
from tests._oidc_jwt_helpers import (
    make_rsa_keypair as _make_rsa_keypair,
)
from tests._oidc_jwt_helpers import (
    mint_token as _mint_token,
)
from tests._oidc_jwt_helpers import (
    mock_discovery_and_jwks as _mock_discovery_and_jwks,
)
from tests._oidc_jwt_helpers import (
    public_jwks as _public_jwks,
)

_BACKPLANE_URL = "https://meho.test"

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

_OP_SELF = "op-self"
_OP_OTHER = "op-other"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the connectors suite)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _DEFAULT_ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _DEFAULT_AUDIENCE)
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
    """Construct a minimal FastAPI app wired for the account UI tests."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(UISessionMiddleware)
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(static_root_dir()), check_dir=False),
        name="ui_static",
    )
    app.include_router(build_ui_auth_router())
    app.include_router(build_ui_router())
    return app


def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    """Insert one ``tenant`` row so the session-tenant chip resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    access_token: str = "unused",
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row and return its UUID (the cookie value)."""

    async def _do() -> uuid.UUID:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            decrypted = await create_session(
                session,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                access_token=access_token,
                refresh_token="refresh-token-plaintext",
                lifetime=lifetime,
            )
            return decrypted.id

    return asyncio.run(_do())


def _force_session_state(
    session_id: uuid.UUID,
    *,
    revoked: bool = False,
    expires_at: datetime | None = None,
) -> None:
    """Mark a seeded session revoked and/or set an explicit ``expires_at``.

    Lets the tests seed a revoked row + an already-expired row to assert
    the active-sessions filter excludes both.
    """

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            row = await session.get(WebSession, session_id)
            assert row is not None
            if revoked:
                row.revoked_at = datetime.now(UTC)
            if expires_at is not None:
                row.expires_at = expires_at

    asyncio.run(_do())


def _revoked_at(session_id: uuid.UUID) -> datetime | None:
    """Return a session row's ``revoked_at`` (None = still active)."""

    async def _do() -> datetime | None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await session.get(WebSession, session_id)
            assert row is not None
            return row.revoked_at

    return asyncio.run(_do())


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + the matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-account-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(session_id: uuid.UUID) -> TestClient:
    """Return a TestClient with the session + CSRF cookies pre-set (no JWKS mock)."""
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, mint_csrf_token(str(session_id)))
    return client


def _authenticated_client_with_role_jwks(
    *,
    tenant_id: uuid.UUID,
    operator_sub: str,
    role: TenantRole,
) -> tuple[TestClient, respx.MockRouter, str, uuid.UUID]:
    """Return TestClient + respx mock + csrf token + session id for the page render.

    The page's role lift re-validates the BFF session's access token
    through the JWT chain, which needs the JWKS endpoint mocked. The
    caller stops ``mock`` in a ``finally``.
    """
    keypair, jwks = _make_keypair_and_jwks()
    access_token = _mint_token(
        keypair,
        sub=operator_sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )
    session_id = _seed_session_sync(
        tenant_id=tenant_id,
        access_token=access_token,
        operator_sub=operator_sub,
    )
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token, session_id


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers for an HTMX state-changing request -- CSRF + HX-Request."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_account_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/account`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/account")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# AC1 -- identity from the freshly-verified token
# ---------------------------------------------------------------------------


def test_account_renders_real_identity() -> None:
    """The page surfaces real operator_sub, tenant, the live role + expiry.

    AC1: a lifted ``Operator`` with ``tenant_role=TENANT_ADMIN`` renders
    ``tenant_admin`` -- NOT the hardcoded literal "Operator" -- and the
    page contains the session's ``operator_sub``.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf, _sid = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_SELF,
        role=TenantRole.TENANT_ADMIN,
    )
    try:
        response = client.get("/ui/account")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Account" in body
    # Real operator_sub.
    assert _OP_SELF in body
    # Live role from the verified token -- the demotion-visible path.
    assert "tenant_admin" in body
    assert 'data-field="role"' in body
    # Tenant name from the session_tenant context shape.
    assert "Tenant tenant-a" in body
    # Session expiry block present.
    assert 'data-field="expires-at"' in body
    # CSRF cookie set so subsequent revokes pass the double-submit gate.
    assert CSRF_COOKIE_NAME in response.cookies


def test_account_role_falls_back_when_token_unverifiable() -> None:
    """With no JWKS mock the role lift fails soft to 'unknown', page still 200s."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # No JWKS mock + a dummy (unverifiable) token -> the lift raises and
    # the route degrades the role to "unknown" rather than 5xx-ing.
    session_id = _seed_session_sync(
        tenant_id=_TENANT_A, operator_sub=_OP_SELF, access_token="not-a-real-jwt"
    )
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/account")
    assert response.status_code == 200, response.text
    assert "unknown" in response.text
    assert _OP_SELF in response.text


# ---------------------------------------------------------------------------
# AC2 -- active-sessions list scoping + "This device"
# ---------------------------------------------------------------------------


def test_account_lists_only_own_active_sessions() -> None:
    """Only the caller's own active rows appear; foreign/revoked/expired excluded."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    # Caller's current + a second own-active device.
    client, mock, _csrf, current_id = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_SELF,
        role=TenantRole.OPERATOR,
    )
    other_device = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    # A second operator's session (same tenant) -- must NOT appear.
    foreign_op = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_OTHER)
    # Same operator_sub on a different tenant -- must NOT appear.
    cross_tenant = _seed_session_sync(tenant_id=_TENANT_B, operator_sub=_OP_SELF)
    # Own revoked + own expired rows -- must NOT appear.
    revoked = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    _force_session_state(revoked, revoked=True)
    expired = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    _force_session_state(expired, expires_at=datetime.now(UTC) - timedelta(hours=1))

    try:
        response = client.get("/ui/account")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # Own active rows present.
    assert f'data-session-row="{current_id}"' in body
    assert f'data-session-row="{other_device}"' in body
    # Foreign / cross-tenant / revoked / expired absent.
    assert str(foreign_op) not in body
    assert str(cross_tenant) not in body
    assert str(revoked) not in body
    assert str(expired) not in body
    # The current session row carries the "This device" marker.
    assert 'data-badge="this-device"' in body


# ---------------------------------------------------------------------------
# AC3 -- single-revoke server-side ownership enforcement
# ---------------------------------------------------------------------------


def test_revoke_rejects_foreign_session() -> None:
    """Revoking another operator's (or another tenant's) session 404s + no-op."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    foreign = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_OTHER)
    cross_tenant = _seed_session_sync(tenant_id=_TENANT_B, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        foreign_resp = client.post(
            f"/ui/account/sessions/{foreign}/revoke", headers=_csrf_headers(csrf)
        )
        cross_resp = client.post(
            f"/ui/account/sessions/{cross_tenant}/revoke", headers=_csrf_headers(csrf)
        )

    assert foreign_resp.status_code == 404, foreign_resp.text
    assert cross_resp.status_code == 404, cross_resp.text
    # Neither foreign row was revoked.
    assert _revoked_at(foreign) is None
    assert _revoked_at(cross_tenant) is None


def test_revoke_other_owned_session_succeeds_and_returns_fragment() -> None:
    """Revoking another OWNED (non-current) session revokes it + returns the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(f"/ui/account/sessions/{other}/revoke", headers=_csrf_headers(csrf))
    assert response.status_code == 200, response.text
    # The swapped-in fragment is the sessions list (no full-page chrome).
    assert 'id="account-sessions"' in response.text
    assert "<html" not in response.text.lower()
    assert _revoked_at(other) is not None
    # The current session stays active.
    assert _revoked_at(session_id) is None


# ---------------------------------------------------------------------------
# AC4 -- revoke-others spares the current session
# ---------------------------------------------------------------------------


def test_revoke_others_spares_current() -> None:
    """revoke-others revokes every other active own session, keeps current active."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other_1 = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other_2 = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    # A different operator's session must be untouched by revoke-others.
    foreign = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_OTHER)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post("/ui/account/sessions/revoke-others", headers=_csrf_headers(csrf))
    assert response.status_code == 200, response.text
    assert 'id="account-sessions"' in response.text
    # Current session still active.
    assert _revoked_at(session_id) is None
    # Both other own sessions revoked.
    assert _revoked_at(other_1) is not None
    assert _revoked_at(other_2) is not None
    # Foreign operator's session untouched.
    assert _revoked_at(foreign) is None


# ---------------------------------------------------------------------------
# AC5 -- self-logout footgun + CSRF
# ---------------------------------------------------------------------------


def test_account_self_revoke_redirects_to_login() -> None:
    """Revoking the current session ('this device') signs out via HX-Redirect."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            f"/ui/account/sessions/{session_id}/revoke", headers=_csrf_headers(csrf)
        )
    assert response.status_code == 200, response.text
    # HTMX client-side redirect to the login surface.
    assert response.headers.get("HX-Redirect") == "/ui/auth/login"
    # The session is now revoked -> the operator is signed out.
    assert _revoked_at(session_id) is not None


def test_account_modal_carries_self_logout_warning() -> None:
    """The 'This device' confirm modal carries the explicit self-logout string."""
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf, _sid = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_SELF,
        role=TenantRole.OPERATOR,
    )
    try:
        response = client.get("/ui/account")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert 'data-warning="self-logout"' in body
    assert "revoking it signs you" in body


def test_revoke_requires_csrf() -> None:
    """A revoke POST without the CSRF token gets csrf_token_invalid 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        # No X-CSRF-Token header and the CSRF cookie removed -> rejected.
        client.cookies.delete(CSRF_COOKIE_NAME)
        response = client.post(
            f"/ui/account/sessions/{other}/revoke", headers={"HX-Request": "true"}
        )
    assert response.status_code == 403, response.text
    assert "csrf_token_invalid" in response.text
    # The target row was NOT revoked.
    assert _revoked_at(other) is None


# ---------------------------------------------------------------------------
# AC6 -- route resolution: literal before param
# ---------------------------------------------------------------------------


def test_revoke_others_resolves_as_literal_route() -> None:
    """``revoke-others`` binds the literal route, not ``{session_id}``.

    If the literal route were registered after the parametrised one,
    ``revoke-others`` would bind as ``session_id="revoke-others"``; the
    single-revoke handler would then try ``uuid.UUID("revoke-others")``
    and 404. A 200 + the sessions-list fragment proves the literal route
    won the first-match-wins lookup.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post("/ui/account/sessions/revoke-others", headers=_csrf_headers(csrf))
    assert response.status_code == 200, response.text
    assert 'id="account-sessions"' in response.text


def test_revoke_malformed_session_id_returns_404() -> None:
    """A non-UUID session id on the single-revoke route 404s (not 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/ui/account/sessions/not-a-uuid/revoke", headers=_csrf_headers(csrf)
        )
    assert response.status_code == 404, response.text
