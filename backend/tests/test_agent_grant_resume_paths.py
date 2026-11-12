# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An agent's approved multi-step call is checked again on every resume path (#6314).

With ``AGENT_GRANT_REQUIRED=on``, when a person runs the approved call from
an approvals surface (console, CLI, REST ``/approve`` and ``/decide``), the
steps are checked for the agent that asked, not only for the person. So a
``deny`` grant added while the call waited refuses the call before the
first step, on every path:

* an agent login that calls over MCP (no waiter in MEHO);
* a hosted agent run, whether its own resume or the approvals surface
  claims the call first.

People and service logins, ``off`` and ``observe`` keep the old behaviour.
``observe`` logs its "would deny" line once per call.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from sqlalchemy import update

from meho_backplane.auth import permissions
from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest
from meho_backplane.operations import dispatch, register_typed_operation
from meho_backplane.operations.approval_queue import (
    ApprovalResultState,
    approve_request,
    result_state_of_envelope,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.deny_reasons import AGENT_GRANT_DENIED, POLICY_DENIED
from meho_backplane.operations.governed_subops import register_governed_subops
from tests.test_agent_grant_required import (
    _AGENT,
    _MULTI_RUNS,
    _WRITE_STEP,
    _audit_rows,
    _grant,
    _multi_step_handler,
    _operator,
    _required_settings_env,
    _reset_state,
    _seed_vault_target,
    _switch,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _NoOpVaultConnector

# The fixtures of the #6189 module, so this module sets up the same way.
__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]

#: A multi-step operation that always waits for approval, for every login.
_OP = "vault.kv.multi_approved"
#: A single-step operation that always waits for approval: it lists no steps.
_SINGLE_OP = "vault.kv.single_approved"


def _reviewer() -> Operator:
    """The person who approves and runs the call from an approvals surface."""
    return Operator(
        sub="reviewer-1",
        name="Reviewer",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_operator().tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register(stub: AsyncMock) -> None:
    _MULTI_RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_OP,
        handler=_multi_step_handler,
        summary="Multi-step op.",
        description="Reads, then writes.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub,
    )
    register_governed_subops(
        composite_op_id=_OP,
        connector_id="vault-1.x",
        sub_op_ids=("GET:/v1/secret/data/{path}", _WRITE_STEP),
    )


async def _register_single(stub: AsyncMock) -> None:
    """A single-step operation next to :data:`_OP`; it lists no steps."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=_SINGLE_OP,
        handler=_multi_step_handler,
        summary="Single-step op.",
        description="Writes once.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=True,
        when_to_use=None,
        embedding_service=stub,
    )


async def _park(operator: Operator, actor: str | None = None, op_id: str = _OP) -> uuid.UUID:
    """The call waits for approval; a person approves it. Returns the request id."""
    target = await _seed_vault_target()
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: actor}):
        parked = await dispatch(
            operator=operator, connector_id="vault-1.x", op_id=op_id, target=target, params={}
        )
    assert parked.status == "awaiting_approval", parked
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=_reviewer())
        await session.commit()
    return request_id


async def _deny_the_write_step() -> None:
    """An admin changes the step's grant to ``deny`` while the call waits."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentPermission)
            .where(AgentPermission.op_pattern == _WRITE_STEP)
            .values(verdict="deny")
        )
        await session.commit()


async def _resume(request_id: uuid.UUID, operator: Operator, actor: str | None = None) -> Any:
    """Run the approved call. *operator* is the reviewer, or the requester itself."""
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: actor}):
            return await resume_dispatch_after_approval(operator=operator, request=request)


async def _request(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        return request


async def _assert_refused_before_the_first_step(result: Any, request_id: uuid.UUID) -> None:
    """Refused, nothing ran, one ``denied`` audit row, the request ended as not run."""
    assert result.status == "denied", result
    assert result.extras["reason_code"] == AGENT_GRANT_DENIED
    assert _WRITE_STEP in result.error
    assert f"agent {_AGENT} may not run" in result.error
    assert _MULTI_RUNS == []
    denied = [r for r in await _audit_rows(_OP) if r.policy_decision == "deny"]
    assert len(denied) == 1
    assert denied[0].payload["error"]["reason_code"] == AGENT_GRANT_DENIED
    stored = (await _request(request_id)).resume_result
    assert stored is not None
    assert result_state_of_envelope(stored) is ApprovalResultState.FAILED
    assert stored["extras"]["reason"] == result.extras["reason"]


# --- An agent login over MCP: no waiter, a person runs the call ------------------


async def test_agent_login_call_run_by_a_reviewer_is_refused_when_a_step_grant_changed(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register(stub_embedding_service)
    await _grant(_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park(_operator())
    await _deny_the_write_step()

    result = await _resume(request_id, _reviewer())

    await _assert_refused_before_the_first_step(result, request_id)


async def test_agent_login_call_run_by_a_reviewer_runs_when_the_grants_did_not_change(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _switch(monkeypatch, "on")
    await _register(stub_embedding_service)
    await _grant(_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park(_operator())

    result = await _resume(request_id, _reviewer())

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1


async def test_agent_call_with_no_record_on_the_card_is_refused_when_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """Made while the switch was ``off``: the agent's grants cannot be checked."""
    await _register(stub_embedding_service)
    await _grant(_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park(_operator())
    _switch(monkeypatch, "on")

    result = await _resume(request_id, _reviewer())

    assert result.status == "denied", result
    assert result.extras["reason_code"] == POLICY_DENIED
    assert "The agent must ask again" in result.error
    assert _MULTI_RUNS == []


async def test_single_step_call_with_no_record_on_the_card_runs_when_on(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    """No steps to check, so a card without a record does not stop the call."""
    await _register(stub_embedding_service)
    await _register_single(stub_embedding_service)
    await _grant(_SINGLE_OP, "needs-approval")
    request_id = await _park(_operator(), op_id=_SINGLE_OP)
    preflight = ((await _request(request_id)).proposed_effect or {}).get("permission_preflight")
    assert (preflight or {}).get("requester") is None
    _switch(monkeypatch, "on")

    result = await _resume(request_id, _reviewer())

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1


# --- A hosted agent run: both orders of the race ----------------------------------


@pytest.mark.parametrize("first", ["agent run", "approvals surface"])
async def test_hosted_run_call_is_refused_whoever_runs_it_first(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, first: str
) -> None:
    """The run's own resume and the approvals surface race; the result is the same."""
    _switch(monkeypatch, "on")
    await _register(stub_embedding_service)
    await _grant(_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    person = _operator(PrincipalKind.USER)
    request_id = await _park(person, actor=_AGENT)
    await _deny_the_write_step()

    run_first = first == "agent run"
    result = await (
        _resume(request_id, person, actor=_AGENT) if run_first else _resume(request_id, _reviewer())
    )
    late = await (
        _resume(request_id, _reviewer()) if run_first else _resume(request_id, person, actor=_AGENT)
    )

    await _assert_refused_before_the_first_step(result, request_id)
    assert late.status != "ok", late
    assert _MULTI_RUNS == []


# --- People and service logins ----------------------------------------------------


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.SERVICE])
async def test_people_and_services_are_not_checked_again(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, kind: PrincipalKind
) -> None:
    _switch(monkeypatch, "on")
    await _register(stub_embedding_service)
    await _grant(_WRITE_STEP, "deny")
    request_id = await _park(_operator(kind))

    result = await _resume(request_id, _reviewer())

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1
    assert "permission_preflight" not in ((await _request(request_id)).proposed_effect or {})


# --- off and observe ----------------------------------------------------------------


async def test_off_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    await _register(stub_embedding_service)
    await _grant(_OP, "needs-approval")
    await _grant(_WRITE_STEP, "auto-execute")
    request_id = await _park(_operator())
    await _deny_the_write_step()

    result = await _resume(request_id, _reviewer())

    assert result.status == "ok", result.error
    assert len(_MULTI_RUNS) == 1
    assert "permission_preflight" not in ((await _request(request_id)).proposed_effect or {})


@pytest.mark.parametrize("resumer", ["agent", "reviewer"])
async def test_observe_logs_the_would_deny_line_once_per_call(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, resumer: str
) -> None:
    """The line comes when the call is made, not again when it resumes."""
    _switch(monkeypatch, "observe")
    await _register(stub_embedding_service)
    await _grant(_OP, "needs-approval")
    spy = MagicMock()
    monkeypatch.setattr(permissions, "_log", spy)
    request_id = await _park(_operator())

    result = await _resume(request_id, _operator() if resumer == "agent" else _reviewer())

    assert result.status == "ok", result.error
    lines = [
        c.kwargs
        for c in spy.info.call_args_list
        if c.args == ("agent_no_grant_would_deny",) and c.kwargs["op_id"] == _WRITE_STEP
    ]
    assert len(lines) == 1, lines
