# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""After the step's approval, and when the operation's row cannot be saved (#4275).

Companion to ``test_operations_composite_wait_audit.py``. Every test drives
the real dispatcher against the migrated SQLite engine. Covered:

* after approval the second run gets a new audit id whose parent is the
  ``approval.request`` row, on the person's path and on the agent's path;
* two waits in a row replay as one tree (run 1 → request 1 → run 2 →
  request 2 → run 3), and runs 1 and 2 keep ``awaiting_approval``;
* a row that cannot be saved turns the result into ``audit_commit_failed``,
  and for a wait the result keeps the saved request's id;
* a call cancelled after the request is saved and before the operation's row
  leaves the step records as loose rows, and the request still runs the
  second run when approved.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations._audit as audit_module
from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires; keep the agent wait short."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS", "2.0")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so ``register_typed_operation`` skips ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """An :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


async def _resume(request: ApprovalRequest, *, sub: str = "op-approver") -> Any:
    """The person's path: the approver's surface runs the approved request."""
    return await resume_dispatch_after_approval(
        operator=h.operator(sub=sub), request=request, params=None
    )


# ---------------------------------------------------------------------------
# After approval: the second run links to the approval request
# ---------------------------------------------------------------------------


async def test_second_run_on_the_persons_path_links_to_the_request_row(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """Approved in the console / REST / CLI: new audit id, parent = ``approval.request`` row."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, h.WAIT_OP_ID, "step_waits_composite")
    first = await h.run(h.WAIT_OP_ID)
    assert first.status == "awaiting_approval", first.error
    request = await h.approve(UUID(first.extras["approval_request_id"]))

    second = await _resume(request)

    assert second.status == "ok", second.error
    assert second.audit_id is not None
    assert second.audit_id != first.audit_id
    runs = await h.rows(h.WAIT_OP_ID)
    assert [r.id for r in runs] == [first.audit_id, second.audit_id]
    assert runs[0].payload["result_status"] == "awaiting_approval"
    assert runs[1].payload["result_status"] == "ok"
    assert runs[1].parent_audit_id == request.request_audit_id


async def test_second_run_on_the_agents_path_links_to_the_request_row(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """An agent that waited runs the approved request itself: same link, new audit id."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, h.WAIT_OP_ID, "step_waits_composite")
    agent = h.operator(sub="agent-requester", principal_kind=PrincipalKind.AGENT)
    call_arguments: dict[str, Any] = {
        "connector_id": h.CONNECTOR_ID,
        "op_id": h.WAIT_OP_ID,
        "params": {},
        "target": None,
    }
    first = await h.run(h.WAIT_OP_ID, agent)
    assert first.status == "awaiting_approval", first.error
    request = await h.approve(UUID(first.extras["approval_request_id"]))

    resumed = await resume_or_surface_awaiting_approval(
        operator=agent,
        call_arguments=call_arguments,
        awaiting_envelope=first.model_dump(mode="json"),
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "ok", resumed
    runs = await h.rows(h.WAIT_OP_ID)
    assert len(runs) == 2
    assert runs[0].id == first.audit_id
    assert runs[0].payload["result_status"] == "awaiting_approval"
    assert runs[1].payload["result_status"] == "ok"
    assert runs[1].id != first.audit_id
    assert runs[1].parent_audit_id == request.request_audit_id


async def test_two_waits_in_a_row_replay_as_one_tree(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """Run 1 waits, approve, run 2 waits, approve, run 3 ends ok: one tree, no loose rows."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, h.TWO_WAIT_OP_ID, "two_waits_composite")
    session_id = uuid.uuid4()

    token = agent_session_id_var.set(session_id)
    try:
        run_1 = await h.run(h.TWO_WAIT_OP_ID)
    finally:
        agent_session_id_var.reset(token)
    assert run_1.status == "awaiting_approval", run_1.error
    request_1 = await h.approve(UUID(run_1.extras["approval_request_id"]))

    run_2 = await _resume(request_1)
    assert run_2.status == "awaiting_approval", run_2.error
    assert run_2.extras["approval_request_id"] != run_1.extras["approval_request_id"]
    request_2 = await h.approve(UUID(run_2.extras["approval_request_id"]), sub="op-approver-2")

    run_3 = await _resume(request_2, sub="op-approver-2")
    assert run_3.status == "ok", run_3.error

    runs = await h.rows(h.TWO_WAIT_OP_ID)
    assert [r.payload["result_status"] for r in runs] == [
        "awaiting_approval",
        "awaiting_approval",
        "ok",
    ]
    assert [r.id for r in runs] == [run_1.audit_id, run_2.audit_id, run_3.audit_id]
    assert runs[0].parent_audit_id is None
    assert runs[1].parent_audit_id == request_1.request_audit_id
    assert runs[2].parent_audit_id == request_2.request_audit_id

    # One tree: run 1 → request 1 → run 2 → request 2 → run 3.
    forest = await h.replay(session_id)
    assert len(forest) == 1
    node = forest[0]
    chain = [node.path]
    for expected in ("approval.request", h.TWO_WAIT_OP_ID, "approval.request", h.TWO_WAIT_OP_ID):
        (node,) = [child for child in node.children if child.path == expected]
        chain.append(node.path)
    assert chain == [
        h.TWO_WAIT_OP_ID,
        "approval.request",
        h.TWO_WAIT_OP_ID,
        "approval.request",
        h.TWO_WAIT_OP_ID,
    ]
    assert node.children == []


# ---------------------------------------------------------------------------
# The row cannot be saved, or the call stops before it is saved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "handler_name", "who"),
    [
        (h.WAIT_OP_ID, "step_waits_composite", h.operator()),
        (h.DENY_OP_ID, "step_denied_composite", h.operator(principal_kind=PrincipalKind.AGENT)),
    ],
    ids=["wait", "deny"],
)
async def test_unsaved_operation_row_turns_the_result_into_audit_commit_failed(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    handler_name: str,
    who: Operator,
) -> None:
    """Without a saved row the caller gets ``audit_commit_failed``, never a wait or a deny."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, op_id, handler_name)
    h.fail_operation_row(monkeypatch, op_id, RuntimeError("audit DB unavailable"))

    result = await h.run(op_id, who)

    assert result.status == "error"
    assert result.extras["error_code"] == "audit_commit_failed"
    assert result.extras["execution_outcome"] == "uncertain"
    assert result.extras["retry"] is False
    assert result.audit_id is None
    assert await h.rows(op_id) == []
    if op_id == h.WAIT_OP_ID:
        request = await h.request(UUID(result.extras["approval_request_id"]))
        assert request.status == ApprovalRequestStatus.PENDING.value
        assert str(request.id) in result.extras["remediation"]
    else:
        assert "approval_request_id" not in result.extras


async def test_cancelled_before_the_operation_row_leaves_loose_rows_and_a_valid_request(
    stub_embedding_service: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped after the request is saved: no repair row, loose rows, approval still works."""
    await h.register_child(stub_embedding_service)
    await h.register_composite(session, h.WAIT_OP_ID, "step_waits_composite")
    real_writer = h.fail_operation_row(monkeypatch, h.WAIT_OP_ID, asyncio.CancelledError())
    session_id = uuid.uuid4()

    token = agent_session_id_var.set(session_id)
    try:
        with pytest.raises(asyncio.CancelledError):
            await h.run(h.WAIT_OP_ID)
    finally:
        agent_session_id_var.reset(token)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        request = (await s.execute(select(ApprovalRequest))).scalar_one()
    assert request.status == ApprovalRequestStatus.PENDING.value
    request_row = await h.row(request.request_audit_id)
    assert request_row is not None
    operation_audit_id = request_row.parent_audit_id
    assert operation_audit_id is not None
    assert await h.row(operation_audit_id) is None
    assert await h.rows(h.WAIT_OP_ID) == []
    steps = await h.rows(h.CHILD_OP_ID)
    assert [step.parent_audit_id for step in steps] == [operation_audit_id]

    # Both saved rows point at an id with no row, so the replay view shows them as roots.
    forest = await h.replay(session_id)
    assert sorted(node.path for node in forest) == sorted([h.CHILD_OP_ID, "approval.request"])

    # MEHO is back: approving the request still runs the second run.
    monkeypatch.setattr(audit_module, "write_audit_row", real_writer)
    approved = await h.approve(request.id)
    second = await _resume(approved)
    assert second.status == "ok", second.error
    runs = await h.rows(h.WAIT_OP_ID)
    assert len(runs) == 1
    assert runs[0].id == second.audit_id
    assert runs[0].parent_audit_id == request.request_audit_id
