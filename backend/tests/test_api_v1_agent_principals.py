# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.agent_principals`.

Coverage matrix (G11.2-T1 #815 acceptance criteria):

* Round-trip: POST register (201) -> GET list -> GET show -> DELETE revoke.
* RBAC: ``operator`` may list/show but not register/revoke (403);
  ``read_only`` is rejected on every verb; ``tenant_admin`` passes
  everywhere.
* Duplicate register returns 409.
* Show/revoke of missing name returns 404.
* ``include_revoked=true`` query param includes revoked principals.
* Cross-tenant isolation: tenant B's principal is invisible to tenant A;
  tenant A's show/revoke on tenant B's name returns 404.
* Keycloak admin not configured → 503.
* Keycloak admin error (non-conflict) → 502.

The Keycloak admin client is monkey-patched so tests do not require a
running Keycloak instance.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import meho_backplane.audit as _audit_module
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
    KeycloakClientNotFoundError,
)
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# A fake Keycloak internal UUID returned by our mock create_client.
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the broadcast publisher so tests don't time out on Valkey.

    AuditMiddleware calls ``publish_event`` after every request.  Without a
    running Valkey the redis-py client stalls for ``socket_connect_timeout``
    (3 s) on each call, adding ~3 s per API call to the test wall-clock.
    Patching the name in ``meho_backplane.audit``'s module namespace skips
    the real XADD; broadcast behaviour is covered by test_broadcast_publisher.
    """

    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_audit_module, "publish_event", _noop)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    # Provide dummy Keycloak admin env vars (will be overridden per test).
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cr3t")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: uuid.UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


async def _seed_tenants() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            existing = await session.execute(select(Tenant).where(Tenant.id == tid))
            if existing.scalar_one_or_none() is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _fetch_principals(tenant_id: uuid.UUID) -> list[AgentPrincipal]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentPrincipal)
            .where(AgentPrincipal.tenant_id == tenant_id)
            .order_by(AgentPrincipal.name)
        )
        return list(result.scalars().all())


def _mock_kc_ok(internal_id: str = _KC_INTERNAL_ID) -> MagicMock:
    """Return a mock KeycloakAdminClient that succeeds on create/secret/disable."""
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=internal_id)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.disable_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)
    return factory


@pytest.fixture(autouse=True)
def _stub_vault_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the scheduler Vault write the register path now performs (#1478).

    Registration persists the captured Keycloak secret to Vault under the
    scheduler service token; these route tests have no live Vault, so the
    write is stubbed to a no-op. The Vault-persistence behaviour itself is
    covered by ``test_auth_agent_principals.py`` (unit) and the
    integration suite (live Vault + Keycloak).
    """

    async def _noop_write(identity_ref: str, client_secret: str) -> str:
        return f"secret/data/agents/{identity_ref}/credentials"

    monkeypatch.setattr("meho_backplane.auth.agent_principals.write_agent_secret", _noop_write)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle_round_trip(client: TestClient) -> None:
    """POST register -> GET list -> GET show -> DELETE revoke."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-rt")
    tok = _token(key)
    factory = _mock_kc_ok()

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {tok}"}

        # Register.
        created = client.post(
            "/api/v1/agent-principals",
            json={"name": "deploy-bot"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "deploy-bot"
        assert body["keycloak_client_id"] == "agent:deploy-bot"
        assert body["keycloak_internal_id"] == _KC_INTERNAL_ID
        assert body["revoked"] is False
        assert body["created_by_sub"] == "op-admin"

        # List.
        listed = client.get("/api/v1/agent-principals", headers=headers)
        assert listed.status_code == 200
        principals = listed.json()["principals"]
        assert len(principals) == 1
        assert principals[0]["name"] == "deploy-bot"

        # Show.
        shown = client.get("/api/v1/agent-principals/deploy-bot", headers=headers)
        assert shown.status_code == 200
        assert shown.json()["keycloak_client_id"] == "agent:deploy-bot"

        # Revoke.
        revoked = client.delete("/api/v1/agent-principals/deploy-bot/revoke", headers=headers)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked"] is True

    # After revoke, the row is still in DB but revoked=True.
    rows = await _fetch_principals(_TENANT_A)
    assert len(rows) == 1
    assert rows[0].revoked is True


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_can_read_but_not_write(client: TestClient) -> None:
    """``operator`` lists/shows but is 403 on register/revoke."""
    await _seed_tenants()
    admin_key = make_rsa_keypair("kid-adm")
    op_key = make_rsa_keypair("kid-op")
    factory = _mock_kc_ok()

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(admin_key))
        client.post(
            "/api/v1/agent-principals",
            json={"name": "read-test-bot"},
            headers={"Authorization": f"Bearer {_token(admin_key)}"},
        )

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(op_key))
        op_headers = {"Authorization": f"Bearer {_token(op_key, role=TenantRole.OPERATOR)}"}
        assert client.get("/api/v1/agent-principals", headers=op_headers).status_code == 200
        assert (
            client.get("/api/v1/agent-principals/read-test-bot", headers=op_headers).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/agent-principals",
                json={"name": "other-bot"},
                headers=op_headers,
            ).status_code
            == 403
        )
        assert (
            client.delete(
                "/api/v1/agent-principals/read-test-bot/revoke", headers=op_headers
            ).status_code
            == 403
        )


@pytest.mark.asyncio
async def test_read_only_rejected_everywhere(client: TestClient) -> None:
    """``read_only`` token is 403 on list and register alike."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        assert client.get("/api/v1/agent-principals", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/v1/agent-principals",
                json={"name": "x"},
                headers=headers,
            ).status_code
            == 403
        )


# ---------------------------------------------------------------------------
# Conflict, not-found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_register_returns_409(client: TestClient) -> None:
    """Registering the same name twice returns 409."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-409")
    factory = _mock_kc_ok()
    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        first = client.post("/api/v1/agent-principals", json={"name": "dup-bot"}, headers=headers)
        assert first.status_code == 201
        second = client.post("/api/v1/agent-principals", json={"name": "dup-bot"}, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "agent_principal_already_exists"


@pytest.mark.asyncio
async def test_show_missing_returns_404(client: TestClient) -> None:
    """GET on an unknown name returns 404."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-404")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/agent-principals/ghost",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "agent_principal_not_found"


@pytest.mark.asyncio
async def test_revoke_missing_returns_404(client: TestClient) -> None:
    """DELETE /revoke on an unknown name returns 404."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-rev404")
    factory = _mock_kc_ok()
    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete(
            "/api/v1/agent-principals/ghost/revoke",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "agent_principal_not_found"


# ---------------------------------------------------------------------------
# include_revoked query param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_revoked_param(client: TestClient) -> None:
    """Revoked principals are hidden by default and visible with include_revoked=true."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-increv")
    factory = _mock_kc_ok()
    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        # Register + immediately revoke.
        client.post("/api/v1/agent-principals", json={"name": "to-revoke"}, headers=headers)
        client.delete("/api/v1/agent-principals/to-revoke/revoke", headers=headers)

        # Default list hides revoked.
        listed_default = client.get("/api/v1/agent-principals", headers=headers)
        assert listed_default.status_code == 200
        assert listed_default.json()["principals"] == []

        # include_revoked=true shows it.
        listed_with = client.get("/api/v1/agent-principals?include_revoked=true", headers=headers)
        assert listed_with.status_code == 200
        items = listed_with.json()["principals"]
        assert len(items) == 1
        assert items[0]["revoked"] is True


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: TestClient) -> None:
    """Tenant B's principal is invisible to tenant A; cross-tenant probe → 404."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-a")
    key_b = make_rsa_keypair("kid-b")
    factory = _mock_kc_ok()

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key_b))
        # Tenant B registers a principal.
        client.post(
            "/api/v1/agent-principals",
            json={"name": "tenant-b-bot"},
            headers={"Authorization": f"Bearer {_token(key_b, tenant_id=_TENANT_B)}"},
        )

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key_a))
        headers_a = {"Authorization": f"Bearer {_token(key_a)}"}
        # Tenant A list sees nothing.
        listed = client.get("/api/v1/agent-principals", headers=headers_a)
        assert listed.status_code == 200
        assert listed.json()["principals"] == []
        # Tenant A show of tenant B's name → 404.
        shown = client.get("/api/v1/agent-principals/tenant-b-bot", headers=headers_a)
        assert shown.status_code == 404


# ---------------------------------------------------------------------------
# Keycloak admin error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_not_configured_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KeycloakAdminNotConfiguredError`` on register → 503.

    The 503 detail is the gold-standard three-clause message
    (G0.14-T7 #1148): domain code + named env vars + doc reference.
    Symmetric with ``/ui/auth/login``'s
    :data:`~meho_backplane.ui.auth.flow.MISSING_CLIENT_SECRET_DETAIL`
    and compliant with the convention codified in
    ``docs/codebase/error-message-shape.md`` (G0.14-T11 #1141).
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-503")

    def _raise_not_configured() -> None:
        raise KeycloakAdminNotConfiguredError("not configured")

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            side_effect=_raise_not_configured,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agent-principals",
            json={"name": "bot-503"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail == KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL
    # Sanity: the three load-bearing parts of the convention are
    # present. The constant assertion above is the wire-stable
    # contract; the substring assertions below pin the convention's
    # three-clause shape (code prefix + env vars + doc reference)
    # so a future refactor that rewords the detail must keep all
    # three.
    assert detail.startswith("keycloak_admin_not_configured")
    assert "KEYCLOAK_ADMIN_URL" in detail
    assert "KEYCLOAK_ADMIN_CLIENT_ID" in detail
    assert "KEYCLOAK_ADMIN_CLIENT_SECRET" in detail
    assert "docs/cross-repo/keycloak-agent-client.md" in detail


@pytest.mark.asyncio
async def test_keycloak_admin_error_returns_502(client: TestClient) -> None:
    """A generic ``KeycloakAdminError`` on register → 502."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-502")

    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(side_effect=KeycloakAdminError("internal keycloak error"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agent-principals",
            json={"name": "bot-502"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "keycloak_admin_error"


# ---------------------------------------------------------------------------
# Kill switch + orphan rollback (AC: revoke disables the backing client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_fires_keycloak_kill_switch(client: TestClient) -> None:
    """Revoke disables the backing Keycloak client — the kill switch fires."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-kill")
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=_KC_INTERNAL_ID)
    mock_client.disable_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        client.post("/api/v1/agent-principals", json={"name": "kill-bot"}, headers=headers)
        resp = client.delete("/api/v1/agent-principals/kill-bot/revoke", headers=headers)
        assert resp.status_code == 200, resp.text

    mock_client.disable_client.assert_awaited_once_with(_KC_INTERNAL_ID)


@pytest.mark.asyncio
async def test_revoke_swallows_keycloak_not_found(client: TestClient) -> None:
    """A Keycloak 404 on disable is swallowed — revoke stays idempotent (200)."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-rev-gone")
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=_KC_INTERNAL_ID)
    mock_client.disable_client = AsyncMock(
        side_effect=KeycloakClientNotFoundError("client already gone")
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        client.post("/api/v1/agent-principals", json={"name": "gone-bot"}, headers=headers)
        resp = client.delete("/api/v1/agent-principals/gone-bot/revoke", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked"] is True

    mock_client.disable_client.assert_awaited_once_with(_KC_INTERNAL_ID)


@pytest.mark.asyncio
async def test_revoke_aborts_without_marking_revoked_when_disable_fails(
    client: TestClient,
) -> None:
    """A non-404 Keycloak disable failure aborts revoke (502) and leaves the
    row active — MEHO never reports a still-live principal as revoked."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-rev-fail")
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=_KC_INTERNAL_ID)
    mock_client.disable_client = AsyncMock(
        side_effect=KeycloakAdminError("keycloak disable failed")
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        client.post("/api/v1/agent-principals", json={"name": "stuck-bot"}, headers=headers)
        resp = client.delete("/api/v1/agent-principals/stuck-bot/revoke", headers=headers)
        assert resp.status_code == 502, resp.text

    # The disable failed before any DB write, so the row stays active.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentPrincipal).where(AgentPrincipal.name == "stuck-bot")
        )
        row = result.scalar_one()
    assert row.revoked is False


@pytest.mark.asyncio
async def test_register_rolls_back_orphan_client_on_db_failure(client: TestClient) -> None:
    """When the DB row can't be written after the Keycloak client is created,
    register deletes the orphaned client so no unrevocable identity is left."""
    await _seed_tenants()
    # Pre-seed a principal so the second insert violates the unique constraint
    # after the (mocked) Keycloak client has already been created.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AgentPrincipal(
                tenant_id=_TENANT_A,
                name="orphan-bot",
                keycloak_client_id="agent:orphan-bot",
                keycloak_internal_id="existing-internal-id",
                owner_sub="op-admin",
                revoked=False,
                created_by_sub="op-admin",
            )
        )
        await session.commit()

    new_internal_id = "cc000000-0000-0000-0000-00000000ffff"
    key = make_rsa_keypair("kid-orphan")
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=new_internal_id)
    mock_client.delete_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agent-principals",
            json={"name": "orphan-bot"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    assert resp.status_code == 409, resp.text
    # The just-created (now orphaned) Keycloak client must be deleted.
    mock_client.delete_client.assert_awaited_once_with(new_internal_id)


# ---------------------------------------------------------------------------
# Vault secret persistence at registration (G0.19-T2 #1478)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_persists_captured_secret_to_vault(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register captures the Keycloak secret and persists it to Vault.

    The scheduler reads the secret Vault-first, so registration must write
    it there for an API-registered agent to be schedulable without a pod
    env-var wire-up.
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-vault")
    factory = _mock_kc_ok()

    captured: dict[str, str] = {}

    async def _capture_write(identity_ref: str, client_secret: str) -> str:
        captured["identity_ref"] = identity_ref
        captured["secret"] = client_secret
        return f"secret/data/agents/{identity_ref}/credentials"

    monkeypatch.setattr("meho_backplane.auth.agent_principals.write_agent_secret", _capture_write)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agent-principals",
            json={"name": "vault-bot"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    assert resp.status_code == 201, resp.text
    # The captured secret is the one Keycloak's get_client_secret returned.
    assert captured == {
        "identity_ref": "agent:vault-bot",
        "secret": "generated-secret",
    }


@pytest.mark.asyncio
async def test_register_rolls_back_client_on_vault_write_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Vault-write failure rolls back the just-created Keycloak client.

    Registering an agent whose secret never reached Vault would leave it
    unschedulable (the scheduler reads Vault-first), so register fails
    closed and deletes the orphaned client.
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-vault-fail")
    new_internal_id = "dd000000-0000-0000-0000-00000000aaaa"
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=new_internal_id)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.delete_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    from meho_backplane.scheduler.vault_credentials import SchedulerVaultBrokerError

    async def _failing_write(identity_ref: str, client_secret: str) -> str:
        raise SchedulerVaultBrokerError("vault unreachable")

    monkeypatch.setattr("meho_backplane.auth.agent_principals.write_agent_secret", _failing_write)

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agent-principals",
            json={"name": "vault-fail-bot"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    # The route maps the broker error to 502; the key assertions are the
    # rollback + no DB row.
    assert resp.status_code == 502, resp.text
    mock_client.delete_client.assert_awaited_once_with(new_internal_id)
    assert await _fetch_principals(_TENANT_A) == []


@pytest.mark.asyncio
async def test_register_skips_vault_when_token_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``VAULT_SCHEDULER_TOKEN`` -> register skips the Vault write (no 5xx).

    Backward compatibility: an env-var-only deployment that has not wired a
    scheduler Vault token still registers agents successfully; the agent
    relies on the env-var fallback. Uses the *real* ``write_agent_secret``
    (the autouse stub is overridden) to exercise the not-configured branch.
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-no-token")
    factory = _mock_kc_ok()

    # Override the autouse no-op stub with the real broker write, and
    # ensure no scheduler Vault token is configured.
    import meho_backplane.scheduler.vault_credentials as vc_module

    monkeypatch.setattr(
        "meho_backplane.auth.agent_principals.write_agent_secret",
        vc_module.write_agent_secret,
    )
    monkeypatch.delenv("VAULT_SCHEDULER_TOKEN", raising=False)
    get_settings.cache_clear()

    with (
        patch(
            "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agent-principals",
            json={"name": "no-token-bot"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    assert resp.status_code == 201, resp.text
    rows = await _fetch_principals(_TENANT_A)
    assert [row.name for row in rows] == ["no-token-bot"]
