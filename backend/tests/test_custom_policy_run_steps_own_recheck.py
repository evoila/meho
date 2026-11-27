# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The re-check of a step's own approval reads the stored risk level too.

The whole path: a service login starts the multi-step operation
``probe.composite.remote_steps``. Its first step waits for a person, who
approves it with a token, and the run goes on. Its second step,
``POST:/probe/things/{id}/fetch2`` (connector ``probe-1.x``), declares level
``dangerous`` and no approval flag. Its stored row has a caller-address
field ``url``. The same operation also runs as a child call of
``probe.composite.remote_child``. Both runs are stored under connector
``probe-1.x`` too, so a person's approval of the step can start the run
again.

With ``CUSTOM_POLICY`` on, the caller-address rule makes the step wait for
its own approval, and the Admin ``admin-probe-5`` approves it:

* When the stored row of the step's operation gets the level
  ``destructive`` before the run goes on, the step does not run. It gets
  the same answer as a child call of the same operation: a refusal because
  the risk level is now higher than the approved one.
* Without the level change, the step runs.

With the switch off, every answer is the one of main (values recorded from
a run on main): the step goes ahead under the run's approval and no stored
row is read; the child call waits for its own approval, and its re-check
refuses it when the stored level rose and runs it when it did not.

All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, run_step_rules
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.operations.composite import (
    DispatchChild,
    composite_resume_state,
    enforce_subop_policy,
)
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_remote_steps_helpers as r
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step
from tests._people_behind_helpers import give_service_a_known_person

pytestmark = pytest.mark.asyncio

_CONNECTOR = "probe-1.x"
_RUN = "probe.composite.remote_steps"
_CHILD_RUN = "probe.composite.remote_child"
_FETCH2 = Step("POST:/probe/things/{id}/fetch2", "dangerous", False)
_ADDRESS_ROW: dict[str, Any] = {
    "schema": {
        "type": "object",
        "properties": {"url": {"type": "string", CALLER_ADDRESS_MARKER: True}},
    },
    "typed": True,
}
_ADMIN_SUB = "admin-probe-5"
#: The refusal of a step and of a child call whose stored level rose to ``destructive``.
_RISEN = (
    "the risk level of 'POST:/probe/things/{id}/fetch2' is now 'destructive', "
    "higher than the approved one"
)
_BOTH_PATHS = pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])


class _ProbeConnector(h.NoOpVaultConnector):
    product = "probe"
    version = "1.x"
    impl_id = "probe"


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from c.reset(monkeypatch)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


async def _first_step(operator: Operator, target: Any, run_op_id: str) -> Any:
    if composite_resume_state(run_op_id) is not None:
        return None
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=h.STEP_A,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={"name": "vm-1"},
    )
    if gated is None:
        c.ran.append(h.STEP_A)
    return gated


async def remote_steps(operator: Any, target: Any, params: dict[str, Any]) -> Any:
    """A step that waits, then ``fetch2`` through the step gate."""
    gated = await _first_step(operator, target, _RUN)
    if gated is not None:
        return gated
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=_FETCH2.op_id,
        safety_level=_FETCH2.safety_level,
        requires_approval=_FETCH2.requires_approval,
        target=target,
        params={},
        resume_state={"at": "fetch2"},
        after_main_effect=True,
    )
    if gated is not None:
        return gated
    c.ran.append(_FETCH2.op_id)
    return {"done": True}


async def remote_child(
    operator: Any, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """A step that waits, then ``fetch2`` as a child call."""
    gated = await _first_step(operator, target, _CHILD_RUN)
    if gated is not None:
        return gated
    return await dispatch_child(connector_id=_CONNECTOR, op_id=_FETCH2.op_id, params={})


def _run_row(op_id: str, handler: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        tenant_id=None,
        product="probe",
        version="1.x",
        impl_id="probe",
        op_id=op_id,
        source_kind="composite",
        handler_ref=f"{__name__}.{handler}",
        summary="The run.",
        description="The run.",
        tags=[],
        parameter_schema={"type": "object"},
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=h.EMBEDDING,
    )


def _admin() -> Operator:
    return Operator(
        sub=_ADMIN_SUB,
        name="Probe Admin",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=h.TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        approver=True,
    )


async def _start_and_approve_first(session: AsyncSession, stub: AsyncMock, *, child: bool) -> Any:
    """Store the rows, start as a service login, approve the first step with a token, go on."""
    await h.register_child(stub)
    register_connector_v2(product="probe", version="1.x", impl_id="probe", cls=_ProbeConnector)
    async with get_sessionmaker()() as s, s.begin():
        s.add(r.probe_row(_FETCH2, None, _ADDRESS_ROW))
    run_op_id = _CHILD_RUN if child else _RUN
    session.add(_run_row(run_op_id, "remote_child" if child else "remote_steps"))
    await session.commit()
    c.current.append(_FETCH2)
    service = h.operator(sub=c.SERVICE_SUB, principal_kind=PrincipalKind.SERVICE)
    await give_service_a_known_person(h.TENANT_ID, c.SERVICE_SUB)
    first = await dispatch(
        operator=service, connector_id=_CONNECTOR, op_id=run_op_id, target=None, params={}
    )
    assert first.status == "awaiting_approval", first.error
    request = await h.approve(UUID(first.extras["approval_request_id"]), sub=c.APPROVER_SUB)
    return await resume_dispatch_after_approval(
        operator=c.token_approver(), request=request, params=None
    )


async def _approve_own_and_go_on(*, raise_level: bool) -> Any:
    """The Admin approves the step's own request; the stored level may rise; the run goes on."""
    _, own = await c.requests()
    assert own.op_id == _FETCH2.op_id
    async with get_sessionmaker()() as s:
        await approve_request(s, own.id, operator=_admin(), params=None)
        await s.commit()
    if raise_level:
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(
                update(EndpointDescriptor)
                .where(EndpointDescriptor.op_id == _FETCH2.op_id)
                .values(safety_level="destructive")
            )
    return await resume_dispatch_after_approval(
        operator=_admin(), request=await h.request(own.id), params=None
    )


def _answer(result: Any) -> tuple[str, str | None, str | None]:
    return result.status, result.extras.get("error_code"), result.extras.get("reason_code")


@_BOTH_PATHS
async def test_on_a_level_raised_while_the_own_approval_waits_stops_the_step(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    """The step and a child call of the same operation get one answer; neither runs."""
    c.switch(monkeypatch, on=True)
    waiting = await _start_and_approve_first(session, stub_embedding_service, child=child)
    assert waiting.status == "awaiting_approval", waiting.error
    result = await _approve_own_and_go_on(raise_level=True)
    assert _answer(result) == ("denied", "denied", "policy_denied"), result
    assert _RISEN in result.extras["reason"]
    assert c.ran == [h.STEP_A]


@_BOTH_PATHS
async def test_on_without_a_level_change_the_approved_step_runs(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    waiting = await _start_and_approve_first(session, stub_embedding_service, child=child)
    assert waiting.status == "awaiting_approval", waiting.error
    result = await _approve_own_and_go_on(raise_level=False)
    assert result.status == "ok", result.error
    assert c.ran == [h.STEP_A, _FETCH2.op_id]
    assert len(await c.requests()) == 2


async def test_off_the_step_goes_ahead_and_no_stored_row_is_read(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """As on main: the step runs under the run's approval and never waits on its own."""
    c.switch(monkeypatch, on=False)
    reads: list[str] = []
    real = run_step_rules._stored_step_rows

    async def _read(step: EndpointDescriptor, tenant_id: Any) -> Any:
        reads.append(step.op_id)
        return await real(step, tenant_id)

    monkeypatch.setattr(run_step_rules, "_stored_step_rows", _read)
    result = await _start_and_approve_first(session, stub_embedding_service, child=False)
    assert reads == []
    assert result.status == "ok", result.error
    assert c.ran == [h.STEP_A, _FETCH2.op_id]
    assert len(await c.requests()) == 1


@pytest.mark.parametrize("raise_level", [True, False], ids=["level-raised", "no-change"])
async def test_off_a_child_call_waits_and_is_checked_as_on_main(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    raise_level: bool,
) -> None:
    """As on main: the child call's stored row waits; its re-check reads the stored level."""
    c.switch(monkeypatch, on=False)
    waiting = await _start_and_approve_first(session, stub_embedding_service, child=True)
    assert waiting.status == "awaiting_approval", waiting.error
    result = await _approve_own_and_go_on(raise_level=raise_level)
    if raise_level:
        assert _answer(result) == ("denied", "denied", "policy_denied"), result
        assert _RISEN in result.extras["reason"]
        assert c.ran == [h.STEP_A]
    else:
        assert result.status == "ok", result.error
        assert c.ran == [h.STEP_A, _FETCH2.op_id]
