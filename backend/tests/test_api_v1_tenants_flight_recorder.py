# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.tenants` (#3272).

Covers the operator mutation surface for the per-tenant flight-recorder policy
(``PATCH /api/v1/tenants/flight-recorder-policy``):

* Happy path: each of the three fields flips + the resolved policy is returned;
  the DB row reflects the write.
* Tri-state semantics: ``flight_recorder_agent_readable`` sets ``true`` /
  ``false`` and clears with an explicit ``null``; ``flight_recorder_retention_days``
  sets, clears (``null``), and rejects out-of-bounds; ``flight_recorder_enabled``
  rejects an explicit ``null`` (the column is NOT NULL).
* Null-vs-absent: an omitted field leaves its column untouched.
* RBAC: ``operator`` / ``read_only`` -> 403 ``insufficient_role``;
  ``tenant_admin`` -> 200.
* Tenant isolation: a PATCH writes only the caller's own tenant (no path/body
  tenant id exists to reach another tenant).
* Audit: an applied change writes one ``audit_log`` row naming field / old / new;
  a no-op PATCH binds no policy-change keys.
* Cache invalidation (the load-bearing one): flipping the flag through the route
  is reflected by the next :func:`should_capture` **without** a cache reset or
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
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.flight_recorder.config import (
    reset_flight_recorder_config_cache_for_testing,
    should_capture,
    should_expose_to_agent,
)
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

_ROUTE = "/api/v1/tenants/flight-recorder-policy"
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
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_flight_recorder_config_cache_for_testing()


def _token(
    key: Any,
    *,
    sub: str = "op-admin",
    role: TenantRole = TenantRole.TENANT_ADMIN,
    tenant_id: UUID = _TENANT_A,
) -> str:
    return mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(tenant_id))


async def _seed_tenants(
    *,
    a_enabled: bool = False,
    a_agent_readable: bool | None = None,
    a_retention: int | None = None,
) -> None:
    """Insert the two test tenants; tenant A carries the given policy seed."""
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
                    flight_recorder_enabled=a_enabled,
                    flight_recorder_agent_readable=a_agent_readable,
                    flight_recorder_retention_days=a_retention,
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


def _patch(
    client: TestClient,
    token: str,
    body: dict[str, Any],
) -> Any:
    return client.patch(_ROUTE, json=body, headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# Happy path + resolved read-back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_enables_capture_and_returns_resolved_policy(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-enable")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_enabled": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(_TENANT_A)
    assert body["flight_recorder_enabled"] is True
    assert body["flight_recorder_agent_readable"] is None
    assert body["flight_recorder_retention_days"] is None
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.flight_recorder_enabled is True


@pytest.mark.asyncio
async def test_partial_patch_leaves_absent_fields_untouched(client: TestClient) -> None:
    """A PATCH that sends only one field does not disturb the others (null-vs-absent)."""
    await _seed_tenants(a_enabled=True, a_agent_readable=True, a_retention=14)
    key = make_rsa_keypair("kid-partial")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_retention_days": 30})
    assert resp.status_code == 200, resp.text
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.flight_recorder_enabled is True  # untouched
    assert tenant.flight_recorder_agent_readable is True  # untouched
    assert tenant.flight_recorder_retention_days == 30  # changed


# ---------------------------------------------------------------------------
# Tri-state semantics: null clears, absent keeps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_readable_tristate_set_false_then_clear_to_null(client: TestClient) -> None:
    await _seed_tenants(a_enabled=True, a_agent_readable=True)
    key = make_rsa_keypair("kid-agent")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        # Force OFF while capture stays on (the F5 independent gate-off).
        resp_false = _patch(client, _token(key), {"flight_recorder_agent_readable": False})
        # Clear back to inherit with an explicit null.
        resp_null = _patch(client, _token(key), {"flight_recorder_agent_readable": None})
    assert resp_false.status_code == 200, resp_false.text
    assert resp_false.json()["flight_recorder_agent_readable"] is False
    assert resp_null.status_code == 200, resp_null.text
    assert resp_null.json()["flight_recorder_agent_readable"] is None
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.flight_recorder_agent_readable is None  # inherit


@pytest.mark.asyncio
async def test_retention_set_then_clear_to_null(client: TestClient) -> None:
    await _seed_tenants(a_retention=14)
    key = make_rsa_keypair("kid-retention")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp_null = _patch(client, _token(key), {"flight_recorder_retention_days": None})
    assert resp_null.status_code == 200, resp_null.text
    assert resp_null.json()["flight_recorder_retention_days"] is None
    tenant = await _fetch_tenant(_TENANT_A)
    assert tenant.flight_recorder_retention_days is None  # back to global default


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_days", [0, -1, 366, 100000])
async def test_retention_out_of_bounds_rejected(client: TestClient, bad_days: int) -> None:
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-bound-{bad_days}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_retention_days": bad_days})
    assert resp.status_code == 422, resp.text
    # unchanged
    assert (await _fetch_tenant(_TENANT_A)).flight_recorder_retention_days is None


@pytest.mark.asyncio
async def test_enabled_explicit_null_rejected(client: TestClient) -> None:
    """``flight_recorder_enabled`` has no inherit state -- explicit null is a 422."""
    await _seed_tenants(a_enabled=True)
    key = make_rsa_keypair("kid-null-enabled")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_enabled": None})
    assert resp.status_code == 422, resp.text
    assert (await _fetch_tenant(_TENANT_A)).flight_recorder_enabled is True  # unchanged


@pytest.mark.asyncio
async def test_unknown_field_rejected(client: TestClient) -> None:
    await _seed_tenants()
    key = make_rsa_keypair("kid-extra")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_bogus": True})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
async def test_non_admin_roles_get_403(client: TestClient, role: TenantRole) -> None:
    await _seed_tenants()
    key = make_rsa_keypair(f"kid-rbac-{role.value}")
    token = _token(key, sub=f"op-{role.value}", role=role)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, token, {"flight_recorder_enabled": True})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "insufficient_role"
    # The rejected write mutated nothing.
    assert (await _fetch_tenant(_TENANT_A)).flight_recorder_enabled is False


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_touches_only_callers_own_tenant(client: TestClient) -> None:
    """Tenant A's admin PATCH must not touch tenant B (no cross-tenant path)."""
    await _seed_tenants()
    key = make_rsa_keypair("kid-isolation")
    token = _token(key, tenant_id=_TENANT_A)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, token, {"flight_recorder_enabled": True})
    assert resp.status_code == 200, resp.text
    assert (await _fetch_tenant(_TENANT_A)).flight_recorder_enabled is True
    assert (await _fetch_tenant(_TENANT_B)).flight_recorder_enabled is False


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_writes_audit_row_naming_field_old_new(client: TestClient) -> None:
    await _seed_tenants(a_enabled=False, a_retention=None)
    key = make_rsa_keypair("kid-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(
            client,
            _token(key),
            {"flight_recorder_enabled": True, "flight_recorder_retention_days": 14},
        )
    assert resp.status_code == 200, resp.text
    rows = await _fetch_audit_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["flight_recorder_policy_changed"] is True
    assert payload["tenant_id"] == str(_TENANT_A)
    assert payload["flight_recorder_enabled_before"] is False
    assert payload["flight_recorder_enabled_after"] is True
    assert payload["flight_recorder_retention_days_before"] == "inherit"
    assert payload["flight_recorder_retention_days_after"] == 14


@pytest.mark.asyncio
async def test_noop_patch_binds_no_policy_audit_keys(client: TestClient) -> None:
    """A PATCH that sends the current value writes an audit row with no policy keys."""
    await _seed_tenants(a_enabled=True)
    key = make_rsa_keypair("kid-noop")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_enabled": True})
    assert resp.status_code == 200, resp.text
    rows = await _fetch_audit_rows()
    assert len(rows) == 1
    assert "flight_recorder_policy_changed" not in rows[0].payload


# ---------------------------------------------------------------------------
# Cache invalidation (the load-bearing property)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_flip_reflected_by_resolver_without_restart(client: TestClient) -> None:
    """Flipping the flag through the route is seen by the next ``should_capture``.

    Primes the resolver cache (capture OFF), PATCHes capture ON, then re-reads
    WITHOUT resetting the cache. A stale cache would still answer ``False``; the
    ``True`` proves the handler evicted the per-tenant entry.
    """
    await _seed_tenants(a_enabled=False)
    reset_flight_recorder_config_cache_for_testing()
    # Prime the cache: capture OFF for tenant A.
    assert await should_capture(tenant_id=_TENANT_A) is False
    key = make_rsa_keypair("kid-cache")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_enabled": True})
    assert resp.status_code == 200, resp.text
    # No cache reset here on purpose -- the route must have invalidated it.
    assert await should_capture(tenant_id=_TENANT_A) is True


@pytest.mark.asyncio
async def test_agent_read_flip_reflected_without_restart(client: TestClient) -> None:
    """The same cache-eviction proof for the F5 agent-read gate."""
    await _seed_tenants(a_enabled=True, a_agent_readable=None)
    reset_flight_recorder_config_cache_for_testing()
    # Inherit -> follows capture default (ON) -> exposed.
    assert await should_expose_to_agent(tenant_id=_TENANT_A) is True
    key = make_rsa_keypair("kid-agent-cache")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_agent_readable": False})
    assert resp.status_code == 200, resp.text
    assert await should_expose_to_agent(tenant_id=_TENANT_A) is False


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unseeded_tenant_is_autoprovisioned_then_patched(client: TestClient) -> None:
    """An unseeded tenant is get-or-created by the ``ensure_tenant`` middleware.

    The route's own ``tenant_not_found`` 404 is defensive depth only: every
    authenticated request runs ``ensure_tenant`` first, so the row exists by the
    time the handler reads it and the PATCH lands 200 with the flag applied.
    """
    # Deliberately do NOT seed tenants -- the middleware provisions the row.
    key = make_rsa_keypair("kid-autoseed")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = _patch(client, _token(key), {"flight_recorder_enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["flight_recorder_enabled"] is True
    assert (await _fetch_tenant(_TENANT_A)).flight_recorder_enabled is True
