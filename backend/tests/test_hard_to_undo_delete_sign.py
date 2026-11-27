# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, an operation that is hard to undo has a delete sign.

Each call goes the whole path: a service login sends
``POST /api/v1/operations/call`` with a signed token, the real gate decides,
and the test reads the audit rows.

* Switch on: a standing grant and a run approval do not cover an operation
  with ``hard_to_undo``. The call waits for a person, and no grant use row
  is written. A new grant for it is refused. They cover an operation with
  no ``hard_to_undo`` and no other delete sign.
* Switch off: the grant and the run approval cover the call.
* Sandbox mode, switch on: a delete grant covers the call, also for
  ``stops`` and ``overwrites``. An ordinary grant does not.

The connector, the product and the target are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import SandboxDeleteGrant, ServicePrincipalGrant
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
    custom_policy,
    decision_rows,
    finish,
    register_op,
    request_op_ids,
    run_approval_uses,
    seed_run_approval,
    seed_target,
    service_client,
    start,
)
from tests._hard_to_undo_helpers import call as call_op
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    seed_grant_row,
    turn_sandbox_mode_on,
)

_CONNECTOR = "undo-api-1.0"
_OP = "undo.widget.reset"
_PLAIN_OP = "undo.widget.reboot"
_TARGET = "undo-widget-01"
_RUN = "run-undo-widget"
_KINDS = ("deletes", "stops", "overwrites")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    add_connector("undo", "1.0", "undo-api")
    yield
    finish()


async def _register(hard_to_undo: str | None, op_id: str = _OP) -> None:
    await register_op(op_id, hard_to_undo)


async def _seed_target() -> Any:
    return await seed_target(_TARGET)


async def _seed_grant(op_id: str = _OP) -> Any:
    """A standing grant of the service login, written before the switch is turned on."""
    return await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=op_id,
        connector_id=_CONNECTOR,
        target_product="undo",
    )


async def _seed_run_approval(target_id: Any, op_id: str = _OP) -> Any:
    return await seed_run_approval(
        target_id, work_ref=_RUN, connector_id=_CONNECTOR, op_ids=(op_id,)
    )


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with service_client() as c:
        yield c


async def _call(
    client: tuple[httpx.AsyncClient, Any], *, work_ref: str | None = None, op_id: str = _OP
) -> dict[str, Any]:
    return await call_op(
        client, connector_id=_CONNECTOR, op_id=op_id, target=_TARGET, work_ref=work_ref
    )


async def _waits_for_a_person(result: dict[str, Any]) -> None:
    assert result["status"] == "awaiting_approval", result
    assert RUNS == []
    assert await request_op_ids() == [_OP]
    assert [r for r in await decision_rows() if r.path == "approval.decision"] == []


# --- Switch on: no standing grant and no run approval covers it -------------------


@pytest.mark.parametrize("kind", _KINDS)
async def test_switch_on_a_standing_grant_does_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register(kind)
    await _seed_target()
    grant_id = await _seed_grant()

    await _waits_for_a_person(await _call(client))
    assert await grant_use_rows(grant_id) == []


@pytest.mark.parametrize("kind", _KINDS)
async def test_switch_on_a_run_approval_does_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register(kind)
    approval_id = await _seed_run_approval(await _seed_target())

    await _waits_for_a_person(await _call(client, work_ref=_RUN))
    assert await run_approval_uses(approval_id) == 0


async def test_switch_on_a_new_grant_for_it_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_policy(monkeypatch, on=True)
    await _register("stops")
    payload = ServiceGrantCreate(
        principal_sub=SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
        reason="reset widgets at night",
    )
    with pytest.raises(GrantValidationError, match="hard to undo"):
        await ServicePrincipalGrantService().create(TENANT, "op-admin", payload)
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(ServicePrincipalGrant))).scalars().all() == []


async def test_switch_on_a_standing_grant_covers_an_op_without_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register(None, _PLAIN_OP)
    await _seed_target()
    grant_id = await _seed_grant(_PLAIN_OP)

    result = await _call(client, op_id=_PLAIN_OP)
    assert result["status"] == "ok", result
    assert len(RUNS) == 1
    assert await request_op_ids() == []
    assert len(await grant_use_rows(grant_id)) == 1


async def test_switch_on_a_run_approval_covers_an_op_without_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register(None, _PLAIN_OP)
    approval_id = await _seed_run_approval(await _seed_target(), _PLAIN_OP)

    result = await _call(client, work_ref=_RUN, op_id=_PLAIN_OP)
    assert result["status"] == "ok", result
    assert len(RUNS) == 1
    assert await request_op_ids() == []
    assert await run_approval_uses(approval_id) == 1


# --- Switch off: the grant and the run approval cover the call ----------------------


@pytest.mark.parametrize("kind", _KINDS)
async def test_switch_off_a_standing_grant_still_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    custom_policy(monkeypatch, on=False)
    await _register(kind)
    await _seed_target()
    grant_id = await _seed_grant()

    result = await _call(client)
    assert result["status"] == "ok", result
    assert len(RUNS) == 1
    assert len(await grant_use_rows(grant_id)) == 1


async def test_switch_off_a_run_approval_still_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=False)
    await _register("overwrites")
    approval_id = await _seed_run_approval(await _seed_target())

    result = await _call(client, work_ref=_RUN)
    assert result["status"] == "ok", result
    assert len(RUNS) == 1
    assert await run_approval_uses(approval_id) == 1


async def test_switch_off_a_new_grant_for_it_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_policy(monkeypatch, on=False)
    await _register("stops")
    payload = ServiceGrantCreate(
        principal_sub=SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
        reason="reset widgets at night",
    )
    entry = await ServicePrincipalGrantService().create(TENANT, "op-admin", payload)
    assert entry.op_id == _OP


# --- Sandbox mode: the delete exception stays ---------------------------------------


@pytest.mark.parametrize("kind", _KINDS)
async def test_sandbox_switch_on_a_delete_grant_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], kind: str
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    custom_policy(monkeypatch, on=True)
    await _register(kind)
    await _seed_target()
    grant_id = await seed_delete_grant(
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
    )

    result = await _call(client)
    assert result["status"] == "ok", result
    assert len(RUNS) == 1
    assert len(await grant_use_rows(grant_id)) == 1


async def test_sandbox_switch_on_an_ordinary_grant_does_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    custom_policy(monkeypatch, on=True)
    await _register("stops")
    await _seed_target()
    grant_id = await _seed_grant()

    await _waits_for_a_person(await _call(client))
    assert await grant_use_rows(grant_id) == []


# --- Unused delete grant table with the mode off -----------------------------------


async def test_switch_on_without_sandbox_reads_no_delete_grant(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    await _register("deletes")
    await _seed_target()
    grant_id = await seed_delete_grant(
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=_OP,
        connector_id=_CONNECTOR,
        target_product="undo",
    )
    await _waits_for_a_person(await _call(client))
    assert await grant_use_rows(grant_id) == []
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(SandboxDeleteGrant))).scalars().all()) == 1
