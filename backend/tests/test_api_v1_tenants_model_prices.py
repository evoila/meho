# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``/api/v1/tenants/model-prices`` (#5853).

A tenant admin reads, sets and removes the price of an agent model that MEHO
has no price for. Checks:

* Set, read back, change, remove; the tenant row holds decimal strings.
* A model in ``MODEL_PRICING`` is refused (422); MEHO's price wins.
* Bad input is 422: a negative or too large price, too many decimals, a
  model name with a space, an unknown key, a missing ``price`` key.
* At most ``MAX_MODEL_PRICES`` models; changing a priced model still works
  at the cap.
* ``operator`` and ``read_only`` get 403; a write touches only the caller's
  tenant.
* An applied change writes the audit row; a repeat of the same price changes
  nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1 import tenants as tenants_api
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
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

_ROUTE = "/api/v1/tenants/model-prices"
_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_MODEL = "openai:open-model-test"
_PRICE = {"input_per_million": "0.40", "output_per_million": "1.60"}
#: One signing key for the module: the JWKS cache keeps the first key per kid.
_KEY = make_rsa_keypair("kid-prices")


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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _token(key: Any, *, role: TenantRole = TenantRole.TENANT_ADMIN) -> str:
    return mint_token(key, sub="op-admin", tenant_role=role.value, tenant_id=str(_TENANT_A))


async def _seed_tenants(a_prices: dict[str, object] | None = None) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT_A, slug="tenant-a", name="A", agent_model_prices=a_prices))
        session.add(Tenant(id=_TENANT_B, slug="tenant-b", name="B"))
        await session.commit()


async def _stored(tenant_id: UUID) -> dict[str, object] | None:
    async with get_sessionmaker()() as session:
        tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        return tenant.agent_model_prices


def _call(client: TestClient, method: str, body: dict[str, Any] | None = None, **kw: Any) -> Any:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEY))
        return client.request(
            method, _ROUTE, json=body, headers={"Authorization": f"Bearer {_token(_KEY, **kw)}"}
        )


@pytest.mark.asyncio
async def test_set_read_change_and_remove_a_price(client: TestClient) -> None:
    await _seed_tenants()
    empty = _call(client, "GET")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"tenant_id": str(_TENANT_A), "prices": []}

    resp = _call(client, "PATCH", {"model": _MODEL, "price": _PRICE})
    assert resp.status_code == 200, resp.text
    assert resp.json()["prices"] == [
        {"model": _MODEL, "input_per_million": "0.40", "output_per_million": "1.60"}
    ]
    assert await _stored(_TENANT_A) == {_MODEL: {"input": "0.40", "output": "1.60"}}
    assert _call(client, "GET").json()["prices"] == resp.json()["prices"]

    changed = _call(
        client,
        "PATCH",
        {"model": _MODEL, "price": {"input_per_million": 1, "output_per_million": 2}},
    )
    assert changed.status_code == 200, changed.text
    assert await _stored(_TENANT_A) == {_MODEL: {"input": "1", "output": "2"}}

    removed = _call(client, "PATCH", {"model": _MODEL, "price": None})
    assert removed.status_code == 200, removed.text
    assert removed.json()["prices"] == []
    assert await _stored(_TENANT_A) is None


@pytest.mark.asyncio
async def test_removing_an_unpriced_model_changes_nothing(client: TestClient) -> None:
    await _seed_tenants()
    resp = _call(client, "PATCH", {"model": _MODEL, "price": None})
    assert resp.status_code == 200, resp.text
    assert await _stored(_TENANT_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["anthropic:claude-sonnet-5-5", "claude-sonnet-5-5"])
async def test_model_meho_prices_is_refused(client: TestClient, model: str) -> None:
    await _seed_tenants()
    resp = _call(client, "PATCH", {"model": model, "price": _PRICE})
    assert resp.status_code == 422, resp.text
    assert await _stored(_TENANT_A) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"model": _MODEL, "price": {"input_per_million": "-1", "output_per_million": "1"}},
        {"model": _MODEL, "price": {"input_per_million": "1", "output_per_million": "100001"}},
        {"model": _MODEL, "price": {"input_per_million": "0.0000001", "output_per_million": "1"}},
        {"model": _MODEL, "price": {"input_per_million": "NaN", "output_per_million": "1"}},
        {"model": _MODEL, "price": {"input_per_million": "1"}},
        {"model": "open model", "price": _PRICE},
        {"model": "", "price": _PRICE},
        {"model": _MODEL},
        {"model": _MODEL, "price": _PRICE, "extra": 1},
    ],
    ids=[
        "negative",
        "too-large",
        "too-many-decimals",
        "nan",
        "missing-output",
        "space-in-name",
        "empty-name",
        "missing-price-key",
        "unknown-key",
    ],
)
async def test_bad_input_is_422(client: TestClient, body: dict[str, Any]) -> None:
    await _seed_tenants()
    resp = _call(client, "PATCH", body)
    assert resp.status_code == 422, resp.text
    assert await _stored(_TENANT_A) is None


@pytest.mark.asyncio
async def test_cap_on_the_number_of_models(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tenants_api, "MAX_MODEL_PRICES", 1)
    await _seed_tenants(a_prices={_MODEL: {"input": "1", "output": "1"}})
    refused = _call(client, "PATCH", {"model": "openai:second-model", "price": _PRICE})
    assert refused.status_code == 422, refused.text
    # Changing the model that is already priced still works at the cap.
    changed = _call(client, "PATCH", {"model": _MODEL, "price": _PRICE})
    assert changed.status_code == 200, changed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [TenantRole.OPERATOR, TenantRole.READ_ONLY])
@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_non_admin_forbidden(client: TestClient, role: TenantRole, method: str) -> None:
    await _seed_tenants()
    body = {"model": _MODEL, "price": _PRICE} if method == "PATCH" else None
    resp = _call(client, method, body, role=role)
    assert resp.status_code == 403, resp.text
    assert await _stored(_TENANT_A) is None


@pytest.mark.asyncio
async def test_write_touches_only_the_callers_tenant(client: TestClient) -> None:
    await _seed_tenants()
    resp = _call(client, "PATCH", {"model": _MODEL, "price": _PRICE})
    assert resp.status_code == 200, resp.text
    assert await _stored(_TENANT_B) is None


@pytest.mark.asyncio
async def test_applied_change_writes_the_audit_row(client: TestClient) -> None:
    await _seed_tenants()
    resp = _call(client, "PATCH", {"model": _MODEL, "price": _PRICE})
    assert resp.status_code == 200, resp.text
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(AuditLog))).scalars().all())
    payload = rows[0].payload
    assert payload["agent_model_price_changed"] is True
    assert payload["agent_model_price_model"] == _MODEL
    assert payload["agent_model_price_before"] == "none"
    assert payload["agent_model_price_after"] == {"input": "0.40", "output": "1.60"}
    assert payload["tenant_id"] == str(_TENANT_A)
