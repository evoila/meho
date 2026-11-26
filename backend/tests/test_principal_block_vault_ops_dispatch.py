# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault operations run through ``dispatch`` while an admin blocks the caller.

* ``vault.token.create_and_store``: the caller is blocked after the new token
  exists and before it is stored. The store is not made, MEHO revokes the
  new token, and the answer is the block refusal.
* ``vault.kv.read``: the caller is blocked after the Vault login. The read
  is not sent, and the answer is the block refusal.

Vault is faked in-process (``tests/_vault_fakes.py``). All names and values
are obvious fakes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth import vault as auth_vault
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import reset_block_cache
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.vault import (
    VaultConnector,
    ops_token_store,
    register_vault_identity_token_operations,
    register_vault_typed_operations,
)
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._principal_block_send_helpers import add_tenant, block_after_login, block_the_caller
from ._vault_fakes import _FakeClient, install_fake_client

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000b1c01")
_KV_PATH = f"tenants/{_TENANT_ID}/ci/vault"
_STORE_PARAMS: dict[str, Any] = {
    "role_name": "ci-runner",
    "ttl": "24h",
    "store": {"vault_kv": {"path": _KV_PATH}},
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    reset_block_cache()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
async def _registered() -> AsyncIterator[None]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_vault_typed_operations(embedding_service=embedding)
    await register_vault_identity_token_operations(embedding_service=embedding)
    await add_tenant(_TENANT_ID)
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    client = install_fake_client(monkeypatch, secret={"password": "made-up-value"})
    client.auth.token.minted_accessor = "fake-accessor-for-block"
    return client


def _caller() -> Operator:
    return Operator(
        sub="blocked-caller",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


async def _run(op_id: str, params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_caller(),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=True,
    )


def _is_block_refusal(result: OperationResult) -> bool:
    return result.status == "denied" and (result.error or "").startswith("principal_blocked")


async def test_token_store_revokes_the_new_token_when_the_caller_is_blocked_before_the_store(
    fake: _FakeClient, _registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mint = ops_token_store._mint

    async def _mint_then_block(operator: Operator, target: Any, params: Any) -> Any:
        minted = await real_mint(operator, target, params)
        await block_the_caller(operator)
        return minted

    monkeypatch.setattr(ops_token_store, "_mint", _mint_then_block)

    result = await _run(ops_token_store.TOKEN_STORE_OP, dict(_STORE_PARAMS))

    assert len(fake.auth.token.create_calls) == 1
    assert fake.secrets.kv.v2.patch_calls == []
    assert fake.secrets.kv.v2.put_calls == []
    assert fake.auth.token.minted_client_token in fake.auth.token.revoked_tokens
    assert _is_block_refusal(result), (result.status, result.error)


async def test_kv_read_is_not_sent_once_the_caller_is_blocked_after_the_login(
    fake: _FakeClient, _registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens = block_after_login(monkeypatch, auth_vault, "vault_client_for_operator", _caller())

    result = await _run("vault.kv.read", {"path": f"tenants/{_TENANT_ID}/app"})

    assert opens == [1]
    assert len(fake.auth.jwt.login_calls) == 1
    assert fake.secrets.kv.v2.read_calls == []
    assert _is_block_refusal(result), (result.status, result.error)
