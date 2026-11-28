# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A blocked login gets the block refusal before the next Vault call or store request.

Each test runs a call with two sends, blocks the login between them, and
checks that the second send is not made and the block refusal is raised.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth import vault as auth_vault
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.principal_block import (
    PrincipalBlockedStepError,
    block_principal,
    reset_block_cache,
)
from meho_backplane.auth.principal_block_run import tracking_run
from meho_backplane.auth.send_check import to_thread_checked
from meho_backplane.connectors.secret import handoff as handoff_module
from meho_backplane.connectors.secret.handoff import _wrap_on_local_store
from meho_backplane.connectors.vault import ops as vault_ops
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()
    reset_block_cache()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s_:
        s_.add(Tenant(id=h.TENANT_ID, slug="block-vault", name="Block Vault"))
        await s_.commit()
        yield s_


def _admin() -> Operator:
    return Operator(
        sub="admin-person",
        name=None,
        email=None,
        raw_jwt="<test>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )


async def _block_the_caller() -> None:
    await block_principal(_admin(), h.operator().sub, "left the team")
    reset_block_cache()


def _running() -> Any:
    operator = h.operator()
    return tracking_run(is_step=False, login=(operator.sub, operator.tenant_id))


def _fake_vault(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> MagicMock:
    client = MagicMock()
    client.secrets.kv.v2.create_or_update_secret.return_value = {"data": {"version": 1}}
    client.sys.wrap.return_value = {"wrap_info": {"token": "t", "ttl": 60}}

    @asynccontextmanager
    async def _open(*_args: Any, **_kwargs: Any) -> AsyncIterator[MagicMock]:
        yield client

    monkeypatch.setattr(module, name, _open)
    return client


async def test_thread_helper_stops_the_second_call_of_a_blocked_login(
    session: AsyncSession,
) -> None:
    calls: list[int] = []
    with _running():
        await to_thread_checked(calls.append, 1)
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await to_thread_checked(calls.append, 2)
    assert calls == [1]


async def test_thread_helper_runs_every_call_outside_an_operation(session: AsyncSession) -> None:
    calls: list[int] = []
    await _block_the_caller()
    await to_thread_checked(calls.append, 1)
    assert calls == [1]


async def test_vault_write_is_not_made_once_the_login_is_blocked(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _fake_vault(monkeypatch, vault_ops, "vault_client_for_target")
    monkeypatch.setattr(vault_ops, "enforce_tenant_scope", lambda *_a, **_k: None)
    params = {"path": "apps/one", "data": {"k": "v"}}
    with _running():
        await vault_ops.vault_kv_put(h.operator(), None, params)
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await vault_ops.vault_kv_put(h.operator(), None, params)
    assert client.secrets.kv.v2.create_or_update_secret.call_count == 1


async def test_vault_login_is_not_sent_for_a_blocked_login(
    session: AsyncSession,
) -> None:
    client = MagicMock()
    with _running():
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await auth_vault._to_thread_jwt_login(client, role="r", jwt="j", mount_path="jwt")
    client.auth.jwt.jwt_login.assert_not_called()


async def test_handoff_wrap_is_not_made_once_the_login_is_blocked(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _fake_vault(monkeypatch, auth_vault, "vault_client_for_operator")
    material = MagicMock()
    material.value = b"value"
    with _running():
        await _wrap_on_local_store(h.operator(), material, b"key", 60)
        await _block_the_caller()
        with pytest.raises(PrincipalBlockedStepError):
            await _wrap_on_local_store(h.operator(), material, b"key", 60)
    assert client.sys.wrap.call_count == 1


async def test_handoff_store_client_stops_the_request_of_a_blocked_login(
    session: AsyncSession,
) -> None:
    store = handoff_module._RemoteStore(
        label="store", host="store.example.com", url="https://store.example.com", verify=True
    )
    client = handoff_module._build_store_client(store)
    request = httpx.Request("POST", "https://store.example.com/v1/sys/wrapping/unwrap")
    hooks = client.event_hooks["request"]
    try:
        with _running():
            for hook in hooks:
                await hook(request)
            await _block_the_caller()
            with pytest.raises(PrincipalBlockedStepError):
                for hook in hooks:
                    await hook(request)
    finally:
        await client.aclose()
