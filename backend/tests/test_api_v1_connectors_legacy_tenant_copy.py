# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""REST surface for an old tenant connector copy under a former product name (#4889).

The service matrix lives in
:mod:`tests.test_operations_ingest_legacy_tenant_copy`; this module pins
what the ``/api/v1/connectors`` routes answer for the same rows: the old
copy is reviewable with ``prefer=tenant`` and ``prefer=legacy``, every
other read / write on the id is a structured ``409
connector_legacy_tenant_copy`` that never says
"not yet ingested", DELETE removes only the old copy, and the listing
marks the right row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connectors_ingest import router as connectors_ingest_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_VERSION = "9.0"
_IMPL_ID = "vrli-rest"
_PRODUCT = "vrli"
_LEGACY_PRODUCT = "vcf-logs"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_BUILTIN_OP = "vrli.event.query"
_LEGACY_OP = "GET:/api/v1/group-0/0"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(connectors_ingest_router)
    yield TestClient(app)


def _token(*, tenant_id: uuid.UUID, platform_admin: bool = False) -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-legacy")
    token = _mint_token(
        key,
        sub="op-tenant-admin",
        tenant_id=str(tenant_id),
        tenant_role="tenant_admin",
        platform_admin=platform_admin,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(tenant_id: uuid.UUID, *, current_copy: bool = False) -> None:
    """Built-in ``vrli`` copy (one enabled typed op) + old ``vcf-logs`` tenant copy.

    *current_copy* adds a tenant copy under the current product too, with
    one staged group and one op.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if current_copy:
            current_group = uuid.uuid4()
            session.add(
                OperationGroup(
                    id=current_group,
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    group_key="current",
                    name="Current",
                    when_to_use="Use the current copy.",
                    review_status="staged",
                )
            )
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id="GET:/api/v1/current",
                    source_kind="ingested",
                    method="GET",
                    path="/api/v1/current",
                    group_id=current_group,
                    summary="Current operation",
                    is_enabled=False,
                )
            )
        builtin_group = uuid.uuid4()
        session.add(
            OperationGroup(
                id=builtin_group,
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="events",
                name="Events",
                when_to_use="Query log events.",
                review_status="enabled",
            )
        )
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=_BUILTIN_OP,
                source_kind="typed",
                method=None,
                path=None,
                group_id=builtin_group,
                summary="Query events",
                is_enabled=True,
            )
        )
        legacy_group = uuid.uuid4()
        session.add(
            OperationGroup(
                id=legacy_group,
                tenant_id=tenant_id,
                product=_LEGACY_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="group-0",
                name="Group 0",
                when_to_use="Use group 0.",
                review_status="staged",
            )
        )
        for index in range(3):
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_LEGACY_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=f"GET:/api/v1/group-0/{index}",
                    source_kind="ingested",
                    method="GET",
                    path=f"/api/v1/group-0/{index}",
                    group_id=legacy_group,
                    summary=f"Operation {index}",
                    is_enabled=False,
                )
            )
        await session.commit()


async def _state(
    tenant_id: uuid.UUID | None, product: str
) -> tuple[dict[str, str], dict[str, bool]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_stmt = select(OperationGroup).where(
            OperationGroup.product == product,
            OperationGroup.version == _VERSION,
            OperationGroup.impl_id == _IMPL_ID,
        )
        op_stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == _VERSION,
            EndpointDescriptor.impl_id == _IMPL_ID,
        )
        if tenant_id is None:
            group_stmt = group_stmt.where(OperationGroup.tenant_id.is_(None))
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            group_stmt = group_stmt.where(OperationGroup.tenant_id == tenant_id)
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        group_rows = (await session.execute(group_stmt)).scalars()
        op_rows = (await session.execute(op_stmt)).scalars()
        groups = {g.group_key: g.review_status for g in group_rows}
        ops = {o.op_id: o.is_enabled for o in op_rows}
        return groups, ops


_BUILTIN_STATE = ({"events": "enabled"}, {_BUILTIN_OP: True})


def _assert_legacy_409(response: Any, tenant_id: uuid.UUID) -> None:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["detail"] == "connector_legacy_tenant_copy"
    assert detail["connector_id"] == _CONNECTOR_ID
    assert detail["legacy_product"] == _LEGACY_PRODUCT
    assert detail["product"] == _PRODUCT
    assert detail["tenant_id"] == str(tenant_id)
    assert _LEGACY_PRODUCT in detail["message"]
    assert f"meho connector delete {_CONNECTOR_ID}" in detail["message"]
    body = json.dumps(response.json())
    assert "not yet ingested" not in body
    assert "connector ingest" not in body
    assert "connector_not_ingested" not in body


@pytest.mark.asyncio
async def test_review_prefer_tenant_returns_old_copy(client: TestClient) -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    key, token = _token(tenant_id=tenant_id)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            f"/api/v1/connectors/{_CONNECTOR_ID}/review",
            params={"prefer": "tenant"},
            headers=_authed(token),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["product"] == _LEGACY_PRODUCT
    assert body["tenant_id"] == str(tenant_id)
    assert body["total_op_count"] == 3


@pytest.mark.asyncio
async def test_review_prefer_legacy_returns_old_copy_next_to_current_copy(
    client: TestClient,
) -> None:
    """With a current tenant copy too, ``prefer=legacy`` is the way to the old copy."""
    tenant_id = uuid.uuid4()
    await _seed(tenant_id, current_copy=True)
    key, token = _token(tenant_id=tenant_id)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        seen = {
            prefer: client.get(
                f"/api/v1/connectors/{_CONNECTOR_ID}/review",
                params={"prefer": prefer},
                headers=_authed(token),
            )
            for prefer in ("legacy", "tenant", "builtin")
        }

    for response in seen.values():
        assert response.status_code == 200, response.text
    legacy, current, builtin = (seen[p].json() for p in ("legacy", "tenant", "builtin"))
    assert (legacy["product"], legacy["tenant_id"]) == (_LEGACY_PRODUCT, str(tenant_id))
    assert legacy["total_op_count"] == 3
    assert (current["product"], current["tenant_id"]) == (_PRODUCT, str(tenant_id))
    assert current["total_op_count"] == 1
    assert (builtin["product"], builtin["tenant_id"]) == (_PRODUCT, None)


@pytest.mark.asyncio
async def test_review_without_prefer_is_409_naming_old_copy_not_ingest(
    client: TestClient,
) -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    key, token = _token(tenant_id=tenant_id)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            f"/api/v1/connectors/{_CONNECTOR_ID}/review",
            headers=_authed(token),
        )

    _assert_legacy_409(response, tenant_id)


_WRITE_CALLS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", f"/api/v1/connectors/{_CONNECTOR_ID}/enable", None),
    ("POST", f"/api/v1/connectors/{_CONNECTOR_ID}/disable", None),
    ("POST", f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads", None),
    ("PATCH", f"/api/v1/connectors/{_CONNECTOR_ID}/groups/group-0", {"name": "Renamed"}),
    ("PATCH", f"/api/v1/connectors/{_CONNECTOR_ID}/operations/{_LEGACY_OP}", {"is_enabled": True}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    _WRITE_CALLS,
    ids=["enable", "disable", "enable-reads", "edit-group", "edit-op"],
)
async def test_writes_are_409_and_change_neither_copy(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """Even a platform admin cannot change the built-in copy through the old id by accident."""
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    legacy_before = await _state(tenant_id, _LEGACY_PRODUCT)
    key, token = _token(tenant_id=tenant_id, platform_admin=True)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.request(method, path, json=body, headers=_authed(token))

    _assert_legacy_409(response, tenant_id)
    assert await _state(None, _PRODUCT) == _BUILTIN_STATE
    assert await _state(tenant_id, _LEGACY_PRODUCT) == legacy_before


@pytest.mark.asyncio
async def test_delete_removes_old_copy_and_builtin_stays(client: TestClient) -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    key, token = _token(tenant_id=tenant_id)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        deleted = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}", headers=_authed(token))
        review = client.get(
            f"/api/v1/connectors/{_CONNECTOR_ID}/review",
            headers=_authed(token),
        )

    assert deleted.status_code == 204, deleted.text
    assert await _state(tenant_id, _LEGACY_PRODUCT) == ({}, {})
    assert await _state(None, _PRODUCT) == _BUILTIN_STATE
    # The plain review now reaches the built-in copy, the one calls use.
    assert review.status_code == 200, review.text
    assert review.json()["product"] == _PRODUCT
    assert review.json()["tenant_id"] is None


@pytest.mark.asyncio
async def test_list_marks_old_copy_and_leaves_builtin_unshadowed(client: TestClient) -> None:
    tenant_id = uuid.uuid4()
    await _seed(tenant_id)
    key, token = _token(tenant_id=tenant_id)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))

    assert response.status_code == 200, response.text
    rows = {
        item["scope"]: item
        for item in response.json()["items"]
        if item["connector_id"] == _CONNECTOR_ID
    }
    assert set(rows) == {"builtin", "tenant"}
    assert rows["builtin"]["shadowed_by_tenant_scope"] is False
    assert rows["builtin"]["next_step"] is None
    assert rows["tenant"]["product"] == _LEGACY_PRODUCT
    assert rows["tenant"]["dispatchable"] is False
    assert rows["tenant"]["next_step"]["verb"] == f"meho connector delete {_CONNECTOR_ID}"
    assert _LEGACY_PRODUCT in rows["tenant"]["next_step"]["rationale"]
