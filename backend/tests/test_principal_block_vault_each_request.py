# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each Vault request of a running call passes the block check.

The real ``hvac`` client talks to a stand-in Vault on the local host
(``tests/_vault_http_standin.py``). A real block row is written while one
request is on the wire, or while the call waits for a free worker thread.
The next request of the call is then not sent.

* A merge write of ``secret.move`` and a ``vault.kv.patch``: the caller is
  blocked during the read, and the write is not sent.
* A call that waits for a worker thread: the caller is blocked while it
  waits, and its request is not sent.
* The shared credential reader: the caller is blocked during the Vault
  login, and the read is not sent.
* ``vault.kv.read`` through ``dispatch``: the answer says ``not_started``
  when no Vault request was sent, and ``uncertain`` after one.
* ``vault.kv.put`` that waits for approval, through ``dispatch``: the caller
  is blocked during the Vault login of the permission check. The
  capabilities request is not sent, the answer is the block refusal, and
  no approval request is made.

The end of MEHO's own Vault login is a clean-up: it is always sent.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth import vault as auth_vault
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import PrincipalBlockedStepError, reset_block_cache
from meho_backplane.auth.principal_block_run import request_was_sent, tracking_run
from meho_backplane.auth.send_check import to_thread_checked
from meho_backplane.connectors._shared.vault_creds import VaultCredentialBackend
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.connectors.secret.vault_endpoint import VaultKvSecretEndpoint
from meho_backplane.connectors.vault import VaultConnector, register_vault_typed_operations
from meho_backplane.connectors.vault import ops as vault_ops
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._principal_block_send_helpers import add_tenant, block_the_caller
from ._vault_http_standin import VaultStandIn, vault_standin

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000b1c03")
_ITEM = f"tenants/{_TENANT_ID}/app"
_ITEM_KEY = f"secret/{_ITEM}"
_DATA = f"/v1/secret/data/{_ITEM}"


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
    yield
    get_settings.cache_clear()


@pytest.fixture
async def vault(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[VaultStandIn]:
    await add_tenant(_TENANT_ID)
    with vault_standin(monkeypatch) as standin:
        standin.items[_ITEM_KEY] = (1, {"user": "made-up-user"})
        yield standin


def _caller() -> Operator:
    return Operator(
        sub="blocked-vault-caller",
        name=None,
        email=None,
        raw_jwt="made.up.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _running() -> Any:
    return tracking_run(is_step=False, login=(_caller().sub, _TENANT_ID))


async def _block() -> None:
    await block_the_caller(_caller())


# ---------------------------------------------------------------------------
# A block during the read: the write of the same library call is not sent
# ---------------------------------------------------------------------------


async def test_merge_write_is_not_sent_when_the_caller_is_blocked_during_the_read(
    vault: VaultStandIn,
) -> None:
    vault.on_request("GET", _DATA, _block)
    endpoint = VaultKvSecretEndpoint(f"{_ITEM}#password")

    with _running(), pytest.raises(PrincipalBlockedStepError):
        await endpoint.write_secret_merged(_caller(), SecretMaterial(b"made-up-value"))

    assert vault.sent("GET", _DATA) == 1
    assert vault.sent("POST", _DATA) == 0
    assert vault.items[_ITEM_KEY] == (1, {"user": "made-up-user"})
    assert vault.sent("POST", "/v1/auth/token/revoke-self") == 1


async def test_kv_patch_write_is_not_sent_when_the_caller_is_blocked_during_the_read(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.on_request("GET", _DATA, _block)
    params = {"path": _ITEM, "data": {"password": "made-up-value"}}

    with _running(), pytest.raises(PrincipalBlockedStepError):
        await vault_ops.vault_kv_patch(_caller(), None, params)

    assert vault.sent("GET", _DATA) == 1
    assert vault.sent("POST", _DATA) == 0
    assert vault.sent("PATCH", _DATA) == 0
    assert vault.items[_ITEM_KEY] == (1, {"user": "made-up-user"})


async def test_both_requests_are_sent_when_nobody_blocks_the_caller(vault: VaultStandIn) -> None:
    endpoint = VaultKvSecretEndpoint(f"{_ITEM}#password")

    with _running():
        await endpoint.write_secret_merged(_caller(), SecretMaterial(b"made-up-value"))

    assert vault.sent("POST", _DATA) == 1
    assert vault.items[_ITEM_KEY] == (2, {"user": "made-up-user", "password": "made-up-value"})


# ---------------------------------------------------------------------------
# A block while the call waits for a worker thread
# ---------------------------------------------------------------------------


async def _with_one_busy_worker(work: Any) -> tuple[asyncio.Task[Any], threading.Event]:
    """Start *work* while the only worker thread is busy; return its task and the release."""
    loop = asyncio.get_running_loop()
    release = threading.Event()
    loop.run_in_executor(None, release.wait)
    task = asyncio.ensure_future(work)
    for _ in range(20):
        await asyncio.sleep(0)
    return task, release


@pytest.fixture
async def one_worker() -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    yield
    loop.set_default_executor(ThreadPoolExecutor())
    executor.shutdown(wait=False)


async def test_login_blocked_while_waiting_for_a_worker_is_not_sent(
    vault: VaultStandIn, one_worker: None
) -> None:
    async def _open() -> None:
        async with auth_vault.vault_client_for_operator(_caller()):
            pass

    with _running():
        task, release = await _with_one_busy_worker(_open())
        await _block()
        release.set()
        with pytest.raises(PrincipalBlockedStepError):
            await task

    assert vault.sent("POST", "/v1/auth/jwt/login") == 0
    assert vault.requests == []


async def test_write_blocked_while_waiting_for_a_worker_is_not_sent(
    vault: VaultStandIn, one_worker: None
) -> None:
    with _running():
        async with auth_vault.vault_client_for_operator(_caller()) as client:
            write = to_thread_checked(
                client.secrets.kv.v2.create_or_update_secret,
                path=_ITEM,
                secret={"password": "made-up-value"},
                mount_point="secret",
            )
            task, release = await _with_one_busy_worker(write)
            await _block()
            release.set()
            with pytest.raises(PrincipalBlockedStepError):
                await task

    assert vault.sent("POST", "/v1/auth/jwt/login") == 1
    assert vault.sent("POST", _DATA) == 0
    assert vault.sent("POST", "/v1/auth/token/revoke-self") == 1


# ---------------------------------------------------------------------------
# The shared credential reader
# ---------------------------------------------------------------------------


async def test_credential_read_is_not_sent_when_the_caller_is_blocked_during_the_login(
    vault: VaultStandIn,
) -> None:
    vault.on_request("POST", "/v1/auth/jwt/login", _block)

    with _running(), pytest.raises(PrincipalBlockedStepError):
        await VaultCredentialBackend().load_secret_data(_ITEM, _caller(), target_name="harbor-1")

    assert vault.sent("POST", "/v1/auth/jwt/login") == 1
    assert vault.sent("GET", _DATA) == 0
    assert vault.sent("POST", "/v1/auth/token/revoke-self") == 1


async def test_credential_read_runs_when_nobody_blocks_the_caller(vault: VaultStandIn) -> None:
    with _running():
        data = await VaultCredentialBackend().load_secret_data(
            _ITEM, _caller(), target_name="harbor-1"
        )
        assert request_was_sent()

    assert data == {"user": "made-up-user"}
    assert vault.sent("GET", _DATA) == 1


# ---------------------------------------------------------------------------
# Through dispatch: the outcome follows the Vault requests that were sent
# ---------------------------------------------------------------------------


@pytest.fixture
async def registered(vault: VaultStandIn) -> AsyncIterator[None]:
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    reset_dispatcher_caches()
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_vault_typed_operations(embedding_service=embedding)
    yield
    reset_dispatcher_caches()
    clear_registry()


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool) -> OperationResult:
    return await dispatch(
        operator=_caller(),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=approved,
    )


def _is_block_refusal(result: OperationResult) -> bool:
    return result.status == "denied" and (result.error or "").startswith("principal_blocked")


async def test_kv_read_blocked_before_the_first_vault_request_says_not_started(
    vault: VaultStandIn, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = vault_ops.vault_client_for_target

    def _block_first(operator: Operator, target: Any) -> Any:
        class _Opener:
            async def __aenter__(self) -> Any:
                await _block()
                self._inner = real(operator, target)
                return await self._inner.__aenter__()

            async def __aexit__(self, *exc: object) -> Any:
                return await self._inner.__aexit__(*exc)

        return _Opener()

    monkeypatch.setattr(vault_ops, "vault_client_for_target", _block_first)

    result = await _dispatch("vault.kv.read", {"path": _ITEM}, approved=True)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "not_started"
    assert "may have run" not in (result.error or "")
    assert vault.requests == []


async def test_kv_read_blocked_after_the_vault_login_says_uncertain(
    vault: VaultStandIn, registered: None
) -> None:
    vault.on_request("POST", "/v1/auth/jwt/login", _block)

    result = await _dispatch("vault.kv.read", {"path": _ITEM}, approved=True)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "uncertain"
    assert vault.sent("POST", "/v1/auth/jwt/login") == 1
    assert vault.sent("GET", _DATA) == 0


async def test_preflight_of_a_call_that_waits_for_approval_stops_for_a_blocked_caller(
    vault: VaultStandIn, registered: None
) -> None:
    vault.on_request("POST", "/v1/auth/jwt/login", _block)
    params = {"path": _ITEM, "data": {"password": "made-up-value"}}

    result = await _dispatch("vault.kv.put", params, approved=False)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "not_started"
    assert vault.sent("POST", "/v1/auth/jwt/login") == 1
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 0
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(ApprovalRequest))).scalars().all()
    assert rows == []


async def test_preflight_of_a_call_that_waits_for_approval_runs_for_a_caller_not_blocked(
    vault: VaultStandIn, registered: None
) -> None:
    params = {"path": _ITEM, "data": {"password": "made-up-value"}}

    result = await _dispatch("vault.kv.put", params, approved=False)

    assert result.status == "awaiting_approval", (result.status, result.error)
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 1
    assert vault.sent("POST", _DATA) == 0
