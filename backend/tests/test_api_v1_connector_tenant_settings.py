# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route tests for ``/api/v1/connectors/{id}/tenant-settings`` (#5029).

The behaviour (dispatch, search, groups, audit) is pinned in
:mod:`tests.test_tenant_operation_settings`. This module pins who may
change a setting:

* a ``tenant_admin`` changes their own tenant;
* a ``tenant_admin`` naming another tenant gets 403
  ``cross_tenant_requires_platform_admin``;
* a ``platform_admin`` changes any tenant;
* an ``operator`` may read but not change.

It also pins the 404 / 409 answers and that GET shows both values.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connector_tenant_settings import router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, TenantOperationSetting
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa5029")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb5029")
_CONNECTOR_ID = "acme-ops-2"
_OP_ID = "GET:/widgets/{id}"
_BASE = f"/api/v1/connectors/{_CONNECTOR_ID}/tenant-settings"
_OP_PATH = f"{_BASE}/operations/{_OP_ID}"
_GROUP_PATH = f"{_BASE}/groups/widgets"
#: One signing key for the module: the JWKS cache keeps the first key it
#: sees within a test, so every token must use the same key.
_KEY = make_rsa_keypair("kid-tenant-settings")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    yield TestClient(app)


async def _seed(*, tenant_id: uuid.UUID | None = None) -> None:
    """One group with one ingested op, built-in unless *tenant_id* is set."""
    async with get_sessionmaker()() as session, session.begin():
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product="acme",
                version="2",
                impl_id="acme-ops",
                group_key="widgets",
                name="Widgets",
                when_to_use="Widget operations.",
                review_status="enabled",
            )
        )
        session.add(
            EndpointDescriptor(
                tenant_id=tenant_id,
                product="acme",
                version="2",
                impl_id="acme-ops",
                op_id=_OP_ID,
                source_kind="ingested",
                method="GET",
                path="/widgets/{id}",
                group_id=group_id,
                parameter_schema={"type": "object"},
                is_enabled=True,
            )
        )


async def _settings() -> dict[uuid.UUID, bool]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(TenantOperationSetting.tenant_id, TenantOperationSetting.enabled)
        )
        return dict(rows.tuples().all())


def _call(
    client: TestClient,
    method: str,
    path: str,
    *,
    role: str = "tenant_admin",
    platform_admin: bool = False,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    token = mint_token(
        _KEY,
        sub=f"op-{role}",
        tenant_id=str(_TENANT_A),
        tenant_role=role,
        platform_admin=platform_admin,
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        return client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            params=params,
        )


@pytest.mark.asyncio
async def test_tenant_admin_changes_own_tenant_and_get_shows_both_values(
    client: TestClient,
) -> None:
    await _seed()

    put = _call(client, "PUT", _OP_PATH, body={"enabled": False})
    assert put.status_code == 200, put.text
    assert put.json() == {
        "op_id": _OP_ID,
        "group_key": "widgets",
        "default_enabled": True,
        "tenant_enabled": False,
        "enabled": False,
    }
    assert await _settings() == {_TENANT_A: False}

    get = _call(client, "GET", _BASE, role="operator")
    assert get.status_code == 200, get.text
    assert get.json()["tenant_id"] == str(_TENANT_A)
    items = get.json()["items"]
    assert [(i["op_id"], i["default_enabled"], i["tenant_enabled"]) for i in items] == [
        (_OP_ID, True, False)
    ]

    reset = _call(client, "PUT", _OP_PATH, body={"enabled": None})
    assert reset.status_code == 200, reset.text
    assert reset.json()["tenant_enabled"] is None
    assert await _settings() == {}


@pytest.mark.asyncio
async def test_operator_role_cannot_change(client: TestClient) -> None:
    await _seed()

    response = _call(client, "PUT", _OP_PATH, role="operator", body={"enabled": False})

    assert response.status_code == 403
    assert await _settings() == {}


@pytest.mark.asyncio
async def test_tenant_admin_cannot_change_another_tenant(client: TestClient) -> None:
    await _seed()

    response = _call(
        client,
        "PUT",
        _OP_PATH,
        body={"enabled": False},
        params={"tenant_id": str(_TENANT_B)},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "cross_tenant_requires_platform_admin"
    assert await _settings() == {}


@pytest.mark.asyncio
async def test_platform_admin_changes_any_tenant(client: TestClient) -> None:
    await _seed()

    op = _call(
        client,
        "PUT",
        _OP_PATH,
        platform_admin=True,
        body={"enabled": False},
        params={"tenant_id": str(_TENANT_B)},
    )
    group = _call(
        client,
        "PUT",
        _GROUP_PATH,
        platform_admin=True,
        body={"enabled": True},
        params={"tenant_id": str(_TENANT_B)},
    )

    assert op.status_code == 200, op.text
    assert group.status_code == 200, group.text
    assert group.json()["tenant_id"] == str(_TENANT_B)
    assert [item["enabled"] for item in group.json()["items"]] == [True]
    assert await _settings() == {_TENANT_B: True}


@pytest.mark.asyncio
async def test_unknown_op_is_404(client: TestClient) -> None:
    await _seed()

    response = _call(client, "PUT", f"{_BASE}/operations/GET:/nothing", body={"enabled": False})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_tenant_with_its_own_copy_gets_409(client: TestClient) -> None:
    await _seed()
    await _seed(tenant_id=_TENANT_A)

    response = _call(client, "PUT", _OP_PATH, body={"enabled": False})

    assert response.status_code == 409
    assert response.json()["detail"]["detail"] == "tenant_owns_connector"


@pytest.mark.asyncio
async def test_body_must_name_enabled(client: TestClient) -> None:
    await _seed()

    response = _call(client, "PUT", _OP_PATH, body={})

    assert response.status_code == 422
