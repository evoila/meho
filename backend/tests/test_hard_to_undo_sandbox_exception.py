# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In sandbox mode, ``hard_to_undo`` never opens the sandbox delete exception.

The exception lets a delete grant cover an operation that waits on every
call because its connector marked it as able to read, write or make a
credential, and that is marked for the sandbox delete grant. It opens only
for a delete sign other than ``hard_to_undo``: a delete name or pattern, a
vendor delete action name, the ``destructive`` level, the HTTP verb
``DELETE`` or the ``destructive`` tag. An operation of that kind with only
``hard_to_undo`` is covered by no grant, with ``CUSTOM_POLICY`` on or off.
An operation with ``hard_to_undo`` and no such mark keeps the ordinary
sandbox rule: with the switch on, a delete grant covers it and an ordinary
grant does not.

Each call goes over ``POST /api/v1/operations/call``. The connector, the
operations and the target are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.models import ServicePrincipalGrant
from meho_backplane.op_classes import (
    _CREDENTIAL_CAPABLE,
    _SANDBOX_DELETE_GRANT,
    declare_credential_capable,
    declare_sandbox_delete_grant,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from tests._hard_to_undo_helpers import (
    RUNS,
    SERVICE,
    TENANT,
    add_connector,
    call,
    custom_policy,
    finish,
    register_op,
    request_op_ids,
    seed_target,
    service_client,
    start,
)
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    seed_grant_row,
    soon,
    turn_sandbox_mode_on,
)

_CONNECTOR = "cowapi-1.x"
_TARGET = "cow-box-01"
_RESET = "cowapi.box.reset"
_DELETE = "cowapi.box.delete"
_HALT = "cowapi.box.halt"
_MARKED = (_RESET, _DELETE)
_OTHER_SERVICE = "svc:cow-other"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    turn_sandbox_mode_on(monkeypatch)
    add_connector("cowapi", "1.x", "cowapi")
    declare_credential_capable(_MARKED)
    declare_sandbox_delete_grant(_MARKED)
    yield
    for op_id in _MARKED:
        _CREDENTIAL_CAPABLE.discard(op_id)
        _SANDBOX_DELETE_GRANT.discard(op_id)
    finish()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with service_client() as c:
        yield c


async def _register(op_id: str, hard_to_undo: str | None) -> None:
    await register_op(op_id, hard_to_undo, product="cowapi", version="1.x", impl_id="cowapi")
    await seed_target(_TARGET, product="cowapi", version="1.x")


async def _delete_grant(op_id: str) -> uuid.UUID:
    return await seed_delete_grant(
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=op_id,
        connector_id=_CONNECTOR,
        target_product="cowapi",
    )


async def _ask_for_a_delete_grant(op_id: str) -> Any:
    """A tenant admin asks for a delete grant in the web console."""
    payload = ServiceGrantCreate(
        principal_sub=SERVICE,
        op_id=op_id,
        connector_id=_CONNECTOR,
        target_name_pattern=_TARGET,
        reason="clean up test boxes",
        expires_at=soon(),
    )
    return await ServicePrincipalGrantService().create(
        TENANT,
        "tenant-admin",
        payload,
        from_console=True,
        creator_role=TenantRole.TENANT_ADMIN,
    )


async def _call(client: tuple[httpx.AsyncClient, Any], op_id: str, sub: str = SERVICE) -> Any:
    return await call(client, connector_id=_CONNECTOR, op_id=op_id, target=_TARGET, sub=sub)


@pytest.mark.parametrize("on", [True, False], ids=["case_13_switch_on", "case_14_switch_off"])
async def test_a_marked_op_with_only_hard_to_undo_waits_under_a_delete_grant(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register(_RESET, "deletes")
    grant_id = await _delete_grant(_RESET)

    result = await _call(client, _RESET)
    assert result["status"] == "awaiting_approval", result
    assert RUNS == []
    assert await request_op_ids() == [_RESET]
    assert await grant_use_rows(grant_id) == []


@pytest.mark.parametrize("on", [True, False], ids=["switch_on", "switch_off"])
async def test_case_15_a_delete_grant_for_a_marked_op_with_only_hard_to_undo_is_refused(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register(_RESET, "deletes")

    with pytest.raises(GrantValidationError) as refused:
        await _ask_for_a_delete_grant(_RESET)
    assert "credential" in str(refused.value)


@pytest.mark.parametrize("on", [True, False], ids=["switch_on", "switch_off"])
async def test_case_16_a_marked_op_with_a_delete_name_keeps_the_exception(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    await _register(_DELETE, None)
    grant_id = await _delete_grant(_DELETE)

    result = await _call(client, _DELETE)
    assert result["status"] == "ok", result
    assert len(RUNS) == 1
    assert len(await grant_use_rows(grant_id)) == 1

    entry = await _ask_for_a_delete_grant(_DELETE)
    assert entry.delete_grant is True
    assert entry.op_id == _DELETE


async def test_case_17_an_unmarked_hard_to_undo_op_keeps_the_ordinary_sandbox_rule(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register(_HALT, "stops")
    delete_grant_id = await _delete_grant(_HALT)
    ordinary_grant_id = await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=TENANT,
        principal_sub=_OTHER_SERVICE,
        op_id=_HALT,
        connector_id=_CONNECTOR,
        target_product="cowapi",
    )

    first = await _call(client, _HALT)
    assert first["status"] == "ok", first
    assert len(RUNS) == 1
    assert len(await grant_use_rows(delete_grant_id)) == 1

    second = await _call(client, _HALT, sub=_OTHER_SERVICE)
    assert second["status"] == "awaiting_approval", second
    assert len(RUNS) == 1
    assert await request_op_ids() == [_HALT]
    assert await grant_use_rows(ordinary_grant_id) == []
