# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Vault identity token role and key ops.

Vault signs identity tokens (JWTs) with a role (``identity/oidc/role``)
and a key (``identity/oidc/key``). The six ops live in
``connectors/vault/ops_identity_oidc.py`` and register in the ``identity``
group.

Coverage:

* Registration: the four reads are ``safe`` with no approval; the two
  deletes are ``dangerous`` and need approval. The group text names all six.
* Lists return ``{"keys": [...]}`` and turn the empty-store 404 into
  ``{"keys": []}``. Reads unwrap Vault's ``data`` envelope; an unknown name
  is a ``connector_error`` naming ``InvalidPath``.
* The key delete turns Vault's "still in use" answer into
  ``VaultOidcKeyInUseError`` whose remediation names the roles.
* Every handler logs in under the target's Vault role.
* Each delete has a permission check before approval on its own path for
  ``delete``; without it MEHO refuses the call and writes no approval request.
* The full flow: a role delete and a key delete each wait for approval, a
  second person approves, the delete runs once and an audit row is written.

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
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.schemas import AuthModel, OperationResult
from meho_backplane.connectors.vault import register_vault_identity_token_operations
from meho_backplane.connectors.vault.ops_identity import IDENTITY_WHEN_TO_USE
from meho_backplane.connectors.vault.ops_identity_oidc import (
    OIDC_KEY_OP_DELETE,
    OIDC_KEY_OP_LIST,
    OIDC_KEY_OP_READ,
    OIDC_ROLE_OP_DELETE,
    OIDC_ROLE_OP_LIST,
    OIDC_ROLE_OP_READ,
    vault_identity_oidc_key_delete,
    vault_identity_oidc_key_delete_capability_preflight,
    vault_identity_oidc_key_list,
    vault_identity_oidc_key_read,
    vault_identity_oidc_role_delete,
    vault_identity_oidc_role_delete_capability_preflight,
    vault_identity_oidc_role_list,
    vault_identity_oidc_role_read,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target

from ._vault_fakes import install_fake_client

_ROLE = "ci-deployer"
_KEY = "ci-signing"
_ROLE_PATH = f"identity/oidc/role/{_ROLE}"
_KEY_PATH = f"identity/oidc/key/{_KEY}"
_TARGET_LOGIN = {"role": "meho-identity-admin", "jwt": "op-jwt", "path": "jwt-meho"}

_ROLE_DATA: dict[str, Any] = {
    "key": _KEY,
    "client_id": "example-audience",
    "ttl": 3600,
    "template": "",
}
_KEY_DATA: dict[str, Any] = {
    "algorithm": "RS256",
    "allowed_client_ids": ["*"],
    "rotation_period": 86400,
    "verification_ttl": 86400,
}


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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def _registered_ops() -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_vault_identity_token_operations(embedding_service=service)
    yield


def _make_operator(sub: str = "test-operator", jwt: str = "fake.jwt.value") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=jwt,
        tenant_id=uuid.UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _make_vault_target(**extras: Any) -> Target:
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
    return await dispatch(
        operator=_make_operator(),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=approved,
    )


async def _approval_rows_for(op_id: str) -> int:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.op_id == op_id)
            )
        ).scalar_one()


def _key_in_use(*roles: str) -> hvac.exceptions.InvalidRequest:
    """Vault's 400 answer when a key is still used by roles."""
    message = (
        f'unable to delete key "{_KEY}" because it is currently referenced by '
        f"these roles: {', '.join(roles)}"
    )
    return hvac.exceptions.InvalidRequest(
        errors=[message], method="delete", url=f"https://vault.test/v1/{_KEY_PATH}"
    )


# ---------------------------------------------------------------------------
# Registration / classification
# ---------------------------------------------------------------------------

_EXPECTED: dict[str, tuple[str, bool]] = {
    OIDC_ROLE_OP_LIST: ("safe", False),
    OIDC_ROLE_OP_READ: ("safe", False),
    OIDC_ROLE_OP_DELETE: ("dangerous", True),
    OIDC_KEY_OP_LIST: ("safe", False),
    OIDC_KEY_OP_READ: ("safe", False),
    OIDC_KEY_OP_DELETE: ("dangerous", True),
}


@pytest.mark.parametrize("op_id", list(_EXPECTED))
async def test_ops_register_with_expected_safety_and_approval(
    op_id: str, _registered_ops: None
) -> None:
    expected_safety, expected_approval = _EXPECTED[op_id]
    async with get_sessionmaker()() as session:
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
    assert group_key == "identity"


def test_identity_group_text_names_the_new_ops() -> None:
    for op_id in _EXPECTED:
        assert op_id in IDENTITY_WHEN_TO_USE


@pytest.mark.parametrize(
    ("op_id", "op_class"),
    [
        (OIDC_ROLE_OP_LIST, "read"),
        (OIDC_KEY_OP_LIST, "read"),
        (OIDC_ROLE_OP_DELETE, "write"),
        (OIDC_KEY_OP_DELETE, "write"),
    ],
)
def test_ops_classify_without_a_secret_class(op_id: str, op_class: str) -> None:
    assert classify_op(op_id) == op_class


# ---------------------------------------------------------------------------
# Lists and reads
# ---------------------------------------------------------------------------


async def test_role_list_returns_names(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.oidc_roles_payload = {"data": {"keys": [_ROLE, "nightly"]}}

    result = await _dispatch_vault(OIDC_ROLE_OP_LIST, {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": [_ROLE, "nightly"]}


async def test_key_list_returns_names(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.oidc_keys_payload = {"data": {"keys": ["default", _KEY]}}

    result = await _dispatch_vault(OIDC_KEY_OP_LIST, {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": ["default", _KEY]}


@pytest.mark.parametrize("op_id", [OIDC_ROLE_OP_LIST, OIDC_KEY_OP_LIST])
async def test_empty_list_is_empty_keys(
    op_id: str, monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.raise_on_list_oidc_roles = hvac.exceptions.InvalidPath("none")
    fake.secrets.identity.raise_on_list_oidc_keys = hvac.exceptions.InvalidPath("none")

    result = await _dispatch_vault(op_id, {})

    assert result.status == "ok", result.error
    assert result.result == {"keys": []}


async def test_role_read_returns_settings(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.oidc_role_payloads[_ROLE] = {"data": dict(_ROLE_DATA)}

    result = await _dispatch_vault(OIDC_ROLE_OP_READ, {"name": _ROLE})

    assert result.status == "ok", result.error
    assert result.result == _ROLE_DATA


async def test_key_read_returns_settings(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.oidc_key_payloads[_KEY] = {"data": dict(_KEY_DATA)}

    result = await _dispatch_vault(OIDC_KEY_OP_READ, {"name": _KEY})

    assert result.status == "ok", result.error
    assert result.result == _KEY_DATA


@pytest.mark.parametrize("op_id", [OIDC_ROLE_OP_READ, OIDC_KEY_OP_READ])
async def test_read_unknown_name_is_connector_error(
    op_id: str, monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    install_fake_client(monkeypatch)

    result = await _dispatch_vault(op_id, {"name": "ghost"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "InvalidPath"


@pytest.mark.parametrize("op_id", [OIDC_ROLE_OP_DELETE, OIDC_KEY_OP_READ])
async def test_name_with_a_slash_is_rejected(
    op_id: str, monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(op_id, {"name": "../entity"})

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert fake.secrets.identity.oidc_role_delete_calls == []


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


async def test_role_delete_after_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(OIDC_ROLE_OP_DELETE, {"name": _ROLE})

    assert result.status == "ok", result.error
    assert result.result == {"name": _ROLE, "deleted": True}
    assert fake.secrets.identity.oidc_role_delete_calls == [_ROLE]


async def test_key_delete_after_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(OIDC_KEY_OP_DELETE, {"name": _KEY})

    assert result.status == "ok", result.error
    assert result.result == {"name": _KEY, "deleted": True}
    assert fake.secrets.identity.oidc_key_delete_calls == [_KEY]


async def test_key_in_use_names_the_roles(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.raise_on_delete_oidc_key = _key_in_use(_ROLE, "nightly")

    result = await _dispatch_vault(OIDC_KEY_OP_DELETE, {"name": _KEY})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "VaultOidcKeyInUseError"
    assert result.extras["http_status"] == 400
    assert f"{_ROLE}, nightly" in result.extras["remediation"]
    assert "vault.identity.oidc.role.delete" in result.extras["remediation"]


async def test_other_key_delete_error_is_left_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vault refuses to delete the 'default' key; that error passes through unchanged."""
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.raise_on_delete_oidc_key = hvac.exceptions.InvalidRequest(
        errors=['deletion of key "default" not allowed']
    )

    with pytest.raises(hvac.exceptions.InvalidRequest):
        await vault_identity_oidc_key_delete(_make_operator(), None, {"name": "default"})


# ---------------------------------------------------------------------------
# Per-target Vault role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        pytest.param(vault_identity_oidc_role_list, {}, id="role-list"),
        pytest.param(vault_identity_oidc_role_read, {"name": _ROLE}, id="role-read"),
        pytest.param(vault_identity_oidc_role_delete, {"name": _ROLE}, id="role-delete"),
        pytest.param(vault_identity_oidc_key_list, {}, id="key-list"),
        pytest.param(vault_identity_oidc_key_read, {"name": _KEY}, id="key-read"),
        pytest.param(vault_identity_oidc_key_delete, {"name": _KEY}, id="key-delete"),
    ],
)
async def test_handlers_log_in_with_the_target_role(
    monkeypatch: pytest.MonkeyPatch, handler: Any, params: dict[str, Any]
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.secrets.identity.oidc_role_payloads[_ROLE] = {"data": dict(_ROLE_DATA)}
    fake.secrets.identity.oidc_key_payloads[_KEY] = {"data": dict(_KEY_DATA)}
    fake.secrets.identity.oidc_roles_payload = {"data": {"keys": [_ROLE]}}
    fake.secrets.identity.oidc_keys_payload = {"data": {"keys": [_KEY]}}
    target = _make_vault_target(vault_role="meho-identity-admin", vault_mount="jwt-meho")

    await handler(_make_operator(jwt="op-jwt"), target, params)

    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


# ---------------------------------------------------------------------------
# Delete preflights
# ---------------------------------------------------------------------------


def test_only_the_deletes_have_a_preflight() -> None:
    assert OIDC_ROLE_OP_DELETE in _PERMISSION_PREFLIGHTS
    assert OIDC_KEY_OP_DELETE in _PERMISSION_PREFLIGHTS
    for op_id in (OIDC_ROLE_OP_LIST, OIDC_ROLE_OP_READ, OIDC_KEY_OP_LIST, OIDC_KEY_OP_READ):
        assert op_id not in _PERMISSION_PREFLIGHTS


@pytest.mark.parametrize(
    ("preflight", "name", "path"),
    [
        (vault_identity_oidc_role_delete_capability_preflight, _ROLE, _ROLE_PATH),
        (vault_identity_oidc_key_delete_capability_preflight, _KEY, _KEY_PATH),
    ],
)
async def test_preflight_probes_the_path_under_the_target_role(
    monkeypatch: pytest.MonkeyPatch, preflight: Any, name: str, path: str
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {path: ["delete", "read"]}
    target = _make_vault_target(vault_role="meho-identity-admin", vault_mount="jwt-meho")

    result = await preflight(_make_operator(jwt="op-jwt"), {"name": name}, target=target)

    assert result is not None
    assert result["path"] == path
    assert result["required"] == ["delete"]
    assert result["will_be_denied"] is False
    assert fake.sys.get_capabilities_calls == [{"paths": [path], "token": None, "accessor": None}]
    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


@pytest.mark.parametrize(
    ("op_id", "name", "path"),
    [
        (OIDC_ROLE_OP_DELETE, _ROLE, _ROLE_PATH),
        (OIDC_KEY_OP_DELETE, _KEY, _KEY_PATH),
    ],
)
async def test_delete_without_rights_is_refused_before_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None, op_id: str, name: str, path: str
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {path: ["read"]}

    result = await _dispatch_vault(op_id, {"name": name}, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == path
    assert result.extras["required"] == ["delete"]
    assert await _approval_rows_for(op_id) == 0
    assert fake.secrets.identity.oidc_role_delete_calls == []
    assert fake.secrets.identity.oidc_key_delete_calls == []


# ---------------------------------------------------------------------------
# Full flow: wait for approval -> second person approves -> delete -> audit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "name", "path"),
    [
        (OIDC_ROLE_OP_DELETE, _ROLE, _ROLE_PATH),
        (OIDC_KEY_OP_DELETE, _KEY, _KEY_PATH),
    ],
)
async def test_delete_waits_for_approval_then_runs_once_with_an_audit_row(
    monkeypatch: pytest.MonkeyPatch, _registered_ops: None, op_id: str, name: str, path: str
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {path: ["delete"]}
    requester = _make_operator("op-requester")

    parked = await dispatch(
        operator=requester,
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params={"name": name},
    )
    assert parked.status == "awaiting_approval", parked.error
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    assert fake.secrets.identity.oidc_role_delete_calls == []
    assert fake.secrets.identity.oidc_key_delete_calls == []

    approver = _make_operator("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resumed = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resumed.status == "ok", resumed.error
    assert resumed.result == {"name": name, "deleted": True}
    deletes = (
        fake.secrets.identity.oidc_role_delete_calls + fake.secrets.identity.oidc_key_delete_calls
    )
    assert deletes == [name]

    async with get_sessionmaker()() as s:
        audit_rows = (
            (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["op_id"] == op_id
