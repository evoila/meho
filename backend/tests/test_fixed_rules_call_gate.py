# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Whole-path tests of three fixed rules at the call gate.

* A ``destructive`` operation whose level on the row is ``dangerous`` (as an
  admin's lowering would make it) still waits. No standing grant and no
  run approval covers it, because its code level is still a delete sign.
  In sandbox mode a delete grant still covers it for a service login.
* A command that runs as root on a host waits for a service login, also
  with a run approval for it (``root_shell_waits``).
* With the switch ``CUSTOM_POLICY`` on, an operation that changes rights in
  a target system (``changes_rights``) waits: also for a person in the web
  console, and also when a standing grant names it
  (``target_rights_waits``). With the switch off it gets today's answer.

Each call goes over ``POST /api/v1/operations/call`` with a signed token,
or through the dispatcher for the person in the web console, as the
console does. The connector, the operations and the target are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ServicePrincipalGrant, Target
from meho_backplane.op_classes import _ROOT_SHELL, declare_root_shell
from meho_backplane.operations import dispatch
from meho_backplane.operations import op_policy as op_policy_module
from meho_backplane.policy.fixed_rules import FIXED_RULES, root_shell_rule
from tests._fixed_rules_helpers import (
    CONNECTOR,
    PRODUCT,
    RUNS,
    TARGET,
    call_as,
    call_rows,
    register,
)
from tests._hard_to_undo_helpers import (
    SERVICE,
    TENANT,
    add_connector,
    custom_policy,
    finish,
    request_op_ids,
    run_approval_uses,
    seed_run_approval,
    seed_target,
    service_client,
    start,
)
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    seed_grant_row,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)

_LOWERED = "rulebox.box.wipe"
_ROOT = "rulebox.host.run"
_RIGHTS = "rulebox.member.add"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    start(monkeypatch)
    add_connector(PRODUCT["product"], PRODUCT["version"], PRODUCT["impl_id"])
    declare_root_shell((_ROOT,))
    RUNS.clear()
    yield
    _ROOT_SHELL.discard(_ROOT)
    turn_sandbox_mode_off(monkeypatch)
    RUNS.clear()
    finish()


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with service_client() as c:
        yield c


async def _service(client: tuple[httpx.AsyncClient, Any], op_id: str, **kw: Any) -> Any:
    return await call_as(client, op_id, kind="service", sub=SERVICE, **kw)


async def _grant(op_id: str) -> Any:
    return await seed_grant_row(
        model=ServicePrincipalGrant,
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=op_id,
        connector_id=CONNECTOR,
        target_product=PRODUCT["product"],
    )


def _lower_to_dangerous(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the effective level of ``_LOWERED`` ``dangerous``, with approval off.

    That is what an admin's lowering of the level would give. The code's own
    level stays ``destructive`` (``row.code_safety_level``).
    """
    real = op_policy_module.effective_policy_map

    async def lowered(*args: Any, **kwargs: Any) -> Any:
        policies = await real(*args, **kwargs)
        return {
            key: replace(policy, safety_level="dangerous", requires_approval=False)
            if key.op_id == _LOWERED
            else policy
            for key, policy in policies.items()
        }

    monkeypatch.setattr(op_policy_module, "effective_policy_map", lowered)


async def _seed_lowered(monkeypatch: pytest.MonkeyPatch) -> Any:
    await register(_LOWERED, safety_level="destructive")
    _lower_to_dangerous(monkeypatch)
    return await seed_target(TARGET, product="rulebox", version="1.x")


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [True, False], ids=["switch_on", "switch_off"])
async def test_a_lowered_destructive_call_waits_and_no_grant_covers_it(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any], on: bool
) -> None:
    custom_policy(monkeypatch, on=on)
    target_id = await _seed_lowered(monkeypatch)
    grant_id = await _grant(_LOWERED)
    approval_id = await seed_run_approval(
        target_id, work_ref="run-1", connector_id=CONNECTOR, op_ids=(_LOWERED,)
    )

    with_grant = await _service(client, _LOWERED)
    with_run_approval = await _service(client, _LOWERED, work_ref="run-1")
    agent = await call_as(client, _LOWERED, kind="agent", sub="agent:rule-bot")

    assert [with_grant["status"], with_run_approval["status"], agent["status"]] == [
        "awaiting_approval"
    ] * 3
    assert RUNS == []
    assert await request_op_ids() == [_LOWERED] * 3
    assert await grant_use_rows(grant_id) == []
    assert await run_approval_uses(approval_id) == 0


@pytest.mark.asyncio
async def test_in_sandbox_mode_a_delete_grant_still_covers_a_lowered_destructive_call(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await _seed_lowered(monkeypatch)
    ordinary_id = await _grant(_LOWERED)
    delete_grant_id = await seed_delete_grant(
        tenant_id=TENANT,
        principal_sub=SERVICE,
        op_id=_LOWERED,
        connector_id=CONNECTOR,
        target_product="rulebox",
    )

    result = await _service(client, _LOWERED)

    assert result["status"] == "ok", result
    assert RUNS == ["ran"]
    assert len(await grant_use_rows(delete_grant_id)) == 1
    assert await grant_use_rows(ordinary_id) == []


@pytest.mark.asyncio
async def test_a_root_command_waits_also_with_a_run_approval(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    await register(_ROOT, safety_level="safe", requires_approval=False)
    target_id = await seed_target(TARGET, product="rulebox", version="1.x")
    approval_id = await seed_run_approval(
        target_id, work_ref="run-2", connector_id=CONNECTOR, op_ids=(_ROOT,)
    )

    result = await _service(client, _ROOT, work_ref="run-2")

    assert result["status"] == "awaiting_approval", result
    assert RUNS == []
    assert await request_op_ids() == [_ROOT]
    assert await run_approval_uses(approval_id) == 0
    assert root_shell_rule(_ROOT) == "root_shell_waits"
    assert "root" in FIXED_RULES["root_shell_waits"]


def _person_in_the_console() -> Operator:
    return Operator(
        sub="person-console",
        raw_jwt="header.payload.signature",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
        web_session=True,
    )


async def _seed_rights_change() -> tuple[Any, Target]:
    await register(_RIGHTS, requires_approval=False, changes_rights=True)
    target_id = await seed_target(TARGET, product="rulebox", version="1.x")
    async with get_sessionmaker()() as session:
        target = await session.get(Target, target_id)
    assert target is not None
    return await _grant(_RIGHTS), target


async def _console_call(target: Target) -> Any:
    """A person in the web console calls the operation; the console uses the dispatcher."""
    return await dispatch(
        operator=_person_in_the_console(),
        connector_id=CONNECTOR,
        op_id=_RIGHTS,
        target=target,
        params={},
    )


@pytest.mark.asyncio
async def test_with_the_switch_on_a_change_of_rights_in_a_target_waits(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=True)
    grant_id, target = await _seed_rights_change()

    service = await _service(client, _RIGHTS)
    person = await _console_call(target)

    assert service["status"] == "awaiting_approval", service
    assert person.status == "awaiting_approval", person
    assert RUNS == []
    assert await request_op_ids() == [_RIGHTS, _RIGHTS]
    assert await grant_use_rows(grant_id) == []
    assert "target_rights_waits" in FIXED_RULES
    assert [row.status_code for row in await call_rows(_RIGHTS)] == []


@pytest.mark.asyncio
async def test_with_the_switch_off_a_change_of_rights_in_a_target_gets_todays_answer(
    monkeypatch: pytest.MonkeyPatch, client: tuple[httpx.AsyncClient, Any]
) -> None:
    custom_policy(monkeypatch, on=False)
    grant_id, target = await _seed_rights_change()

    service = await _service(client, _RIGHTS)
    person = await _console_call(target)

    assert service["status"] == "ok", service
    assert person.status == "ok", person
    assert RUNS == ["ran", "ran"]
    assert await request_op_ids() == []
    assert len(await grant_use_rows(grant_id)) == 1
    assert [row.status_code for row in await call_rows(_RIGHTS)] == [200, 200]
