# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Vault token-role ops.

A token role is a saved set of rules in Vault for making tokens. The three
ops live in ``connectors/vault/ops_token_role.py`` and register in the
``token`` group next to the token lifecycle ops.

Coverage:

* Registration: ``role.write`` is ``dangerous`` and needs approval;
  ``role.read`` / ``role.list`` are ``safe`` with no approval. All three
  sit in the ``token`` group, whose text names them.
* Broadcast class: the write classifies ``write`` (its ``.write`` suffix),
  the list classifies ``read``, the single read classifies ``other`` like
  the other ``vault.*.read`` ops.
* ``write`` posts only the fields the caller gave to
  ``/v1/auth/token/roles/<role_name>`` and echoes them back.
* ``read`` returns the settings unwrapped from Vault's ``data`` envelope;
  an unknown role is a ``connector_error`` naming ``InvalidPath``.
* ``list`` returns ``{"keys": [...]}`` and turns the empty-store 404 into
  ``{"keys": []}``.
* All three handlers log in under the target's Vault role
  (``extras["vault_role"]`` / ``extras["vault_mount"]``), like the
  key-value and login ops.
* The write's permission preflight probes ``auth/token/roles/<role_name>``
  under the target's role for ``create`` + ``update``; without them MEHO
  refuses the call before approval (``write_capability_denied``) and writes
  no approval request. A probe fault is fail-soft (no banner, park goes on).

Vault is faked in-process through ``tests/_vault_fakes.py`` (hvac is
``requests``-based, so there is no HTTP to intercept).
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
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.schemas import AuthModel, OperationResult
from meho_backplane.connectors.vault import (
    register_vault_identity_token_operations,
    register_vault_typed_operations,
)
from meho_backplane.connectors.vault.ops_token import TOKEN_WHEN_TO_USE
from meho_backplane.connectors.vault.ops_token_role import (
    TOKEN_ROLE_OP_LIST,
    TOKEN_ROLE_OP_READ,
    TOKEN_ROLE_OP_WRITE,
    vault_token_role_list,
    vault_token_role_read,
    vault_token_role_write,
    vault_token_role_write_capability_preflight,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target

from ._vault_fakes import install_fake_client

_ROLE = "ci-runner"
_ROLE_API_PATH = f"/v1/auth/token/roles/{_ROLE}"
_ROLE_AUTH_PATH = f"auth/token/roles/{_ROLE}"
_TARGET_LOGIN = {"role": "meho-token-admin", "jwt": "op-jwt", "path": "jwt-meho"}

_ROLE_DATA: dict[str, Any] = {
    "name": _ROLE,
    "allowed_policies": ["ci-read"],
    "disallowed_policies": [],
    "orphan": True,
    "renewable": True,
    "token_period": 86400,
    "token_explicit_max_ttl": 0,
    "token_type": "default",
    "path_suffix": "",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars needed by Settings / VaultConnector."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_CHECK_RUNNER_ROLE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_vault_typed_ops(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    """Upsert every Vault typed-op descriptor row, including the token-role ops."""
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_identity_token_operations(embedding_service=stub_embedding_service)
    yield


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=uuid.UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_vault_target(**extras: Any) -> Target:
    """A vault ``Target`` whose ``extras`` name the per-target Vault role."""
    now = datetime.now(UTC)
    return Target(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="vault-main",
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


async def _dispatch_vault(
    op_id: str, params: dict[str, Any], *, approved: bool = True
) -> OperationResult:
    """Dispatch a vault op; ``approved=True`` takes the post-approval resume path."""
    return await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=approved,
    )


async def _approval_rows_for(op_id: str) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.op_id == op_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Registration / classification
# ---------------------------------------------------------------------------

#: op_id -> (safety_level, requires_approval, group_key) the descriptor must carry.
_EXPECTED: dict[str, tuple[str, bool, str]] = {
    TOKEN_ROLE_OP_WRITE: ("dangerous", True, "token"),
    TOKEN_ROLE_OP_READ: ("safe", False, "token"),
    TOKEN_ROLE_OP_LIST: ("safe", False, "token"),
}


@pytest.mark.parametrize("op_id", list(_EXPECTED))
async def test_ops_register_with_expected_safety_approval_group(
    op_id: str, _registered_vault_typed_ops: None
) -> None:
    expected_safety, expected_approval, expected_group = _EXPECTED[op_id]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
        group_key = (
            await session.execute(
                select(OperationGroup.group_key).where(OperationGroup.id == row.group_id)
            )
        ).scalar_one()
    assert row.safety_level == expected_safety
    assert row.requires_approval is expected_approval
    assert group_key == expected_group


def test_token_group_text_names_the_role_ops() -> None:
    for op_id in _EXPECTED:
        assert op_id in TOKEN_WHEN_TO_USE


@pytest.mark.parametrize(
    ("op_id", "op_class"),
    [
        (TOKEN_ROLE_OP_WRITE, "write"),
        (TOKEN_ROLE_OP_READ, "other"),
        (TOKEN_ROLE_OP_LIST, "read"),
    ],
)
def test_role_ops_classify_without_a_secret_class(op_id: str, op_class: str) -> None:
    """A token role holds no secret, so none of the ops is credential-class."""
    assert classify_op(op_id) == op_class


# ---------------------------------------------------------------------------
# vault.token.role.write
# ---------------------------------------------------------------------------


async def test_write_sends_only_the_given_fields(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    given = {
        "role_name": _ROLE,
        "allowed_policies": ["ci-read"],
        "orphan": True,
        "token_period": "24h",
    }

    result = await _dispatch_vault(TOKEN_ROLE_OP_WRITE, given)

    assert result.status == "ok", result.error
    sent = {"allowed_policies": ["ci-read"], "orphan": True, "token_period": "24h"}
    assert fake.adapter.post_calls == [{"url": _ROLE_API_PATH, "json": sent}]
    assert result.result == {"role_name": _ROLE, "written": True, "settings": sent}


async def test_write_with_only_a_name_sends_an_empty_body(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """No optional field given: nothing is sent, so an existing role keeps every setting."""
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(TOKEN_ROLE_OP_WRITE, {"role_name": _ROLE})

    assert result.status == "ok", result.error
    assert fake.adapter.post_calls == [{"url": _ROLE_API_PATH, "json": {}}]
    assert result.result == {"role_name": _ROLE, "written": True, "settings": {}}


async def test_write_rejects_a_role_name_with_a_slash(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """A '/' would point the POST at another Vault path; the schema stops it."""
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(TOKEN_ROLE_OP_WRITE, {"role_name": "../accessors"})

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert fake.adapter.post_calls == []


# ---------------------------------------------------------------------------
# vault.token.role.read / vault.token.role.list
# ---------------------------------------------------------------------------


async def test_read_returns_the_role_settings(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.token.role_payloads[_ROLE] = {"request_id": "r1", "data": dict(_ROLE_DATA)}

    result = await _dispatch_vault(TOKEN_ROLE_OP_READ, {"role_name": _ROLE})

    assert result.status == "ok", result.error
    assert result.result == _ROLE_DATA
    assert fake.auth.token.read_role_calls == [_ROLE]


async def test_read_unknown_role_surfaces_connector_error(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.token.raise_on_read_role = hvac.exceptions.InvalidPath("no such role")

    result = await _dispatch_vault(TOKEN_ROLE_OP_READ, {"role_name": "ghost"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "InvalidPath"


async def test_list_returns_the_role_names(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.token.roles_payload = {"data": {"keys": [_ROLE, "nightly"]}}

    result = await _dispatch_vault(TOKEN_ROLE_OP_LIST, {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": [_ROLE, "nightly"]}
    assert fake.auth.token.list_roles_calls == 1


async def test_list_empty_store_normalises_to_empty_keys(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.auth.token.raise_on_list_roles = hvac.exceptions.InvalidPath("none")

    result = await _dispatch_vault(TOKEN_ROLE_OP_LIST, {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": []}


# ---------------------------------------------------------------------------
# Per-target Vault role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        pytest.param(vault_token_role_write, {"role_name": _ROLE, "orphan": True}, id="write"),
        pytest.param(vault_token_role_read, {"role_name": _ROLE}, id="read"),
        pytest.param(vault_token_role_list, {}, id="list"),
    ],
)
async def test_handlers_log_in_with_the_target_role(
    monkeypatch: pytest.MonkeyPatch, handler: Any, params: dict[str, Any]
) -> None:
    """Each handler logs in under ``extras["vault_role"]`` on ``extras["vault_mount"]``."""
    fake = install_fake_client(monkeypatch)
    fake.auth.token.role_payloads[_ROLE] = {"data": dict(_ROLE_DATA)}
    fake.auth.token.roles_payload = {"data": {"keys": [_ROLE]}}
    target = _make_vault_target(vault_role="meho-token-admin", vault_mount="jwt-meho")

    await handler(_make_operator(jwt="op-jwt"), target, params)

    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


# ---------------------------------------------------------------------------
# Write preflight
# ---------------------------------------------------------------------------


def test_write_preflight_is_registered() -> None:
    assert TOKEN_ROLE_OP_WRITE in _PERMISSION_PREFLIGHTS
    assert TOKEN_ROLE_OP_READ not in _PERMISSION_PREFLIGHTS
    assert TOKEN_ROLE_OP_LIST not in _PERMISSION_PREFLIGHTS


async def test_write_preflight_probes_the_role_path_under_the_target_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_ROLE_AUTH_PATH: ["create", "update"]}
    target = _make_vault_target(vault_role="meho-token-admin", vault_mount="jwt-meho")

    result = await vault_token_role_write_capability_preflight(
        _make_operator(jwt="op-jwt"), {"role_name": _ROLE, "orphan": True}, target=target
    )

    assert result == {
        "check": "vault.capabilities-self",
        "path": _ROLE_AUTH_PATH,
        "required": ["create", "update"],
        "granted": ["create", "update"],
        "will_be_denied": False,
        "principal_sub": "test-operator",
    }
    assert fake.sys.get_capabilities_calls == [
        {"paths": [_ROLE_AUTH_PATH], "token": None, "accessor": None}
    ]
    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


@pytest.mark.parametrize("granted", [["read"], ["update"], ["create"], []])
async def test_write_preflight_denies_without_create_and_update(
    monkeypatch: pytest.MonkeyPatch, granted: list[str]
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_ROLE_AUTH_PATH: granted}

    result = await vault_token_role_write_capability_preflight(
        _make_operator(), {"role_name": _ROLE}, target=None
    )

    assert result is not None
    assert result["will_be_denied"] is True
    assert result["granted"] == sorted(granted)


async def test_write_preflight_probe_fault_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.raise_on_get_capabilities = hvac.exceptions.VaultDown("sealed")

    result = await vault_token_role_write_capability_preflight(
        _make_operator(), {"role_name": _ROLE}, target=None
    )

    assert result is None


async def test_write_without_rights_is_refused_before_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """No ``create`` + ``update`` on the role path: MEHO does not ask for approval."""
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_ROLE_AUTH_PATH: ["read"]}

    result = await _dispatch_vault(
        TOKEN_ROLE_OP_WRITE, {"role_name": _ROLE, "orphan": True}, approved=False
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == _ROLE_AUTH_PATH
    assert result.extras["required"] == ["create", "update"]
    assert result.extras["granted"] == ["read"]
    assert await _approval_rows_for(TOKEN_ROLE_OP_WRITE) == 0
    assert fake.adapter.post_calls == []


async def test_write_with_rights_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """With the rights in place the write parks; nothing reaches Vault before approval."""
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_ROLE_AUTH_PATH: ["create", "update"]}

    result = await _dispatch_vault(
        TOKEN_ROLE_OP_WRITE, {"role_name": _ROLE, "orphan": True}, approved=False
    )

    assert result.status == "awaiting_approval", result.error
    assert await _approval_rows_for(TOKEN_ROLE_OP_WRITE) == 1
    assert fake.adapter.post_calls == []
