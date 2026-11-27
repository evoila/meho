# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``harbor.robot.create`` with ``vault_target`` while an admin blocks the caller.

The run goes through ``dispatch``. The caller is blocked right after the
Vault login of the secret write. The write is not sent (``vault.kv.patch``,
and ``vault.kv.put`` for a new item), and the answer is the block refusal.

Harbor is faked with respx and Vault with the in-process fake. The secret
is a made-up value.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import hvac.exceptions
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth import vault as auth_vault
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.connectors.harbor import HarborConnector, ops_robot_vault
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import HarborTargetLike
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

from ._principal_block_send_helpers import add_tenant, block_after_login, block_the_caller
from ._vault_fakes import install_fake_client

pytestmark = pytest.mark.asyncio

_TENANT_ID = UUID("00000000-0000-0000-0000-0000000b1c02")
_TARGET_NAME = "harbor-block"
_HOST = "harbor.test.invalid"
_ROBOTS_URL = f"https://{_HOST}/api/v2.0/robots"
_ROBOT_URL = f"{_ROBOTS_URL}/7"
_PARAMS: dict[str, Any] = {
    "name": "bot",
    "project": "proj",
    "duration": -1,
    "vault_target": f"vault:tenants/{_TENANT_ID}/ci/harbor#secret",
}


async def _stub_credentials_loader(
    _target: HarborTargetLike, _operator: Operator
) -> dict[str, str]:
    return {"username": "admin", "password": "test-password"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    reset_block_cache()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def harbor() -> AsyncIterator[HarborConnector]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.25] * 384
    embedding.encode.return_value = [[0.25] * 384]
    embedding.dimension = 384
    reset_dispatcher_caches()
    await register_harbor_robot_operations(embedding_service=embedding)
    await add_tenant(_TENANT_ID)
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="harbor",
                version="2.11.0",
                host=_HOST,
                port=443,
                secret_ref=f"harbor/{_TARGET_NAME}",
                auth_model="shared_service_account",
            )
        )
        await session.commit()
    connector = HarborConnector(credentials_loader=_stub_credentials_loader)
    _CONNECTOR_INSTANCE_CACHE[HarborConnector] = connector
    yield connector
    await connector.aclose()
    reset_dispatcher_caches()


def _caller() -> Operator:
    return Operator(
        sub="blocked-robot-maker",
        name=None,
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _create_robot() -> OperationResult:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_ROBOTS_URL).respond(
            201, json={"id": 7, "name": "robot$proj+bot", "secret": "made-up-secret-zz9"}
        )
        mock.get(_ROBOT_URL).respond(404)
        mock.delete(_ROBOT_URL).respond(200)
        return await dispatch(
            operator=_caller(),
            connector_id="harbor-rest-2.x",
            op_id="harbor.robot.create",
            target=target,
            params=dict(_PARAMS),
            _approved=True,
        )


def _is_block_refusal(result: OperationResult) -> bool:
    return result.status == "denied" and (result.error or "").startswith("principal_blocked")


async def test_robot_secret_patch_is_not_sent_once_the_caller_is_blocked(
    harbor: HarborConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch)
    opens = block_after_login(monkeypatch, auth_vault, "vault_client_for_operator", _caller())

    result = await _create_robot()

    assert opens == [1]
    assert fake.secrets.kv.v2.patch_calls == []
    assert fake.secrets.kv.v2.put_calls == []
    assert _is_block_refusal(result), (result.status, result.error)


async def test_robot_secret_put_of_a_new_item_is_not_sent_once_the_caller_is_blocked(
    harbor: HarborConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch, patch_exc=hvac.exceptions.InvalidPath("none"))
    opens = block_after_login(
        monkeypatch, auth_vault, "vault_client_for_operator", _caller(), on_open=2
    )

    result = await _create_robot()

    assert opens == [1, 2]
    assert len(fake.secrets.kv.v2.patch_calls) == 1
    assert fake.secrets.kv.v2.put_calls == []
    assert _is_block_refusal(result), (result.status, result.error)


# ---------------------------------------------------------------------------
# A block after the create and before the store: the new robot is deleted
# ---------------------------------------------------------------------------

_ROBOT = {
    "id": 7,
    "name": "robot$proj+bot",
    "level": "project",
    "permissions": [{"kind": "project", "namespace": "proj", "access": []}],
}


class _Robots:
    """A stand-in Harbor that keeps its robots."""

    def __init__(self, *, delete_fails: bool = False) -> None:
        self.robots: dict[int, dict[str, Any]] = {}
        self.delete_fails = delete_fails
        self.calls = 0

    def mount(self, mock: respx.MockRouter) -> None:
        mock.post(_ROBOTS_URL).mock(side_effect=self._create)
        mock.get(_ROBOT_URL).mock(side_effect=self._read)
        mock.delete(_ROBOT_URL).mock(side_effect=self._delete)

    def _create(self, _request: httpx.Request) -> httpx.Response:
        self.robots[7] = dict(_ROBOT)
        return httpx.Response(201, json={**_ROBOT, "secret": "made-up-secret-zz9"})

    def _read(self, _request: httpx.Request) -> httpx.Response:
        robot = self.robots.get(7)
        return httpx.Response(200, json=robot) if robot else httpx.Response(404)

    def _delete(self, _request: httpx.Request) -> httpx.Response:
        if self.delete_fails:
            return httpx.Response(500, json={"errors": [{"message": "stand-in failure"}]})
        self.robots.pop(7, None)
        return httpx.Response(200)


def _block_before_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ops_robot_vault._write

    async def _block_then_write(operator: Operator, *args: Any) -> Any:
        await block_the_caller(operator)
        return await real(operator, *args)

    monkeypatch.setattr(ops_robot_vault, "_write", _block_then_write)


async def _run(op_id: str, params: dict[str, Any], harbor_standin: _Robots) -> OperationResult:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    with respx.mock(assert_all_called=False) as mock:
        harbor_standin.mount(mock)
        result = await dispatch(
            operator=_caller(),
            connector_id="harbor-rest-2.x",
            op_id=op_id,
            target=target,
            params=dict(params),
            _approved=True,
        )
        harbor_standin.calls = len(mock.calls)
    return result


async def _denied_row(op_id: str) -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars()
        (row,) = [r for r in rows if r.payload.get("result_status") == "denied"]
    return row


async def test_new_robot_is_deleted_when_the_caller_is_blocked_before_the_store(
    harbor: HarborConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch)
    _block_before_the_store(monkeypatch)
    standin = _Robots()

    result = await _run("harbor.robot.create", _PARAMS, standin)

    assert _is_block_refusal(result), (result.status, result.error)
    assert "not_removed" not in result.extras
    assert standin.robots == {}
    assert fake.auth.jwt.login_calls == []
    assert fake.secrets.kv.v2.patch_calls == []
    assert fake.secrets.kv.v2.put_calls == []

    # The blocked caller's next calls stop before any request reaches Harbor.
    again = await _run("harbor.robot.create", _PARAMS, standin)
    assert _is_block_refusal(again), (again.status, again.error)
    assert standin.calls == 0
    delete = await _run("harbor.robot.delete", {"project": "proj", "id": 7}, standin)
    assert _is_block_refusal(delete), (delete.status, delete.error)
    assert standin.calls == 0


async def test_a_failed_delete_names_the_robot_that_stays_in_the_answer_and_the_audit_row(
    harbor: HarborConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fake_client(monkeypatch)
    _block_before_the_store(monkeypatch)
    standin = _Robots(delete_fails=True)

    result = await _run("harbor.robot.create", _PARAMS, standin)

    stays = "the new Harbor robot with id 7 was not deleted"
    assert _is_block_refusal(result), (result.status, result.error)
    assert stays in (result.error or "")
    assert result.extras["not_removed"] == [stays]
    assert 7 in standin.robots
    assert fake.secrets.kv.v2.patch_calls == []
    row = await _denied_row("harbor.robot.create")
    assert row.payload["error"]["not_removed"] == [stays]
    assert "made-up-secret-zz9" not in str(row.payload)
