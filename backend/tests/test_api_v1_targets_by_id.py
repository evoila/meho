# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the id lookup on the single-target routes of ``/api/v1/targets``.

``GET``, ``PATCH``, ``POST .../probe`` and ``DELETE`` on
``/api/v1/targets/{value}`` accept a name, an alias or an id. The order
is: exact name, then alias, then id (only when the value is a UUID).
The id step finds only a live target of the caller's tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _operator_token,
    _settings_env,  # noqa: F401
)

_NO_TARGET_MESSAGE = "No target has this name, alias or id."


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_describe_target_by_id(client: TestClient) -> None:
    """``GET`` with the target's id returns that target."""
    t = await _insert_target(name="node-10", product="ssh", host="10.0.0.10")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(f"/api/v1/targets/{t.id}", headers=_bearer(_operator_token(key)))
    assert response.status_code == 200
    assert response.json()["id"] == str(t.id)
    assert response.json()["name"] == "node-10"


@pytest.mark.asyncio
async def test_describe_target_by_id_with_space_in_name(client: TestClient) -> None:
    """A target whose name has a space is found by its id too."""
    t = await _insert_target(name="node-11 157", product="ssh", host="10.0.0.11")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(f"/api/v1/targets/{t.id}", headers=_bearer(_operator_token(key)))
    assert response.status_code == 200
    assert response.json()["name"] == "node-11 157"


@pytest.mark.asyncio
async def test_update_target_by_id(client: TestClient) -> None:
    """``PATCH`` with the target's id changes that target."""
    t = await _insert_target(name="node-12", product="ssh", host="10.0.0.12")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.patch(
            f"/api/v1/targets/{t.id}",
            json={"notes": "changed by id"},
            headers=_bearer(_admin_token(key)),
        )
    assert response.status_code == 200
    assert response.json()["id"] == str(t.id)
    assert response.json()["notes"] == "changed by id"


@pytest.mark.asyncio
async def test_probe_target_by_id(client: TestClient) -> None:
    """``POST .../probe`` with the target's id finds the target.

    No connector is registered for ``nsx`` in this test, so a found
    target answers 501 (naming the product), not 404.
    """
    t = await _insert_target(name="my-nsx", product="nsx", host="10.0.0.13")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/targets/{t.id}/probe", headers=_bearer(_operator_token(key))
        )
    assert response.status_code == 501
    assert "nsx" in response.json()["detail"]


@pytest.mark.asyncio
async def test_delete_target_by_id_writes_same_audit_row(client: TestClient) -> None:
    """``DELETE`` by id soft-deletes the row and writes the usual audit row.

    The audit row has ``target_id`` set and op id ``targets.delete``,
    the same as a delete by name.
    """
    t = await _insert_target(name="node-14", product="ssh", host="10.0.0.14")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.delete(f"/api/v1/targets/{t.id}", headers=_bearer(_admin_token(key)))
    assert response.status_code == 204

    sm = get_sessionmaker()
    async with sm() as session:
        row = (await session.execute(select(TargetORM).where(TargetORM.id == t.id))).scalar_one()
        audit_rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.method == "DELETE")
                    .where(AuditLog.target_id == t.id)
                )
            )
            .scalars()
            .all()
        )
    assert row.deleted_at is not None
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.payload["op_id"] == "targets.delete"
    assert audit.payload["op_class"] == "write"
    assert audit.status_code == 204

    # A second delete by the same id answers 404: the row is no longer live.
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        again = client.delete(f"/api/v1/targets/{t.id}", headers=_bearer(_admin_token(key)))
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_target_named_like_uuid_is_found_by_name_first(client: TestClient) -> None:
    """A name match wins over an id match.

    Target ``a`` is named after the id of target ``b``. A lookup with
    that value returns ``a`` (by name), not ``b`` (by id).
    """
    b = await _insert_target(name="node-b", product="ssh", host="10.0.0.21")
    a = await _insert_target(name=str(b.id), product="ssh", host="10.0.0.20")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(f"/api/v1/targets/{b.id}", headers=_bearer(_operator_token(key)))
    assert response.status_code == 200
    assert response.json()["id"] == str(a.id)


@pytest.mark.asyncio
async def test_alias_match_wins_over_id_match(client: TestClient) -> None:
    """An alias match also comes before an id match."""
    b = await _insert_target(name="node-d", product="ssh", host="10.0.0.23")
    c = await _insert_target(name="node-c", product="ssh", host="10.0.0.22", aliases=[str(b.id)])
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(f"/api/v1/targets/{b.id}", headers=_bearer(_operator_token(key)))
    assert response.status_code == 200
    assert response.json()["id"] == str(c.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "patch", "probe", "delete"])
async def test_other_tenants_target_id_returns_404(client: TestClient, method: str) -> None:
    """The id of another tenant's target answers 404 on every route."""
    other_tenant = uuid.uuid4()
    t = await _insert_target(tenant_id=other_tenant, name="foreign", product="ssh", host="h")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        headers = _bearer(_admin_token(key, DEFAULT_TENANT_ID))
        url = f"/api/v1/targets/{t.id}"
        if method == "get":
            response = client.get(url, headers=headers)
        elif method == "patch":
            response = client.patch(url, json={"notes": "x"}, headers=headers)
        elif method == "probe":
            response = client.post(f"{url}/probe", headers=headers)
        else:
            response = client.delete(url, headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "no_target"

    sm = get_sessionmaker()
    async with sm() as session:
        row = (await session.execute(select(TargetORM).where(TargetORM.id == t.id))).scalar_one()
    assert row.deleted_at is None
    assert row.notes is None


@pytest.mark.asyncio
async def test_deleted_target_id_returns_404(client: TestClient) -> None:
    """The id of a deleted target answers 404."""
    t = await _insert_target(
        name="retired", product="ssh", host="10.0.0.30", deleted_at=datetime.now(UTC)
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(f"/api/v1/targets/{t.id}", headers=_bearer(_operator_token(key)))
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "no_target"


@pytest.mark.asyncio
async def test_unknown_uuid_404_says_no_name_alias_or_id(client: TestClient) -> None:
    """A UUID that matches nothing gets a 404 that names all three lookups."""
    value = str(uuid.uuid4())
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(f"/api/v1/targets/{value}", headers=_bearer(_operator_token(key)))
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "no_target"
    assert detail["query"] == value
    assert detail["matches"] == []
    assert detail["message"] == _NO_TARGET_MESSAGE


def test_unknown_name_404_is_unchanged(client: TestClient) -> None:
    """A value that is not a UUID keeps today's 404 body (no id step)."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get("/api/v1/targets/no-such-name", headers=_bearer(_operator_token(key)))
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error": "no_target",
        "query": "no-such-name",
        "matches": [],
    }
