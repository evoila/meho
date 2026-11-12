# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Follow-ups to the up-front step check of an agent's multi-step call (#6298).

With ``AGENT_GRANT_REQUIRED=on``:

* A multi-step operation that changes something and declares no steps is
  refused for an agent before it starts, with ``agent_steps_not_declared``.
  ``observe`` only logs it; ``off`` and people and service logins are not
  touched.
* The refusal for a step with a ``deny`` grant names the agent and says how
  an admin changes the grant.
* An approved call that the agent resumes is checked again before the
  first step runs.
* A composite that checks a step that is not on its list logs one warning
  and goes on.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import update

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest
from meho_backplane.operations import _preview_steps, composite, dispatch
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approve_request,
    result_state_of_envelope,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.deny_reasons import (
    AGENT_GRANT_DENIED,
    AGENT_STEPS_NOT_DECLARED,
    deny_reason_code,
)
from meho_backplane.operations.governed_subops import register_governed_subops
from meho_backplane.operations.typed_register import register_composite_operation
from tests.test_agent_grant_required import (
    _AGENT,
    _MULTI_OP,
    _MULTI_RUNS,
    _WRITE_STEP,
    _approval_requests,
    _audit_rows,
    _dispatch,
    _grant,
    _operator,
    _register_multi_step_op,
    _required_settings_env,
    _reset_state,
    _seed_vault_target,
    _switch,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _FakeTarget, _NoOpVaultConnector

# The fixtures of the #6189 module, so this module sets up the same way.
__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]

_UNDECLARED_OP = "vault.composite.undeclared"
_OTHER_STEP = "POST:/v1/other/{path}"
#: The calls of the composite handlers below; a refused call adds none.
_COMPOSITE_RUNS: list[str] = []


async def _composite_handler(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> dict[str, Any]:
    del operator, target, params, dispatch_child
    _COMPOSITE_RUNS.append(_UNDECLARED_OP)
    return {"ran": True}


async def _register_undeclared_write(stub: AsyncMock) -> None:
    """A multi-step operation that changes something and declares no steps."""
    _COMPOSITE_RUNS.clear()
    await register_composite_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_UNDECLARED_OP,
        handler=_composite_handler,
        summary="A test change.",
        description="Changes something in steps.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="dangerous",
        requires_approval=False,
        embedding_service=stub,
    )


async def _preview(operator: Operator, op_id: str) -> dict[str, Any]:
    return await preview_dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id=op_id,
        target=_FakeTarget(product="vault"),
        params={},
    )


def _log_spy(monkeypatch: pytest.MonkeyPatch, module: Any) -> MagicMock:
    spy = MagicMock()
    monkeypatch.setattr(module, "_log", spy)
    return spy


def _lines(calls: list[Any], event: str) -> list[dict[str, Any]]:
    return [c.kwargs for c in calls if c.args == (event,)]


# --- A multi-step write that declares no steps ----------------------------------


@pytest.mark.parametrize("op_verdict", ["auto-execute", "needs-approval"])
async def test_undeclared_steps_are_refused_up_front_when_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, op_verdict: str
) -> None:
    """Refused before any step and before any approval request, with an audit row."""
    _switch(monkeypatch, "on")
    await _register_undeclared_write(stub_embedding_service)
    await _grant(_UNDECLARED_OP, op_verdict)

    result = await _dispatch(_operator(), _UNDECLARED_OP)

    assert result.status == "denied", result
    assert result.extras["error_code"] == AGENT_STEPS_NOT_DECLARED
    assert result.extras["reason_code"] == AGENT_STEPS_NOT_DECLARED
    assert result.error.startswith(f"denied: {AGENT_STEPS_NOT_DECLARED}: agent {_AGENT} ")
    assert repr(_UNDECLARED_OP) in result.error
    assert "register_governed_subops" in result.error
    assert _COMPOSITE_RUNS == []
    assert await _approval_requests() == []
    (row,) = await _audit_rows(_UNDECLARED_OP)
    assert row.policy_decision == "deny"
    assert row.payload["error"]["reason_code"] == AGENT_STEPS_NOT_DECLARED
    # The preview gives the same answer.
    preview = await _preview(_operator(), _UNDECLARED_OP)
    assert preview["policy_verdict"] == "deny", preview
    assert result.error == f"denied: {preview['policy_reason']}"


async def test_undeclared_steps_are_only_logged_with_observe(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "observe")
    await _register_undeclared_write(stub_embedding_service)
    await _grant(_UNDECLARED_OP, "needs-approval")
    spy = _log_spy(monkeypatch, _preview_steps)

    result = await _dispatch(_operator(), _UNDECLARED_OP)

    assert result.status == "awaiting_approval", result
    lines = _lines(spy.info.call_args_list, "agent_steps_not_declared_would_deny")
    assert lines == [
        {"agent": _AGENT, "tenant_id": str(_operator().tenant_id), "op_id": _UNDECLARED_OP}
    ]
    preview = await _preview(_operator(), _UNDECLARED_OP)
    assert preview["policy_verdict"] == "needs-approval", preview


async def test_undeclared_steps_are_not_checked_when_off(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    await _register_undeclared_write(stub_embedding_service)
    await _grant(_UNDECLARED_OP, "needs-approval")
    spy = _log_spy(monkeypatch, _preview_steps)

    result = await _dispatch(_operator(), _UNDECLARED_OP)

    assert result.status == "awaiting_approval", result
    assert spy.info.call_args_list == []
    preview = await _preview(_operator(), _UNDECLARED_OP)
    assert preview["policy_verdict"] == "needs-approval", preview


async def test_declared_no_steps_is_not_refused(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """An operation that declared its steps is left to the step check."""
    _switch(monkeypatch, "on")
    await _register_undeclared_write(stub_embedding_service)
    register_governed_subops(
        composite_op_id=_UNDECLARED_OP, connector_id="vault-1.x", sub_op_ids=(_WRITE_STEP,)
    )
    await _grant(_UNDECLARED_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")

    result = await _dispatch(_operator(), _UNDECLARED_OP)

    assert result.status == "awaiting_approval", result


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.SERVICE])
async def test_people_and_services_are_not_touched_by_undeclared_steps(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, kind: PrincipalKind
) -> None:
    """Switch on or off: the same answer and the same preview."""
    await _register_undeclared_write(stub_embedding_service)
    answers: dict[str, tuple[str, str, str | None]] = {}
    for mode in ("off", "on"):
        _switch(monkeypatch, mode)
        result = await _dispatch(_operator(kind), _UNDECLARED_OP)
        preview = await _preview(_operator(kind), _UNDECLARED_OP)
        answers[mode] = (result.status, preview["policy_verdict"], preview["policy_reason"])

    assert answers["on"] == answers["off"]
    assert answers["on"][0] != "denied"


def test_the_code_is_worked_out_from_the_text() -> None:
    assert (
        deny_reason_code(f"{AGENT_STEPS_NOT_DECLARED}: agent {_AGENT} may not run")
        == AGENT_STEPS_NOT_DECLARED
    )


# --- The text for a step with a deny grant ---------------------------------------


async def test_denied_step_text_names_the_agent_and_how_to_change_the_grant(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "auto-execute")
    await _grant(_WRITE_STEP, "deny")

    result = await _dispatch(_operator(), _MULTI_OP)

    assert result.status == "denied", result
    assert result.extras["reason_code"] == AGENT_GRANT_DENIED
    assert f"agent {_AGENT} may not run the operation {_MULTI_OP!r}" in result.error
    assert f"pattern={_WRITE_STEP!r}" in result.error
    assert f"meho agent grant list --principal {_AGENT}" in result.error
    assert "meho agent grant revoke <grant-id>" in result.error
    preview = await _preview(_operator(), _MULTI_OP)
    assert result.error == f"denied: {preview['policy_reason']}"


# --- The resumed call is checked again --------------------------------------------


async def _park_and_approve() -> uuid.UUID:
    """The agent's call waits; a person approves it. Returns the request id."""
    target = await _seed_vault_target()
    parked = await dispatch(
        operator=_operator(), connector_id="vault-1.x", op_id=_MULTI_OP, target=target, params={}
    )
    assert parked.status == "awaiting_approval", parked
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=_operator(PrincipalKind.USER))
        await session.commit()
    return request_id


async def _change_step_grant_to_deny() -> None:
    """An admin changes the step's grant to ``deny`` while the call waits."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentPermission)
            .where(AgentPermission.op_pattern == _WRITE_STEP)
            .values(verdict="deny")
        )
        await session.commit()


async def _resume_as_agent(request_id: uuid.UUID) -> Any:
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        return await resume_dispatch_after_approval(operator=_operator(), request=request)


async def _stored_result(request_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        assert request.resume_result is not None
        return dict(request.resume_result)


async def test_resumed_call_is_refused_when_a_step_grant_changed(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """A deny grant added while the call waited: refused before the first step."""
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park_and_approve()
    await _change_step_grant_to_deny()

    result = await _resume_as_agent(request_id)

    assert result.status == "denied", result
    assert result.extras["reason_code"] == AGENT_GRANT_DENIED
    assert _WRITE_STEP in result.error
    assert _MULTI_RUNS == []
    denied_rows = [r for r in await _audit_rows(_MULTI_OP) if r.policy_decision == "deny"]
    assert len(denied_rows) == 1
    assert denied_rows[0].payload["error"]["reason_code"] == AGENT_GRANT_DENIED
    stored = await _stored_result(request_id)
    assert result_state_of_envelope(stored) is ApprovalResultState.FAILED
    assert stored["extras"]["reason"] == result.extras["reason"]


async def test_resumed_call_runs_when_the_grants_did_not_change(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park_and_approve()

    result = await _resume_as_agent(request_id)

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1


@pytest.mark.parametrize("mode", ["observe", "off"])
async def test_resumed_call_is_not_checked_again_when_not_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, mode: str
) -> None:
    _switch(monkeypatch, mode)
    await _register_multi_step_op(stub_embedding_service)
    await _grant(_MULTI_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park_and_approve()
    await _change_step_grant_to_deny()

    result = await _resume_as_agent(request_id)

    assert result.status == "ok", result.error


# --- A step that is not on the list ------------------------------------------------


async def _checks_an_unlisted_step(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> dict[str, Any]:
    del dispatch_child
    refusal = await enforce_subop_policy(
        operator=operator,
        connector_id="vault-1.x",
        op_id=_OTHER_STEP,
        safety_level="safe",
        requires_approval=False,
        target=target,
        params=params,
    )
    assert refusal is None, refusal
    _COMPOSITE_RUNS.append(_OTHER_STEP)
    return {"ran": True}


async def _register_step_checker(stub: AsyncMock, listed: tuple[str, ...]) -> None:
    """A composite that checks ``_OTHER_STEP``; its list is *listed*."""
    _COMPOSITE_RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_composite_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_UNDECLARED_OP,
        handler=_checks_an_unlisted_step,
        summary="A test read.",
        description="Checks one step.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        requires_approval=False,
        embedding_service=stub,
    )
    register_governed_subops(
        composite_op_id=_UNDECLARED_OP, connector_id="vault-1.x", sub_op_ids=listed
    )


@pytest.mark.parametrize("listed", [(_WRITE_STEP,), ()])
async def test_a_step_not_on_the_list_logs_one_warning_and_goes_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, listed: tuple[str, ...]
) -> None:
    await _register_step_checker(stub_embedding_service, listed)
    spy = _log_spy(monkeypatch, composite)

    result = await _dispatch(_operator(PrincipalKind.USER), _UNDECLARED_OP)

    assert result.status == "ok", result.error
    assert _COMPOSITE_RUNS == [_OTHER_STEP]
    assert _lines(spy.warning.call_args_list, "composite_step_not_declared") == [
        {"composite_op_id": _UNDECLARED_OP, "step_op_id": _OTHER_STEP}
    ]


async def test_a_step_on_the_list_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    await _register_step_checker(stub_embedding_service, (_OTHER_STEP,))
    spy = _log_spy(monkeypatch, composite)

    result = await _dispatch(_operator(PrincipalKind.USER), _UNDECLARED_OP)

    assert result.status == "ok", result.error
    assert _COMPOSITE_RUNS == [_OTHER_STEP]
    assert _lines(spy.warning.call_args_list, "composite_step_not_declared") == []
