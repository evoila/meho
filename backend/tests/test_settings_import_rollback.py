# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A failed settings import keeps nothing, also not the writes of its savepoints.

The import writes each tenant row, policy block, convention and target in a
savepoint, then inserts the revision. When the revision insert fails, the
request's transaction rolls back. These tests run on the normal engine and
check that no write of a savepoint is kept.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import InstanceSettingsRevision, Target, Tenant, TenantConvention
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
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_SLUG = "rollback-check"
_TARGET = "rollback-target"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture(params=[False, True], ids=["revision-stored", "revision-refused"])
def refused(request: pytest.FixtureRequest) -> Iterator[bool]:
    """With ``True``, the insert of the settings revision fails after the savepoints ran.

    The ``False`` case is the control: the same file applies and its writes are stored.
    """

    def _refuse(*_args: Any) -> None:
        raise RuntimeError("revision insert refused")

    if not request.param:
        yield False
        return
    event.listen(InstanceSettingsRevision, "before_insert", _refuse)
    try:
        yield True
    finally:
        event.remove(InstanceSettingsRevision, "before_insert", _refuse)


async def _seed_tenant() -> None:
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


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


def _file(tenant_id: uuid.UUID = _TENANT, **tenant: Any) -> str:
    doc = {
        "apiVersion": "meho.evoila.com/v1",
        "kind": "Settings",
        "spec": {"tenants": [{"id": str(tenant_id), **tenant}]},
    }
    return yaml.safe_dump(doc, sort_keys=False)


def _apply(client: TestClient, content: str, *, platform_admin: bool = False) -> Any:
    key = make_rsa_keypair("kid-settings-rollback")
    token = mint_token(
        key,
        sub="admin-person",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
        platform_admin=platform_admin,
    )
    with respx.mock as mock, settings_import_at_once(app):
        mock_discovery_and_jwks(mock, public_jwks(key))
        return client.post(
            "/api/v1/settings/import",
            content=content.encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
        )


def _assert_outcome(resp: Any, refused: bool) -> None:
    assert resp.status_code == (500 if refused else 200), resp.text
    if not refused:
        assert resp.json()["failed"] == []


@pytest.mark.asyncio
async def test_new_tenant_row_is_kept_only_on_commit(client: TestClient, refused: bool) -> None:
    new_tenant = uuid.uuid4()
    content = _file(new_tenant, policies={"flight_recorder": {"retention_days": 30}})
    resp = _apply(client, content, platform_admin=True)

    _assert_outcome(resp, refused)
    assert (new_tenant in {t.id for t in await _rows(Tenant)}) is not refused
    assert len(await _rows(InstanceSettingsRevision)) == (0 if refused else 1)


@pytest.mark.asyncio
async def test_policies_are_kept_only_on_commit(client: TestClient, refused: bool) -> None:
    await _seed_tenant()
    resp = _apply(client, _file(policies={"flight_recorder": {"retention_days": 30}}))

    _assert_outcome(resp, refused)
    (tenant,) = [t for t in await _rows(Tenant) if t.id == _TENANT]
    assert tenant.flight_recorder_retention_days == (14 if refused else 30)
    assert len(await _rows(InstanceSettingsRevision)) == (0 if refused else 1)


@pytest.mark.asyncio
async def test_convention_is_kept_only_on_commit(client: TestClient, refused: bool) -> None:
    await _seed_tenant()
    content = _file(
        conventions=[
            {"slug": _SLUG, "title": "Night", "kind": "reference", "body": "Writes at night."}
        ]
    )
    resp = _apply(client, content)

    _assert_outcome(resp, refused)
    assert (_SLUG in {c.slug for c in await _rows(TenantConvention)}) is not refused
    assert len(await _rows(InstanceSettingsRevision)) == (0 if refused else 1)


@pytest.mark.asyncio
async def test_target_is_kept_only_on_commit(client: TestClient, refused: bool) -> None:
    await _seed_tenant()
    content = _file(targets=[{"name": _TARGET, "product": "ssh", "host": "app.example.test"}])
    resp = _apply(client, content)

    _assert_outcome(resp, refused)
    assert (_TARGET in {t.name for t in await _rows(Target)}) is not refused
    assert len(await _rows(InstanceSettingsRevision)) == (0 if refused else 1)
