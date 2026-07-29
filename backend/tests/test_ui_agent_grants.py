# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the agent-grants console UI surface.

Initiative #1824 (G10.8 Agents console), Task #1832 (T5). The
acceptance criteria on issue #1832 are:

* Non-admins get a 403 / empty surface -- grants are tenant_admin even
  to read (the listing reveals the tenant's least-privilege posture).
* Create / elevate surface 422 validation inline; elevate enforces
  ``expires_at``.
* Verdict + expiry render unambiguously (a ``deny`` grant must not look
  like an allow).
* CSRF double-submit on all writes.

Suite shape mirrors :mod:`backend.tests.test_ui_agents`: a minimal
FastAPI app wired with the chassis middlewares + the BFF auth router +
the UI router; a ``web_session`` row seeded with a real Keycloak-minted
access token so the ``resolve_grants_admin_or_403`` gate can re-verify
the token and pick up the right :class:`TenantRole`.
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
from meho_backplane.db.models import AgentPermission, AgentPrincipal, Tenant
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
    """Construct a minimal FastAPI app wired for the grants UI tests."""
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
    """Insert one ``tenant`` row so the grant FK resolves."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(Tenant(id=tenant_id, slug=slug, name=f"Tenant {slug}"))

    asyncio.run(_do())


def _register_principal(tenant_id: uuid.UUID, client_id: str) -> None:
    """Register an agent principal so a create/elevate for it is enforceable.

    The BFF create/elevate routes go through ``AgentGrantService.grant``,
    which rejects a ``principal_sub`` naming no non-revoked agent principal
    in the tenant (#2489); a clean-create test must register it first.
    """

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentPrincipal(
                    tenant_id=tenant_id,
                    name=f"seed-{uuid.uuid4().hex[:12]}",
                    keycloak_client_id=client_id,
                    keycloak_internal_id=str(uuid.uuid4()),
                    owner_sub=_OP_A,
                    created_by_sub=_OP_A,
                )
            )

    asyncio.run(_do())


def _seed_grant(
    *,
    tenant_id: uuid.UUID,
    principal_sub: str = "agent-bot",
    op_pattern: str = "vault.kv.*",
    target_scope: str | None = None,
    verdict: str = "auto-execute",
    expires_at: datetime | None = None,
    created_by_sub: str = _OP_A,
) -> uuid.UUID:
    """Persist one ``agent_permission`` row directly."""
    grant_id = uuid.uuid4()

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                AgentPermission(
                    id=grant_id,
                    tenant_id=tenant_id,
                    principal_sub=principal_sub,
                    op_pattern=op_pattern,
                    target_scope=target_scope,
                    verdict=verdict,
                    created_by_sub=created_by_sub,
                    expires_at=expires_at,
                ),
            )

    asyncio.run(_do())
    return grant_id


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
        keypair = _make_rsa_keypair("ui-grants-test-kid")
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


def _admin_token(jwks_keypair: Any, *, tenant_id: uuid.UUID = _TENANT_A) -> str:
    """Mint a tenant_admin access token for the standard operator."""
    return _mint_token(
        jwks_keypair,
        sub=_OP_A,
        tenant_id=str(tenant_id),
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def _operator_token(jwks_keypair: Any) -> str:
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


def _future_iso(hours: int = 24) -> str:
    """An offset-bearing future ISO timestamp for an elevation expiry."""
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Authentication / RBAC boundary
# ---------------------------------------------------------------------------


def test_list_unauthenticated_redirects_to_login() -> None:
    """``GET /ui/agents/grants`` without a session 302s to the BFF login."""
    with respx.mock(assert_all_called=False):
        client = TestClient(_build_app(), follow_redirects=False)
        response = client.get("/ui/agents/grants")
    assert response.status_code == 302
    assert response.headers["location"].startswith("/ui/auth/login?return_to=")


def test_list_non_admin_is_forbidden() -> None:
    """Reading grants is tenant_admin-only -- a non-admin gets 403."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/grants")
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert "agent_grants_require_tenant_admin" in response.text


def test_create_modal_non_admin_is_forbidden() -> None:
    """A non-admin cannot even load the create modal (writes are admin)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/grants/create", headers={"HX-Request": "true"})
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


def test_list_full_page_renders_empty_state() -> None:
    """``GET /ui/agents/grants`` with no grants renders the empty state + chrome."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/grants")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<title>Agent grants" in body
    assert 'id="grants-rows"' in body
    assert "No grants" in body
    assert CSRF_COOKIE_NAME in response.cookies


def test_list_renders_verdict_badges_unambiguously() -> None:
    """Each verdict renders its own badge colour; deny is error, not success."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_grant(tenant_id=_TENANT_A, op_pattern="auto.*", verdict="auto-execute")
    _seed_grant(tenant_id=_TENANT_A, op_pattern="appr.*", verdict="needs-approval")
    _seed_grant(tenant_id=_TENANT_A, op_pattern="deny.*", verdict="deny")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/grants")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    # The deny badge carries the error colour and the deny label adjacent.
    assert "badge-error" in body
    assert "badge-success" in body
    assert "badge-warning" in body
    assert 'data-verdict-badge="deny"' in body
    # A deny grant must never carry the permissive success colour on its badge.
    deny_row_start = body.index('data-verdict="deny"')
    deny_row = body[deny_row_start : deny_row_start + 600]
    assert "badge-error" in deny_row
    assert "badge-success" not in deny_row


def test_list_distinguishes_elevation_from_permanent() -> None:
    """A grant with an expiry reads as an elevation that auto-expires."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_grant(tenant_id=_TENANT_A, op_pattern="perm.*", verdict="deny")
    _seed_grant(
        tenant_id=_TENANT_A,
        op_pattern="elev.*",
        verdict="auto-execute",
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/grants")
    finally:
        mock.stop()
    body = response.text
    assert "permanent" in body
    assert "auto-expires" in body
    assert "elevation" in body


def test_list_filters_by_principal_sub() -> None:
    """The ``principal_sub`` filter narrows the table to one principal."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_grant(tenant_id=_TENANT_A, principal_sub="agent-keep", op_pattern="keep.*")
    _seed_grant(tenant_id=_TENANT_A, principal_sub="agent-drop", op_pattern="drop.*")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(
            "/ui/agents/grants?principal_sub=agent-keep",
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    body = response.text
    assert "keep.*" in body
    assert "drop.*" not in body


def test_list_hides_expired_by_default() -> None:
    """An expired elevation is hidden unless ``include_expired`` is set."""
    _seed_tenant(_TENANT_A, "tenant-a")
    # Seed a row already in the past (bypassing the service's future check).
    _seed_grant(
        tenant_id=_TENANT_A,
        op_pattern="stale.*",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        default = client.get("/ui/agents/grants", headers={"HX-Request": "true"})
        expired = client.get(
            "/ui/agents/grants?include_expired=true", headers={"HX-Request": "true"}
        )
    finally:
        mock.stop()
    assert "stale.*" not in default.text
    assert "stale.*" in expired.text


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------


def test_detail_renders_full_grant() -> None:
    """The detail page renders the full grant + revoke affordance."""
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(
        tenant_id=_TENANT_A, principal_sub="agent-detail", op_pattern="show.*", verdict="deny"
    )
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/grants/{grant_id}")
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "agent-detail" in body
    assert "show.*" in body
    assert f"/ui/agents/grants/{grant_id}/revoke" in body


def test_detail_absent_id_is_404() -> None:
    """An absent grant id renders 404 (info-leak avoidance)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/grants/{uuid.uuid4()}")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


def test_detail_malformed_id_is_404() -> None:
    """A non-UUID grant id surfaces as 404, not 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get("/ui/agents/grants/not-a-uuid")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


def test_detail_cross_tenant_is_404() -> None:
    """A grant owned by another tenant is invisible (404, never 403)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    other = _seed_grant(tenant_id=_TENANT_B, op_pattern="theirs.*")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(f"/ui/agents/grants/{other}")
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_success_redirects() -> None:
    """A clean create persists the grant and HX-Redirects to the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_principal(_TENANT_A, "agent-new")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/grants/create",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "vault.kv.*",
                "target_scope": "",
                "verdict": "needs-approval",
                "expires_at": "",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/grants"


def test_create_bad_target_scope_renders_inline_422() -> None:
    """A non-UUID, non-``*`` target_scope re-renders the modal inline with 422."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/grants/create",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "vault.kv.*",
                "target_scope": "not-a-uuid",
                "verdict": "deny",
                "expires_at": "",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="target_scope"' in response.text


def test_create_empty_op_pattern_renders_inline_422() -> None:
    """An empty op_pattern fails Pydantic validation inline (not a 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/grants/create",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "",
                "target_scope": "",
                "verdict": "deny",
                "expires_at": "",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="op_pattern"' in response.text


def test_create_without_csrf_is_rejected() -> None:
    """A create POST without the CSRF double-submit pair is blocked."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    # with_csrf=False: no cookie set, and we omit the header too.
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=False)
    try:
        response = client.post(
            "/ui/agents/grants/create",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "vault.kv.*",
                "target_scope": "",
                "verdict": "deny",
                "expires_at": "",
            },
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Elevate
# ---------------------------------------------------------------------------


def test_elevate_requires_expires_at() -> None:
    """An elevation with no expires_at re-renders inline with the field error."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/grants/elevate",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "vault.kv.*",
                "target_scope": "",
                "verdict": "needs-approval",
                "expires_at": "",
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="expires_at"' in response.text


def test_elevate_past_expiry_renders_inline_422() -> None:
    """A past expires_at is rejected by the service inline (not a 500)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    try:
        response = client.post(
            "/ui/agents/grants/elevate",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "vault.kv.*",
                "target_scope": "",
                "verdict": "needs-approval",
                "expires_at": past,
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    assert 'data-error-for="expires_at"' in response.text


def test_elevate_success_redirects() -> None:
    """A clean elevation with a future expiry persists + HX-Redirects."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_principal(_TENANT_A, "agent-new")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            "/ui/agents/grants/elevate",
            data={
                "principal_sub": "agent-new",
                "op_pattern": "vault.kv.write",
                "target_scope": "*",
                "verdict": "auto-execute",
                "expires_at": _future_iso(),
            },
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/grants"


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


def test_revoke_modal_renders_confirm() -> None:
    """The revoke modal names the grant being revoked (native dialog confirm)."""
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, principal_sub="agent-rv", op_pattern="rv.*")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(
            f"/ui/agents/grants/{grant_id}/revoke", headers={"HX-Request": "true"}
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    body = response.text
    assert "<dialog" in body
    assert "agent-rv" in body
    assert f"/ui/agents/grants/{grant_id}/revoke" in body


def test_revoke_success_redirects() -> None:
    """A clean revoke removes the row and HX-Redirects to the table."""
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, op_pattern="gone.*")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/grants/{grant_id}/revoke",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/grants"


def test_revoke_absent_is_404() -> None:
    """Revoking an absent / already-revoked grant returns 404."""
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/grants/{uuid.uuid4()}/revoke",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404, response.text


def test_revoke_cross_tenant_is_404() -> None:
    """Revoking another tenant's grant returns 404 and leaves the row intact."""
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    other = _seed_grant(tenant_id=_TENANT_B, op_pattern="theirs.*")
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        response = client.post(
            f"/ui/agents/grants/{other}/revoke",
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 404, response.text
