# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A settings import that creates or changes a target waits for a person.

The import runs as a person with a token (``POST /api/v1/settings/import``,
the call ``meho settings import`` makes):

* **One new target** -- one ``targets.register`` request waits, the card
  shows the target's values, no target row is written. After approval the
  target exists.
* **One changed target** -- one ``targets.update`` request waits, the card
  shows the old and the new value of each changed field, the row is
  unchanged. After approval the row has the new values.
* **No target change** -- the import runs as before: it writes at once and
  makes no request.
* **A target in a tenant the same file creates** -- it is not sent; the
  answer says to import the file again.
* **The same file imported twice** -- the second import makes no new
  request while the first one waits; it lists the waiting request.
* **A target call that runs at once** -- it is listed in ``targets_written``.
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

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Target, Tenant
from meho_backplane.main import app
from meho_backplane.operations import dispatcher

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._settings_target_approvals import approve_waiting, targets_ops_registered
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

pytestmark = pytest.mark.usefixtures(targets_ops_registered.__name__)

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


async def _tenant() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))


def _file(tenants: list[dict[str, Any]]) -> bytes:
    document = {
        "apiVersion": "meho.evoila.com/v1",
        "kind": "Settings",
        "spec": {"tenants": tenants},
    }
    return yaml.safe_dump(document).encode()


def _import(content: bytes, client: TestClient, *, platform_admin: bool = False) -> Any:
    key = make_rsa_keypair("kid-settings-targets")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = mint_token(
            key,
            sub="importing-person",
            tenant_role=TenantRole.TENANT_ADMIN.value,
            tenant_id=str(_TENANT),
            platform_admin=platform_admin or None,
        )
        response = client.post(
            "/api/v1/settings/import",
            content=content,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
        )
    assert response.status_code == 200, response.text
    return response.json()


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _target(name: str) -> Target | None:
    async with get_sessionmaker()() as session:
        return await session.scalar(select(Target).where(Target.name == name))


async def test_a_new_target_waits_and_is_written_after_approval(client: TestClient) -> None:
    await _tenant()
    new = {
        "name": "vault-02",
        "product": "vault",
        "host": "vault-02.example",
        "extras": {"file_hosts": ["files.example"]},
    }

    body = _import(_file([{"id": str(_TENANT), "targets": [new]}]), client)

    (waiting,) = body["waiting_for_approval"]
    assert waiting["name"] == "vault-02"
    assert waiting["op_id"] == "targets.register"
    assert waiting["path"] == "spec.tenants[0].targets[0]"
    assert body["applied"] is False and body["revision_id"] is None
    assert await _target("vault-02") is None
    (request,) = await _requests()
    assert str(request.id) == waiting["approval_request_id"]
    assert request.principal_sub == "importing-person"
    assert request.proposed_effect is not None
    card = request.proposed_effect["preview"]["target"]
    assert {key: card[key] for key in new} == new

    await approve_waiting(body, _TENANT)

    row = await _target("vault-02")
    assert row is not None
    assert (row.host, row.extras) == ("vault-02.example", {"file_hosts": ["files.example"]})


async def test_a_changed_target_waits_and_the_card_shows_old_and_new(
    client: TestClient,
) -> None:
    await _tenant()
    await _insert_target(
        tenant_id=_TENANT,
        name="avi-01",
        product="ssh",
        host="avi-01.example",
        extras={"file_hosts": ["old.example"]},
    )
    changed = {
        "name": "avi-01",
        "product": "ssh",
        "host": "avi-01.example",
        "extras": {"file_hosts": ["new.example"]},
    }

    body = _import(_file([{"id": str(_TENANT), "targets": [changed]}]), client)

    (waiting,) = body["waiting_for_approval"]
    assert waiting["op_id"] == "targets.update"
    row = await _target("avi-01")
    assert row is not None and row.extras == {"file_hosts": ["old.example"]}
    (request,) = await _requests()
    assert request.params["target_id"] == str(row.id)
    assert request.proposed_effect is not None
    changes = request.proposed_effect["preview"]["changes"]
    assert changes["extras"] == {
        "from": {"file_hosts": ["old.example"]},
        "to": {"file_hosts": ["new.example"]},
    }

    await approve_waiting(body, _TENANT)

    row = await _target("avi-01")
    assert row is not None and row.extras == {"file_hosts": ["new.example"]}


async def test_an_import_without_a_target_change_runs_at_once(client: TestClient) -> None:
    await _tenant()
    await _insert_target(tenant_id=_TENANT, name="same-01", product="ssh", host="same.example")
    same = {"name": "same-01", "product": "ssh", "host": "same.example"}

    body = _import(
        _file(
            [{"id": str(_TENANT), "policies": {"announce_gate_enabled": True}, "targets": [same]}]
        ),
        client,
    )

    assert body["waiting_for_approval"] == []
    assert body["applied"] is True and body["revision_id"]
    assert await _requests() == []
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, _TENANT)
    assert tenant is not None and tenant.announce_gate_enabled is True


async def test_a_target_of_a_new_tenant_is_not_sent(client: TestClient) -> None:
    await _tenant()
    other = uuid.uuid4()
    target = {"name": "new-01", "product": "ssh", "host": "new.example"}

    body = _import(
        _file([{"id": str(_TENANT)}, {"id": str(other), "targets": [target]}]),
        client,
        platform_admin=True,
    )

    assert body["waiting_for_approval"] == []
    (failed,) = body["failed"]
    assert failed.startswith("`spec.tenants[1].targets[0]` (new-01) was not sent for approval")
    assert "Import the file again" in failed
    assert await _requests() == []
    assert await _target("new-01") is None


async def test_a_second_import_lists_the_waiting_request_and_makes_no_new_one(
    client: TestClient,
) -> None:
    await _tenant()
    await _insert_target(tenant_id=_TENANT, name="old-01", product="ssh", host="old.example")
    content = _file(
        [
            {
                "id": str(_TENANT),
                "targets": [
                    {"name": "new-01", "product": "ssh", "host": "new.example"},
                    {"name": "old-01", "product": "ssh", "host": "old-moved.example"},
                ],
            }
        ]
    )

    first = _import(content, client)
    clear_jwks_cache()  # the second import signs with a new key under the same kid
    second = _import(content, client)

    assert len(await _requests()) == 2
    assert [w.get("already_waiting") for w in first["waiting_for_approval"]] == [False, False]
    assert [w["already_waiting"] for w in second["waiting_for_approval"]] == [True, True]
    assert [w["approval_request_id"] for w in second["waiting_for_approval"]] == [
        w["approval_request_id"] for w in first["waiting_for_approval"]
    ]


async def test_a_target_call_that_runs_at_once_is_listed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _tenant()

    async def _runs_at_once(**_: Any) -> OperationResult:
        return OperationResult(status="ok", op_id="targets.register", duration_ms=1.0)

    monkeypatch.setattr(dispatcher, "dispatch", _runs_at_once)
    target = {"name": "now-01", "product": "ssh", "host": "now.example"}

    body = _import(_file([{"id": str(_TENANT), "targets": [target]}]), client)

    assert body["waiting_for_approval"] == []
    assert body["targets_written"] == [
        {"path": "spec.tenants[0].targets[0]", "name": "now-01", "op_id": "targets.register"}
    ]
