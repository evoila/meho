# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.token.create_and_store``: make a Vault token and store it, never show it.

What these tests prove:

* **Registration.** The op is ``dangerous``, needs approval, sits in the
  ``token`` group and is pinned ``credential_mint``; it has a preview
  builder and a permission preflight; the flight recorder keeps no bodies.
* **Vault KV store.** The token is made from the role under the target's
  Vault role, checked to be an orphan, and written with the ``vault.kv.patch``
  handler (``vault.kv.put`` for a new path). The result has the accessor,
  expiry, policies and destination only.
* **Kubernetes store.** The token is written to one key of an existing
  Secret through the ``k8s.secret.update`` handler in ``patch`` mode.
* **Checks before the token exists.** A refused tenant check, a missing
  target, a refused MEHO policy and a missing Secret all stop the call
  before Vault is asked for a token.
* **Clean up.** A failed write, a cancellation and ``orphan: false`` revoke
  the new token with the token itself; when that revoke fails the error
  names the accessor and ``vault.token.revoke_accessor``.
* **No token anywhere.** The token is in no result, audit row (``payload``
  and ``raw_payload``), approval preview or log line.
* **Before approval.** Without ``update`` on ``auth/token/create/<role>`` (or
  without the KV write rights) MEHO does not ask for approval.

Vault is faked in-process (``tests/_vault_fakes.py``); the Kubernetes API is
a fake ``CoreV1Api``. All names and values are obvious fakes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import hvac.exceptions
import pytest
import structlog
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import _CREDENTIAL_MINT_OPS, classify_op
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    VaultConnector,
    register_vault_identity_token_operations,
    register_vault_typed_operations,
)
from meho_backplane.connectors.vault.ops_token import TOKEN_WHEN_TO_USE
from meho_backplane.connectors.vault.ops_token_store import (
    TOKEN_STORE_OP,
    vault_token_create_and_store,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
    Target,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import _PERMISSION_PREFLIGHTS, _PREVIEW_BUILDERS
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from meho_backplane.settings import get_settings

from ._vault_fakes import _FakeClient, install_fake_client

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000004959")
_ROLE = "ci-runner"
_CREATE_PATH = f"auth/token/create/{_ROLE}"
_KV_PATH = f"tenants/{_TENANT_ID}/ci/vault"
_KV_AUTH_PATH = f"secret/data/{_KV_PATH}"
_K8S_TARGET = "cluster-a"
_ACCESSOR = "fake-accessor-for-store"

_KV_PARAMS: dict[str, Any] = {
    "role_name": _ROLE,
    "ttl": "24h",
    "store": {"vault_kv": {"path": _KV_PATH}},
}
_K8S_PARAMS: dict[str, Any] = {
    "role_name": _ROLE,
    "store": {
        "k8s_secret": {
            "target": _K8S_TARGET,
            "namespace": "ci",
            "name": "vault-token",
            "key": "token",
        }
    },
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def _registered() -> AsyncIterator[None]:
    """Vault + Kubernetes descriptor rows (the k8s policy gate reads k8s.secret.update)."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_vault_typed_operations(embedding_service=embedding)
    await register_vault_identity_token_operations(embedding_service=embedding)
    from meho_backplane.operations import typed_register

    with patch.object(typed_register, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = install_fake_client(monkeypatch)
    client.auth.token.minted_accessor = _ACCESSOR
    return client


def _token(fake: _FakeClient) -> str:
    return fake.auth.token.minted_client_token


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _run(params: dict[str, Any], *, approved: bool = True) -> OperationResult:
    return await dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id=TOKEN_STORE_OP,
        target=None,
        params=params,
        _approved=approved,
    )


async def _audit_blob() -> str:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return json.dumps([[r.payload, r.raw_payload] for r in rows], default=str)


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _made_no_token(fake: _FakeClient) -> bool:
    return not fake.auth.token.create_calls and not fake.auth.token.create_orphan_calls


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_op_is_dangerous_needs_approval_and_is_a_credential_mint(_registered: None) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == TOKEN_STORE_OP)
            )
        ).scalar_one()
        group_key = (
            await session.execute(
                select(OperationGroup.group_key).where(OperationGroup.id == row.group_id)
            )
        ).scalar_one()
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True
    assert group_key == "token"
    assert TOKEN_STORE_OP in _CREDENTIAL_MINT_OPS
    assert classify_op(TOKEN_STORE_OP) == "credential_mint"


def test_preview_preflight_group_text_and_flight_recorder() -> None:
    assert TOKEN_STORE_OP in _PREVIEW_BUILDERS
    assert TOKEN_STORE_OP in _PERMISSION_PREFLIGHTS
    assert TOKEN_STORE_OP in TOKEN_WHEN_TO_USE
    exclusion = classify_body_exclusion(TOKEN_STORE_OP)
    assert exclusion.excluded


# ---------------------------------------------------------------------------
# Vault KV store
# ---------------------------------------------------------------------------


async def test_kv_store_writes_the_token_and_returns_no_token(
    fake: _FakeClient, _registered: None
) -> None:
    with structlog.testing.capture_logs() as logs:
        result = await _run(_KV_PARAMS)

    assert result.status == "ok", result.error
    out = result.result
    assert out["accessor"] == _ACCESSOR
    assert out["ttl_seconds"] == 86400
    assert out["expire_time"] is not None
    assert out["stored_to"] == {
        "kind": "vault_kv",
        "mount": "secret",
        "path": _KV_PATH,
        "field": "token",
        "version": fake.secrets.kv.v2.version + 1,
    }
    assert set(out) == {"accessor", "expire_time", "ttl_seconds", "policies", "stored_to"}
    # Made from the role, under MEHO's login; written with the patch handler.
    assert fake.auth.token.create_calls == [{"role_name": _ROLE, "ttl": "24h"}]
    assert fake.secrets.kv.v2.patch_calls == [
        {"path": _KV_PATH, "secret": {"token": _token(fake)}, "mount_point": "secret"}
    ]
    # Not revoked: the token must stay valid after the call.
    assert _token(fake) not in fake.auth.token.revoked_tokens
    # Nowhere else.
    assert _token(fake) not in json.dumps(result.model_dump(mode="json"))
    assert _token(fake) not in await _audit_blob()
    assert TOKEN_STORE_OP in await _audit_blob()
    assert _token(fake) not in repr(logs)


async def test_kv_store_uses_put_when_the_path_is_new(fake: _FakeClient, _registered: None) -> None:
    fake.secrets.kv.v2.patch_exc = hvac.exceptions.InvalidPath("no secret")

    result = await _run({**_KV_PARAMS, "store": {"vault_kv": {"path": _KV_PATH, "field": "t"}}})

    assert result.status == "ok", result.error
    assert fake.secrets.kv.v2.put_calls == [
        {"path": _KV_PATH, "secret": {"t": _token(fake)}, "cas": None, "mount_point": "secret"}
    ]
    assert result.result["stored_to"]["field"] == "t"


async def test_without_a_role_the_token_comes_from_create_orphan(
    fake: _FakeClient, _registered: None
) -> None:
    params = {"policies": ["ci-read"], "ttl_period": "1h", "store": _KV_PARAMS["store"]}

    result = await _run(params)

    assert result.status == "ok", result.error
    assert fake.auth.token.create_calls == []
    assert fake.auth.token.create_orphan_calls == [{"policies": ["ci-read"], "period": "1h"}]
    assert result.result["policies"] == ["ci-read"]


async def test_a_periodic_token_without_expiry_has_no_expire_time(
    fake: _FakeClient, _registered: None
) -> None:
    fake.auth.token.minted_lease_duration = 0

    result = await _run(_KV_PARAMS)

    assert result.status == "ok", result.error
    assert result.result["expire_time"] is None
    assert result.result["ttl_seconds"] == 0


async def test_tokens_log_in_with_the_vault_targets_role(fake: _FakeClient) -> None:
    from datetime import UTC, datetime

    from meho_backplane.connectors.schemas import AuthModel
    from meho_backplane.targets.schemas import Target as TargetSchema

    now = datetime.now(UTC)
    target = TargetSchema(
        id=uuid.uuid4(),
        tenant_id=_TENANT_ID,
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
        extras={"vault_role": "meho-token-maker", "vault_mount": "jwt-meho"},
        notes=None,
        fingerprint=None,
        preferred_impl_id=None,
        created_at=now,
        updated_at=now,
    )

    await vault_token_create_and_store(_operator(), target, dict(_KV_PARAMS))

    roles = {call["role"] for call in fake.auth.jwt.login_calls}
    assert roles == {"meho-token-maker"}


# ---------------------------------------------------------------------------
# Clean up after a failure
# ---------------------------------------------------------------------------


async def test_a_failed_write_revokes_the_new_token(fake: _FakeClient, _registered: None) -> None:
    fake.secrets.kv.v2.patch_exc = hvac.exceptions.Forbidden("permission denied")

    with structlog.testing.capture_logs() as logs:
        result = await _run(_KV_PARAMS)

    assert result.status == "error"
    assert _token(fake) in fake.auth.token.revoked_tokens
    remediation = result.extras["remediation"]
    assert "MEHO revoked the new token" in remediation
    assert "Forbidden" in remediation
    dumped = json.dumps(result.model_dump(mode="json"))
    assert _token(fake) not in dumped
    assert _token(fake) not in await _audit_blob()
    assert _token(fake) not in repr(logs)


async def test_a_failed_write_and_a_failed_revoke_name_the_accessor(
    fake: _FakeClient, _registered: None
) -> None:
    fake.secrets.kv.v2.patch_exc = hvac.exceptions.Forbidden("permission denied")
    fake.auth.token.raise_on_revoke = hvac.exceptions.Forbidden("revoke refused")

    result = await _run(_KV_PARAMS)

    assert result.status == "error"
    remediation = result.extras["remediation"]
    assert "could NOT revoke" in remediation
    assert _ACCESSOR in remediation
    assert "vault.token.revoke_accessor" in remediation
    assert _token(fake) not in json.dumps(result.model_dump(mode="json"))


async def test_a_token_with_a_parent_is_revoked_and_the_call_fails(
    fake: _FakeClient, _registered: None
) -> None:
    fake.auth.token.minted_orphan = False

    result = await _run(_KV_PARAMS)

    assert result.status == "error"
    assert _token(fake) in fake.auth.token.revoked_tokens
    assert "orphan=true" in result.extras["remediation"]
    assert fake.secrets.kv.v2.patch_calls == []
    assert fake.secrets.kv.v2.put_calls == []
    assert _token(fake) not in json.dumps(result.model_dump(mode="json"))


async def test_a_cancellation_during_the_write_still_revokes(
    fake: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meho_backplane.connectors.vault import ops as kv_ops

    async def _cancelled(*_args: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(kv_ops, "vault_kv_patch", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await vault_token_create_and_store(_operator(), None, dict(_KV_PARAMS))

    assert _token(fake) in fake.auth.token.revoked_tokens


# ---------------------------------------------------------------------------
# Checks before the token exists
# ---------------------------------------------------------------------------


async def test_a_refused_tenant_check_makes_no_token(fake: _FakeClient, _registered: None) -> None:
    other = {"vault_kv": {"path": "tenants/someone-else/ci/vault"}}

    result = await _run({**_KV_PARAMS, "store": other})

    assert result.status == "error"
    assert result.extras["exception_class"] == "TokenStoreError"
    assert "No token was made" in result.extras["remediation"]
    assert _made_no_token(fake)


async def test_a_path_with_the_mount_in_it_makes_no_token(
    fake: _FakeClient, _registered: None
) -> None:
    result = await _run({**_KV_PARAMS, "store": {"vault_kv": {"path": f"secret/{_KV_PATH}"}}})

    assert result.status == "error"
    assert "No token was made" in result.extras["remediation"]
    assert _made_no_token(fake)


def test_store_must_name_exactly_one_destination() -> None:
    from meho_backplane.connectors.vault.ops_token_store_schemas import (
        VAULT_TOKEN_CREATE_AND_STORE_PARAMETER_SCHEMA,
    )
    from meho_backplane.operations._validate import validate_params

    schema = VAULT_TOKEN_CREATE_AND_STORE_PARAMETER_SCHEMA

    both = {"store": {**_KV_PARAMS["store"], **_K8S_PARAMS["store"]}}
    assert validate_params(schema, both)
    assert validate_params(schema, {"store": {}})
    assert not validate_params(schema, {"store": _KV_PARAMS["store"]})


# ---------------------------------------------------------------------------
# Kubernetes Secret store
# ---------------------------------------------------------------------------


async def _seed_target(name: str, product: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            Target(
                tenant_id=_TENANT_ID,
                name=name,
                aliases=[],
                product=product,
                host=f"{name}.test.invalid",
                port=6443,
                secret_ref=f"{product}/{name}",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes="seeded by test_connectors_vault_token_store.py",
            )
        )
        await session.commit()


def _secret(*, immutable: bool | None = None) -> k8s.V1Secret:
    return k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(name="vault-token", namespace="ci", resource_version="3"),
        type="Opaque",
        data={"token": "b2xk", "other": "a2VlcA=="},
        immutable=immutable,
    )


@pytest.fixture
def core_v1(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """A fake ``CoreV1Api`` behind a fake API client for every Kubernetes target."""
    monkeypatch.setattr(
        KubernetesConnector, "_get_api_client", AsyncMock(return_value=MagicMock(close=AsyncMock()))
    )
    api = MagicMock()
    api.read_namespaced_secret = AsyncMock(return_value=_secret())
    api.patch_namespaced_secret = AsyncMock()
    with patch("meho_backplane.connectors.kubernetes.ops_secret_write.client.CoreV1Api") as cls:
        cls.return_value = api
        yield api


async def test_k8s_store_patches_one_key_and_returns_no_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    await _seed_target(_K8S_TARGET, "k8s")

    with structlog.testing.capture_logs() as logs:
        result = await _run(_K8S_PARAMS)

    assert result.status == "ok", result.error
    core_v1.patch_namespaced_secret.assert_awaited_once_with(
        name="vault-token", namespace="ci", body={"stringData": {"token": _token(fake)}}
    )
    assert result.result["stored_to"] == {
        "kind": "k8s_secret",
        "target": _K8S_TARGET,
        "namespace": "ci",
        "name": "vault-token",
        "key": "token",
    }
    assert _token(fake) not in fake.auth.token.revoked_tokens
    assert _token(fake) not in json.dumps(result.model_dump(mode="json"))
    assert _token(fake) not in await _audit_blob()
    assert _token(fake) not in repr(logs)


async def test_k8s_missing_secret_makes_no_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    await _seed_target(_K8S_TARGET, "k8s")
    core_v1.read_namespaced_secret.side_effect = ApiException(status=404, reason="Not Found")

    result = await _run(_K8S_PARAMS)

    assert result.status == "error"
    assert "does not exist" in result.extras["remediation"]
    assert "No token was made" in result.extras["remediation"]
    assert _made_no_token(fake)
    core_v1.patch_namespaced_secret.assert_not_awaited()


async def test_k8s_immutable_secret_makes_no_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    await _seed_target(_K8S_TARGET, "k8s")
    core_v1.read_namespaced_secret.return_value = _secret(immutable=True)

    result = await _run(_K8S_PARAMS)

    assert result.status == "error"
    assert "immutable" in result.extras["remediation"]
    assert _made_no_token(fake)


async def test_k8s_unknown_target_makes_no_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    result = await _run(_K8S_PARAMS)

    assert result.status == "error"
    assert "no such target" in result.extras["remediation"]
    assert _made_no_token(fake)


async def test_k8s_store_on_a_non_kubernetes_target_makes_no_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    await _seed_target(_K8S_TARGET, "vault")

    result = await _run(_K8S_PARAMS)

    assert result.status == "error"
    assert "not a Kubernetes target" in result.extras["remediation"]
    assert _made_no_token(fake)


async def test_k8s_refused_policy_makes_no_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    """Outside an approved run, ``k8s.secret.update`` needs approval: refused."""
    await _seed_target(_K8S_TARGET, "k8s")

    with pytest.raises(Exception, match="policy_denied") as caught:
        await vault_token_create_and_store(_operator(), None, dict(_K8S_PARAMS))

    assert "No token was made" in str(caught.value)
    assert _made_no_token(fake)
    core_v1.read_namespaced_secret.assert_not_awaited()


async def test_k8s_failed_write_revokes_the_new_token(
    fake: _FakeClient, core_v1: MagicMock, _registered: None
) -> None:
    await _seed_target(_K8S_TARGET, "k8s")
    core_v1.patch_namespaced_secret.side_effect = ApiException(status=500, reason="boom")

    result = await _run(_K8S_PARAMS)

    assert result.status == "error"
    assert _token(fake) in fake.auth.token.revoked_tokens
    assert "MEHO revoked the new token" in result.extras["remediation"]
    assert _token(fake) not in json.dumps(result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Before approval: preview + permission preflight
# ---------------------------------------------------------------------------


async def test_without_update_on_the_create_path_no_approval_is_asked(
    fake: _FakeClient, _registered: None
) -> None:
    fake.sys.capabilities_by_path = {
        _CREATE_PATH: ["read"],
        _KV_AUTH_PATH: ["create", "read", "update"],
    }

    result = await _run(_KV_PARAMS, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == _CREATE_PATH
    assert result.extras["required"] == ["update"]
    assert await _approval_rows() == []
    assert _made_no_token(fake)


async def test_without_the_kv_write_rights_no_approval_is_asked(
    fake: _FakeClient, _registered: None
) -> None:
    fake.sys.capabilities_by_path = {_CREATE_PATH: ["update"], _KV_AUTH_PATH: ["read", "update"]}

    result = await _run(_KV_PARAMS, approved=False)

    assert result.status == "denied"
    assert result.extras["error_code"] == "write_capability_denied"
    assert result.extras["path"] == _KV_AUTH_PATH
    assert result.extras["required"] == ["create", "read", "update"]
    assert await _approval_rows() == []


async def test_without_a_role_the_preflight_checks_create_orphan(
    fake: _FakeClient, _registered: None
) -> None:
    fake.sys.capabilities_by_path = {"auth/token/create-orphan": []}
    params = {"store": {"k8s_secret": _K8S_PARAMS["store"]["k8s_secret"]}}

    result = await _run(params, approved=False)

    assert result.status == "denied"
    assert result.extras["path"] == "auth/token/create-orphan"


async def test_with_the_rights_the_call_waits_and_the_preview_shows_no_token(
    fake: _FakeClient, _registered: None
) -> None:
    fake.sys.capabilities_by_path = {
        _CREATE_PATH: ["update"],
        _KV_AUTH_PATH: ["create", "read", "update"],
    }

    result = await _run({**_KV_PARAMS, "num_uses": 0}, approved=False)

    assert result.status == "awaiting_approval", result.error
    assert _made_no_token(fake)
    rows = await _approval_rows()
    assert len(rows) == 1
    effect = dict(rows[0].proposed_effect)
    preview = effect["preview"]
    assert preview["action"] == "create_and_store_token"
    assert preview["role_name"] == _ROLE
    assert preview["vault_path"] == _CREATE_PATH
    assert preview["ttl"] == "24h"
    assert preview["num_uses"] == 0
    assert preview["store"] == {"vault_kv": {"mount": "secret", "path": _KV_PATH, "field": "token"}}
    assert preview["token_value_returned"] is False
    assert effect["permission_preflight"]["will_be_denied"] is False
    assert _token(fake) not in json.dumps(effect, default=str)
