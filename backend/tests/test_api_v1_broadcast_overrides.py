# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.broadcast_overrides`.

Coverage matrix (Task #381 / G6.3-T4 acceptance criteria):

* Happy path: POST creates a row (201 + row body); GET lists own
  tenant; DELETE removes a row (204).
* RBAC: ``operator`` and ``read_only`` callers get 403 on every
  verb. ``tenant_admin`` is the only role accepted.
* Pydantic validation: invalid ``scope_field``, half-set scope pair
  (``scope_field`` without ``scope_value``), regex characters in
  ``op_id_pattern`` all return 422.
* 409 on composite-uniqueness violation (the natural-key uniqueness
  contract T1 #378 ships via ``broadcast_override_tenant_unique_idx``).
* Cross-tenant isolation: tenant B's POST is invisible to tenant A's
  GET; tenant A's DELETE on tenant B's row returns 404 (never 403 --
  existence is not leaked across tenant boundaries).
* Cache invalidation: after a CRUD mutation, T2's
  ``_TENANT_CACHE`` no longer contains the operator's tenant entry
  (the next publish hydrates from DB).
* Audit-row enrichment: every successful mutation produces an audit
  row whose payload carries ``override_op`` / ``override_id`` /
  ``override_pattern`` / ``override_detail``.

The tests drive the production ``meho_backplane.main:app`` so the
real middleware chain (RequestContext → BroadcastDetail → Audit →
router) is exercised. DB is the autouse ``_default_database_url``
fixture's per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.broadcast.overrides import (
    _TENANT_CACHE,
    reset_overrides_cache_for_testing,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, BroadcastOverride, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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


@pytest.fixture(autouse=True)
def _reset_resolver_cache() -> Iterator[None]:
    """T2's per-tenant cache is module-level; wipe between cases."""
    reset_overrides_cache_for_testing()
    yield
    reset_overrides_cache_for_testing()


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(tenant_id),
    )


async def _seed_tenants() -> None:
    """Insert the two test tenants if they don't already exist."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            existing = await session.execute(select(Tenant).where(Tenant.id == tid))
            if existing.scalar_one_or_none() is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _fetch_overrides(tenant_id: UUID) -> list[BroadcastOverride]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(BroadcastOverride)
            .where(BroadcastOverride.tenant_id == tenant_id)
            .order_by(BroadcastOverride.created_at),
        )
        return list(result.scalars().all())


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


# ---------------------------------------------------------------------------
# Happy path -- POST + GET + DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_override_returns_201(
    client: TestClient,
) -> None:
    """``POST /api/v1/broadcast/overrides`` returns 201 + the new row body."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-post")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={
                "op_id_pattern": "k8s.configmap.info",
                "scope_field": "namespace",
                "scope_value": "kube-system",
                "detail": "aggregate",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["op_id_pattern"] == "k8s.configmap.info"
    assert body["scope_field"] == "namespace"
    assert body["scope_value"] == "kube-system"
    assert body["detail"] == "aggregate"
    assert body["created_by_sub"] == "op-admin"
    assert UUID(body["id"])  # parseable
    # Row landed in DB under tenant A.
    rows = await _fetch_overrides(_TENANT_A)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_get_lists_own_tenant_overrides(client: TestClient) -> None:
    """``GET`` returns the operator's tenant's rules; ordering by ``created_at``."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-get")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Seed two rules.
        for pattern, detail in (("vault.kv.*", "full"), ("audit.*", "aggregate")):
            client.post(
                "/api/v1/broadcast/overrides",
                json={"op_id_pattern": pattern, "detail": detail},
                headers={"Authorization": f"Bearer {token}"},
            )
        resp = client.get(
            "/api/v1/broadcast/overrides",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    patterns = {row["op_id_pattern"] for row in body}
    assert patterns == {"vault.kv.*", "audit.*"}


@pytest.mark.asyncio
async def test_delete_removes_own_row_returns_204(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-delete")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        create = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token}"},
        )
        override_id = create.json()["id"]
        resp = client.delete(
            f"/api/v1/broadcast/overrides/{override_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 204
    assert resp.text == ""
    rows = await _fetch_overrides(_TENANT_A)
    assert rows == []


# ---------------------------------------------------------------------------
# RBAC -- only tenant_admin is admitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
)
async def test_non_admin_roles_get_403_on_get(
    client: TestClient,
    role: TenantRole,
) -> None:
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-rbac-{role.value}")
    token = _token(key, role=role)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(
            "/api/v1/broadcast/overrides",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "insufficient_role"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
)
async def test_non_admin_roles_get_403_on_post(
    client: TestClient,
    role: TenantRole,
) -> None:
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-rbac-{role.value}-post")
    token = _token(key, role=role)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [TenantRole.OPERATOR, TenantRole.READ_ONLY],
)
async def test_non_admin_roles_get_403_on_delete(
    client: TestClient,
    role: TenantRole,
) -> None:
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-rbac-{role.value}-delete")
    token = _token(key, role=role)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.delete(
            f"/api/v1/broadcast/overrides/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Pydantic validation -- 422 on shape errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_with_invalid_scope_field_returns_422(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-validate-scope")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={
                "op_id_pattern": "vault.kv.*",
                "scope_field": "principal_sub",  # not in the allowlist
                "scope_value": "op-1",
                "detail": "aggregate",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_with_half_set_scope_pair_returns_422(client: TestClient) -> None:
    """``scope_field`` set but ``scope_value`` NULL is rejected by model_validator."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-validate-pair")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={
                "op_id_pattern": "vault.kv.*",
                "scope_field": "namespace",
                # scope_value missing
                "detail": "aggregate",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_with_regex_chars_in_pattern_returns_422(client: TestClient) -> None:
    """``op_id_pattern`` containing regex syntax is rejected -- glob-only per #376."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-validate-regex")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={
                "op_id_pattern": "vault\\.kv\\..+",  # regex syntax
                "detail": "aggregate",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422
    assert "glob" in resp.json()["detail"][0]["msg"].lower()


@pytest.mark.asyncio
async def test_post_with_invalid_detail_returns_422(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-validate-detail")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "verbose"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 409 on composite-uniqueness violation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_duplicate_returns_409(client: TestClient) -> None:
    """Second POST with identical ``(pattern, scope_field, scope_value)`` → 409."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-dup")
    token = _token(key)
    body = {
        "op_id_pattern": "k8s.configmap.info",
        "scope_field": "namespace",
        "scope_value": "kube-system",
        "detail": "aggregate",
    }
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        first = client.post(
            "/api/v1/broadcast/overrides",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert first.status_code == 201
        second = client.post(
            "/api/v1/broadcast/overrides",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert second.status_code == 409
    assert second.json()["detail"] == "broadcast_override_already_exists"


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_does_not_leak_cross_tenant(client: TestClient) -> None:
    """Tenant A's POST is invisible to tenant B's GET."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-a")
    key_b = make_rsa_keypair("kid-xt-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    with respx.mock as r:
        # Same JWKS endpoint serves both keys.
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        resp = client.get(
            "/api/v1/broadcast/overrides",
            headers={"Authorization": f"Bearer {token_b}"},
        )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_delete_cross_tenant_returns_404(client: TestClient) -> None:
    """Tenant A's DELETE on tenant B's row → 404 (NOT 403 -- no existence leak)."""
    await _seed_tenants()
    key_a = make_rsa_keypair("kid-xt-del-a")
    key_b = make_rsa_keypair("kid-xt-del-b")
    token_a = _token(key_a, tenant_id=_TENANT_A)
    token_b = _token(key_b, sub="op-b", tenant_id=_TENANT_B)
    with respx.mock as r:
        combined_jwks = {
            "keys": [public_jwks(key_a)["keys"][0], public_jwks(key_b)["keys"][0]],
        }
        mock_discovery_and_jwks(r, combined_jwks)
        create = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        override_id = create.json()["id"]
        resp = client.delete(
            f"/api/v1/broadcast/overrides/{override_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "broadcast_override_not_found"
    # The row still exists in tenant B's table.
    rows = await _fetch_overrides(_TENANT_B)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Cache invalidation -- T2's per-tenant cache is wiped on every mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_invalidates_resolver_cache(client: TestClient) -> None:
    """POST → next resolver lookup sees the new rule, not the stale cache.

    The route handler calls ``invalidate_tenant_cache`` on success.
    The post-route audit-middleware publish path then re-hydrates the
    cache via the resolver -- but with FRESH rows, not the sentinel.
    The contract this test pins: after a POST, the cache (if present)
    contains the newly-created rule, never the stale pre-seed.
    """
    import time as time_module

    await _seed_tenants()
    # Pre-seed the cache with a sentinel empty rule set under a long TTL.
    _TENANT_CACHE[_TENANT_A] = ([], time_module.monotonic() + 60.0)

    key = make_rsa_keypair("kid-invalidate-post")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201
    # If the cache was re-hydrated by the post-route publish path,
    # it must contain the freshly-created rule -- never the empty
    # sentinel. Either "evicted" (no entry) or "repopulated with the
    # new rule" is acceptable; the prohibited shape is "stale empty
    # list".
    entry = _TENANT_CACHE.get(_TENANT_A)
    if entry is not None:
        rules, _expires_at = entry
        assert len(rules) == 1
        assert rules[0].op_id_pattern == "vault.kv.*"


@pytest.mark.asyncio
async def test_delete_invalidates_resolver_cache(client: TestClient) -> None:
    """DELETE → next resolver lookup no longer sees the deleted rule."""
    import time as time_module

    await _seed_tenants()
    key = make_rsa_keypair("kid-invalidate-delete")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        create = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token}"},
        )
        override_id = create.json()["id"]
        # Pre-seed the cache (as if a prior publish populated it).
        _TENANT_CACHE[_TENANT_A] = (
            list(await _fetch_overrides(_TENANT_A)),
            time_module.monotonic() + 60.0,
        )
        assert len(_TENANT_CACHE[_TENANT_A][0]) == 1
        client.delete(
            f"/api/v1/broadcast/overrides/{override_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    # Post-DELETE: cache either gone, or repopulated empty.
    entry = _TENANT_CACHE.get(_TENANT_A)
    if entry is not None:
        rules, _expires_at = entry
        assert rules == []


# ---------------------------------------------------------------------------
# Audit-row enrichment -- mutations carry the override diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_audit_row_carries_override_diff(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit-post")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        create = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert create.status_code == 201
    override_id = create.json()["id"]
    rows = await _fetch_audit_rows()
    post_rows = [
        row for row in rows if row.method == "POST" and row.path == "/api/v1/broadcast/overrides"
    ]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "meho.broadcast.overrides.set"
    assert payload["op_class"] == "write"
    assert payload["override_op"] == "set"
    assert payload["override_id"] == override_id
    assert payload["override_pattern"] == "vault.kv.*"
    assert payload["override_detail"] == "aggregate"


@pytest.mark.asyncio
async def test_delete_audit_row_carries_override_diff(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit-delete")
    token = _token(key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        create = client.post(
            "/api/v1/broadcast/overrides",
            json={"op_id_pattern": "vault.kv.*", "detail": "aggregate"},
            headers={"Authorization": f"Bearer {token}"},
        )
        override_id = create.json()["id"]
        client.delete(
            f"/api/v1/broadcast/overrides/{override_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    rows = await _fetch_audit_rows()
    delete_rows = [row for row in rows if row.method == "DELETE"]
    assert len(delete_rows) == 1
    payload = delete_rows[0].payload
    assert payload["op_id"] == "meho.broadcast.overrides.remove"
    assert payload["op_class"] == "write"
    assert payload["override_op"] == "remove"
    assert payload["override_id"] == override_id
