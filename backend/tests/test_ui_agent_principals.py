# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Agent principals console UI surface.

Initiative #1824 (G10.8 Agents console), Task #1831 (T4). The acceptance
criteria on issue #1831 are:

* ``/ui/agents/principals`` lists the tenant's registered principals
  (name / keycloak_client_id / revoked / owner / created_by /
  created_at); an ``include_revoked`` toggle joins revoked rows.
* Register / revoke are tenant_admin only -- soft-hidden on the list and
  hard 403 server-side.
* Revoke (the Keycloak kill switch) requires type-to-confirm of the
  principal name; success swaps the row to its revoked state.
* Keycloak / Vault failures (503 / 502) render the actionable backend
  detail, not a generic error.
* CSRF double-submit on register + revoke.

Suite shape mirrors :mod:`backend.tests.test_ui_agents`: a minimal
FastAPI app wired with the chassis middlewares + the BFF auth router +
the UI router; a ``web_session`` row seeded with a real Keycloak-minted
access token so the ``resolve_operator_or_403`` write dep can re-verify
the token and pick up the right :class:`TenantRole`.

The Keycloak-client-create / Vault-write internals of the register and
revoke service methods are covered by
:mod:`backend.tests.test_api_v1_agent_principals` and
:mod:`backend.tests.test_agents_service`; here we exercise the UI route
layer (RBAC gate, CSRF, error rendering, type-to-confirm) by
monkeypatching :class:`AgentPrincipalService.register` / ``revoke`` to
return a row or raise the boundary exceptions, so the tests stay focused
on the surface and free of a live-Keycloak dependency.
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

from meho_backplane.auth.agent_principals import (
    AgentPrincipalExistsError,
    AgentPrincipalNotFoundError,
    AgentPrincipalRead,
    AgentPrincipalService,
)
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AgentPrincipal, Tenant
from meho_backplane.scheduler.vault_credentials import SchedulerVaultBrokerError
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
_OP_A = "op-alice"


@pytest.fixture(autouse=True)
def _bff_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis + BFF env vars for every test (mirrors the agents suite)."""
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
    """Construct a minimal FastAPI app wired for the principals UI tests."""
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
    """Insert one ``tenant`` row so the agent-principal FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _seed_principal(
    *,
    tenant_id: uuid.UUID,
    name: str,
    revoked: bool = False,
    owner_sub: str = _OP_A,
    created_by_sub: str = _OP_A,
) -> None:
    """Persist one ``agent_principal`` row directly (bypasses the service)."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentPrincipal(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    keycloak_client_id=f"agent:{name}",
                    keycloak_internal_id=f"kc-{uuid.uuid4()}",
                    owner_sub=owner_sub,
                    revoked=revoked,
                    created_by_sub=created_by_sub,
                ),
            )

    asyncio.run(_do())


def _seed_session_sync(
    *,
    tenant_id: uuid.UUID,
    access_token: str,
    operator_sub: str,
    lifetime: timedelta = timedelta(hours=1),
) -> uuid.UUID:
    """Create a ``web_session`` row carrying *access_token* and return its UUID."""

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


def _make_keypair_and_jwks() -> tuple[Any, dict[str, Any]]:
    """Mint a stable RSA-2048 keypair + matching JWKS document."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        keypair = _make_rsa_keypair("ui-principals-test-kid")
    return keypair, _public_jwks(keypair)


def _authenticated_client(
    *,
    session_id: uuid.UUID,
    jwks: dict[str, Any],
    with_csrf: bool = False,
) -> tuple[TestClient, respx.MockRouter, str]:
    """Return a TestClient + an open respx mock + a CSRF token."""
    mock = respx.mock(assert_all_called=False)
    mock.start()
    _mock_discovery_and_jwks(mock, jwks)
    client = TestClient(_build_app(), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    csrf_token = mint_csrf_token(str(session_id))
    if with_csrf:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, mock, csrf_token


def _admin_session(jwks_keypair: Any) -> str:
    """Mint a tenant_admin access token for the standard operator."""
    return _mint_token(
        jwks_keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def _operator_session(jwks_keypair: Any) -> str:
    """Mint an operator (non-admin) access token for the standard operator."""
    return _mint_token(
        jwks_keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.OPERATOR.value,
    )


def _csrf_headers(token: str) -> dict[str, str]:
    """Headers a state-changing HTMX request carries (token + HX-Request)."""
    return {"X-CSRF-Token": token, "HX-Request": "true"}


def _principal_read(
    *,
    tenant_id: uuid.UUID,
    name: str,
    revoked: bool = False,
) -> AgentPrincipalRead:
    """Build an :class:`AgentPrincipalRead` for monkeypatched service returns."""
    now = datetime.now(UTC)
    return AgentPrincipalRead(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        keycloak_client_id=f"agent:{name}",
        keycloak_internal_id=f"kc-{uuid.uuid4()}",
        owner_sub=_OP_A,
        revoked=revoked,
        created_by_sub=_OP_A,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/agents/principals`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/agents/principals")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


def test_list_full_page_renders_empty_state() -> None:
    """``GET /ui/agents/principals`` with no principals renders the empty state."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Agent principals" in body
    assert 'id="principals-table"' in body
    assert "No active agent principals" in body
    assert CSRF_COOKIE_NAME in response.cookies


def test_list_renders_rows_with_columns() -> None:
    """Seeded principals render with the scannable columns; revoked hidden by default."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="deploy-bot")
    _seed_principal(tenant_id=_TENANT_A, name="old-bot", revoked=True)
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert "deploy-bot" in body
    assert "agent:deploy-bot" in body
    # Revoked principals are excluded from the default (active) view.
    assert "old-bot" not in body


def test_list_include_revoked_toggle_shows_revoked() -> None:
    """``include_revoked=true`` joins revoked rows; the fragment carries no chrome."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="active-bot")
    _seed_principal(tenant_id=_TENANT_A, name="dead-bot", revoked=True)
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(
            "/ui/agents/principals?include_revoked=true",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'id="principals-table"' in body
    assert "<title>" not in body
    assert "active-bot" in body
    assert "dead-bot" in body
    assert 'data-revoked="true"' in body


def test_list_is_tenant_scoped() -> None:
    """Tenant B's principals never appear in tenant A's list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_principal(tenant_id=_TENANT_A, name="mine-bot")
    _seed_principal(tenant_id=_TENANT_B, name="not-mine-bot")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "mine-bot" in response.text
    assert "not-mine-bot" not in response.text


def test_list_hides_write_affordances_for_operator() -> None:
    """A non-admin operator sees no register / revoke affordances (soft gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="locked-bot")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/ui/agents/principals/register"' not in body
    assert "/ui/agents/principals/locked-bot/revoke" not in body


def test_list_shows_write_affordances_for_admin() -> None:
    """A tenant_admin sees the register button + per-row revoke buttons."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/ui/agents/principals/register"' in body
    assert "/ui/agents/principals/kill-me/revoke" in body


def test_principals_path_not_shadowed_by_agent_detail() -> None:
    """``/ui/agents/principals`` resolves to the list, not the agent-detail 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _, jwks = _make_keypair_and_jwks()
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token="unused", operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals")
    finally:
        mock.stop()
    assert response.status_code == 200
    assert "Agent principals" in response.text


# ---------------------------------------------------------------------------
# Register -- RBAC gate + happy path + errors
# ---------------------------------------------------------------------------


def test_register_modal_requires_tenant_admin() -> None:
    """``GET /ui/agents/principals/register`` 403s for a non-admin (hard gate)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals/register")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_register_modal_renders_for_admin() -> None:
    """A tenant_admin gets the register modal fragment."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals/register")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'id="principals-register-modal"' in body
    assert 'name="name"' in body
    assert 'name="owner_sub"' in body


def test_register_persists_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid register calls the service and HX-Redirects to the list."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    captured: dict[str, Any] = {}

    async def _fake_register(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        captured.update(kwargs)
        return _principal_read(tenant_id=_TENANT_A, name=kwargs["payload"].name)

    monkeypatch.setattr(AgentPrincipalService, "register", _fake_register)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "deploy-bot", "owner_sub": ""},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/agents/principals"
    assert captured["payload"].name == "deploy-bot"
    # Empty owner_sub coerces to None (defaults to the registering operator).
    assert captured["payload"].owner_sub is None


def test_register_requires_csrf() -> None:
    """A register POST with no CSRF header is rejected by the chassis middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "no-csrf-bot"},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_register_duplicate_name_renders_409_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate (tenant, name) re-renders the modal with a 409 name error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_exists(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise AgentPrincipalExistsError(kwargs["payload"].name)

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_exists)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "dup-bot"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 409, response.text
    body = response.text
    assert 'data-error-for="name"' in body
    assert "already exists" in body


def test_register_bad_name_renders_422_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name outside the safe alphabet re-renders the modal with a 422 name error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_value(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise ValueError("agent principal name contains characters outside the safe set")

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_value)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "bad name"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="name"' in response.text


def test_register_keycloak_unconfigured_renders_503_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keycloak unconfigured renders the gold-standard 503 detail in a banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_unconfigured(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise KeycloakAdminNotConfiguredError("unset")

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_unconfigured)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "no-kc-bot"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 503, response.text
    body = response.text
    assert "data-register-banner" in body
    # The actionable three-clause detail, not a generic message.
    assert "KEYCLOAK_ADMIN_URL" in body
    assert KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL in body


def test_register_keycloak_error_renders_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic Keycloak API failure renders a 502 banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_kc(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise KeycloakAdminError("boom")

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_kc)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "flaky-kc-bot"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 502, response.text
    assert "keycloak_admin_error" in response.text


def test_register_vault_error_renders_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Vault credential-write failure renders a 502 banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_vault(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise SchedulerVaultBrokerError("vault down")

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_vault)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "no-vault-bot"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 502, response.text
    assert "scheduler_vault_write_error" in response.text


def test_register_dead_vault_token_banner_names_the_remint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead scheduler token renders the re-mint banner, not the policy one (#2652).

    Both failures are a Vault 403, so the console used to tell the
    operator to widen a policy that was already correct. The broker's
    ``lookup-self`` disposition rides on the exception and the banner
    follows it.
    """
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_dead_token(self: AgentPrincipalService, **kwargs: Any) -> AgentPrincipalRead:
        raise SchedulerVaultBrokerError("vault denied", token_invalid=True)

    monkeypatch.setattr(AgentPrincipalService, "register", _raise_dead_token)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/register",
            data={"name": "dead-token-bot"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 502, response.text
    assert "scheduler_vault_token_invalid" in response.text
    assert "re-mint" in response.text.lower()
    assert "scheduler_vault_write_error" not in response.text
    assert "policy must grant" not in response.text


# ---------------------------------------------------------------------------
# Revoke (kill switch) -- RBAC + type-to-confirm + happy path + errors
# ---------------------------------------------------------------------------


def test_revoke_modal_requires_tenant_admin() -> None:
    """``GET .../{name}/revoke`` 403s for a non-admin operator."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="locked-bot")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals/locked-bot/revoke")
    finally:
        mock.stop()
    assert response.status_code == 403


def test_revoke_modal_renders_type_to_confirm_for_admin() -> None:
    """The revoke modal carries the type-to-confirm gate + kill-switch warning."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals/kill-me/revoke")
    finally:
        mock.stop()
    assert response.status_code == 200
    body = response.text
    assert 'id="principals-revoke-modal"' in body
    assert 'name="confirm_name"' in body
    # The submit button is gated until the typed name matches (Alpine).
    assert ':disabled="typed.trim() !== expected"' in body
    assert "kill switch" in body
    # Type-to-confirm expects the exact principal name.
    assert '"kill-me"' in body


def test_revoke_modal_missing_principal_returns_404() -> None:
    """The revoke modal for an absent name returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals/ghost/revoke")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_revoke_modal_cross_tenant_returns_404() -> None:
    """Tenant A's principal is invisible (404) to a tenant B admin."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_principal(tenant_id=_TENANT_A, name="a-bot")
    keypair, jwks = _make_keypair_and_jwks()
    token = _mint_token(
        keypair,
        sub=_OP_A,
        tenant_id=str(_TENANT_B),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_B, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/principals/a-bot/revoke")
    finally:
        mock.stop()
    assert response.status_code == 404


def test_revoke_happy_path_kills_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching confirm_name revokes the principal and HX-Redirects."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    revoked_names: list[str] = []

    async def _fake_revoke(
        self: AgentPrincipalService, tenant_id: uuid.UUID, name: str
    ) -> AgentPrincipalRead:
        revoked_names.append(name)
        return _principal_read(tenant_id=tenant_id, name=name, revoked=True)

    monkeypatch.setattr(AgentPrincipalService, "revoke", _fake_revoke)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/kill-me/revoke",
            data={"confirm_name": "kill-me"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers.get("HX-Redirect") == "/ui/agents/principals"
    assert revoked_names == ["kill-me"]


def test_revoke_mismatch_does_not_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-matching confirm_name re-renders the modal with a 422 and no kill."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    called = False

    async def _fake_revoke(
        self: AgentPrincipalService, tenant_id: uuid.UUID, name: str
    ) -> AgentPrincipalRead:
        nonlocal called
        called = True
        return _principal_read(tenant_id=tenant_id, name=name, revoked=True)

    monkeypatch.setattr(AgentPrincipalService, "revoke", _fake_revoke)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/kill-me/revoke",
            data={"confirm_name": "wrong-name"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert "did not match" in response.text
    # The kill switch must not have fired on a mismatch.
    assert called is False


def test_revoke_requires_csrf() -> None:
    """A revoke POST with no CSRF header is rejected by the chassis middleware."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.post(
            "/ui/agents/principals/kill-me/revoke",
            data={"confirm_name": "kill-me"},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403


def test_revoke_missing_principal_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoke whose service raises NotFound surfaces as 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_not_found(
        self: AgentPrincipalService, tenant_id: uuid.UUID, name: str
    ) -> AgentPrincipalRead:
        raise AgentPrincipalNotFoundError(name)

    monkeypatch.setattr(AgentPrincipalService, "revoke", _raise_not_found)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/kill-me/revoke",
            data={"confirm_name": "kill-me"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404


def test_revoke_keycloak_unconfigured_renders_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Keycloak-unconfigured revoke renders the actionable 503 banner."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_principal(tenant_id=_TENANT_A, name="kill-me")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)

    async def _raise_unconfigured(
        self: AgentPrincipalService, tenant_id: uuid.UUID, name: str
    ) -> AgentPrincipalRead:
        raise KeycloakAdminNotConfiguredError("unset")

    monkeypatch.setattr(AgentPrincipalService, "revoke", _raise_unconfigured)

    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/principals/kill-me/revoke",
            data={"confirm_name": "kill-me"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 503, response.text
    body = response.text
    assert "data-revoke-banner" in body
    assert "KEYCLOAK_ADMIN_URL" in body
