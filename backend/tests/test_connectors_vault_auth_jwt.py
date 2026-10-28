# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Vault JWT/OIDC login-method ops.

The four ops live in ``connectors/vault/ops_auth_jwt.py`` and register in
the ``auth`` group next to the userpass and approle ops.

Coverage:

* Registration: ``role.write`` is ``dangerous`` and needs approval; the
  three reads are ``safe`` with no approval. All four sit in the ``auth``
  group, whose text names them and the JWT/OIDC login method.
* ``role.write`` posts the fields the caller gave to
  ``/v1/auth/<mount>/role/<name>``, plus the current ``role_type`` and
  ``bound_claims_type`` when not given, because Vault resets those two. A
  fake that merges like Vault proves that a one-field write leaves the
  other fields as they were. The answer says that only new logins get the
  new settings.
* ``config.read`` never returns ``oidc_client_secret`` or
  ``provider_config``.
* ``role.list`` turns Vault's ``404`` into ``{"keys": []}``.
* The mount is a parameter (default ``jwt``) and a mount or role name
  that could leave ``auth/<mount>/role/`` is refused by the schema.
* All handlers log in under the target's Vault role.
* The write's permission preflight probes ``auth/<mount>/role/<name>``
  for ``create`` + ``read`` + ``update``; without them MEHO refuses the call before
  approval and writes no approval request.

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
from meho_backplane.connectors.vault import (
    register_vault_auth_jwt_operations,
    register_vault_typed_operations,
)
from meho_backplane.connectors.vault.ops_auth import AUTH_WHEN_TO_USE
from meho_backplane.connectors.vault.ops_auth_jwt import (
    AUTH_JWT_OP_CONFIG_READ,
    AUTH_JWT_OP_ROLE_LIST,
    AUTH_JWT_OP_ROLE_READ,
    AUTH_JWT_OP_ROLE_WRITE,
    vault_auth_jwt_config_read,
    vault_auth_jwt_role_list,
    vault_auth_jwt_role_read,
    vault_auth_jwt_role_write,
    vault_auth_jwt_role_write_capability_preflight,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target

from ._vault_fakes import install_fake_client

_ROLE = "ci-login"
_ROLE_API_PATH = f"/v1/auth/jwt/role/{_ROLE}"
_ROLE_AUTH_PATH = f"auth/jwt/role/{_ROLE}"
_TARGET_LOGIN = {"role": "meho-auth-admin", "jwt": "op-jwt", "path": "jwt-meho"}

#: A login role as Vault returns it: 30 fields, none of them secret.
_ROLE_DATA: dict[str, Any] = {
    "role_type": "jwt",
    "user_claim": "sub",
    "user_claim_json_pointer": False,
    "bound_audiences": ["meho"],
    "bound_subject": "",
    "bound_claims": None,
    "bound_claims_type": "string",
    "groups_claim": "groups",
    "claim_mappings": None,
    "oidc_scopes": [],
    "allowed_redirect_uris": [],
    "verbose_oidc_logging": False,
    "max_age": 0,
    "clock_skew_leeway": 60,
    "expiration_leeway": 150,
    "not_before_leeway": 150,
    "token_ttl": 3600,
    "token_max_ttl": 0,
    "token_explicit_max_ttl": 0,
    "token_period": 0,
    "token_policies": ["meho"],
    "token_bound_cidrs": [],
    "token_no_default_policy": False,
    "token_num_uses": 0,
    "token_type": "default",
    "bound_cidrs": [],
    "policies": ["meho"],
    "period": 0,
    "num_uses": 0,
    "ttl": 0,
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
    """Upsert the Vault typed-op rows: the auth group first, then the JWT/OIDC ops."""
    await register_vault_typed_operations(embedding_service=stub_embedding_service)
    await register_vault_auth_jwt_operations(embedding_service=stub_embedding_service)
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

#: op_id -> (safety_level, requires_approval) the descriptor must carry.
_EXPECTED: dict[str, tuple[str, bool]] = {
    AUTH_JWT_OP_ROLE_WRITE: ("dangerous", True),
    AUTH_JWT_OP_ROLE_READ: ("safe", False),
    AUTH_JWT_OP_ROLE_LIST: ("safe", False),
    AUTH_JWT_OP_CONFIG_READ: ("safe", False),
}


@pytest.mark.parametrize("op_id", list(_EXPECTED))
async def test_ops_register_in_the_auth_group(
    op_id: str, _registered_vault_typed_ops: None
) -> None:
    expected_safety, expected_approval = _EXPECTED[op_id]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
        group = (
            await session.execute(select(OperationGroup).where(OperationGroup.id == row.group_id))
        ).scalar_one()
    assert row.safety_level == expected_safety
    assert row.requires_approval is expected_approval
    assert group.group_key == "auth"
    assert group.when_to_use == AUTH_WHEN_TO_USE


def test_auth_group_text_names_the_jwt_oidc_login_method() -> None:
    assert "JWT/OIDC" in AUTH_WHEN_TO_USE
    assert "vault.auth.jwt.role.{list,read,write}" in AUTH_WHEN_TO_USE
    assert AUTH_JWT_OP_CONFIG_READ in AUTH_WHEN_TO_USE


@pytest.mark.parametrize(
    ("op_id", "op_class"),
    [
        (AUTH_JWT_OP_ROLE_WRITE, "write"),
        (AUTH_JWT_OP_ROLE_READ, "other"),
        (AUTH_JWT_OP_ROLE_LIST, "read"),
        (AUTH_JWT_OP_CONFIG_READ, "other"),
    ],
)
def test_ops_classify_without_a_secret_class(op_id: str, op_class: str) -> None:
    assert classify_op(op_id) == op_class


# ---------------------------------------------------------------------------
# vault.auth.jwt.role.write
# ---------------------------------------------------------------------------


#: Fields Vault's role write sets back to these defaults when left out.
_RESET_WHEN_MISSING: dict[str, str] = {"role_type": "oidc", "bound_claims_type": "string"}


class _VaultRoleStore:
    """Stands in for Vault's role write, as the upstream JWT plugin does it.

    A field in the body replaces the stored value and every other field
    stays, except ``role_type`` and ``bound_claims_type``: Vault sets those
    back to ``oidc`` / ``string`` when the body leaves them out.
    """

    def __init__(self, stored: dict[str, Any]) -> None:
        self.stored = dict(stored)

    def apply(self, body: dict[str, Any]) -> None:
        # Vault stores a duration as seconds; "4h" is the only one these tests send.
        seconds = {"4h": 4 * 3600}
        for field, default in _RESET_WHEN_MISSING.items():
            self.stored[field] = body.get(field, default)
        for field, value in body.items():
            self.stored[field] = seconds.get(value, value) if isinstance(value, str) else value


#: A jwt role with glob claims: the two fields Vault would reset differ
#: from Vault's defaults, so a reset would show up.
_JWT_GLOB_ROLE: dict[str, Any] = _ROLE_DATA | {"role_type": "jwt", "bound_claims_type": "glob"}


async def test_partial_write_keeps_every_other_field(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """Write only token_ttl: the other 29 fields read back unchanged."""
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[_ROLE_API_PATH] = {"data": dict(_JWT_GLOB_ROLE)}
    vault = _VaultRoleStore(_JWT_GLOB_ROLE)

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_WRITE, {"role_name": _ROLE, "token_ttl": "4h"})

    assert result.status == "ok", result.error
    sent = {"token_ttl": "4h", "role_type": "jwt", "bound_claims_type": "glob"}
    assert fake.adapter.post_calls == [{"url": _ROLE_API_PATH, "json": sent}]
    vault.apply(fake.adapter.post_calls[0]["json"])
    changed = {k for k in _JWT_GLOB_ROLE if vault.stored[k] != _JWT_GLOB_ROLE[k]}
    assert changed == {"token_ttl"}
    assert vault.stored["token_ttl"] == 4 * 3600
    assert set(vault.stored) == set(_JWT_GLOB_ROLE)
    assert result.result["settings"] == {"token_ttl": "4h"}
    assert result.result["kept_from_current_role"] == {
        "role_type": "jwt",
        "bound_claims_type": "glob",
    }


async def test_body_with_only_the_given_fields_would_reset_the_role_type() -> None:
    """Why the write reads first: Vault turns a jwt role into an oidc role."""
    vault = _VaultRoleStore(_JWT_GLOB_ROLE)

    vault.apply({"token_ttl": "4h"})

    assert vault.stored["role_type"] == "oidc"
    assert vault.stored["bound_claims_type"] == "string"


async def test_write_with_both_reset_fields_given_does_not_read_first(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    given = {"role_name": _ROLE, "role_type": "jwt", "bound_claims_type": "string"}

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_WRITE, given)

    assert result.status == "ok", result.error
    assert fake.adapter.get_calls == []
    assert fake.adapter.post_calls[0]["json"] == {"role_type": "jwt", "bound_claims_type": "string"}
    assert result.result["kept_from_current_role"] == {}


async def test_write_answer_says_only_new_logins_get_the_change(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[f"/v1/auth/oidc/role/{_ROLE}"] = {"data": dict(_ROLE_DATA)}

    result = await _dispatch_vault(
        AUTH_JWT_OP_ROLE_WRITE,
        {"mount": "oidc", "role_name": _ROLE, "token_policies": ["operator"], "token_ttl": 3600},
    )

    assert result.status == "ok", result.error
    assert result.result["mount"] == "oidc"
    assert result.result["role_name"] == _ROLE
    assert result.result["written"] is True
    assert result.result["settings"] == {"token_policies": ["operator"], "token_ttl": 3600}
    assert "Only new logins get the new settings" in result.result["note"]
    assert "keep their old lifetime" in result.result["note"]


async def test_new_role_gets_only_the_given_fields(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """No role yet (Vault 404 on the read): nothing is copied, no hvac defaults are sent."""
    fake = install_fake_client(monkeypatch)
    fake.adapter.raise_on_get[_ROLE_API_PATH] = hvac.exceptions.InvalidPath("none")

    result = await _dispatch_vault(
        AUTH_JWT_OP_ROLE_WRITE,
        {"role_name": _ROLE, "user_claim": "sub", "bound_audiences": ["meho"]},
    )

    assert result.status == "ok", result.error
    assert fake.adapter.post_calls[0]["json"] == {"user_claim": "sub", "bound_audiences": ["meho"]}
    assert result.result["kept_from_current_role"] == {}


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"role_name": "../config"}, id="role-name-dots"),
        pytest.param({"role_name": "a/b"}, id="role-name-slash"),
        pytest.param({"mount": "../token", "role_name": _ROLE}, id="mount-dots"),
        pytest.param({"mount": "jwt/../token", "role_name": _ROLE}, id="mount-inner-dots"),
        pytest.param({"mount": "/jwt", "role_name": _ROLE}, id="mount-leading-slash"),
        pytest.param({"role_name": _ROLE, "policies": ["x"]}, id="unknown-field"),
    ],
)
async def test_write_rejects_params_outside_the_schema(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None, params: dict[str, Any]
) -> None:
    fake = install_fake_client(monkeypatch)

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_WRITE, params)

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert fake.adapter.post_calls == []


# ---------------------------------------------------------------------------
# vault.auth.jwt.role.read / vault.auth.jwt.role.list
# ---------------------------------------------------------------------------


async def test_read_returns_the_role_settings(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[_ROLE_API_PATH] = {"request_id": "r1", "data": dict(_ROLE_DATA)}

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_READ, {"role_name": _ROLE})

    assert result.status == "ok", result.error
    assert result.result == _ROLE_DATA
    assert fake.adapter.get_calls == [_ROLE_API_PATH]


async def test_read_unknown_role_surfaces_connector_error(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.raise_on_get["/v1/auth/oidc/role/ghost"] = hvac.exceptions.InvalidPath("none")

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_READ, {"mount": "oidc", "role_name": "ghost"})

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "InvalidPath"


async def test_list_returns_the_role_names(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.list_payloads["/v1/auth/oidc/role"] = {"data": {"keys": ["humans", "admins"]}}

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_LIST, {"mount": "oidc"})

    assert result.status == "ok", result.error
    assert result.result == {"mount": "oidc", "keys": ["humans", "admins"]}
    assert fake.adapter.list_calls == ["/v1/auth/oidc/role"]


async def test_list_not_found_normalises_to_empty_keys(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.raise_on_list["/v1/auth/jwt/role"] = hvac.exceptions.InvalidPath("none")

    result = await _dispatch_vault(AUTH_JWT_OP_ROLE_LIST, {})

    assert result.status == "ok", result.error
    assert result.result == {"mount": "jwt", "keys": []}


# ---------------------------------------------------------------------------
# vault.auth.jwt.config.read
# ---------------------------------------------------------------------------


async def test_config_read_never_returns_the_client_secret(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """Even when Vault sends them, oidc_client_secret and provider_config are dropped."""
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads["/v1/auth/oidc/config"] = {
        "data": {
            "oidc_discovery_url": "https://idp.example.test/realms/meho",
            "oidc_client_id": "vault",
            "oidc_client_secret": "must-not-leak",
            "provider_config": {"provider": "gsuite", "gsuite_service_account": "{}"},
            "bound_issuer": "",
            "default_role": "humans",
            "jwt_supported_algs": ["RS256"],
            "a_field_from_a_later_vault": "x",
        }
    }

    result = await _dispatch_vault(AUTH_JWT_OP_CONFIG_READ, {"mount": "oidc"})

    assert result.status == "ok", result.error
    assert result.result == {
        "mount": "oidc",
        "oidc_discovery_url": "https://idp.example.test/realms/meho",
        "oidc_client_id": "vault",
        "bound_issuer": "",
        "default_role": "humans",
        "jwt_supported_algs": ["RS256"],
    }


# ---------------------------------------------------------------------------
# Per-target Vault role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        pytest.param(
            vault_auth_jwt_role_write, {"role_name": _ROLE, "token_ttl": "1h"}, id="write"
        ),
        pytest.param(vault_auth_jwt_role_read, {"role_name": _ROLE}, id="read"),
        pytest.param(vault_auth_jwt_role_list, {}, id="list"),
        pytest.param(vault_auth_jwt_config_read, {}, id="config-read"),
    ],
)
async def test_handlers_log_in_with_the_target_role(
    monkeypatch: pytest.MonkeyPatch, handler: Any, params: dict[str, Any]
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.adapter.payloads[_ROLE_API_PATH] = {"data": dict(_ROLE_DATA)}
    fake.adapter.payloads["/v1/auth/jwt/config"] = {"data": {"default_role": _ROLE}}
    fake.adapter.list_payloads["/v1/auth/jwt/role"] = {"data": {"keys": [_ROLE]}}
    target = _make_vault_target(vault_role="meho-auth-admin", vault_mount="jwt-meho")

    await handler(_make_operator(jwt="op-jwt"), target, params)

    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


# ---------------------------------------------------------------------------
# Write preflight
# ---------------------------------------------------------------------------


def test_write_preflight_is_registered() -> None:
    assert AUTH_JWT_OP_ROLE_WRITE in _PERMISSION_PREFLIGHTS
    for op_id in (AUTH_JWT_OP_ROLE_READ, AUTH_JWT_OP_ROLE_LIST, AUTH_JWT_OP_CONFIG_READ):
        assert op_id not in _PERMISSION_PREFLIGHTS


async def test_write_preflight_probes_the_role_path_on_the_given_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {"auth/oidc/role/humans": ["create", "read", "update"]}
    target = _make_vault_target(vault_role="meho-auth-admin", vault_mount="jwt-meho")

    result = await vault_auth_jwt_role_write_capability_preflight(
        _make_operator(jwt="op-jwt"),
        {"mount": "oidc", "role_name": "humans", "token_ttl": "1h"},
        target=target,
    )

    assert result is not None
    assert result["path"] == "auth/oidc/role/humans"
    assert result["required"] == ["create", "read", "update"]
    assert result["will_be_denied"] is False
    assert fake.auth.jwt.login_calls == [_TARGET_LOGIN]


async def test_write_without_rights_is_refused_before_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_ROLE_AUTH_PATH: ["read"]}

    result = await _dispatch_vault(
        AUTH_JWT_OP_ROLE_WRITE, {"role_name": _ROLE, "token_ttl": "4h"}, approved=False
    )

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == _ROLE_AUTH_PATH
    assert await _approval_rows_for(AUTH_JWT_OP_ROLE_WRITE) == 0
    assert fake.adapter.post_calls == []


async def test_write_with_rights_waits_for_approval(
    monkeypatch: pytest.MonkeyPatch, _registered_vault_typed_ops: None
) -> None:
    """With the rights in place the write waits for approval; nothing reaches Vault."""
    fake = install_fake_client(monkeypatch)
    fake.sys.capabilities_by_path = {_ROLE_AUTH_PATH: ["create", "read", "update"]}

    result = await _dispatch_vault(
        AUTH_JWT_OP_ROLE_WRITE, {"role_name": _ROLE, "token_ttl": "4h"}, approved=False
    )

    assert result.status == "awaiting_approval", result.error
    assert await _approval_rows_for(AUTH_JWT_OP_ROLE_WRITE) == 1
    assert fake.adapter.post_calls == []
