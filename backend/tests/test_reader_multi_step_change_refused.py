# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Reader's agent or hosted run that starts a multi-step operation with a change step is refused.

The multi-step operation is ``safe``, with no approval flag and no class.
Its one step is sent through ``run_governed_subop`` without the approval
flag, at ``safe`` or ``caution``. The step is a plain write (class
``write``), a credential write or a credential mint. Each case runs through
the real ``dispatch``, and the agent login also through the REST and MCP
call doors:

* The step is on the operation's step list: a Reader's agent login and a
  Reader's hosted run are refused with ``insufficient_role`` and the Reader
  sentence. No approval request is stored and the step is not sent.
* The step is not on a list: the call waits for approval as before. After
  a person approves it, the step is refused with the Reader sentence and
  is not sent.
* A list with only read steps: the call waits for approval, and after the
  approval the read step is sent.
* An Operator's agent login and hosted run keep their answers.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation, delegated_operator
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.op_classes import _DECLARED, declare_op_classes
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import approve_request, resume_dispatch_after_approval
from meho_backplane.operations.governed_subops import register_governed_subops
from meho_backplane.operations.role_floors import READER_CHANGE_CODE, holds_change_step
from meho_backplane.settings import get_settings
from tests import _composite_wait_audit_helpers as h
from tests import _credential_step_always_waits_helpers as s
from tests._composite_wait_audit_helpers import NoOpVaultConnector
from tests.test_agent_grant_required import _AGENT, _TENANT, _reset_state
from tests.test_approved_run_rights import _required_settings_env, _token
from tests.test_reader_change_refused import _ANSWER, _REST_BODY, _mcp_call, client, mcp_as

__all__ = ["_required_settings_env", "_reset_state", "client", "mcp_as"]

pytestmark = pytest.mark.asyncio

_CLASS_STEP = "vault.standin.step.plain"
_CHANGE_STEPS = {"plain_write": _CLASS_STEP, "credential_write": s.WRITE_STEP}
_CHANGE_STEPS["credential_mint"] = s.MINT_STEP
_READ_STEPS = {"get_path": "GET:/standin/config", "list_name": "vault.standin.step.config.list"}
_LEVELS = ["safe", "caution"]
_LOGINS = ["agent", "hosted"]
_PLAIN_FORBIDDEN = "forbidden: 'call_operation' requires a higher role"


@pytest.fixture(autouse=True)
async def _stand_ins(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    s.sent.clear()
    declare_op_classes(
        write=(_CLASS_STEP,), credential_write=(s.WRITE_STEP,), credential_mint=(s.MINT_STEP,)
    )
    register_connector_v2(product="vault", version="", impl_id="", cls=NoOpVaultConnector)
    async with get_sessionmaker()() as session:
        await h.register_composite(
            session, s.COMPOSITE_OP_ID, "one_step_composite", module=s.__name__
        )
    yield
    for op_id in (_CLASS_STEP, s.WRITE_STEP, s.MINT_STEP):
        _DECLARED.pop(op_id, None)
    s.sent.clear()
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


def _list(step: str) -> None:
    register_governed_subops(
        composite_op_id=s.COMPOSITE_OP_ID, connector_id=s.CONNECTOR_ID, sub_op_ids=(step,)
    )


def _login(login: str, role: TenantRole) -> Operator:
    agent = login == "agent"
    return Operator(
        sub=f"{login}-{role.value}",
        name="A Caller",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.AGENT if agent else PrincipalKind.USER,
        client_id=_AGENT if agent else None,
    )


async def _run(login: str, role: TenantRole, step: str, level: str) -> OperationResult:
    who = _login(login, role)
    hosted = login == "hosted"
    with actor_delegation(_AGENT) if hosted else contextlib.nullcontext():
        return await dispatch(
            operator=delegated_operator(who) if hosted else who,
            connector_id=s.CONNECTOR_ID,
            op_id=s.COMPOSITE_OP_ID,
            target=None,
            params={"step": step, "level": level},
        )


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at)
        return list((await session.execute(stmt)).scalars().all())


async def _approve_and_resume(result: OperationResult) -> OperationResult:
    request_id = UUID(result.extras["approval_request_id"])
    approver = Operator(
        sub="op-approver",
        name="An Approver",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        web_session=True,
    )
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
    assert request is not None
    resumed: OperationResult = await resume_dispatch_after_approval(
        operator=approver, request=request, params=None
    )
    return resumed


def _assert_reader_refused(result: OperationResult) -> None:
    assert result.status == "denied", result
    assert result.extras["error_code"] == READER_CHANGE_CODE
    assert result.extras["reason"] == _ANSWER


# --- The step list -------------------------------------------------------------------


async def test_a_listed_change_step_makes_the_operation_a_change() -> None:
    assert not holds_change_step(s.COMPOSITE_OP_ID), "no list, no known step"
    for step in _CHANGE_STEPS.values():
        _list(step)
        assert holds_change_step(s.COMPOSITE_OP_ID), step
    for step in _READ_STEPS.values():
        _list(step)
        assert not holds_change_step(s.COMPOSITE_OP_ID), step


async def test_a_step_of_a_listed_step_counts_and_a_list_that_loops_ends() -> None:
    inner = "vault.composite.standin_inner"
    register_governed_subops(
        composite_op_id=s.COMPOSITE_OP_ID,
        connector_id=s.CONNECTOR_ID,
        sub_op_ids=(_READ_STEPS["list_name"], inner),
    )
    register_governed_subops(
        composite_op_id=inner,
        connector_id=s.CONNECTOR_ID,
        sub_op_ids=(s.COMPOSITE_OP_ID, _READ_STEPS["get_path"]),
    )
    assert not holds_change_step(s.COMPOSITE_OP_ID)

    register_governed_subops(
        composite_op_id=inner, connector_id=s.CONNECTOR_ID, sub_op_ids=(s.MINT_STEP,)
    )
    assert holds_change_step(s.COMPOSITE_OP_ID)


# --- A Reader's agent login and hosted run ---------------------------------------------


@pytest.mark.parametrize("step", list(_CHANGE_STEPS.values()), ids=list(_CHANGE_STEPS))
@pytest.mark.parametrize("level", _LEVELS)
@pytest.mark.parametrize("login", _LOGINS)
async def test_a_reader_change_step_on_the_list_is_refused_before_the_call_waits(
    step: str, level: str, login: str
) -> None:
    _list(step)

    result = await _run(login, TenantRole.READ_ONLY, step, level)

    _assert_reader_refused(result)
    assert await _requests() == []
    assert s.sent == []


@pytest.mark.parametrize("step", list(_CHANGE_STEPS.values()), ids=list(_CHANGE_STEPS))
@pytest.mark.parametrize("login", _LOGINS)
async def test_a_reader_change_step_not_on_a_list_is_refused_after_the_approval(
    step: str, login: str
) -> None:
    first = await _run(login, TenantRole.READ_ONLY, step, "safe")
    assert first.status == "awaiting_approval", first.error

    resumed = await _approve_and_resume(first)

    assert s.sent == [], "the change step is not sent"
    (request,) = await _requests()
    assert request.op_id == s.COMPOSITE_OP_ID
    assert resumed.status != "ok", resumed
    assert _ANSWER in str(resumed.error), resumed


@pytest.mark.parametrize("step", list(_READ_STEPS.values()), ids=list(_READ_STEPS))
@pytest.mark.parametrize("login", _LOGINS)
async def test_a_reader_multi_step_read_waits_and_then_runs(step: str, login: str) -> None:
    _list(step)

    first = await _run(login, TenantRole.READ_ONLY, step, "safe")

    assert first.status == "awaiting_approval", first.error
    assert s.sent == []
    second = await _approve_and_resume(first)
    assert second.status == "ok", second.error
    assert s.sent == [step]
    assert len(await _requests()) == 1


# --- An Operator's agent login and hosted run -------------------------------------------


@pytest.mark.parametrize("login", _LOGINS)
async def test_an_operator_read_and_plain_safe_write_step_run_at_once(login: str) -> None:
    for step in (_READ_STEPS["list_name"], _CLASS_STEP):
        _list(step)
        s.sent.clear()

        result = await _run(login, TenantRole.OPERATOR, step, "safe")

        assert result.status == "ok", (step, result.error)
        assert s.sent == [step]
    assert await _requests() == []


@pytest.mark.parametrize(
    ("step", "level"),
    [(_CLASS_STEP, "caution"), (s.WRITE_STEP, "safe"), (s.MINT_STEP, "caution")],
    ids=["plain_write-caution", "credential_write-safe", "credential_mint-caution"],
)
@pytest.mark.parametrize("login", _LOGINS)
async def test_an_operator_caution_or_credential_step_waits_for_its_own_approval(
    step: str, level: str, login: str
) -> None:
    _list(step)

    result = await _run(login, TenantRole.OPERATOR, step, level)

    assert result.status == "awaiting_approval", result.error
    assert s.sent == []
    (request,) = await _requests()
    assert request.op_id == step


# --- The REST and MCP call doors of a Reader's agent login ---------------------------------


@pytest.mark.parametrize("step", list(_CHANGE_STEPS.values()), ids=list(_CHANGE_STEPS))
async def test_a_reader_agent_rest_call_gets_the_code_and_the_sentence(
    client: tuple[httpx.AsyncClient, Any], step: str
) -> None:
    ac, key = client
    _list(step)
    headers = _token(key, "reader-agent", role=TenantRole.READ_ONLY, kind="agent", client_id=_AGENT)

    response = await ac.post(
        "/api/v1/operations/call",
        json={
            "connector_id": s.CONNECTOR_ID,
            "op_id": s.COMPOSITE_OP_ID,
            "params": {"step": step, "level": "safe"},
        },
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert response.json() == _REST_BODY
    assert await _requests() == []
    assert s.sent == []


async def test_a_reader_agent_rest_call_of_a_multi_step_read_keeps_its_answer(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client
    _list(_READ_STEPS["get_path"])
    headers = _token(key, "reader-agent", role=TenantRole.READ_ONLY, kind="agent", client_id=_AGENT)

    response = await ac.post(
        "/api/v1/operations/call",
        json={"connector_id": s.CONNECTOR_ID, "op_id": s.COMPOSITE_OP_ID, "params": {}},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert response.json() != _REST_BODY
    assert response.json()["detail"] == READER_CHANGE_CODE


def _reader_agent() -> Operator:
    return _login("agent", TenantRole.READ_ONLY)


@pytest.mark.parametrize("step", list(_CHANGE_STEPS.values()), ids=list(_CHANGE_STEPS))
async def test_a_reader_agent_mcp_call_gets_the_code_and_the_sentence(
    client: tuple[httpx.AsyncClient, Any], mcp_as: Callable[[Operator], None], step: str
) -> None:
    ac, _ = client
    _list(step)
    mcp_as(_reader_agent())

    body = await _mcp_call(
        ac,
        "call_operation",
        {
            "connector_id": s.CONNECTOR_ID,
            "op_id": s.COMPOSITE_OP_ID,
            "params": {"step": step, "level": "safe"},
        },
    )

    assert body["error"]["message"] == _ANSWER, body
    assert await _requests() == []
    assert s.sent == []


async def test_a_reader_agent_mcp_call_of_a_multi_step_read_keeps_its_answer(
    client: tuple[httpx.AsyncClient, Any], mcp_as: Callable[[Operator], None]
) -> None:
    ac, _ = client
    _list(_READ_STEPS["list_name"])
    mcp_as(_reader_agent())

    body = await _mcp_call(
        ac,
        "call_operation",
        {"connector_id": s.CONNECTOR_ID, "op_id": s.COMPOSITE_OP_ID, "params": {}},
    )

    assert body["error"]["message"] == _PLAIN_FORBIDDEN, body
