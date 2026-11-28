# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A run that changed a target and cannot undo it finishes its store, through ``dispatch``.

* ``rke2.token.rotate``: the caller is blocked after the node rotated its
  token and before MEHO stores the new token. The store runs to its end (its
  Vault login and its write), and the answer and the audit row are the
  operation's own. A caller blocked before the rotate gets the block
  refusal: no rotate command and no store.
* ``vault.auth.userpass.write`` with ``generate_password`` and ``rotate``:
  the caller is blocked while the new password is written to KV. Setting
  only that password on the login runs to its end, and the answer says
  ``written``. A change of the policies keeps the check: it is not sent, the
  login keeps its policies, and the answer is the block refusal. A login
  that does not exist is not made. A caller blocked before the store gets
  the block refusal: no store and no set. A login removed during the store
  is not made again, and the answer and the audit row say that the new
  password is stored and not set, and where it is stored.

The node is a scripted SSH connection under the real ``_run_command`` and
``run_capped``. Vault is the stand-in HTTP server with the real ``hvac``
client. All names and values are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth import send_check
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.auth.principal_block_run import in_cleanup
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.rke2 import Rke2SshConnector
from meho_backplane.connectors.rke2 import ops_write as rke2_write
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import VaultConnector, register_vault_typed_operations
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings

from ._principal_block_send_helpers import add_tenant, block_the_caller
from ._ssh_fakes import FakeSshProcess
from ._vault_http_standin import VaultStandIn, vault_standin

pytestmark = pytest.mark.asyncio

_TENANT_ID = UUID("00000000-0000-0000-0000-0000000b1c05")
_NEW_TOKEN = "made-up-rotated-token-value"
_TOKEN_ITEM = f"secret/tenants/{_TENANT_ID}/rke2/node-a/server-token"
_ROTATE = "rke2.token.rotate"
_USERPASS = "vault.auth.userpass.write"
_PASSWORD_ITEM = f"tenants/{_TENANT_ID}/logins/svc-a"
_PASSWORD_DATA = f"/v1/secret/data/{_PASSWORD_ITEM}"
_USER_PATH = "/v1/auth/userpass/users/svc-a"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    reset_block_cache()
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


@pytest.fixture
async def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[VaultStandIn]:
    await add_tenant(_TENANT_ID)
    with vault_standin(monkeypatch) as standin:
        yield standin


def _caller() -> Operator:
    return Operator(
        sub="blocked-rotator",
        name=None,
        email=None,
        raw_jwt="made.up.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _is_block_refusal(result: OperationResult) -> bool:
    return result.status == "denied" and (result.error or "").startswith("principal_blocked")


async def _dispatch_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars()
        return [r for r in rows if r.method == "DISPATCH"]


# ---------------------------------------------------------------------------
# rke2.token.rotate
# ---------------------------------------------------------------------------


class _Node:
    """A scripted RKE2 server node that records each command."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.rotated = False
        self.closed = False

    def _answer(self, cmd: str) -> str:
        if cmd.startswith("printf 'ACTIVE="):
            return (
                "ACTIVE=active\nUNIT=rke2-server.service\n"
                "VERSION=rke2 version v1.29.3+rke2r2 (a1b2c3d)\n"
            )
        if cmd.startswith("set -e; umask 077; f=$(mktemp)"):
            self.rotated = True
            return f"{rke2_write._ROTATED_TOKEN_MARKER}={_NEW_TOKEN}\n"
        return ""

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def create_process(self, cmd: str, **_kwargs: object) -> FakeSshProcess:
        self.commands.append(cmd)
        return FakeSshProcess(command=cmd, stdout=self._answer(cmd), exit_status=0)


class _ScriptedRke2(Rke2SshConnector):
    def __init__(self, node: _Node) -> None:
        super().__init__()
        self.node = node

    async def _connect(self, target: Any, operator: Operator | None = None) -> Any:
        return self.node

    async def _resolve_secret(self, target: Any, operator: Any = None) -> dict[str, Any]:
        return {"username": "root", "password": "made-up-sudo"}


class _Target:
    def __init__(self) -> None:
        self.product = "rke2"
        self.fingerprint = type("_Fp", (), {"version": "1.x"})()
        self.preferred_impl_id: str | None = "rke2-ssh"
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = "node-a"
        self.host = "node-a.test.invalid"
        self.port = 22
        self.auth_model = "shared_service_account"


async def _node() -> _Node:
    if ("rke2", "1.x", "rke2-ssh") not in all_connectors_v2():
        register_connector_v2(
            product="rke2", version="1.x", impl_id="rke2-ssh", cls=Rke2SshConnector
        )
    await Rke2SshConnector.register_operations()
    node = _Node()
    _CONNECTOR_INSTANCE_CACHE[Rke2SshConnector] = _ScriptedRke2(node)  # type: ignore[assignment]
    return node


async def _rotate() -> OperationResult:
    return await dispatch(
        operator=_caller(),
        connector_id="rke2-ssh-1.x",
        op_id=_ROTATE,
        target=_Target(),
        params={},
        _approved=True,
    )


def _block_after(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = getattr(rke2_write, name)

    async def _run_then_block(*args: Any, **kwargs: Any) -> Any:
        answer = await real(*args, **kwargs)
        await block_the_caller(_caller())
        return answer

    monkeypatch.setattr(rke2_write, name, _run_then_block)


async def test_token_store_finishes_when_the_caller_is_blocked_after_the_rotate(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = await _node()
    _block_after(monkeypatch, "run_remote_bash_with_sudo")

    result = await _rotate()

    assert result.status == "ok", (result.status, result.error)
    assert node.rotated
    assert vault.sent("POST", "/v1/auth/jwt/login") == 1
    assert vault.items[_TOKEN_ITEM][1] == {"token": _NEW_TOKEN}
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["rotated"] is True
    assert payload["token_ref"]["path"] == f"tenants/{_TENANT_ID}/rke2/node-a/server-token"
    (row,) = await _dispatch_rows(_ROTATE)
    assert row.payload["result_status"] == "ok"
    assert _NEW_TOKEN not in json.dumps(row.payload) + json.dumps(row.raw_payload)
    assert _NEW_TOKEN not in json.dumps(result.model_dump(mode="json"))


async def test_nothing_is_rotated_or_stored_when_the_caller_is_blocked_before_the_rotate(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = await _node()
    _block_after(monkeypatch, "_fingerprint_gate")

    result = await _rotate()

    assert _is_block_refusal(result), (result.status, result.error)
    assert not node.rotated
    assert not any(c.startswith("set -e; umask 077") for c in node.commands)
    assert vault.requests == []


# ---------------------------------------------------------------------------
# vault.auth.userpass.write with generate_password
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_ops(vault: VaultStandIn) -> AsyncIterator[VaultStandIn]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_vault_typed_operations(embedding_service=embedding)
    yield vault
    reset_dispatcher_caches()
    clear_registry()


_CASES = [
    pytest.param(True, id="rotate"),
    pytest.param(False, id="first-password"),
]


def _prepare(vault: VaultStandIn, rotate: bool) -> dict[str, Any]:
    if rotate:
        # The login exists, and KV holds its old password.
        vault.users["svc-a"] = {"password": "made-up-old", "token_policies": ["app"]}
        vault.items[f"secret/{_PASSWORD_ITEM}"] = (1, {"password": "made-up-old"})
    params: dict[str, Any] = {"username": "svc-a", "generate_password": {"path": _PASSWORD_ITEM}}
    if rotate:
        params["rotate"] = True
    return params


async def _write_login(params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_caller(),
        connector_id="vault-1.x",
        op_id=_USERPASS,
        target=None,
        params=params,
        _approved=True,
    )


async def _block() -> None:
    await block_the_caller(_caller())


async def test_password_set_finishes_when_the_caller_is_blocked_during_the_store(
    vault_ops: VaultStandIn,
) -> None:
    params = _prepare(vault_ops, rotate=True)
    vault_ops.on_request("POST", _PASSWORD_DATA, _block)

    result = await _write_login(params)

    assert result.status == "ok", (result.status, result.error)
    payload = result.result
    assert isinstance(payload, dict)
    assert payload["written"] is True
    assert payload["credential_generated"] is True
    stored = vault_ops.items[f"secret/{_PASSWORD_ITEM}"][1]["password"]
    assert stored != "made-up-old"
    assert vault_ops.user_writes == [(_USER_PATH, {"password": stored})]
    assert vault_ops.users["svc-a"]["password"] == stored
    assert vault_ops.users["svc-a"]["token_policies"] == ["app"]
    (row,) = await _dispatch_rows(_USERPASS)
    assert row.payload["result_status"] == "ok"
    assert stored not in json.dumps(row.payload) + json.dumps(row.raw_payload)


async def test_a_blocked_rotation_sets_only_the_stored_password_and_keeps_the_policies(
    vault_ops: VaultStandIn,
) -> None:
    params = {**_prepare(vault_ops, rotate=True), "token_policies": ["new-rights"]}
    vault_ops.on_request("POST", _PASSWORD_DATA, _block)

    result = await _write_login(params)

    assert _is_block_refusal(result), (result.status, result.error)
    stored = vault_ops.items[f"secret/{_PASSWORD_ITEM}"][1]["password"]
    assert stored != "made-up-old"
    # Only the password set reaches Vault; the policy request is not sent.
    assert vault_ops.user_writes == [(_USER_PATH, {"password": stored})]
    assert vault_ops.users["svc-a"]["password"] == stored
    assert vault_ops.users["svc-a"]["token_policies"] == ["app"]
    (row,) = await _dispatch_rows(_USERPASS)
    assert row.payload["result_status"] == "denied"
    assert stored not in json.dumps(row.payload) + json.dumps(row.raw_payload)
    assert stored not in json.dumps(result.model_dump(mode="json"))


async def test_a_blocked_run_makes_no_new_login(vault_ops: VaultStandIn) -> None:
    params = {**_prepare(vault_ops, rotate=False), "token_policies": ["new-rights"]}
    vault_ops.on_request("POST", _PASSWORD_DATA, _block)

    result = await _write_login(params)

    assert _is_block_refusal(result), (result.status, result.error)
    assert vault_ops.sent("POST", _PASSWORD_DATA) == 1
    assert "svc-a" not in vault_ops.users
    assert vault_ops.user_writes == []


async def test_an_unblocked_rotation_sets_the_password_and_the_policies(
    vault_ops: VaultStandIn,
) -> None:
    params = {**_prepare(vault_ops, rotate=True), "token_policies": ["new-rights"]}

    result = await _write_login(params)

    assert result.status == "ok", (result.status, result.error)
    stored = vault_ops.items[f"secret/{_PASSWORD_ITEM}"][1]["password"]
    assert vault_ops.user_writes == [
        (_USER_PATH, {"password": stored}),
        (_USER_PATH, {"token_policies": ["new-rights"]}),
    ]
    assert vault_ops.users["svc-a"]["password"] == stored
    assert vault_ops.users["svc-a"]["token_policies"] == ["new-rights"]


async def test_the_password_set_with_no_block_check_uses_the_login_path_and_sends_only_the_password(
    vault_ops: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    unchecked: list[tuple[str, str, dict[str, Any]]] = []
    real_send = send_check._CheckedAdapter.send

    def _record(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        if in_cleanup():
            body = json.loads(request.body) if request.body else {}
            unchecked.append((request.method, urlsplit(request.url).path, body))
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(send_check._CheckedAdapter, "send", _record)
    params = {**_prepare(vault_ops, rotate=True), "token_policies": ["new-rights"]}

    result = await _write_login(params)

    assert result.status == "ok", (result.status, result.error)
    stored = vault_ops.items[f"secret/{_PASSWORD_ITEM}"][1]["password"]
    # The end of MEHO's own Vault login is the other request with no check.
    assert unchecked == [
        ("GET", _USER_PATH, {}),
        ("POST", _USER_PATH, {"password": stored}),
        ("POST", "/v1/auth/token/revoke-self", {}),
    ]
    assert vault_ops.sent("POST", f"{_USER_PATH}/password") == 0


@pytest.mark.parametrize("rotate", _CASES)
async def test_nothing_is_stored_or_set_when_the_caller_is_blocked_before_the_store(
    vault_ops: VaultStandIn, rotate: bool
) -> None:
    params = _prepare(vault_ops, rotate)
    vault_ops.on_request("GET", _USER_PATH, _block)

    result = await _write_login(params)

    assert _is_block_refusal(result), (result.status, result.error)
    assert vault_ops.sent("POST", _PASSWORD_DATA) == 0
    assert vault_ops.sent("POST", _USER_PATH) == 0
    if rotate:
        assert vault_ops.items[f"secret/{_PASSWORD_ITEM}"] == (1, {"password": "made-up-old"})
        assert vault_ops.users["svc-a"]["password"] == "made-up-old"


async def test_a_login_removed_during_the_store_of_a_blocked_rotation_is_not_made_again(
    vault_ops: VaultStandIn,
) -> None:
    params = _prepare(vault_ops, rotate=True)

    async def _remove_the_login_and_block() -> None:
        vault_ops.users.pop("svc-a")
        await _block()

    vault_ops.on_request("POST", _PASSWORD_DATA, _remove_the_login_and_block)

    result = await _write_login(params)

    assert result.status != "ok", (result.status, result.error)
    assert "svc-a" not in vault_ops.users
    assert vault_ops.user_writes == []
    stored = vault_ops.items[f"secret/{_PASSWORD_ITEM}"][1]["password"]
    assert stored not in json.dumps(result.model_dump(mode="json"))
    (row,) = await _dispatch_rows(_USERPASS)
    assert row.payload["result_status"] != "ok"
    assert stored not in json.dumps(row.payload) + json.dumps(row.raw_payload)


_REMOVE_CASES = [
    pytest.param(True, id="caller-blocked"),
    pytest.param(False, id="caller-not-blocked"),
]


@pytest.mark.parametrize("block", _REMOVE_CASES)
async def test_a_login_removed_during_the_store_says_the_password_is_stored_and_not_set(
    vault_ops: VaultStandIn, block: bool
) -> None:
    params = _prepare(vault_ops, rotate=True)

    async def _remove_the_login() -> None:
        vault_ops.users.pop("svc-a")
        if block:
            await _block()

    vault_ops.on_request("POST", _PASSWORD_DATA, _remove_the_login)

    result = await _write_login(params)

    assert result.status == "error", (result.status, result.error)
    assert "svc-a" not in vault_ops.users
    assert vault_ops.user_writes == []
    stored = vault_ops.items[f"secret/{_PASSWORD_ITEM}"][1]["password"]
    assert stored != "made-up-old"
    place = ("'secret'", f"'{_PASSWORD_ITEM}'", "'password'")

    error = result.error or ""
    assert "stored and not set" in error
    assert all(part in error for part in place), error
    extras = result.extras
    assert "stored and not set" in extras["exception_message"]
    assert all(part in extras["exception_message"] for part in place)
    assert extras["password_stored"] is True
    assert extras["password_set"] is False
    assert extras["stored_mount"] == "secret"
    assert extras["stored_path"] == _PASSWORD_ITEM
    assert extras["stored_field"] == "password"
    assert stored not in json.dumps(result.model_dump(mode="json"))

    (row,) = await _dispatch_rows(_USERPASS)
    assert row.payload["result_status"] == "error"
    audit_error = row.payload["error"]
    assert audit_error["exception_message"] == extras["exception_message"]
    assert audit_error["password_stored"] is True
    assert audit_error["password_set"] is False
    assert audit_error["stored_mount"] == "secret"
    assert audit_error["stored_path"] == _PASSWORD_ITEM
    assert audit_error["stored_field"] == "password"
    assert stored not in json.dumps(row.payload) + json.dumps(row.raw_payload)
