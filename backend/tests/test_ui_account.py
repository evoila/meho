# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Account UI surface.

Initiative #1842 (G10.11), Task #1892. Acceptance criteria from #1892:

1. ``GET /ui/account`` renders the operator's real ``operator_sub``,
   tenant, role from the freshly-verified token, and session
   ``expires_at`` -- a lifted ``Operator`` with ``tenant_role=TENANT_ADMIN``
   renders ``tenant_admin`` (not the literal "Operator").
2. The active-sessions list shows the calling person's own active rows
   in every tenant (own ``operator_sub``, ``revoked_at IS NULL``, not
   expired), each with its tenant; a second operator's / a revoked / an
   expired row never appears. The current row carries the "This device"
   marker.
3. ``POST /ui/account/sessions/revoke`` (the session ref in the
   ``session_ref`` form field) revokes the caller's own session in any
   tenant; another operator's session ref returns 404 and revokes nothing.
4. ``POST /ui/account/sessions/revoke-others`` revokes all of this
   person's *other* active sessions, in every tenant, but leaves the
   current one active.
5. Revoking the current session signs the operator out (self-logout
   warning string in the modal; response redirects to login). Missing
   CSRF token on a revoke gets ``csrf_token_invalid`` 403.
6. The account page holds no session id at all: each row is named by
   its session ref, the revoke posts to one fixed path with the ref in
   the form body, and the router has no path parameter under
   ``/ui/account/sessions/``.

Harness mirrors :mod:`backend.tests.test_ui_connectors_view`: a minimal
FastAPI app with the UI session + CSRF middlewares, a ``web_session``
row seeded with a real JWKS-signed access token so the role lift can
re-verify it, and a respx-mocked discovery + JWKS endpoint.
"""

from __future__ import annotations

import asyncio
import re
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
from structlog.testing import capture_logs

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
from meho_backplane.ui.auth.session_ref import session_ref
from meho_backplane.ui.auth.session_store import (
    create_session,
    reset_fernet_cache_for_testing,
)
from meho_backplane.ui.csrf import CSRF_COOKIE_NAME, CSRFMiddleware, mint_csrf_token
from meho_backplane.ui.paths import static_root_dir
from meho_backplane.ui.routes import build_router as build_ui_router
from meho_backplane.ui.routes.account import build_account_router
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


_REVOKE_PATH = "/ui/account/sessions/revoke"


def _post_revoke(client: TestClient, csrf: str, session_id: uuid.UUID) -> Any:
    """POST the single-session revoke the way the page does: the ref in the form body."""
    return client.post(
        _REVOKE_PATH,
        data={"session_ref": session_ref(session_id)},
        headers=_csrf_headers(csrf),
    )


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


def test_account_lists_own_active_sessions_in_every_tenant() -> None:
    """The caller's own active rows appear in every tenant; foreign/revoked/expired do not."""
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
    # Same person, session switched to another tenant -- listed with its tenant.
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
    # Own active rows present, in both tenants, each with its tenant.
    assert f'data-session-row="{session_ref(current_id)}"' in body
    assert f'data-session-row="{session_ref(other_device)}"' in body
    assert f'data-session-row="{session_ref(cross_tenant)}"' in body
    assert "tenant Tenant tenant-b" in body
    # Foreign / revoked / expired absent.
    for absent in (foreign_op, revoked, expired):
        assert session_ref(absent) not in body
    # No session id of any kind is in the page.
    for sid in (current_id, other_device, cross_tenant, foreign_op, revoked, expired):
        assert str(sid) not in body
    # The current session row carries the "This device" marker.
    assert 'data-badge="this-device"' in body


# ---------------------------------------------------------------------------
# AC3 -- single-revoke server-side ownership enforcement
# ---------------------------------------------------------------------------


def test_revoke_rejects_foreign_session() -> None:
    """Revoking another operator's session 404s + no-op, in this and another tenant."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    foreign = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_OTHER)
    foreign_b = _seed_session_sync(tenant_id=_TENANT_B, operator_sub=_OP_OTHER)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        foreign_resp = _post_revoke(client, csrf, foreign)
        foreign_b_resp = _post_revoke(client, csrf, foreign_b)

    assert foreign_resp.status_code == 404, foreign_resp.text
    assert foreign_b_resp.status_code == 404, foreign_b_resp.text
    assert _revoked_at(foreign) is None
    assert _revoked_at(foreign_b) is None


def test_revoke_own_session_in_another_tenant() -> None:
    """The caller's own session that switched to another tenant can be revoked by its ref."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    cross_tenant = _seed_session_sync(tenant_id=_TENANT_B, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = _post_revoke(client, csrf, cross_tenant)

    assert response.status_code == 200, response.text
    assert _revoked_at(cross_tenant) is not None
    assert _revoked_at(session_id) is None


def test_revoke_already_revoked_own_session_returns_404() -> None:
    """A ref of the caller's own revoked session names no active session: 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    gone = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    _force_session_state(gone, revoked=True)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = _post_revoke(client, csrf, gone)

    assert response.status_code == 404, response.text
    assert _revoked_at(session_id) is None


def test_revoke_other_owned_session_succeeds_and_returns_fragment() -> None:
    """Revoking another OWNED (non-current) session revokes it + returns the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = _post_revoke(client, csrf, other)
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
    """revoke-others revokes every other own session in every tenant, keeps current active."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other_1 = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other_2 = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    # The same person's session that switched to another tenant.
    other_tenant = _seed_session_sync(tenant_id=_TENANT_B, operator_sub=_OP_SELF)
    # Different operators' sessions must be untouched by revoke-others.
    foreign = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_OTHER)
    foreign_b = _seed_session_sync(tenant_id=_TENANT_B, operator_sub=_OP_OTHER)

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post("/ui/account/sessions/revoke-others", headers=_csrf_headers(csrf))
    assert response.status_code == 200, response.text
    assert 'id="account-sessions"' in response.text
    # Current session still active.
    assert _revoked_at(session_id) is None
    # Every other own session revoked, also the one in the other tenant.
    assert _revoked_at(other_1) is not None
    assert _revoked_at(other_2) is not None
    assert _revoked_at(other_tenant) is not None
    # Foreign operators' sessions untouched.
    assert _revoked_at(foreign) is None
    assert _revoked_at(foreign_b) is None


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
        response = _post_revoke(client, csrf, session_id)
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
            _REVOKE_PATH, data={"session_ref": session_ref(other)}, headers={"HX-Request": "true"}
        )
    assert response.status_code == 403, response.text
    assert "csrf_token_invalid" in response.text
    # The target row was NOT revoked.
    assert _revoked_at(other) is None


# ---------------------------------------------------------------------------
# AC6 -- no session id in the page or in any URL
# ---------------------------------------------------------------------------


def test_account_page_holds_no_session_id() -> None:
    """The page names each row by its session ref; no session id is anywhere in it.

    The revoke buttons post to one fixed URL with the ref in the form
    body (``hx-vals``). No ``hx-post``, ``href`` or ``action`` carries a ref.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, _csrf, sid = _authenticated_client_with_role_jwks(
        tenant_id=_TENANT_A,
        operator_sub=_OP_SELF,
        role=TenantRole.OPERATOR,
    )
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    try:
        response = client.get("/ui/account")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    urls = re.findall(r'\b(?:hx-post|hx-get|href|action)="([^"]*)"', body)
    assert _REVOKE_PATH in urls
    for session_id in (sid, other):
        assert str(session_id) not in body
        ref = session_ref(session_id)
        assert f'hx-vals=\'{{"session_ref": "{ref}"}}\'' in body
        for url in urls:
            assert ref not in url, url


def test_no_account_route_has_a_session_id_in_its_path() -> None:
    """No route under ``/ui/account/sessions/`` has a path parameter.

    The request path is what the ingress access log and the audit row's
    ``path`` column record, so it must be the same for every session.
    """
    paths = [
        getattr(route, "path", "")
        for route in build_account_router().routes
        if getattr(route, "path", "").startswith("/ui/account/sessions/")
    ]
    assert paths, "the account router lost its session routes"
    assert all("{" not in path for path in paths), paths


def test_revoke_rejects_the_old_path_shape() -> None:
    """A POST with the session id in the URL path is not a route (404) and revokes nothing."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(f"/ui/account/sessions/{other}/revoke", headers=_csrf_headers(csrf))
    assert response.status_code == 404, response.text
    assert _revoked_at(other) is None


@pytest.mark.parametrize(
    "payload",
    [{"session_ref": "not-a-ref"}, {"session_ref": ""}, {"session_ref": "\u00fc"}, {}],
)
def test_revoke_unknown_or_missing_session_ref_returns_404(payload: dict[str, str]) -> None:
    """An unknown, empty, non-ASCII or absent ``session_ref`` 404s (not 422, not 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(_REVOKE_PATH, data=payload, headers=_csrf_headers(csrf))
    assert response.status_code == 404, response.text
    assert _revoked_at(session_id) is None


def test_revoke_by_session_id_is_not_accepted() -> None:
    """The form takes the session ref only; a session id in the form revokes nothing."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        by_field = client.post(
            _REVOKE_PATH, data={"session_id": str(other)}, headers=_csrf_headers(csrf)
        )
        as_ref = client.post(
            _REVOKE_PATH, data={"session_ref": str(other)}, headers=_csrf_headers(csrf)
        )
    assert by_field.status_code == 404, by_field.text
    assert as_ref.status_code == 404, as_ref.text
    assert _revoked_at(other) is None


def test_revoke_logs_the_session_ref_not_the_id() -> None:
    """The revoke log line names the revoked session by its ref only."""
    _seed_tenant(_TENANT_A, "tenant-a")
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    other = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=_OP_SELF)
    with respx.mock(assert_all_called=False), capture_logs() as captured:
        client = _authenticated_client(session_id)
        csrf = client.cookies[CSRF_COOKIE_NAME]
        response = _post_revoke(client, csrf, other)
    assert response.status_code == 200, response.text
    revoked = [e for e in captured if e["event"] == "ui_account_session_revoked"]
    assert len(revoked) == 1
    assert revoked[0]["revoked_session_ref"] == session_ref(other)
    serialised = repr(captured)
    assert str(other) not in serialised
    assert str(session_id) not in serialised
