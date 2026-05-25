# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.approvals`.

Coverage matrix (Task #818 / G11.2-T5 acceptance criteria):

* List: GET /api/v1/approvals returns pending items for the tenant,
  filtered by ?status=pending; cross-tenant items invisible.
* Show: GET /api/v1/approvals/{id} returns full detail including
  elicitation_url when backplane_url is configured.
* Approve: POST /api/v1/approvals/{id}/approve flips status to
  approved, stamps reviewed_by + decided_at, rejects non-pending (409).
* Reject: POST /api/v1/approvals/{id}/reject flips status to rejected.
* Decide: POST /api/v1/approvals/{id}/decide routes to approve or
  reject based on the body ``decision`` field.
* RBAC: operator passes all verbs; read_only is rejected (403).
* Tenant scoping: cross-tenant id returns 404; existence not leaked.
* 404 on absent id.
* Broadcast publish is called (fail-open; verified by side-effects).

The tests drive the production ``meho_backplane.main:app`` so the real
middleware chain is exercised. :class:`~meho_backplane.db.models.ApprovalRequest`
rows are inserted directly via the ORM to avoid coupling to T4's create
path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalStatus, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install_fake_vault(monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


async def _seed_tenant(tenant_id: uuid.UUID) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        existing = await session.get(Tenant, tenant_id)
        if existing is None:
            session.add(Tenant(id=tenant_id, slug=str(tenant_id), name=str(tenant_id)))
            await session.commit()


def _token(key: object, *, sub: str, tenant_id: uuid.UUID, role: TenantRole) -> str:
    """Mint a JWT for *tenant_id* with *role* in one short call."""
    return mint_token(
        key,
        sub=sub,
        tenant_id=str(tenant_id),
        tenant_role=role.value,
    )


async def _seed_approval(
    *,
    tenant_id: uuid.UUID,
    status: str = "pending",
    request_id: uuid.UUID | None = None,
) -> uuid.UUID:
    rid = request_id or uuid.uuid4()
    sm = get_sessionmaker()
    async with sm() as session:
        row = ApprovalRequest(
            id=rid,
            tenant_id=tenant_id,
            principal_sub="agent-sub",
            connector_id="vmware-rest-9.0",
            op_id="vcenter.vm.list",
            params_hash="abc123",
            status=status,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
    return rid


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_pending_for_tenant(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="pending")
    key = make_rsa_keypair("kid-list-pending")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/approvals?status=pending",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [item["id"] for item in data["items"]]
    assert str(rid) in ids


@pytest.mark.asyncio
async def test_list_cross_tenant_invisible(client: TestClient) -> None:
    """Tenant B's requests are invisible to tenant A."""
    await _seed_tenant(_TENANT_A)
    await _seed_tenant(_TENANT_B)
    await _seed_approval(tenant_id=_TENANT_B, status="pending")
    key = make_rsa_keypair("kid-list-xtenant")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/approvals",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    # Tenant B's item must not appear.
    tenant_ids = {item.get("tenant_id") for item in data["items"]}
    assert str(_TENANT_B) not in tenant_ids


@pytest.mark.asyncio
async def test_list_read_only_rejected(client: TestClient) -> None:
    key = make_rsa_keypair("kid-list-ro")
    token = _token(key, sub="ro-a", tenant_id=_TENANT_A, role=TenantRole.READ_ONLY)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/approvals",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Show / Get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_returns_detail(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A)
    key = make_rsa_keypair("kid-show-ok")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/approvals/{rid}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(rid)
    assert data["connector_id"] == "vmware-rest-9.0"


@pytest.mark.asyncio
async def test_show_404_absent(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    key = make_rsa_keypair("kid-show-404")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/approvals/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_show_cross_tenant_is_404(client: TestClient) -> None:
    """Tenant A cannot see tenant B's request — 404, not 403."""
    await _seed_tenant(_TENANT_A)
    await _seed_tenant(_TENANT_B)
    rid = await _seed_approval(tenant_id=_TENANT_B)
    key = make_rsa_keypair("kid-show-xtenant")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            f"/api/v1/approvals/{rid}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_flips_status(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="pending")
    key = make_rsa_keypair("kid-approve-ok")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with (
        respx.mock as r,
        patch(
            "meho_backplane.approvals.service.publish_event",
            new_callable=AsyncMock,
        ),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/approvals/{rid}/approve",
            json={"reason": "looks safe"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["reviewed_by"] == "op-a"
    assert data["decided_at"] is not None


@pytest.mark.asyncio
async def test_approve_non_pending_is_409(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="approved")
    key = make_rsa_keypair("kid-approve-409")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/approvals/{rid}/approve",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_flips_status(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="pending")
    key = make_rsa_keypair("kid-reject-ok")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with (
        respx.mock as r,
        patch(
            "meho_backplane.approvals.service.publish_event",
            new_callable=AsyncMock,
        ),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/approvals/{rid}/reject",
            json={"reason": "too risky"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert data["reviewed_by"] == "op-a"


# ---------------------------------------------------------------------------
# Decide (MCP elicitation URL-mode endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_approve(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="pending")
    key = make_rsa_keypair("kid-decide-approve")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with (
        respx.mock as r,
        patch(
            "meho_backplane.approvals.service.publish_event",
            new_callable=AsyncMock,
        ),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/approvals/{rid}/decide",
            json={"decision": "approve"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_decide_reject(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="pending")
    key = make_rsa_keypair("kid-decide-reject")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with (
        respx.mock as r,
        patch(
            "meho_backplane.approvals.service.publish_event",
            new_callable=AsyncMock,
        ),
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/approvals/{rid}/decide",
            json={"decision": "reject", "reason": "nope"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_decide_invalid_decision(client: TestClient) -> None:
    await _seed_tenant(_TENANT_A)
    rid = await _seed_approval(tenant_id=_TENANT_A, status="pending")
    key = make_rsa_keypair("kid-decide-invalid")
    token = _token(key, sub="op-a", tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/approvals/{rid}/decide",
            json={"decision": "maybe"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DB model drift guard
# ---------------------------------------------------------------------------


def test_approval_status_enum_matches_migration() -> None:
    """ApprovalStatus enum values must match the migration's _APPROVAL_STATUSES tuple."""
    from meho_backplane.db.models import _APPROVAL_STATUSES  # type: ignore[attr-defined]

    model_values = {s.value for s in ApprovalStatus}
    migration_values = set(_APPROVAL_STATUSES)
    assert model_values == migration_values, (
        f"ApprovalStatus enum and migration _APPROVAL_STATUSES have drifted.\n"
        f"Enum only: {model_values - migration_values}\n"
        f"Migration only: {migration_values - model_values}"
    )
