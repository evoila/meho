# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.runner_principals`.

Coverage matrix (Initiative #2415, #2502 acceptance criteria):

* Round-trip: POST register (201) -> GET list -> GET show -> DELETE revoke.
* register stamps the runner mapper set on the Keycloak client
  (``tenant_role=read_only``, ``principal_kind=runner``,
  ``runner_id=<row uuid>``, clientId ``runner:<name>``) — asserted against
  the recorded Keycloak admin transport.
* RBAC: ``operator`` may list/show but not register/revoke (403);
  ``read_only`` is rejected on every verb; ``tenant_admin`` passes.
* Duplicate register -> 409; show/revoke of missing name -> 404.
* ``include_revoked=true`` includes revoked principals.
* Cross-tenant isolation: tenant B's principal is invisible to tenant A.
* Keycloak admin not configured -> 503; other admin error -> 502.
* Revoke ordering: Keycloak ``disable_client`` fires before the row flips;
  a non-404 disable failure aborts revoke (502) with ``revoked=false``.
* Negative cage: a ``principal_kind=runner`` token is 403'd on a
  representative non-gateway route (``GET /api/v1/agent-principals``).

The Keycloak admin client is monkey-patched so tests need no live Keycloak.
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
)
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunnerPrincipal, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

# A fake Keycloak internal UUID returned by our mock create_client.
_KC_INTERNAL_ID = "cc000000-0000-0000-0000-000000000009"


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the broadcast publisher so tests don't time out on Valkey."""

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


@pytest.fixture(autouse=True)
def _stub_vault_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the scheduler Vault write the register path performs (no live Vault)."""

    async def _noop_write(identity_ref: str, client_secret: str) -> str:
        return f"secret/data/runners/{identity_ref}/credentials"

    monkeypatch.setattr("meho_backplane.auth.runner_principals.write_agent_secret", _noop_write)


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: uuid.UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


def _runner_token(
    key: Any,
    *,
    runner_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
) -> str:
    return mint_token(
        key,
        sub="runner-sub",
        tenant_role=TenantRole.READ_ONLY.value,
        tenant_id=str(tenant_id),
        principal_kind="runner",
        runner_id=str(runner_id or uuid.uuid4()),
    )


async def _seed_tenants() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            existing = await session.execute(select(Tenant).where(Tenant.id == tid))
            if existing.scalar_one_or_none() is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _fetch_principals(tenant_id: uuid.UUID) -> list[RunnerPrincipal]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(RunnerPrincipal)
            .where(RunnerPrincipal.tenant_id == tenant_id)
            .order_by(RunnerPrincipal.name)
        )
        return list(result.scalars().all())


def _mock_kc_ok(internal_id: str = _KC_INTERNAL_ID) -> MagicMock:
    """A mock KeycloakAdminClient that succeeds on create/secret/disable."""
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=internal_id)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.disable_client = AsyncMock(return_value=None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_client)


# ---------------------------------------------------------------------------
# Round-trip + runner mapper stamping
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
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {tok}"}

        created = client.post(
            "/api/v1/runner-principals",
            json={"name": "edge-runner"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "edge-runner"
        assert body["keycloak_client_id"] == "runner:edge-runner"
        assert body["keycloak_internal_id"] == _KC_INTERNAL_ID
        assert body["revoked"] is False
        assert body["created_by_sub"] == "op-admin"

        listed = client.get("/api/v1/runner-principals", headers=headers)
        assert listed.status_code == 200
        principals = listed.json()["principals"]
        assert len(principals) == 1
        assert principals[0]["name"] == "edge-runner"

        shown = client.get("/api/v1/runner-principals/edge-runner", headers=headers)
        assert shown.status_code == 200
        assert shown.json()["keycloak_client_id"] == "runner:edge-runner"

        revoked = client.delete("/api/v1/runner-principals/edge-runner/revoke", headers=headers)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked"] is True

    rows = await _fetch_principals(_TENANT_A)
    assert len(rows) == 1
    assert rows[0].revoked is True


@pytest.mark.asyncio
async def test_register_stamps_runner_mappers_on_keycloak_client(client: TestClient) -> None:
    """register mints a ``runner:<name>`` client with the read-only runner mapper set.

    The token's ``runner_id`` claim (hardcoded on the Keycloak client) must
    equal the DB row's id — the invariant the gateway guard binds on.
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-map")
    factory = _mock_kc_ok()
    mock_client = factory.return_value

    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        created = client.post(
            "/api/v1/runner-principals",
            json={"name": "map-runner"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )

    assert created.status_code == 201, created.text
    row_id = created.json()["id"]

    mock_client.create_client.assert_awaited_once()
    kwargs = mock_client.create_client.await_args.kwargs
    assert kwargs["client_id"] == "runner:map-runner"
    assert kwargs["tenant_role"] == "read_only"
    assert kwargs["principal_kind"] == "runner"
    assert kwargs["kind_attribute"] == "runner"
    # The hardcoded runner_id claim == the DB row id (guard binding invariant).
    assert kwargs["extra_hardcoded_claims"] == {"runner_id": row_id}


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
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(admin_key))
        client.post(
            "/api/v1/runner-principals",
            json={"name": "read-test-runner"},
            headers={"Authorization": f"Bearer {_token(admin_key)}"},
        )

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(op_key))
        op_headers = {"Authorization": f"Bearer {_token(op_key, role=TenantRole.OPERATOR)}"}
        assert client.get("/api/v1/runner-principals", headers=op_headers).status_code == 200
        assert (
            client.get("/api/v1/runner-principals/read-test-runner", headers=op_headers).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/runner-principals",
                json={"name": "other-runner"},
                headers=op_headers,
            ).status_code
            == 403
        )
        assert (
            client.delete(
                "/api/v1/runner-principals/read-test-runner/revoke", headers=op_headers
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
        assert client.get("/api/v1/runner-principals", headers=headers).status_code == 403
        assert (
            client.post(
                "/api/v1/runner-principals",
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
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        first = client.post(
            "/api/v1/runner-principals", json={"name": "dup-runner"}, headers=headers
        )
        assert first.status_code == 201
        second = client.post(
            "/api/v1/runner-principals", json={"name": "dup-runner"}, headers=headers
        )
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "runner_principal_already_exists"


@pytest.mark.asyncio
async def test_show_missing_returns_404(client: TestClient) -> None:
    """GET on an unknown name returns 404."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-404")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/runner-principals/ghost",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "runner_principal_not_found"


@pytest.mark.asyncio
async def test_revoke_missing_returns_404(client: TestClient) -> None:
    """DELETE /revoke on an unknown name returns 404."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-rev404")
    factory = _mock_kc_ok()
    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete(
            "/api/v1/runner-principals/ghost/revoke",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "runner_principal_not_found"


# ---------------------------------------------------------------------------
# include_revoked + cross-tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_revoked_param(client: TestClient) -> None:
    """Revoked principals are hidden by default, visible with include_revoked=true."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-increv")
    factory = _mock_kc_ok()
    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        client.post("/api/v1/runner-principals", json={"name": "to-revoke"}, headers=headers)
        client.delete("/api/v1/runner-principals/to-revoke/revoke", headers=headers)

        listed_default = client.get("/api/v1/runner-principals", headers=headers)
        assert listed_default.status_code == 200
        assert listed_default.json()["principals"] == []

        listed_with = client.get("/api/v1/runner-principals?include_revoked=true", headers=headers)
        assert listed_with.status_code == 200
        items = listed_with.json()["principals"]
        assert len(items) == 1
        assert items[0]["revoked"] is True


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: TestClient) -> None:
    """Tenant B's principal is invisible to tenant A; cross-tenant probe → 404."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-a")
    key_b = make_rsa_keypair("kid-b")
    factory = _mock_kc_ok()

    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key_b))
        client.post(
            "/api/v1/runner-principals",
            json={"name": "tenant-b-runner"},
            headers={"Authorization": f"Bearer {_token(key_b, tenant_id=_TENANT_B)}"},
        )

    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key_a))
        headers_a = {"Authorization": f"Bearer {_token(key_a)}"}
        listed = client.get("/api/v1/runner-principals", headers=headers_a)
        assert listed.status_code == 200
        assert listed.json()["principals"] == []
        shown = client.get("/api/v1/runner-principals/tenant-b-runner", headers=headers_a)
        assert shown.status_code == 404


# ---------------------------------------------------------------------------
# Keycloak admin error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keycloak_not_configured_returns_503(client: TestClient) -> None:
    """``KeycloakAdminNotConfiguredError`` on register → 503 with the gold-standard detail."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-503")

    def _raise_not_configured() -> None:
        raise KeycloakAdminNotConfiguredError("not configured")

    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            side_effect=_raise_not_configured,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/runner-principals",
            json={"name": "runner-503"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL


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
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/runner-principals",
            json={"name": "runner-502"},
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "keycloak_admin_error"


# ---------------------------------------------------------------------------
# Revoke ordering (kill switch fires before the row flips)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_disables_keycloak_client(client: TestClient) -> None:
    """Revoke disables the backing Keycloak client — the kill switch fires."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-kill")
    factory = _mock_kc_ok()
    mock_client = factory.return_value

    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        client.post("/api/v1/runner-principals", json={"name": "kill-runner"}, headers=headers)
        resp = client.delete("/api/v1/runner-principals/kill-runner/revoke", headers=headers)
        assert resp.status_code == 200, resp.text

    mock_client.disable_client.assert_awaited_once_with(_KC_INTERNAL_ID)


@pytest.mark.asyncio
async def test_revoke_aborts_without_marking_revoked_when_disable_fails(client: TestClient) -> None:
    """A non-404 Keycloak disable failure aborts revoke (502) and leaves the row active.

    Pins the ordering contract: Keycloak ``enabled=false`` is committed
    *before* the row's ``revoked=true``, so a disable failure means the row
    stays ``revoked=false`` — MEHO never reports a still-live, token-issuing
    runner as revoked.
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-rev-fail")
    mock_client = AsyncMock()
    mock_client.create_client = AsyncMock(return_value=_KC_INTERNAL_ID)
    mock_client.get_client_secret = AsyncMock(return_value="generated-secret")
    mock_client.disable_client = AsyncMock(
        side_effect=KeycloakAdminError("keycloak disable failed")
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=mock_client)

    with (
        patch(
            "meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings",
            factory,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key)}"}
        client.post("/api/v1/runner-principals", json={"name": "stuck-runner"}, headers=headers)
        resp = client.delete("/api/v1/runner-principals/stuck-runner/revoke", headers=headers)
        assert resp.status_code == 502, resp.text

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(RunnerPrincipal).where(RunnerPrincipal.name == "stuck-runner")
        )
        row = result.scalar_one()
    assert row.revoked is False


# ---------------------------------------------------------------------------
# Negative cage on a representative non-gateway route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_kind_token_caged_from_non_gateway_route(client: TestClient) -> None:
    """A ``principal_kind=runner`` token is 403'd on ``GET /api/v1/agent-principals``.

    The route is outside :data:`~meho_backplane.middleware.RUNNER_ALLOWED_PATH_PREFIXES`,
    so the negative cage in ``verify_jwt_and_bind`` fail-closed 403s the
    runner token before the route's RBAC even runs.
    """
    await _seed_tenants()
    key = make_rsa_keypair("kid-caged")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/agent-principals",
            headers={"Authorization": f"Bearer {_runner_token(key)}"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "runner_scope_violation"
