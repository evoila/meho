# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A settings import writes targets at once only for a person in the web console.

The same rule as ``POST`` and ``PATCH /api/v1/targets``:

* A call with a token (CLI, script, AI tool) that applies a file which adds
  or changes a target gets 403 with a plain reason. Nothing is written: no
  target row, no revision, no approval request.
* The preview of the same file works with a token.
* A file with no target change applies with a token.
* A person in a web session applies the target change at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, InstanceSettingsRevision, Target, Tenant
from meho_backplane.instance_settings.service import TARGET_WRITES_NEED_CONSOLE
from meho_backplane.main import app

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._rest_write_helpers import settings_import_at_once
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_URL = "/api/v1/settings/import"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _file(*, targets: list[dict[str, Any]] | None = None, retention_days: int = 14) -> str:
    tenant: dict[str, Any] = {
        "id": str(_TENANT),
        "policies": {"flight_recorder": {"enabled": True, "retention_days": retention_days}},
    }
    if targets is not None:
        tenant["targets"] = targets
    doc = {"apiVersion": "meho.evoila.com/v1", "kind": "Settings", "spec": {"tenants": [tenant]}}
    return yaml.safe_dump(doc, sort_keys=False)


_STORED = {"name": "app-01", "product": "ssh", "host": "10.0.0.10", "verify_tls": True}
_CHANGED = {**_STORED, "host": "10.0.0.20", "verify_tls": False}
_NEW = {"name": "app-02", "product": "ssh", "host": "10.0.0.30"}


async def _seed() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT,
                slug="tenant-a",
                name="Tenant A",
                flight_recorder_enabled=True,
                flight_recorder_retention_days=14,
            )
        )
        await session.commit()
    await _insert_target(tenant_id=_TENANT, **_STORED)


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


async def _retention_days() -> int | None:
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, _TENANT)
    assert tenant is not None
    return tenant.flight_recorder_retention_days


def _post(client: TestClient, content: str, *, preview: bool = False) -> Any:
    key = make_rsa_keypair("kid-settings-targets")
    token = mint_token(
        key, sub="admin-person", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        return client.post(
            _URL,
            params={"preview": "true"} if preview else None,
            content=content.encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
        )


@pytest.mark.asyncio
async def test_token_import_that_changes_a_target_is_refused(client: TestClient) -> None:
    await _seed()
    resp = _post(client, _file(targets=[_CHANGED]))

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == TARGET_WRITES_NEED_CONSOLE
    assert "/ui/settings" in resp.json()["detail"]
    (row,) = await _rows(Target)
    assert (row.host, row.verify_tls) == ("10.0.0.10", True)
    assert await _rows(ApprovalRequest) == []
    assert await _rows(InstanceSettingsRevision) == []


@pytest.mark.asyncio
async def test_token_import_that_adds_a_target_is_refused(client: TestClient) -> None:
    await _seed()
    # The policy change in the same file is not written either.
    resp = _post(client, _file(targets=[_STORED, _NEW], retention_days=30))

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == TARGET_WRITES_NEED_CONSOLE
    assert [t.name for t in await _rows(Target)] == ["app-01"]
    assert await _retention_days() == 14
    assert await _rows(ApprovalRequest) == []
    assert await _rows(InstanceSettingsRevision) == []


@pytest.mark.asyncio
async def test_token_preview_of_a_target_change_works(client: TestClient) -> None:
    await _seed()
    resp = _post(client, _file(targets=[_CHANGED, _NEW]), preview=True)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["mistakes"] == []
    assert [a["key"] for a in body["diff"]["targets"]["added"]] == ["app-02"]
    (change,) = body["diff"]["targets"]["changed"]
    assert change["before"] == {"host": "10.0.0.10", "verify_tls": True}
    assert change["after"] == {"host": "10.0.0.20", "verify_tls": False}
    assert [t.name for t in await _rows(Target)] == ["app-01"]


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", [None, [_STORED]], ids=["no-targets", "same-targets"])
async def test_token_import_with_no_target_change_applies(
    client: TestClient, targets: list[dict[str, Any]] | None
) -> None:
    await _seed()
    resp = _post(client, _file(targets=targets, retention_days=30))

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert await _retention_days() == 30
    assert len(await _rows(InstanceSettingsRevision)) == 1


@pytest.mark.asyncio
async def test_web_session_applies_the_target_change_at_once(client: TestClient) -> None:
    await _seed()
    with settings_import_at_once(app):
        resp = _post(client, _file(targets=[_CHANGED, _NEW]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["applied"] is True
    rows = {t.name: t for t in await _rows(Target)}
    assert (rows["app-01"].host, rows["app-01"].verify_tls) == ("10.0.0.20", False)
    assert rows["app-02"].host == "10.0.0.30"
    assert await _rows(ApprovalRequest) == []
    assert len(await _rows(InstanceSettingsRevision)) == 1
