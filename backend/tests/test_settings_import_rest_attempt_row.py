# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A REST settings import that stops leaves a failure row with the attempt keys only.

The gate is turned on with a token (it needs no reason and no web console).
The revision write fails, so MEHO rolls back: the gate stays as it was, no
revision exists, and the one audit row names the operation, the file hash and
the tenants, and no change. This holds when the revision write fails and when
the final commit fails. Both audit modes.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, InstanceSettingsRevision, Tenant
from meho_backplane.instance_settings.attempt_audit import ATTEMPT_AUDIT_KEYS
from meho_backplane.main import app

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault
from .test_ui_settings_file import (
    cutover_on,  # noqa: F401  (fixture)
)

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_URL = "/api/v1/settings/import"
_MIDDLEWARE_KEYS = {"broadcast_detail_origin", "broadcast_detail_effective", "client"}
_ATTEMPT_PAYLOAD_KEYS = {key.removeprefix("audit_") for key in ATTEMPT_AUDIT_KEYS}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _seed() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT,
                slug="tenant-a",
                name="Tenant A",
                announce_gate_enabled=False,
                run_approval_target_kinds_enabled=False,
                flight_recorder_enabled=True,
                flight_recorder_retention_days=14,
            )
        )
        await session.commit()


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


#: The gate turned on narrows what runs, so a token applies it.
_FILE = yaml.safe_dump(
    {
        "apiVersion": "meho.evoila.com/v1",
        "kind": "Settings",
        "spec": {"tenants": [{"id": str(_TENANT), "policies": {"announce_gate_enabled": True}}]},
    },
    sort_keys=False,
)


def _post_file(client: TestClient) -> Any:
    key = make_rsa_keypair("kid-settings-attempt")
    token = mint_token(
        key, sub="admin-person", tenant_role=TenantRole.TENANT_ADMIN.value, tenant_id=str(_TENANT)
    )
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        return client.post(
            _URL,
            content=_FILE.encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
        )


async def _assert_one_attempt_row(status_code: int) -> None:
    """Nothing was saved, and the one row names the attempt and no change."""
    (tenant,) = [t for t in await _rows(Tenant) if t.id == _TENANT]
    assert tenant.announce_gate_enabled is False
    assert await _rows(InstanceSettingsRevision) == []
    rows = [r for r in await _rows(AuditLog) if r.payload.get("op_id") == "settings.import"]
    (row,) = rows
    assert row.status_code == status_code
    assert set(row.payload) - _MIDDLEWARE_KEYS == _ATTEMPT_PAYLOAD_KEYS
    assert row.payload["op_class"] == "write"
    assert row.payload["settings_import"] is True
    assert row.payload["settings_file_sha256"] == hashlib.sha256(_FILE.encode()).hexdigest()
    assert row.payload["settings_tenant_ids"] == [str(_TENANT)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cutover", [False, True], ids=["cutover-off", "cutover-on"])
async def test_a_failed_rest_import_leaves_one_row_with_the_attempt_keys_only(
    cutover: bool, request: pytest.FixtureRequest, client: TestClient
) -> None:
    if cutover:
        request.getfixturevalue("cutover_on")
    await _seed()

    def _broken(*_args: Any) -> None:
        # An HTTP error, so the failure is answered and not logged as a crash.
        raise HTTPException(status_code=503, detail="the revision cannot be written")

    event.listen(InstanceSettingsRevision, "before_insert", _broken)
    try:
        resp = _post_file(client)
    finally:
        event.remove(InstanceSettingsRevision, "before_insert", _broken)

    assert resp.status_code == 503, resp.text
    await _assert_one_attempt_row(503)


@pytest.mark.asyncio
@pytest.mark.parametrize("cutover", [False, True], ids=["cutover-off", "cutover-on"])
async def test_a_rest_import_whose_commit_fails_leaves_one_row_with_the_attempt_keys_only(
    cutover: bool, request: pytest.FixtureRequest, client: TestClient
) -> None:
    if cutover:
        request.getfixturevalue("cutover_on")
    await _seed()
    revision_written: list[bool] = []

    def _mark(*_args: Any) -> None:
        revision_written.append(True)

    def _broken_commit(_session: Session) -> None:
        # Only the commit that would save the revision fails.
        if revision_written:
            revision_written.clear()
            raise HTTPException(status_code=503, detail="the commit failed")

    event.listen(InstanceSettingsRevision, "before_insert", _mark)
    event.listen(Session, "before_commit", _broken_commit)
    try:
        resp = _post_file(client)
    finally:
        event.remove(Session, "before_commit", _broken_commit)
        event.remove(InstanceSettingsRevision, "before_insert", _mark)

    assert resp.status_code == 503, resp.text
    await _assert_one_attempt_row(503)
