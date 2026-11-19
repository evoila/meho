# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step run with a credential step and a later wait runs to its end.

Every test drives the real dispatcher against the migrated SQLite engine and
counts the requests that reached the target
(``_composite_credential_steps_helpers.sent``). Covered:

* change, wait, change: each change is sent once;
* credential write, wait, change: the write is sent once and the run
  finishes; the stored record says only that the write is finished;
* credential read, wait, change: the read is sent again after the approval
  and the run finishes; when the read gives another answer and the approved
  step changes with it, the run stops and the step is not sent;
* two credential writes, each one waits: each write is sent once;
* a credential write whose answer was lost, then a wait: MEHO asks for no
  approval and stops at once;
* a credential write through ``dispatch_child``, wait, change: the write is
  sent once and gives back no result after the approval;
* the stored record holds no credential value and no field of a credential
  step's answer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import _preview, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.composite_steps import (
    STEP_DONE_NO_RESULT,
    STEP_RUN_AGAIN,
    STEP_STARTED,
    StepJournal,
)
from meho_backplane.settings import get_settings

from . import _composite_credential_steps_helpers as c
from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_MODULE = "tests._composite_credential_steps_helpers"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires; keep a credential write's input encrypted."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # No Vault here: skip the Vault permission check of a credential write before its wait.
    for op_id in (c.CREDENTIAL_WRITE_STEP, c.CREDENTIAL_READ_STEP, c.CREDENTIAL_WRITE_CHILD):
        monkeypatch.delitem(_preview._PERMISSION_PREFLIGHTS, op_id, raising=False)
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches, the connector registry and the sent requests."""
    reset_dispatcher_caches()
    clear_registry()
    c.reset()
    yield
    reset_dispatcher_caches()
    clear_registry()
    c.reset()


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
    async with get_sessionmaker()() as s_:
        yield s_


async def _setup(embedding: AsyncMock, session: AsyncSession, op_id: str, handler: str) -> None:
    await h.register_child(embedding)
    await c.register_credential_write_child(embedding)
    await h.register_composite(session, op_id, handler, module=_MODULE)


async def _approve_and_resume(result: OperationResult) -> OperationResult:
    """A second person approves the waiting step; the person's surface runs it."""
    assert result.status == "awaiting_approval", result.error
    request = await h.approve(UUID(result.extras["approval_request_id"]))
    resumed: OperationResult = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=request, params=None
    )
    return resumed


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as s_:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at)
        return list((await s_.execute(stmt)).scalars().all())


def _steps(request: ApprovalRequest) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = (request.resume_parent or {}).get("steps") or []
    return steps


def _assert_no_secret(request: ApprovalRequest) -> None:
    """No credential value and no field of a credential step's answer is on the request."""
    stored = json.dumps(
        {"resume_parent": request.resume_parent, "params": request.params}, default=str
    )
    assert c.SECRET_VALUE not in stored
    for step in _steps(request):
        if step["op_id"] in (c.CREDENTIAL_WRITE_STEP, c.CREDENTIAL_READ_STEP):
            assert "result" not in step
            assert "version" not in json.dumps(step)


# ---------------------------------------------------------------------------
# Change, wait, change
# ---------------------------------------------------------------------------


async def test_change_wait_change_sends_each_change_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """Change, a step that waits, change: one request each, and the run finishes."""
    await _setup(
        stub_embedding_service, session, c.WRITE_WAIT_WRITE_OP_ID, "write_wait_write_composite"
    )

    first = await h.run(c.WRITE_WAIT_WRITE_OP_ID)
    assert c.sent == [c.CHANGE_STEP]

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert c.sent == [c.CHANGE_STEP, c.WAIT_STEP, c.SECOND_CHANGE_STEP]


# ---------------------------------------------------------------------------
# Credential write, wait, change
# ---------------------------------------------------------------------------


async def test_credential_write_wait_change_finishes_and_sends_the_write_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The write is finished: after the approval it is not sent again and gives no result."""
    await _setup(
        stub_embedding_service,
        session,
        c.CREDENTIAL_WRITE_WAIT_CHANGE_OP_ID,
        "credential_write_wait_change_composite",
    )

    first = await h.run(c.CREDENTIAL_WRITE_WAIT_CHANGE_OP_ID)
    assert first.status == "awaiting_approval", first.error
    assert c.sent == [c.CREDENTIAL_WRITE_STEP]
    (waiting,) = await _requests()
    assert [(step["op_id"], step["status"]) for step in _steps(waiting)] == [
        (c.CREDENTIAL_WRITE_STEP, STEP_DONE_NO_RESULT)
    ]
    _assert_no_secret(waiting)

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert second.result == {"done": True, "credential_replayed": True}
    assert c.sent == [c.CREDENTIAL_WRITE_STEP, c.WAIT_STEP, c.CHANGE_STEP]


async def test_credential_write_through_dispatch_child_is_sent_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """A ``dispatch_child`` credential write gives back ``ok`` with no result after the approval."""
    await _setup(
        stub_embedding_service,
        session,
        c.CHILD_CREDENTIAL_WRITE_OP_ID,
        "child_credential_write_wait_change_composite",
    )

    first = await h.run(c.CHILD_CREDENTIAL_WRITE_OP_ID)
    assert first.status == "awaiting_approval", first.error
    assert c.sent == [c.CREDENTIAL_WRITE_CHILD]
    (waiting,) = await _requests()
    assert [(step["op_id"], step["status"]) for step in _steps(waiting)] == [
        (c.CREDENTIAL_WRITE_CHILD, STEP_DONE_NO_RESULT)
    ]
    assert c.SECRET_VALUE not in json.dumps(waiting.resume_parent)
    assert "result" not in _steps(waiting)[0]

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert second.result == {"done": True, "child_result": None}
    assert c.sent == [c.CREDENTIAL_WRITE_CHILD, c.WAIT_STEP, c.CHANGE_STEP]


async def test_credential_write_with_a_lost_answer_stops_before_the_wait(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """MEHO does not know if the write happened: it asks no person and stops at once."""
    await _setup(
        stub_embedding_service,
        session,
        c.LOST_CREDENTIAL_WRITE_OP_ID,
        "credential_write_answer_lost_composite",
    )

    first = await h.run(c.LOST_CREDENTIAL_WRITE_OP_ID)

    assert first.status == "error"
    assert first.extras["error_code"] == "composite_resume_step_outcome_unknown"
    assert first.extras["blocked_op_id"] == c.CREDENTIAL_WRITE_STEP
    assert await _requests() == [], "no approval request is made"
    assert c.sent == [c.CREDENTIAL_WRITE_STEP], "the step that would wait is not sent"


async def test_two_credential_writes_that_each_wait_are_sent_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """Each credential write waits for its own approval; each is sent once over three runs."""
    await _setup(
        stub_embedding_service,
        session,
        c.TWO_CREDENTIAL_WRITES_OP_ID,
        "two_credential_writes_composite",
    )

    first = await h.run(c.TWO_CREDENTIAL_WRITES_OP_ID)
    assert c.sent == []

    second = await _approve_and_resume(first)
    assert second.status == "awaiting_approval", second.error
    assert c.sent == [c.CREDENTIAL_WRITE_STEP]

    third = await _approve_and_resume(second)
    assert third.status == "ok", third.error
    assert c.sent == [c.CREDENTIAL_WRITE_STEP, c.CREDENTIAL_WRITE_STEP]
    requests = await _requests()
    assert len(requests) == 2
    for request in requests:
        _assert_no_secret(request)


# ---------------------------------------------------------------------------
# Credential read, wait, change
# ---------------------------------------------------------------------------


async def test_credential_read_wait_change_reads_again_and_finishes(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The read is sent again after the approval; the waiting step and the change once each."""
    await _setup(
        stub_embedding_service,
        session,
        c.CREDENTIAL_READ_WAIT_CHANGE_OP_ID,
        "credential_read_wait_change_composite",
    )

    first = await h.run(c.CREDENTIAL_READ_WAIT_CHANGE_OP_ID)
    assert first.status == "awaiting_approval", first.error
    assert c.sent == [c.CREDENTIAL_READ_STEP]
    (waiting,) = await _requests()
    assert waiting.op_id == c.WAIT_STEP
    assert [(step["op_id"], step["status"]) for step in _steps(waiting)] == [
        (c.CREDENTIAL_READ_STEP, STEP_RUN_AGAIN)
    ]
    _assert_no_secret(waiting)

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert c.sent == [c.CREDENTIAL_READ_STEP, c.CREDENTIAL_READ_STEP, c.WAIT_STEP, c.CHANGE_STEP]


async def test_credential_read_with_a_new_answer_stops_before_the_approved_step(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The read gives another version, so the approved step's params differ: nothing is sent."""
    await _setup(
        stub_embedding_service,
        session,
        c.CREDENTIAL_READ_WAIT_CHANGE_OP_ID,
        "credential_read_wait_change_composite",
    )
    first = await h.run(c.CREDENTIAL_READ_WAIT_CHANGE_OP_ID)
    c.read_answer["version"] = 2

    second = await _approve_and_resume(first)

    assert second.status == "error"
    assert second.extras["error_code"] == "composite_resume_repeats_write"
    assert second.extras["blocked_op_id"] == c.WAIT_STEP
    assert c.sent == [c.CREDENTIAL_READ_STEP, c.CREDENTIAL_READ_STEP]


# ---------------------------------------------------------------------------
# The step record
# ---------------------------------------------------------------------------


def test_credential_write_that_did_not_end_well_stays_started() -> None:
    """Only a credential write that ended well is finished; its answer is never kept."""
    journal = StepJournal(composite_op_id="vault.composite.x")
    good = journal.start("vault-1.x", "vault.kv.put", None, {"path": "a"})
    journal.finish(good, {"data": {"value": c.SECRET_VALUE}})
    bad = journal.start("vault-1.x", "vault.kv.put", None, {"path": "b"})
    journal.finish(bad, {"error": c.SECRET_VALUE}, ended_well=False)

    assert good["status"] == STEP_DONE_NO_RESULT
    assert bad["status"] == STEP_STARTED
    assert c.SECRET_VALUE not in json.dumps(journal.stored())
    assert journal.first_unfinished() == "vault.kv.put"


def test_finished_credential_steps_do_not_stop_a_later_wait() -> None:
    """A finished credential write and a credential read are not steps with an unknown end."""
    journal = StepJournal(composite_op_id="vault.composite.x")
    written = journal.start("vault-1.x", "vault.token.create", None, {"policy": "p"})
    journal.finish(written, {"auth": {"client_token": c.SECRET_VALUE}})
    read = journal.start("vault-1.x", "vault.kv.read", None, {"path": "a"})
    journal.finish(read, {"data": {"value": c.SECRET_VALUE}})

    assert (written["status"], read["status"]) == (STEP_DONE_NO_RESULT, STEP_RUN_AGAIN)
    assert journal.first_unfinished() is None
    assert c.SECRET_VALUE not in json.dumps(journal.stored())


def test_read_taken_again_is_stored_once() -> None:
    """A read that a resume sends again has one entry in the record, in its place."""
    first_run = StepJournal(composite_op_id="vault.composite.x")
    first_run.start("vault-1.x", "vault.kv.read", None, {"path": "a"})
    first_run.finish(first_run.start("vault-1.x", "POST:/a", None, {"n": 1}), {"id": "a-1"})
    resume = StepJournal(composite_op_id="vault.composite.x", replay=first_run.stored())

    taken = resume.take("vault-1.x", "vault.kv.read", None, {"path": "a"})
    assert taken is not None and taken["status"] == STEP_RUN_AGAIN
    resume.start("vault-1.x", "vault.kv.read", None, {"path": "a"})

    assert [entry["op_id"] for entry in resume.stored()] == ["vault.kv.read", "POST:/a"]
