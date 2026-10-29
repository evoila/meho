# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which Vault role runs the ``vault.sys.policy.*`` ops, and the delete check (#3945).

* Each policy handler logs in under the target's ``extras["vault_role"]``
  (on ``extras["vault_mount"]``), like the KV, auth and token-role ops. A
  target without a role keeps the deployment default (``meho-mcp``).
* ``vault.sys.policy.delete`` checks ``delete`` on ``sys/policy/<name>``
  before MEHO asks for approval. Without it, the call is refused with
  ``write_capability_denied`` and no approval request is written.

Vault is faked in-process through ``tests/_vault_fakes.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import hvac.exceptions
import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel, OperationResult
from meho_backplane.connectors.vault import VaultConnector, register_vault_sys_typed_operations
from meho_backplane.connectors.vault.ops_sys_policy import (
    vault_sys_policy_delete,
    vault_sys_policy_delete_capability_preflight,
    vault_sys_policy_list,
    vault_sys_policy_read,
    vault_sys_policy_write,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.logging import configure_logging
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target

from ._vault_fakes import install_fake_client

_OP_DELETE = "vault.sys.policy.delete"
_POLICY = "meho-tenant-example"
_POLICY_PATH = f"sys/policy/{_POLICY}"
_TARGET_LOGIN = {"role": "meho-vault-admin", "jwt": "op-jwt", "path": "jwt-meho"}
_DEFAULT_LOGIN = {"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"}


@pytest.fixture(autouse=True)
def _clean_vault_registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_CHECK_RUNNER_ROLE", raising=False)
    monkeypatch.delenv("VAULT_OIDC_ROLE_BY_TENANT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def _registered_vault_sys_ops() -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_vault_sys_typed_operations(embedding_service=service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op-jwt",
        tenant_id=uuid.UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_vault_target(**extras: Any) -> Target:
    now = datetime.now(UTC)
    return Target(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="vault-admin",
        aliases=[],
        product="vault",
        version=None,
        host="vault.example.test",
        port=8200,
        fqdn="vault.example.test",
        secret_ref=None,
        auth_model=AuthModel.SHARED_SERVICE_ACCOUNT,
        vpn_required=False,
        extras=extras,
        notes=None,
        fingerprint=None,
        preferred_impl_id=None,
        created_at=now,
        updated_at=now,
    )


async def _dispatch_delete() -> OperationResult:
    return await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id=_OP_DELETE,
        target=None,
        params={"name": _POLICY},
    )


async def _approval_rows() -> int:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.op_id == _OP_DELETE)
            )
        ).scalar_one()


_HANDLERS = [
    pytest.param(vault_sys_policy_read, {"name": _POLICY}, id="read"),
    pytest.param(vault_sys_policy_list, {}, id="list"),
    pytest.param(vault_sys_policy_write, {"name": _POLICY, "policy": "# body\n"}, id="write"),
    pytest.param(vault_sys_policy_delete, {"name": _POLICY}, id="delete"),
]


@pytest.mark.parametrize(("handler", "params"), _HANDLERS)
async def test_handlers_log_in_with_the_target_role(
    monkeypatch: pytest.MonkeyPatch, handler: Any, params: dict[str, Any]
) -> None:
    fake = install_fake_client(monkeypatch)
    target = _make_vault_target(vault_role="meho-vault-admin", vault_mount="jwt-meho")

    await handler(_make_operator(), target, params)

    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


@pytest.mark.parametrize(("handler", "params"), _HANDLERS)
async def test_handlers_keep_the_default_role_without_a_target_role(
    monkeypatch: pytest.MonkeyPatch, handler: Any, params: dict[str, Any]
) -> None:
    fake = install_fake_client(monkeypatch)

    await handler(_make_operator(), _make_vault_target(), params)

    assert fake.auth.jwt.login_calls == [_DEFAULT_LOGIN]


def test_only_the_delete_has_a_permission_check() -> None:
    assert _OP_DELETE in _PERMISSION_PREFLIGHTS
    for op_id in ("vault.sys.policy.read", "vault.sys.policy.list", "vault.sys.policy.write"):
        assert op_id not in _PERMISSION_PREFLIGHTS


async def test_delete_check_probes_the_policy_path_under_the_target_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_POLICY_PATH: ["delete"]}
    target = _make_vault_target(vault_role="meho-vault-admin", vault_mount="jwt-meho")

    result = await vault_sys_policy_delete_capability_preflight(
        _make_operator(), {"name": _POLICY}, target=target
    )

    assert result == {
        "check": "vault.capabilities-self",
        "path": _POLICY_PATH,
        "required": ["delete"],
        "granted": ["delete"],
        "will_be_denied": False,
        "principal_sub": "test-operator",
    }
    assert fake.sys.get_capabilities_calls == [
        {"paths": [_POLICY_PATH], "token": None, "accessor": None}
    ]
    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


@pytest.mark.parametrize("granted", [["read"], ["update"], []])
async def test_delete_check_denies_without_delete(
    monkeypatch: pytest.MonkeyPatch, granted: list[str]
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_POLICY_PATH: granted}

    result = await vault_sys_policy_delete_capability_preflight(_make_operator(), {"name": _POLICY})

    assert result is not None
    assert result["will_be_denied"] is True


async def test_delete_check_probe_fault_is_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe logs the fault with its traceback. Use the app's logging
    # setup, which leaves local variables out of tracebacks, so the fake
    # client's test data is not printed when this test runs alone.
    configure_logging()
    fake = install_fake_client(monkeypatch)
    fake.sys.raise_on_get_capabilities = hvac.exceptions.VaultDown("sealed")

    result = await vault_sys_policy_delete_capability_preflight(_make_operator(), {"name": _POLICY})

    assert result is None


async def test_delete_without_rights_is_refused_before_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_POLICY_PATH: ["read"]}

    result = await _dispatch_delete()

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == _POLICY_PATH
    assert result.extras["required"] == ["delete"]
    assert await _approval_rows() == 0
    assert fake.sys.policy_delete_calls == []


async def test_delete_with_rights_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_sys_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_POLICY_PATH: ["delete"]}

    result = await _dispatch_delete()

    assert result.status == "awaiting_approval", result.error
    assert await _approval_rows() == 1
    assert fake.sys.policy_delete_calls == []
