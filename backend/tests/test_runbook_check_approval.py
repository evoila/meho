# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A runbook check that waits for approval reads its approved result on a retry.

A ``caution`` check of a person waits for approval, and the step fails.
MEHO keeps the id of the approval request on the step. When the assignee
retries the step:

* while the request waits, the step waits again and MEHO makes no new
  approval request;
* after the request is approved and the approved call ran, the check uses
  the result of that call, and the operation does not run again;
* after a rejection, the check asks for approval again;
* when the check is a multi-step operation and one of its steps waits, the
  retry after the approved run uses the stored result of the multi-step
  operation, and nothing runs again.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.operations.approval_queue import (
    approve_request,
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.runbooks.run_service import PreviousStepFailedError, RunbookRunService
from meho_backplane.runbooks.runs_schemas import (
    CurrentStepResponse,
    NextStepRequest,
    RunCompletedResponse,
    StartRunRequest,
)
from tests.test_runbook_step_caller_identity import (  # noqa: F401 -- autouse fixtures
    _HANDLER_CALLS,
    _OP,
    _TARGET,
    _TENANT,
    _advance,
    _person,
    _reset_dispatcher_state,
    _seed_tenant_and_target,
    _settings_env,
    _start,
    _step_state,
    _template,
)
from tests.test_runbooks_run_service import _seed_published_template, _StubConnector

_MULTI_OP = "stub.multi_step_check"
_MULTI_STEP = "POST:/stub/step"
_CONNECTOR = "stub-1.x"

_APPROVER_SUB = "person-approver"
_APPROVER = _person(_APPROVER_SUB).model_copy(update={"self_approver": True})


#: Each run of :func:`multi_step_check` appends ``"enter"``, and ``"step"``
#: once its step that waits for approval may go ahead.
_MULTI_CALLS: list[str] = []


async def multi_step_check(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """A multi-step operation with one step that waits for approval."""
    _MULTI_CALLS.append("enter")
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=_MULTI_STEP,
        safety_level="safe",
        requires_approval=True,
        target=target,
        params={},
    )
    if gated is not None:
        return gated
    _MULTI_CALLS.append("step")
    return {"ok": True}


@pytest.fixture(autouse=True)
def _reset_multi_calls() -> None:
    _MULTI_CALLS.clear()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.0] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.0] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


async def _retry(service: RunbookRunService, run_id: uuid.UUID) -> Any:
    person = _person()
    return await service.next_step(
        _TENANT,
        person.sub,
        run_id,
        NextStepRequest(last_verified=True, retry=True),
        caller=person,
    )


async def _requests(session: AsyncSession) -> list[ApprovalRequest]:
    rows = (
        await session.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.op_id == _OP)
            .order_by(ApprovalRequest.created_at)
        )
    ).all()
    for row in rows:
        await session.refresh(row)
    return list(rows)


async def _wait_for_approval(
    db_session: AsyncSession, stub_embedding_service: AsyncMock
) -> tuple[RunbookRunService, uuid.UUID, uuid.UUID]:
    """Start a run with a ``caution`` check; the first advance waits for approval."""
    service, run_id, _ = await _start(
        _person(), stub_embedding_service, expect={"ok": True}, safety_level="caution"
    )
    with pytest.raises(PreviousStepFailedError, match="awaiting_approval"):
        await _advance(service, _person(), run_id)
    requests = await _requests(db_session)
    assert len(requests) == 1
    assert _HANDLER_CALLS == []
    return service, run_id, requests[0].id


async def _start_multi_step_check() -> tuple[RunbookRunService, uuid.UUID]:
    """Start a run whose check is :data:`_MULTI_OP`, a ``safe`` multi-step operation."""
    register_connector_v2(product="stub", version="", impl_id="", cls=_StubConnector)
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="stub",
                version="1.x",
                impl_id="stub",
                op_id=_MULTI_OP,
                source_kind="composite",
                handler_ref=f"{__name__}.multi_step_check",
                summary="Multi-step check.",
                description="Multi-step check.",
                tags=[],
                parameter_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=[0.0] * EMBEDDING_DIMENSION,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    await _seed_tenant_and_target()
    await _seed_published_template(_TENANT, "check", body=_template({"ok": True}, _MULTI_OP))
    service = RunbookRunService()
    start = await service.start_run(
        _TENANT,
        _person().sub,
        StartRunRequest(template_slug="check", target=_TARGET, params={}),
    )
    assert isinstance(start, CurrentStepResponse)
    return service, start.run_id


async def _approve_and_run(request_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=_APPROVER)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        result = await resume_dispatch_after_approval(operator=_APPROVER, request=row)
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_retry_after_approval_uses_the_approved_result(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """Check, approval, the approved call runs, retry: the step passes, the op ran once."""
    service, run_id, request_id = await _wait_for_approval(db_session, stub_embedding_service)
    await _approve_and_run(request_id)
    assert _HANDLER_CALLS == [_APPROVER_SUB]

    resp = await _retry(service, run_id)

    assert isinstance(resp, RunCompletedResponse)
    assert _HANDLER_CALLS == [_APPROVER_SUB]
    assert len(await _requests(db_session)) == 1
    state = await _step_state(db_session, run_id)
    assert state.state == "verified"
    assert state.verify_response is not None
    assert state.verify_response["actual"] == {"ok": True}


@pytest.mark.asyncio
async def test_retry_while_waiting_makes_no_new_request(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A retry before the approval waits again, with the same request, and runs nothing."""
    service, run_id, request_id = await _wait_for_approval(db_session, stub_embedding_service)

    for _ in range(2):
        with pytest.raises(PreviousStepFailedError, match=f"approval request {request_id}"):
            await _retry(service, run_id)

    assert _HANDLER_CALLS == []
    assert [row.id for row in await _requests(db_session)] == [request_id]
    assert (await _step_state(db_session, run_id)).state == "failed"


@pytest.mark.asyncio
async def test_retry_after_rejection_asks_again(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """A retry after a rejection makes a new approval request and runs nothing."""
    service, run_id, request_id = await _wait_for_approval(db_session, stub_embedding_service)
    async with get_sessionmaker()() as session:
        await reject_request(session, request_id, operator=_APPROVER, reason="not now")
        await session.commit()

    with pytest.raises(PreviousStepFailedError, match="awaiting_approval"):
        await _retry(service, run_id)

    assert _HANDLER_CALLS == []
    requests = await _requests(db_session)
    assert len(requests) == 2
    assert requests[1].status == "pending"


@pytest.mark.asyncio
async def test_retry_after_step_approval_uses_the_multi_step_result(
    db_session: AsyncSession,
) -> None:
    """A step of the multi-step check waits; after its approved run the retry runs nothing."""
    service, run_id = await _start_multi_step_check()
    with pytest.raises(PreviousStepFailedError, match="awaiting_approval"):
        await _advance(service, _person(), run_id)
    requests = list((await db_session.scalars(select(ApprovalRequest))).all())
    assert len(requests) == 1
    request = requests[0]
    assert request.op_id == _MULTI_STEP
    assert request.connector_id == _CONNECTOR
    assert request.resume_parent is not None
    assert request.resume_parent["op_id"] == _MULTI_OP
    assert _MULTI_CALLS == ["enter"]

    await _approve_and_run(request.id)
    assert _MULTI_CALLS == ["enter", "enter", "step"]

    resp = await _retry(service, run_id)

    assert isinstance(resp, RunCompletedResponse)
    assert _MULTI_CALLS == ["enter", "enter", "step"]
    assert len((await db_session.scalars(select(ApprovalRequest))).all()) == 1
    state = await _step_state(db_session, run_id)
    assert state.state == "verified"
    assert state.verify_response is not None
    assert state.verify_response["actual"] == {"ok": True}


async def _approval_audit_rows(session: AsyncSession, path: str) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.method == "APPROVAL", AuditLog.path == path)
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_approval_request_row_names_run_and_step(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """The ``approval.request`` row of a waiting check has the run id and the step id."""
    _service, run_id, request_id = await _wait_for_approval(db_session, stub_embedding_service)
    state = await _step_state(db_session, run_id)

    rows = await _approval_audit_rows(db_session, "approval.request")

    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == run_id
    assert row.step_id == state.step_id
    assert row.payload["run_id"] == str(run_id)
    assert row.payload["step_id"] == state.step_id
    assert row.payload["approval_request_id"] == str(request_id)
    assert row.operator_sub == _person().sub


@pytest.mark.asyncio
async def test_approval_result_row_names_run_and_step(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """The ``approval.result`` row of a retry has the run id and the step id."""
    service, run_id, _ = await _wait_for_approval(db_session, stub_embedding_service)
    state = await _step_state(db_session, run_id)
    await _retry_waiting(service, run_id)

    rows = await _approval_audit_rows(db_session, "approval.result")

    assert len(rows) == 1
    assert rows[0].run_id == run_id
    assert rows[0].step_id == state.step_id
    assert rows[0].payload["run_id"] == str(run_id)
    assert rows[0].payload["step_id"] == state.step_id


@pytest.mark.asyncio
async def test_approval_decision_row_has_no_run_or_step(
    db_session: AsyncSession,
    stub_embedding_service: AsyncMock,
) -> None:
    """The approver's decision row is not in the runbook step, so it has neither id."""
    _, _, request_id = await _wait_for_approval(db_session, stub_embedding_service)
    await _approve_and_run(request_id)

    rows = await _approval_audit_rows(db_session, "approval.decision")

    assert len(rows) == 1
    assert rows[0].run_id is None
    assert rows[0].step_id is None


async def _retry_waiting(service: RunbookRunService, run_id: uuid.UUID) -> None:
    with pytest.raises(PreviousStepFailedError, match="awaiting_approval"):
        await _retry(service, run_id)
