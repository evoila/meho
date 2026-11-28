# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret writes to Vault while an admin blocks the caller.

* ``secret.move`` through ``dispatch``: the caller is blocked right after the
  Vault login of the sink. The write (plain, and the merge with its read) is
  not sent, and the answer is the block refusal.
* The ``vault-target`` sink (a registered Vault target's login): the same
  write and merge are not sent, and the block refusal is raised.
* The pfSense VPN user's password store: the write is not sent, and the run
  holds the block refusal that the dispatcher answers with.

Vault is faked in-process (``tests/_vault_fakes.py``). All names and values
are obvious fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth import vault as auth_vault
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import PrincipalBlockedStepError, reset_block_cache
from meho_backplane.auth.principal_block_run import raise_if_step_was_refused, tracking_run
from meho_backplane.connectors.pfsense import ops_openvpn_user
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret import vault_target_endpoint
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.connectors.secret.vault_target_endpoint import VaultTargetKvSecretEndpoint
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._principal_block_send_helpers import add_tenant, block_after_login
from ._vault_fakes import _FakeClient, install_fake_vault

pytestmark = pytest.mark.asyncio

_TENANT_ID = UUID("00000000-0000-0000-0000-0000000b1c03")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    reset_block_cache()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
async def tenant() -> AsyncIterator[None]:
    await add_tenant(_TENANT_ID)
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    vault = install_fake_vault(monkeypatch)
    vault.secrets.kv.v2.secret = {"password": "made-up-value-0zzz"}
    return vault


def _caller() -> Operator:
    return Operator(
        sub="blocked-secret-mover",
        name=None,
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _running() -> Any:
    caller = _caller()
    return tracking_run(is_step=False, login=(caller.sub, caller.tenant_id))


def _is_block_refusal(result: OperationResult) -> bool:
    return result.status == "denied" and (result.error or "").startswith("principal_blocked")


# ---------------------------------------------------------------------------
# secret.move to a vault sink, through dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("merge", [False, True], ids=["put", "merge"])
async def test_move_write_to_vault_is_not_sent_once_the_caller_is_blocked(
    fake: _FakeClient, tenant: None, monkeypatch: pytest.MonkeyPatch, merge: bool
) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_secret_broker_operations(embedding_service=embedding)
    # Open 1 reads the source; open 2 is the sink's login.
    opens = block_after_login(
        monkeypatch, auth_vault, "vault_client_for_operator", _caller(), on_open=2
    )

    result = await dispatch(
        operator=_caller(),
        connector_id="secret-broker-1.x",
        op_id="secret.move",
        target=None,
        params={
            "from": "vault:secret/db/prod#password",
            "to": "vault:secret/db/replica#password",
            "merge": merge,
        },
        _approved=True,
    )

    assert opens == [1, 2]
    assert [c["path"] for c in fake.secrets.kv.v2.read_calls] == ["secret/db/prod"]
    assert fake.secrets.kv.v2.put_calls == []
    assert _is_block_refusal(result), (result.status, result.error)


# ---------------------------------------------------------------------------
# The vault-target sink
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("merge", [False, True], ids=["put", "merge"])
async def test_vault_target_sink_write_is_not_sent_once_the_caller_is_blocked(
    fake: _FakeClient, tenant: None, monkeypatch: pytest.MonkeyPatch, merge: bool
) -> None:
    async def _target_row(self: Any, operator: Operator, op_id: str) -> Any:
        return SimpleNamespace(name="vault-prod")

    def _target_login(operator: Operator, target: Any) -> AbstractAsyncContextManager[Any]:
        return auth_vault.vault_client_for_operator(operator)

    monkeypatch.setattr(VaultTargetKvSecretEndpoint, "check_access", _target_row)
    monkeypatch.setattr(vault_target_endpoint, "vault_client_for_target", _target_login)
    opens = block_after_login(
        monkeypatch, vault_target_endpoint, "vault_client_for_target", _caller()
    )
    sink = VaultTargetKvSecretEndpoint("vault-prod/secret/db/replica#password")
    write = sink.write_secret_merged if merge else sink.write_secret

    with _running(), pytest.raises(PrincipalBlockedStepError):
        await write(_caller(), SecretMaterial("made-up-value-0zzz"))

    assert opens == [1]
    assert len(fake.auth.jwt.login_calls) == 1
    assert fake.secrets.kv.v2.read_calls == []
    assert fake.secrets.kv.v2.put_calls == []


# ---------------------------------------------------------------------------
# The pfSense VPN user's password
# ---------------------------------------------------------------------------


async def test_pfsense_password_store_is_not_sent_once_the_caller_is_blocked(
    fake: _FakeClient, tenant: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    opens = block_after_login(monkeypatch, ops_openvpn_user, "vault_client_for_operator", _caller())

    with _running():
        with pytest.raises(RuntimeError, match="PrincipalBlockedStepError"):
            await ops_openvpn_user._store_password(
                _caller(), "secret", "vpn/users/alice", "password", "made-up-pw", (0, {})
            )
        with pytest.raises(PrincipalBlockedStepError):
            raise_if_step_was_refused()

    assert opens == [1]
    assert len(fake.auth.jwt.login_calls) == 1
    assert fake.secrets.kv.v2.put_calls == []
