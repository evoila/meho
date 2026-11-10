# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST``, ``PATCH`` and ``DELETE /api/v1/targets`` are for people only.

* **Machine logins** -- a service login with the ``tenant_admin`` role
  gets 403 ``human_principal_required`` on all three writes. The text
  names ``targets.register`` and ``targets.remove``. An agent login gets
  403 ``insufficient_role`` (MEHO caps its role at ``operator``). Nothing
  is written in any case.
* **Reads** -- a service login still lists and describes targets.
* **People** -- a ``tenant_admin`` person (the CLI uses these routes)
  still creates, changes and removes a target.
* **Wrong role** -- a person with the ``operator`` role still gets
  ``insufficient_role``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.targets import MACHINE_TARGET_WRITE_REMEDIATION
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
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

_NEW_BODY: dict[str, Any] = {"name": "new-system", "product": "ssh", "host": "new-system.example"}
_EXISTING = "existing-system"
_EXISTING_HOST = "existing-system.example"


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _machine_token(key: Any, kind: str) -> str:
    return mint_token(
        key,
        sub=f"{kind}:deploy-bot",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=DEFAULT_TENANT_ID,
        principal_kind=kind,
    )


def _send(client: TestClient, method: str, token: str) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    if method == "POST":
        return client.post("/api/v1/targets", json=_NEW_BODY, headers=headers)
    if method == "PATCH":
        return client.patch(
            f"/api/v1/targets/{_EXISTING}", json={"host": "other.example"}, headers=headers
        )
    return client.delete(f"/api/v1/targets/{_EXISTING}", headers=headers)


async def _live_rows() -> dict[str, TargetORM]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(TargetORM).where(
                TargetORM.tenant_id == uuid.UUID(DEFAULT_TENANT_ID),
                TargetORM.deleted_at.is_(None),
            )
        )
        return {row.name: row for row in rows.scalars().all()}


async def _assert_nothing_written() -> None:
    rows = await _live_rows()
    assert set(rows) == {_EXISTING}
    assert rows[_EXISTING].host == _EXISTING_HOST


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_a_service_login_with_tenant_admin_cannot_write_targets(
    client: TestClient, method: str
) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = _send(client, method, _machine_token(key, "service"))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail == f"human_principal_required: {MACHINE_TARGET_WRITE_REMEDIATION}"
    assert "targets.register" in detail
    assert "targets.remove" in detail
    await _assert_nothing_written()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_an_agent_login_cannot_write_targets(client: TestClient, method: str) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = _send(client, method, _machine_token(key, "agent"))

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
    await _assert_nothing_written()


@pytest.mark.asyncio
async def test_a_service_login_still_reads_targets(client: TestClient) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    key = make_rsa_keypair("kid-A")
    token = _machine_token(key, "service")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        listed = client.get("/api/v1/targets", headers={"Authorization": f"Bearer {token}"})
        described = client.get(
            f"/api/v1/targets/{_EXISTING}", headers={"Authorization": f"Bearer {token}"}
        )

    assert listed.status_code == 200, listed.text
    assert [item["name"] for item in listed.json()["items"]] == [_EXISTING]
    assert described.status_code == 200, described.text


@pytest.mark.asyncio
async def test_a_person_creates_changes_and_removes_a_target(client: TestClient) -> None:
    await _insert_target(name=_EXISTING, host=_EXISTING_HOST)
    key = make_rsa_keypair("kid-A")
    token = _admin_token(key)
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        created = _send(client, "POST", token)
        changed = _send(client, "PATCH", token)
        rows_after_patch = await _live_rows()
        removed = _send(client, "DELETE", token)

    assert created.status_code == 201, created.text
    assert changed.status_code == 200, changed.text
    assert rows_after_patch[_EXISTING].host == "other.example"
    assert removed.status_code == 204, removed.text
    assert set(await _live_rows()) == {"new-system"}


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_a_person_without_tenant_admin_gets_insufficient_role(
    client: TestClient, method: str
) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = _send(client, method, _operator_token(key))

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
