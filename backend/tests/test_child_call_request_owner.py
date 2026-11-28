# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The own approval request of a child call belongs to the caller who started the run.

The whole path: a caller starts a multi-step operation. Its first step
waits for a person. A person approves it, and the run goes on under that
approval (``resume_dispatch_after_approval``). Then a child call
(``dispatch_child``) waits for its own approval request.

* The child's request names the caller who started the run, for a service
  login, a person and a hosted agent run that a person started, with
  ``CUSTOM_POLICY`` off and on. An Admin approves it; the caller reads the
  result, and the first approver may not read it.
* Who may approve the child's request is checked against the caller who
  started the run: the starter may not approve their own run's child call
  (unless they are an Admin), and the first approver may approve it like
  any other approver of that risk level.
* A child call that waits after a re-run made with a token carries no
  "token call" reason, so its starter may not approve it with a click.
* The permission check of a child call with a write on a second target
  names and checks the caller who started the run, and the child runs to
  the end after an Admin approves it.
* A run approved as a whole (its own request, not a step's) works the same
  way: a child call or a step of the step gate that waits belongs to the
  caller who started it.

All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager, nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.delegation import ACTOR_SUB_KEY, delegated_operator
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import _preview
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    ResultAccessForbiddenError,
    SelfApprovalForbiddenError,
    approve_request,
    read_approval_result,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite import DispatchChild, enforce_subop_policy
from meho_backplane.operations.secondary_target_policy import (
    SecondaryTargetWrite,
    requester_policy_preflight,
    secondary_target_refusal,
)
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step
from tests._people_behind_helpers import give_service_a_known_person

pytestmark = pytest.mark.asyncio

#: A child call that waits with the switch off only: the plain "always ask" mark.
_CAUTION_CHILD = Step("probe.things.label", "caution", True)
#: A child call that waits with the switch on: a fixed rule covers a delete name.
_DANGEROUS_CHILD = Step("probe.things.delete", "dangerous", False)
#: The child call that waits in each switch state.
_CHILD_FOR = {False: _CAUTION_CHILD, True: _DANGEROUS_CHILD}

_PERSON_SUB = "person-starter-5"
_AGENT_SUB = "agent:probe-hosted"
_ADMIN_SUB = "admin-probe-8"
_OTHER_SUB = "person-other-9"
_STARTERS = ("service", "person", "hosted")
_BOTH_STATES = pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])


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


def _person(sub: str, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return h.operator(sub=sub).model_copy(update={"tenant_role": role})


async def _starter(kind: str, role: TenantRole) -> tuple[Operator, AbstractContextManager[Any]]:
    """The caller who starts the run, and the binding its call runs in."""
    if kind == "service":
        await give_service_a_known_person(h.TENANT_ID, c.SERVICE_SUB)
        return h.operator(sub=c.SERVICE_SUB, principal_kind=PrincipalKind.SERVICE), nullcontext()
    if kind == "hosted":
        hosted = structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: _AGENT_SUB})
        return delegated_operator(_person(_PERSON_SUB, role)), hosted
    if kind == "console":
        return _person(_PERSON_SUB, role).model_copy(update={"web_session": True}), nullcontext()
    return _person(_PERSON_SUB, role), nullcontext()


async def _approve(request_id: UUID, who: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=who, params=None)
        await s.commit()
    return await h.request(request_id)


async def _child_waits(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    stub: AsyncMock,
    *,
    on: bool,
    starter: str = "service",
    role: TenantRole = TenantRole.OPERATOR,
    step: Step | None = None,
) -> tuple[Operator, ApprovalRequest]:
    """Start the run, approve its first step, and let the run go on to the child call.

    Returns the caller who started the run and the child's own request.
    """
    c.switch(monkeypatch, on=on)
    step = step or _CHILD_FOR[on]
    await c.register(session, stub, step, child=True)
    caller, binding = await _starter(starter, role)
    with binding:
        first = await h.run(c.CHILD_RUN, caller)
    assert first.status == "awaiting_approval", first.error
    approved = await h.approve(UUID(first.extras["approval_request_id"]), sub=c.APPROVER_SUB)
    resumed = await resume_dispatch_after_approval(
        operator=c.token_approver(), request=approved, params=None
    )
    assert resumed.status == "awaiting_approval", resumed.error
    own = await c.assert_waits(step, child=True)
    assert str(own.id) == resumed.extras["approval_request_id"]
    return caller, own


async def _read_result(request_id: UUID, who: Operator) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        row = await read_approval_result(s, operator=who, request_id=request_id)
        await s.commit()
    return row


# -- the owner of the child's request -----------------------------------------------


@pytest.mark.parametrize("starter", _STARTERS)
@_BOTH_STATES
async def test_child_request_belongs_to_the_caller_who_started_the_run(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    starter: str,
    on: bool,
) -> None:
    """The child's request names the starter; after an Admin approves it, the starter reads it."""
    caller, own = await _child_waits(
        monkeypatch, session, stub_embedding_service, on=on, starter=starter
    )

    assert own.principal_sub == caller.sub
    assert own.principal_kind == caller.principal_kind.value
    assert own.principal_act == (_AGENT_SUB if starter == "hosted" else None)

    admin = _person(_ADMIN_SUB, TenantRole.TENANT_ADMIN)
    approved = await _approve(own.id, admin)
    ran: OperationResult = await resume_dispatch_after_approval(
        operator=admin, request=approved, params=None
    )
    assert ran.status == "ok", ran.error
    assert c.ran == [h.STEP_A, _CHILD_FOR[on].op_id]

    read = await _read_result(own.id, caller)
    assert read.resume_result is not None
    assert read.resume_result["status"] == "ok"
    with pytest.raises(ResultAccessForbiddenError):
        await _read_result(own.id, c.token_approver())


# -- who may approve the child's request --------------------------------------------


async def test_off_the_person_who_started_the_run_may_not_approve_its_caution_child(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """An Operator who started the run is refused as a self-approval."""
    caller, own = await _child_waits(
        monkeypatch, session, stub_embedding_service, on=False, starter="person"
    )

    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(own.id, caller)


@pytest.mark.parametrize("approver", [c.APPROVER_SUB, _OTHER_SUB])
async def test_off_the_first_approver_or_another_operator_may_approve_the_caution_child(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    approver: str,
) -> None:
    """The first approver is not the owner; like any Operator they may approve a caution child."""
    _, own = await _child_waits(
        monkeypatch, session, stub_embedding_service, on=False, starter="person"
    )

    approved = await _approve(own.id, _person(approver))
    assert approved.reviewed_by == approver


@pytest.mark.parametrize("starter", ["person", "service"])
async def test_on_an_operator_may_not_approve_the_dangerous_child(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    starter: str,
) -> None:
    """The starter is refused as a self-approval; the first approver needs an Admin's rights."""
    caller, own = await _child_waits(
        monkeypatch, session, stub_embedding_service, on=True, starter=starter
    )

    if starter == "person":
        with pytest.raises(SelfApprovalForbiddenError):
            await _approve(own.id, caller)
    with pytest.raises(ApprovalGroupForbiddenError):
        await _approve(own.id, _person(c.APPROVER_SUB))


async def test_on_an_admin_who_did_not_start_the_run_may_approve_the_dangerous_child(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    _, own = await _child_waits(
        monkeypatch, session, stub_embedding_service, on=True, starter="person"
    )

    approved = await _approve(own.id, _person(_ADMIN_SUB, TenantRole.TENANT_ADMIN))
    assert approved.reviewed_by == _ADMIN_SUB


async def test_on_an_admin_who_started_the_run_may_approve_its_dangerous_child(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """An Admin's self-approval rule covers the child call of their own run."""
    caller, own = await _child_waits(
        monkeypatch,
        session,
        stub_embedding_service,
        on=True,
        starter="person",
        role=TenantRole.TENANT_ADMIN,
    )

    approved = await _approve(own.id, caller)
    assert approved.reviewed_by == caller.sub


# -- a run approved as a whole ------------------------------------------------------


_WHOLE_RUN = "vault.composite.child_after_whole_approval"


async def whole_run(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """No step of its own waits: the run itself waits, then a child call waits."""
    return await dispatch_child(connector_id=h.CONNECTOR_ID, op_id=_CAUTION_CHILD.op_id, params={})


async def test_child_of_a_run_approved_as_a_whole_belongs_to_its_starter(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The run waits as a whole; after its approval the child's request names the starter."""
    c.switch(monkeypatch, on=False)
    await c.register(session, stub_embedding_service, _CAUTION_CHILD, child=True)
    await h.register_composite(session, _WHOLE_RUN, "whole_run", module=__name__)
    async with get_sessionmaker()() as s:
        await s.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _WHOLE_RUN)
            .values(requires_approval=True)
        )
        await s.commit()
    caller = _person(_PERSON_SUB)
    first = await h.run(_WHOLE_RUN, caller)
    assert first.status == "awaiting_approval", first.error

    approved = await h.approve(UUID(first.extras["approval_request_id"]), sub=c.APPROVER_SUB)
    resumed = await resume_dispatch_after_approval(
        operator=c.token_approver(), request=approved, params=None
    )

    assert resumed.status == "awaiting_approval", resumed.error
    own = await h.request(UUID(resumed.extras["approval_request_id"]))
    assert own.op_id == _CAUTION_CHILD.op_id
    assert own.principal_sub == _PERSON_SUB
    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(own.id, caller)


async def test_child_of_a_run_approved_as_a_whole_with_a_waiting_step_belongs_to_its_starter(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The run waits as a whole; a step that waits in the re-run names the starter."""
    c.switch(monkeypatch, on=False)
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, _WHOLE_STEP_RUN, "whole_step_run", module=__name__)
    async with get_sessionmaker()() as s:
        await s.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _WHOLE_STEP_RUN)
            .values(requires_approval=True)
        )
        await s.commit()
    caller = _person(_PERSON_SUB)
    first = await h.run(_WHOLE_STEP_RUN, caller)
    assert first.status == "awaiting_approval", first.error

    approved = await h.approve(UUID(first.extras["approval_request_id"]), sub=c.APPROVER_SUB)
    resumed = await resume_dispatch_after_approval(
        operator=c.token_approver(), request=approved, params=None
    )

    assert resumed.status == "awaiting_approval", resumed.error
    [own] = [row for row in await c.requests() if row.id != approved.id]
    assert own.op_id == _CAUTION_CHILD.op_id
    assert own.principal_sub == _PERSON_SUB
    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(own.id, caller)


_WHOLE_STEP_RUN = "vault.composite.step_after_whole_approval"


async def whole_step_run(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: DispatchChild
) -> Any:
    """No step of its own waits: the run itself waits, then a step of the step gate waits."""
    return await enforce_subop_policy(
        operator=operator,
        connector_id=h.STEP_CONNECTOR_ID,
        op_id=_CAUTION_CHILD.op_id,
        safety_level=_CAUTION_CHILD.safety_level,
        requires_approval=_CAUTION_CHILD.requires_approval,
        target=target,
        params={},
    )


# -- a child call that waits after a re-run made with a token -----------------------

#: A delete name at ``caution`` without the approval flag: with the switch on,
#: a fixed rule makes it wait.
_CAUTION_DELETE = Step("probe.things.delete", "caution", False)


@pytest.mark.parametrize("starter", ["person", "console", "hosted"])
async def test_on_the_starter_may_not_approve_a_child_after_a_token_rerun_with_a_click(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    starter: str,
) -> None:
    """The child's request has no "token call" reason; the starter's click is a self-approval."""
    _, own = await _child_waits(
        monkeypatch,
        session,
        stub_embedding_service,
        on=True,
        starter=starter,
        step=_CAUTION_DELETE,
    )

    assert own.proposed_effect.get("waits_because") is None
    click = _person(_PERSON_SUB).model_copy(update={"web_session": True})
    with pytest.raises(SelfApprovalForbiddenError):
        await _approve(own.id, click)


async def test_on_an_admin_who_did_not_start_the_run_runs_the_child_after_a_token_rerun(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    _, own = await _child_waits(
        monkeypatch,
        session,
        stub_embedding_service,
        on=True,
        starter="person",
        step=_CAUTION_DELETE,
    )

    admin = _person(_ADMIN_SUB, TenantRole.TENANT_ADMIN)
    approved = await _approve(own.id, admin)
    ran = await resume_dispatch_after_approval(operator=admin, request=approved, params=None)

    assert ran.status == "ok", ran.error
    assert c.ran == [h.STEP_A, _CAUTION_DELETE.op_id]


# -- the permission check of a child call with a write on a second target -----------

_SECOND_TARGET = SimpleNamespace(id=None, name="probe-second-target")
#: The answer of the second-target check of each run of the child.
_REFUSALS: list[str | None] = []


def _second_write() -> SecondaryTargetWrite:
    """The child's write on the second target: the child's own operation."""
    return SecondaryTargetWrite(h.CONNECTOR_ID, (c.current[0].op_id,), "probe target")


async def _second_target_preflight(ctx: PreviewContext) -> dict[str, Any]:
    return await requester_policy_preflight(ctx.operator, _second_write(), _SECOND_TARGET)


async def second_target_child(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """The child checks its write on the second target as the Admin who runs it."""
    admin = _person(_ADMIN_SUB, TenantRole.TENANT_ADMIN)
    _REFUSALS.append(await secondary_target_refusal(admin, _second_write(), _SECOND_TARGET))
    c.ran.append(c.current[0].op_id)
    return {"done": True}


@pytest.mark.parametrize("starter", ["person", "service"])
@_BOTH_STATES
async def test_child_permission_check_names_and_checks_the_caller_who_started_the_run(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    starter: str,
    on: bool,
) -> None:
    """The child's record names the starter; it runs to the end after an Admin approves it."""
    step = _CHILD_FOR[on]
    admin = _person(_ADMIN_SUB, TenantRole.TENANT_ADMIN)
    _REFUSALS.clear()
    monkeypatch.setattr(c, "child_handler", second_target_child)
    monkeypatch.setitem(_preview._PERMISSION_PREFLIGHTS, step.op_id, _second_target_preflight)
    caller, own = await _child_waits(
        monkeypatch, session, stub_embedding_service, on=on, starter=starter
    )

    record = own.proposed_effect["permission_preflight"]["requester"]
    assert record["sub"] == caller.sub
    assert record["principal_kind"] == caller.principal_kind.value
    assert record["tenant_role"] == caller.tenant_role.value

    approved = await _approve(own.id, admin)
    ran = await resume_dispatch_after_approval(operator=admin, request=approved, params=None)

    assert ran.status == "ok", ran.error
    assert _REFUSALS == [None]
    assert c.ran == [h.STEP_A, step.op_id]
