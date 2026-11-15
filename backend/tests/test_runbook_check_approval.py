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
* after a rejection, the check asks for approval again.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_queue import (
    approve_request,
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.runbooks.run_service import PreviousStepFailedError, RunbookRunService
from meho_backplane.runbooks.runs_schemas import NextStepRequest, RunCompletedResponse
from tests.test_runbook_step_caller_identity import (  # noqa: F401 -- autouse fixtures
    _HANDLER_CALLS,
    _OP,
    _TENANT,
    _advance,
    _person,
    _reset_dispatcher_state,
    _settings_env,
    _start,
    _step_state,
)

_APPROVER_SUB = "person-approver"
_APPROVER = _person(_APPROVER_SUB).model_copy(update={"self_approver": True})


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
