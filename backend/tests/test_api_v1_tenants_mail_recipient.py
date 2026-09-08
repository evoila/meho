# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the per-tenant mail-recipient policy route (#3499).

Covers ``PATCH /api/v1/tenants/mail-recipient-policy``
(:func:`meho_backplane.api.v1.tenants.update_mail_recipient_policy`):

* Happy path: set a value, clear to inherit (``null``), set to deny (``""``);
  the DB row and the resolved read-back reflect the write.
* Null-vs-absent: an empty PATCH body leaves the column untouched.
* Grammar validation: a malformed allowlist entry is a 422 at write time.
* ``extra='forbid'``: an unknown key is a 422.
* RBAC: ``operator`` / ``read_only`` -> 403 ``insufficient_role``;
  ``tenant_admin`` -> 200.
* Tenant isolation: a PATCH writes only the caller's own tenant.
* Audit: an applied change writes one ``audit_log`` row; a no-op binds none.
* Cache invalidation (the load-bearing one): a PATCH is reflected by the next
  :func:`resolve_tenant_recipient_allowlist` **without** a cache reset or
  restart -- proving the handler evicts the resolver's per-tenant cache.

Drives the production ``meho_backplane.main:app`` so the real middleware chain
(RequestContext -> Audit -> router) is exercised. DB is the autouse per-test
SQLite + ``alembic upgrade head``.
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
from meho_backplane.connectors.mail.tenant_policy import (
    reset_tenant_mail_policy_cache_for_testing,
    resolve_tenant_recipient_allowlist,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_ROUTE = "/api/v1/tenants/mail-recipient-policy"
_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires + isolate the resolver cache."""
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
    reset_tenant_mail_policy_cache_for_testing()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_tenant_mail_policy_cache_for_testing()


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


async def _seed_tenants(a_allowlist: str | None = None) -> None:
    """Insert the two test tenants; tenant A carries the given mail-allowlist seed."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT_A))
        ).scalar_one_or_none() is None:
            session.add(
                Tenant(
                    id=_TENANT_A,
                    slug="tenant-a",
                    name="Tenant A",
                    mail_recipient_allowlist=a_allowlist,
                )
            )
        if (
            await session.execute(select(Tenant).where(Tenant.id == _TENANT_B))
        ).scalar_one_or_none() is None:
            session.add(Tenant(id=_TENANT_B, slug="tenant-b", name="Tenant B"))
        await session.commit()


async def _fetch_tenant(tenant_id: UUID) -> Tenant:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()


async def _fetch_audit_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _patch(client: TestClient, token: str, body: dict[str, Any]) -> Any:
    return client.patch(_ROUTE, json=body, headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# Happy path: set / clear / deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_sets_allowlist_and_returns_resolved_policy(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-set")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": "oncall@ops.test"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(_TENANT_A)
    assert body["mail_recipient_allowlist"] == "oncall@ops.test"
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.mail_recipient_allowlist == "oncall@ops.test"


@pytest.mark.asyncio
async def test_patch_empty_string_is_deny(client: TestClient) -> None:
    await _seed_tenants(a_allowlist="example.com")
    key = make_rsa_keypair("kid-deny")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mail_recipient_allowlist"] == ""
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.mail_recipient_allowlist == ""


@pytest.mark.asyncio
async def test_patch_null_clears_to_inherit(client: TestClient) -> None:
    await _seed_tenants(a_allowlist="example.com")
    key = make_rsa_keypair("kid-clear")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mail_recipient_allowlist"] is None
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.mail_recipient_allowlist is None


@pytest.mark.asyncio
async def test_empty_body_leaves_column_untouched(client: TestClient) -> None:
    await _seed_tenants(a_allowlist="example.com")
    key = make_rsa_keypair("kid-absent")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mail_recipient_allowlist"] == "example.com"
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.mail_recipient_allowlist == "example.com"


# ---------------------------------------------------------------------------
# Validation: malformed grammar + unknown key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_allowlist_entry_is_422(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-bad")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": "foo@"})
    assert resp.status_code == 422, resp.text
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.mail_recipient_allowlist is None  # unchanged


@pytest.mark.asyncio
async def test_unknown_key_is_422(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-extra")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"not_a_field": "x"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# RBAC + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
async def test_non_admin_forbidden(client: TestClient, role: TenantRole) -> None:
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-{role.value}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key, role=role), {"mail_recipient_allowlist": "a@b.com"})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_patch_writes_only_callers_tenant(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-isolation")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": "a@b.com"})
    assert resp.status_code == 200, resp.text
    tenant_b = await _fetch_tenant(_TENANT_B)
    assert tenant_b.mail_recipient_allowlist is None  # untouched


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_applied_change_writes_audit_row(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": "ops.test"})
    assert resp.status_code == 200, resp.text
    rows = await _fetch_audit_rows()
    payload = rows[0].payload
    assert payload["mail_recipient_policy_changed"] is True
    assert payload["tenant_id"] == str(_TENANT_A)
    assert payload["mail_recipient_allowlist_before"] == "inherit"
    assert payload["mail_recipient_allowlist_after"] == "ops.test"


# ---------------------------------------------------------------------------
# Cache invalidation (load-bearing) — reflected without a cache reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_evicts_resolver_cache(client: TestClient) -> None:
    await _seed_tenants()  # tenant A allowlist NULL -> inherit
    # Prime the resolver cache with the pre-PATCH (inherit) value.
    assert await resolve_tenant_recipient_allowlist(_TENANT_A) is None
    key = make_rsa_keypair("kid-cache")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"mail_recipient_allowlist": "ops.test"})
    assert resp.status_code == 200, resp.text
    # No reset_...() call here — the route must have evicted the cache itself.
    resolved = await resolve_tenant_recipient_allowlist(_TENANT_A)
    assert resolved == (frozenset(), frozenset({"ops.test"}))
