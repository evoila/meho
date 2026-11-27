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
* ``vault.kv.put`` that waits for approval, through ``dispatch``: a caller
  blocked during the Vault login of the permission check gets the block
  refusal with ``uncertain``; the capabilities request is not sent. A caller
  blocked before any request of the preparation gets ``not_started``. A block
  during the capabilities request, a block during a request of the op's own
  check, an unreadable block list and a block check that takes too long
  refuse too. No approval request is made in any of them, and the audit row
  has the status of the answer.

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

from meho_backplane.auth import principal_block as principal_block_module
from meho_backplane.auth import send_check as send_check_module
from meho_backplane.auth import vault as auth_vault
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import (
    PrincipalBlockedStepError,
    PrincipalBlockUnavailableError,
    reset_block_cache,
)
from meho_backplane.auth.principal_block_run import (
    request_was_sent,
    step_refused_in_run,
    tracking_run,
)
from meho_backplane.auth.send_check import check_before_send, to_thread_checked
from meho_backplane.connectors._shared.vault_creds import VaultCredentialBackend
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.secret.endpoints import SecretMaterial
from meho_backplane.connectors.secret.vault_endpoint import VaultKvSecretEndpoint
from meho_backplane.connectors.vault import VaultConnector, register_vault_typed_operations
from meho_backplane.connectors.vault import ops as vault_ops
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import _park_check as park_check_module
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations import dispatcher as dispatcher_module
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


async def _approval_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


async def _kv_put_row() -> AuditLog:
    async with get_sessionmaker()() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.path == "vault.kv.put"))
        (row,) = rows.scalars().all()
    return row


_PUT = {"path": _ITEM, "data": {"password": "made-up-value"}}


async def test_block_during_the_login_of_the_approval_preparation_says_uncertain(
    vault: VaultStandIn, registered: None
) -> None:
    vault.on_request("POST", "/v1/auth/jwt/login", _block)

    result = await _dispatch("vault.kv.put", dict(_PUT), approved=False)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "uncertain"
    assert vault.sent("POST", "/v1/auth/jwt/login") == 1
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 0
    assert await _approval_requests() == []
    row = await _kv_put_row()
    assert row.status_code == 403
    assert row.payload["error"]["execution_outcome"] == "uncertain"


async def test_block_before_any_request_of_the_approval_preparation_says_not_started(
    vault: VaultStandIn, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = dispatcher_module.park_check_refusal

    async def _block_then_check(**kwargs: Any) -> Any:
        await _block()
        return await real(**kwargs)

    monkeypatch.setattr(dispatcher_module, "park_check_refusal", _block_then_check)

    result = await _dispatch("vault.kv.put", dict(_PUT), approved=False)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "not_started"
    assert vault.requests == []
    assert await _approval_requests() == []
    assert (await _kv_put_row()).status_code == 403


async def test_block_during_the_last_request_of_the_preparation_makes_no_approval_request(
    vault: VaultStandIn, registered: None
) -> None:
    vault.on_request("POST", "/v1/sys/capabilities-self", _block)

    result = await _dispatch("vault.kv.put", dict(_PUT), approved=False)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "uncertain"
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 1
    assert await _approval_requests() == []


async def test_requests_of_the_ops_own_check_before_approval_pass_the_block_check(
    vault: VaultStandIn, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _check_that_reads_vault(ctx: Any) -> str | None:
        async with auth_vault.vault_client_for_operator(ctx.operator) as client:
            await to_thread_checked(
                client.secrets.kv.v2.read_secret_version, path=_ITEM, mount_point="secret"
            )
        return None

    monkeypatch.setitem(park_check_module._PARK_CHECKS, "vault.kv.put", _check_that_reads_vault)
    vault.on_request("POST", "/v1/auth/jwt/login", _block)

    result = await _dispatch("vault.kv.put", dict(_PUT), approved=False)

    assert _is_block_refusal(result), (result.status, result.error)
    assert result.extras["execution_outcome"] == "uncertain"
    assert vault.sent("GET", _DATA) == 0
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 0
    assert await _approval_requests() == []


def _list_unreadable_after(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch, *, slow: bool = False
) -> None:
    """From the Vault login on, reading the block list fails (or takes too long)."""
    real = principal_block_module.first_blocked_step_login
    broken: list[bool] = []

    async def _read(*args: Any, **kwargs: Any) -> Any:
        if broken:
            if slow:
                await asyncio.sleep(5)
            raise PrincipalBlockUnavailableError
        return await real(*args, **kwargs)

    async def _break() -> None:
        broken.append(True)

    monkeypatch.setattr(principal_block_module, "first_blocked_step_login", _read)
    vault.on_request("POST", "/v1/auth/jwt/login", _break)


async def test_unreadable_block_list_during_the_preparation_is_audited_as_an_error(
    vault: VaultStandIn, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _list_unreadable_after(vault, monkeypatch)

    result = await _dispatch("vault.kv.put", dict(_PUT), approved=False)

    assert result.status == "error", (result.status, result.error)
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    assert result.extras["execution_outcome"] == "uncertain"
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 0
    assert await _approval_requests() == []
    row = await _kv_put_row()
    assert row.status_code == 500
    assert row.payload["error"]["error_code"] == "principal_block_check_unavailable"


async def test_block_check_that_takes_too_long_stops_the_preparation_and_later_sends(
    vault: VaultStandIn, registered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(send_check_module, "_CHECK_WAIT_SECONDS", 0.2)
    _list_unreadable_after(vault, monkeypatch, slow=True)

    result = await _dispatch("vault.kv.put", dict(_PUT), approved=False)

    assert result.status == "error", (result.status, result.error)
    assert result.extras["error_code"] == "principal_block_check_unavailable"
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 0
    assert await _approval_requests() == []
    assert (await _kv_put_row()).status_code == 500


async def test_a_timed_out_check_refuses_every_later_send_of_the_run(
    vault: VaultStandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(send_check_module, "_CHECK_WAIT_SECONDS", 0.2)

    async def _slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(principal_block_module, "first_blocked_step_login", _slow)

    with _running():
        with pytest.raises(PrincipalBlockedStepError) as first:
            await to_thread_checked(lambda: None)
        assert first.value.unavailable
        assert isinstance(step_refused_in_run(), PrincipalBlockedStepError)
        # A caller that caught the error does not go on: the next send is refused
        # without a new read of the list.
        with pytest.raises(PrincipalBlockedStepError):
            await check_before_send()


async def test_preflight_of_a_call_that_waits_for_approval_runs_for_a_caller_not_blocked(
    vault: VaultStandIn, registered: None
) -> None:
    params = {"path": _ITEM, "data": {"password": "made-up-value"}}

    result = await _dispatch("vault.kv.put", params, approved=False)

    assert result.status == "awaiting_approval", (result.status, result.error)
    assert vault.sent("POST", "/v1/sys/capabilities-self") == 1
    assert vault.sent("POST", _DATA) == 0
