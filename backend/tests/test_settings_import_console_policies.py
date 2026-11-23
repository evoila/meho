# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A settings import widens approvals or turns a gate off only in the web console.

* A call with a token that applies a file which turns on
  ``run_approval_target_kinds`` or turns off ``announce_gate_enabled`` gets
  403 with a reason that names each such policy. MEHO writes nothing from
  the file, also not its harmless parts.
* The preview with a token works, shows the change in the diff and names it
  in ``warnings``. It writes nothing.
* The opposite direction (target kinds off, gate on) applies with a token.
* A person in a web session applies every change at once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
import yaml
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1 import instance_settings as settings_api
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import InstanceSettingsRevision, Tenant, TenantConvention
from meho_backplane.instance_settings.console_only import console_only_changes
from meho_backplane.main import app
from meho_backplane.middleware import verify_jwt_and_bind

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

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_URL = "/api/v1/settings/import"
_KINDS = "spec.tenants[0].policies.run_approval_target_kinds"
_GATE = "spec.tenants[0].policies.announce_gate_enabled"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


def _web_person(operator: Operator = Depends(verify_jwt_and_bind)) -> Operator:
    person = settings_api._require_human_settings_admin(operator)
    return person.model_copy(update={"web_session": True})


@pytest.fixture
def web_console() -> Iterator[None]:
    """Mark the caller of the settings import as a person in the web console."""
    gate = settings_api._require_human_settings_admin
    app.dependency_overrides[gate] = _web_person
    try:
        yield
    finally:
        app.dependency_overrides.pop(gate, None)


async def _seed(*, kinds: bool = False, gate: bool = True) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=_TENANT,
                slug="tenant-a",
                name="Tenant A",
                announce_gate_enabled=gate,
                run_approval_target_kinds_enabled=kinds,
                flight_recorder_enabled=True,
                flight_recorder_retention_days=14,
            )
        )
        await session.commit()


async def _tenant() -> Tenant:
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, _TENANT)
    assert tenant is not None
    return tenant


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


def _file(policies: dict[str, Any] | None, **extra: Any) -> str:
    tenant: dict[str, Any] = {"id": str(_TENANT), "policies": policies, **extra}
    doc = {"apiVersion": "meho.evoila.com/v1", "kind": "Settings", "spec": {"tenants": [tenant]}}
    return yaml.safe_dump(doc, sort_keys=False)


def _post(client: TestClient, content: str, *, preview: bool = False) -> Any:
    key = make_rsa_keypair("kid-settings-console-policies")
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


#: (file policies, the refused path) for each loosening change.
_LOOSENING = [
    pytest.param({"run_approval_target_kinds": True}, _KINDS, id="target-kinds-on"),
    pytest.param({"announce_gate_enabled": False}, _GATE, id="gate-off"),
    pytest.param(None, _GATE, id="policies-null-turns-gate-off"),
]


async def _assert_nothing_written() -> None:
    tenant = await _tenant()
    assert tenant.run_approval_target_kinds_enabled is False
    assert tenant.announce_gate_enabled is True
    assert tenant.flight_recorder_retention_days == 14
    assert "change-window" not in {c.slug for c in await _rows(TenantConvention)}
    assert await _rows(InstanceSettingsRevision) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("policies", "path"), _LOOSENING)
async def test_token_import_that_loosens_a_policy_is_refused(
    client: TestClient, policies: dict[str, Any] | None, path: str
) -> None:
    await _seed()
    resp = _post(client, _file(policies))

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert f"`{path}`" in detail
    assert "applies only for a person in the web console" in detail
    assert "Apply it on the settings file page (/ui/settings)." in detail
    await _assert_nothing_written()


@pytest.mark.asyncio
async def test_token_import_names_both_policies_and_writes_no_harmless_part(
    client: TestClient,
) -> None:
    """A file that mixes harmless changes with a loosening one is refused as a whole."""
    await _seed()
    content = _file(
        {
            "run_approval_target_kinds": True,
            "announce_gate_enabled": False,
            "flight_recorder": {"retention_days": 30},
        },
        conventions=[
            {
                "slug": "change-window",
                "title": "Change window",
                "body": "Nights only.",
                "kind": "operational",
            }
        ],
    )
    resp = _post(client, content)

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert f"`{_KINDS}`" in detail and f"`{_GATE}`" in detail
    assert "MEHO wrote nothing from this file." in detail
    await _assert_nothing_written()


@pytest.mark.asyncio
@pytest.mark.parametrize(("policies", "path"), _LOOSENING)
async def test_token_preview_names_the_change_and_writes_nothing(
    client: TestClient, policies: dict[str, Any] | None, path: str
) -> None:
    await _seed()
    resp = _post(client, _file(policies), preview=True)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["mistakes"] == []
    changed = {c["key"] for c in body["diff"]["policies"]["changed"]}
    assert path.rsplit(".", 1)[1] in changed
    (warning,) = [w for w in body["warnings"] if f"`{path}`" in w]
    assert "only for a person in the web console" in warning
    assert "apply the file on the settings file page (/ui/settings)" in warning
    await _assert_nothing_written()


@pytest.mark.asyncio
@pytest.mark.usefixtures("web_console")
async def test_web_console_preview_has_no_console_warning(client: TestClient) -> None:
    await _seed()
    resp = _post(client, _file({"run_approval_target_kinds": True}), preview=True)

    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("web_console")
@pytest.mark.parametrize(("policies", "path"), _LOOSENING)
async def test_web_console_applies_the_change_at_once(
    client: TestClient, policies: dict[str, Any] | None, path: str
) -> None:
    await _seed()
    resp = _post(client, _file(policies))

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    tenant = await _tenant()
    if path == _KINDS:
        assert tenant.run_approval_target_kinds_enabled is True
    else:
        assert tenant.announce_gate_enabled is False
    assert len(await _rows(InstanceSettingsRevision)) == 1


@pytest.mark.asyncio
async def test_token_import_that_narrows_applies(client: TestClient) -> None:
    """Target kinds off and the gate on narrow what runs, so a token applies them."""
    await _seed(kinds=True, gate=False)
    content = _file({"run_approval_target_kinds": False, "announce_gate_enabled": True})
    resp = _post(client, content)

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    tenant = await _tenant()
    assert tenant.run_approval_target_kinds_enabled is False
    assert tenant.announce_gate_enabled is True


@pytest.mark.asyncio
async def test_token_import_with_the_stored_value_is_no_change(client: TestClient) -> None:
    """A file that repeats the stored loose values changes nothing, so it is not refused."""
    await _seed(kinds=True, gate=False)
    content = _file(
        {
            "run_approval_target_kinds": True,
            "announce_gate_enabled": False,
            "flight_recorder": {"retention_days": 30},
        }
    )
    resp = _post(client, content)

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    tenant = await _tenant()
    assert tenant.flight_recorder_retention_days == 30
    assert tenant.run_approval_target_kinds_enabled is True


@pytest.mark.asyncio
async def test_mistakes_answer_422_before_the_console_rule(client: TestClient) -> None:
    await _seed()
    content = _file({"run_approval_target_kinds": True, "no_such_policy": 1})
    resp = _post(client, content)

    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"]
    await _assert_nothing_written()


def test_console_only_changes_names_the_tenant_index() -> None:
    tight = {"run_approval_target_kinds": False, "announce_gate_enabled": True}
    loose = {"run_approval_target_kinds": True, "announce_gate_enabled": False}
    assert console_only_changes(2, tight, loose) == [
        "spec.tenants[2].policies.run_approval_target_kinds",
        "spec.tenants[2].policies.announce_gate_enabled",
    ]
    assert console_only_changes(0, loose, tight) == []
    assert console_only_changes(0, loose, loose) == []
