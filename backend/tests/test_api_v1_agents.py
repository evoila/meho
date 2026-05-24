# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.agents`.

Coverage matrix (Task #809 / G11.1-T2 acceptance criteria):

* Round-trip: POST creates (201) -> GET lists -> GET /{name} shows ->
  PATCH edits -> DELETE removes (204).
* RBAC: ``operator`` may read (list / show) but not write
  (create / edit / delete -> 403); ``read_only`` is rejected on every
  verb; ``tenant_admin`` passes everywhere.
* Pydantic validation: an unknown field, a bad name, an out-of-range
  turn budget, and an invalid model tier all return 422.
* 409 on a duplicate ``(tenant, name)`` create.
* Cross-tenant isolation: tenant B's definition is invisible to tenant
  A's GET; tenant A's GET / PATCH / DELETE on tenant B's name returns
  404 (never 403 -- existence is not leaked across tenant boundaries).
* Audit-row enrichment: a successful create produces an audit row whose
  payload carries ``agent_name`` and whose op_id is ``agent.create``.

The tests drive the production ``meho_backplane.main:app`` so the real
middleware chain (RequestContext -> Audit -> router) is exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_VALID_BODY: dict[str, Any] = {
    "name": "incident-triage",
    "identity_ref": "agent:incident-triage",
    "model_tier": "deep",
    "system_prompt": "You triage infra incidents.",
    "toolset": {"allow": ["call_operation"]},
    "turn_budget": 25,
    "output_schema": {"type": "object"},
    "enabled": True,
}


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
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
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


async def _fetch_defs(tenant_id: UUID) -> list[AgentDefinition]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentDefinition)
            .where(AgentDefinition.tenant_id == tenant_id)
            .order_by(AgentDefinition.name)
        )
        return list(result.scalars().all())


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


# ---------------------------------------------------------------------------
# Happy path -- full CRUD round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_crud_round_trip(client: TestClient) -> None:
    """POST -> GET list -> GET show -> PATCH -> DELETE round-trips."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-crud")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {token}"}

        # Create.
        created = client.post("/api/v1/agents", json=_VALID_BODY, headers=headers)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "incident-triage"
        assert body["model_tier"] == "deep"
        assert body["created_by_sub"] == "op-admin"
        assert UUID(body["id"])

        # List.
        listed = client.get("/api/v1/agents", headers=headers)
        assert listed.status_code == 200
        assert [a["name"] for a in listed.json()["agents"]] == ["incident-triage"]

        # Show.
        shown = client.get("/api/v1/agents/incident-triage", headers=headers)
        assert shown.status_code == 200
        assert shown.json()["turn_budget"] == 25

        # Edit (partial).
        edited = client.patch(
            "/api/v1/agents/incident-triage",
            json={"turn_budget": 50, "enabled": False},
            headers=headers,
        )
        assert edited.status_code == 200
        edited_body = edited.json()
        assert edited_body["turn_budget"] == 50
        assert edited_body["enabled"] is False
        assert edited_body["model_tier"] == "deep"  # unchanged

        # Delete.
        deleted = client.delete("/api/v1/agents/incident-triage", headers=headers)
        assert deleted.status_code == 204
        assert deleted.text == ""

    assert await _fetch_defs(_TENANT_A) == []


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_can_read_but_not_write(client: TestClient) -> None:
    """An ``operator`` lists/shows but is 403 on create/edit/delete."""
    await _seed_tenants()
    admin_key = make_rsa_keypair("kid-admin")
    op_key = make_rsa_keypair("kid-op")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(admin_key))
        # Admin seeds one definition.
        client.post(
            "/api/v1/agents",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {_token(admin_key)}"},
        )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(op_key))
        op_headers = {"Authorization": f"Bearer {_token(op_key, role=TenantRole.OPERATOR)}"}
        assert client.get("/api/v1/agents", headers=op_headers).status_code == 200
        assert client.get("/api/v1/agents/incident-triage", headers=op_headers).status_code == 200
        assert (
            client.post("/api/v1/agents", json=_VALID_BODY, headers=op_headers).status_code == 403
        )
        assert (
            client.patch(
                "/api/v1/agents/incident-triage",
                json={"enabled": False},
                headers=op_headers,
            ).status_code
            == 403
        )
        assert (
            client.delete("/api/v1/agents/incident-triage", headers=op_headers).status_code == 403
        )


@pytest.mark.asyncio
async def test_read_only_is_rejected_everywhere(client: TestClient) -> None:
    """A ``read_only`` token gets 403 on list and create alike."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-ro")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {_token(key, role=TenantRole.READ_ONLY)}"}
        assert client.get("/api/v1/agents", headers=headers).status_code == 403
        assert client.post("/api/v1/agents", json=_VALID_BODY, headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# Validation + conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"systemPrompt": "typo"},  # unknown field
        {"name": "has/slash"},  # bad name
        {"turn_budget": 0},  # below floor
        {"turn_budget": 5000},  # above cap
        {"model_tier": "ultra"},  # not in enum
    ],
)
async def test_create_validation_422(client: TestClient, mutation: dict[str, Any]) -> None:
    """An invalid create body returns 422 from the Pydantic layer."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-422")
    token = _token(key)
    payload = {**_VALID_BODY, **mutation}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_duplicate_create_returns_409(client: TestClient) -> None:
    """A second create on the same ``(tenant, name)`` returns 409."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-409")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        headers = {"Authorization": f"Bearer {token}"}
        first = client.post("/api/v1/agents", json=_VALID_BODY, headers=headers)
        assert first.status_code == 201
        second = client.post("/api/v1/agents", json=_VALID_BODY, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "agent_already_exists"


@pytest.mark.asyncio
async def test_show_missing_returns_404(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-404")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get("/api/v1/agents/nope", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "agent_not_found"


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client: TestClient) -> None:
    """Tenant B's definition is invisible to tenant A; cross-tenant probes 404."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-iso")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Tenant B creates a definition.
        b_token = _token(key, sub="op-b", tenant_id=_TENANT_B)
        client.post(
            "/api/v1/agents",
            json=_VALID_BODY,
            headers={"Authorization": f"Bearer {b_token}"},
        )
        # Tenant A sees nothing and cannot reach B's row.
        a_headers = {"Authorization": f"Bearer {_token(key, tenant_id=_TENANT_A)}"}
        listed = client.get("/api/v1/agents", headers=a_headers)
        assert listed.json()["agents"] == []
        assert client.get("/api/v1/agents/incident-triage", headers=a_headers).status_code == 404
        assert (
            client.patch(
                "/api/v1/agents/incident-triage",
                json={"enabled": False},
                headers=a_headers,
            ).status_code
            == 404
        )
        assert client.delete("/api/v1/agents/incident-triage", headers=a_headers).status_code == 404
    # Tenant B's row is intact.
    b_rows = await _fetch_defs(_TENANT_B)
    assert len(b_rows) == 1
    assert b_rows[0].enabled is True


# ---------------------------------------------------------------------------
# Audit enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_writes_audit_row_with_agent_name(client: TestClient) -> None:
    """A successful create produces an audit row tagged agent.create + name."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/agents", json=_VALID_BODY, headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 201
    rows = await _fetch_audit_rows()
    create_rows = [row for row in rows if row.payload.get("op_id") == "agent.create"]
    assert create_rows, f"no agent.create audit row found in {[r.payload for r in rows]}"
    assert create_rows[-1].payload.get("agent_name") == "incident-triage"
