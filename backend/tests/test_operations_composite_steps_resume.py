# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A multi-step operation that waits and goes on never sends a finished step again.

Every test drives the real dispatcher against the migrated SQLite engine and
counts the changes that reached the target (``_composite_steps_helpers.sent``).
Covered:

* a change, then a step that waits: after the approval the change was sent
  once, for a person, a scheduled run's service login and an agent; the
  change runs through ``dispatch_child`` or through ``run_governed_subop``;
* two steps of one run wait: the run waits at the second one too, and each
  change is sent once over the three runs;
* a step with no stored end (checked only by the gate, or its call failed):
  the run after the approval stops with
  ``composite_resume_step_outcome_unknown`` and sends nothing again;
* a rejected approval: nothing more runs;
* the step record itself: a result with a secret-named value is not kept.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.composite_steps import (
    STEP_DONE,
    STEP_STARTED,
    StepJournal,
)
from meho_backplane.settings import get_settings

from . import _composite_steps_helpers as s
from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_MODULE = "tests._composite_steps_helpers"


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
    """Reset dispatcher caches, the connector registry and the sent changes."""
    reset_dispatcher_caches()
    clear_registry()
    s.sent.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    s.sent.clear()


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
    await s.register_change_child(embedding)
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


def _requester(kind: str) -> Operator:
    if kind == "scheduled":
        return h.operator(sub="svc-scheduler", principal_kind=PrincipalKind.SERVICE)
    return h.operator()


# ---------------------------------------------------------------------------
# A change, then a step that waits: the change is sent once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["person", "scheduled"])
async def test_child_change_before_a_waiting_step_is_sent_once(
    stub_embedding_service: AsyncMock, session: AsyncSession, who: str
) -> None:
    """A ``dispatch_child`` change is not sent again when the run goes on after the approval."""
    await _setup(
        stub_embedding_service, session, s.CHILD_THEN_WAIT_OP_ID, "child_change_then_wait_composite"
    )

    first = await h.run(s.CHILD_THEN_WAIT_OP_ID, _requester(who))
    assert s.sent == [s.CHANGE_CHILD_OP_ID]

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert s.sent == [s.CHANGE_CHILD_OP_ID, s.CREDENTIAL_STEP]
    # The change left one step row: the run after the approval did not run it.
    child_rows = await h.rows(s.CHANGE_CHILD_OP_ID)
    assert [row.payload["result_status"] for row in child_rows] == ["ok"]


@pytest.mark.parametrize("who", ["person", "scheduled"])
async def test_change_before_a_waiting_credential_step_is_sent_once(
    stub_embedding_service: AsyncMock, session: AsyncSession, who: str
) -> None:
    """A direct change and then a credential step that waits: one request each.

    The credential step's params use the change's result (``t-1``). The run
    after the approval gets that result from the stored steps, so the
    approved step still matches and the change is not sent again.
    """
    await _setup(
        stub_embedding_service,
        session,
        s.STEP_THEN_CREDENTIAL_OP_ID,
        "change_then_credential_composite",
    )

    first = await h.run(s.STEP_THEN_CREDENTIAL_OP_ID, _requester(who))
    assert s.sent == [s.CHANGE_STEP]

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert second.result == {"done": True, "id": "t-1"}
    assert s.sent == [s.CHANGE_STEP, s.CREDENTIAL_STEP]


async def test_change_before_a_waiting_step_is_sent_once_on_the_agents_path(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """An agent that waited runs the approved request itself: the change is still sent once."""
    await _setup(
        stub_embedding_service,
        session,
        s.STEP_THEN_CREDENTIAL_OP_ID,
        "change_then_credential_composite",
    )
    agent = h.operator(sub="agent-requester", principal_kind=PrincipalKind.AGENT)
    first = await h.run(s.STEP_THEN_CREDENTIAL_OP_ID, agent)
    assert first.status == "awaiting_approval", first.error
    assert s.sent == [s.CHANGE_STEP]
    await h.approve(UUID(first.extras["approval_request_id"]))

    resumed = await resume_or_surface_awaiting_approval(
        operator=agent,
        call_arguments={
            "connector_id": h.CONNECTOR_ID,
            "op_id": s.STEP_THEN_CREDENTIAL_OP_ID,
            "params": {},
            "target": None,
        },
        awaiting_envelope=first.model_dump(mode="json"),
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "ok", resumed
    assert s.sent == [s.CHANGE_STEP, s.CREDENTIAL_STEP]


# ---------------------------------------------------------------------------
# Two steps of one run wait
# ---------------------------------------------------------------------------


async def test_two_waiting_steps_in_one_run_send_each_change_once(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """Change, wait, change, wait: the run waits twice and sends every change once."""
    await _setup(
        stub_embedding_service, session, s.TWO_WAITS_OP_ID, "change_wait_change_wait_composite"
    )

    first = await h.run(s.TWO_WAITS_OP_ID)
    assert s.sent == [s.CHANGE_STEP]

    second = await _approve_and_resume(first)
    assert second.status == "awaiting_approval", second.error
    assert second.extras["approval_request_id"] != first.extras["approval_request_id"]
    assert s.sent == [s.CHANGE_STEP, s.CREDENTIAL_STEP, s.SECOND_CHANGE_STEP]

    third = await _approve_and_resume(second)
    assert third.status == "ok", third.error
    assert s.sent == [s.CHANGE_STEP, s.CREDENTIAL_STEP, s.SECOND_CHANGE_STEP, s.SECOND_WAIT_STEP]

    requests = await _requests()
    assert len(requests) == 2
    # The second request names the requester of the first, not the approver.
    assert requests[1].principal_sub == requests[0].principal_sub == "op-requester"
    # It stores the steps of the run so far: the three it did, each with its end.
    steps = (requests[1].resume_parent or {}).get("steps")
    assert [(step["op_id"], step["status"]) for step in steps] == [
        (s.CHANGE_STEP, STEP_DONE),
        (s.CREDENTIAL_STEP, STEP_DONE),
        (s.SECOND_CHANGE_STEP, STEP_DONE),
    ]


# ---------------------------------------------------------------------------
# A step with no stored end stops the run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "handler", "blocked"),
    [
        (s.GATED_THEN_WAIT_OP_ID, "gated_change_then_wait_composite", s.CHANGE_STEP),
        (s.LOST_END_OP_ID, "change_without_end_then_wait_composite", s.CHANGE_STEP),
    ],
    ids=["checked-only-by-the-gate", "call-failed"],
)
async def test_step_without_a_stored_end_stops_the_run_and_is_not_sent_again(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    op_id: str,
    handler: str,
    blocked: str,
) -> None:
    """MEHO does not know if the earlier step made its change, so it stops and says so."""
    await _setup(stub_embedding_service, session, op_id, handler)

    first = await h.run(op_id)
    assert s.sent == [s.CHANGE_STEP]

    second = await _approve_and_resume(first)

    assert second.status == "error"
    assert second.extras["error_code"] == "composite_resume_step_outcome_unknown"
    assert second.extras["blocked_op_id"] == blocked
    assert second.extras["execution_outcome"] == "uncertain"
    assert s.sent == [s.CHANGE_STEP], "nothing is sent again, and the run stops"


# ---------------------------------------------------------------------------
# A rejected approval
# ---------------------------------------------------------------------------


async def test_rejected_approval_runs_nothing_more(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """A person rejects the waiting step: no step runs again and no later step runs."""
    await _setup(
        stub_embedding_service,
        session,
        s.STEP_THEN_CREDENTIAL_OP_ID,
        "change_then_credential_composite",
    )
    first = await h.run(s.STEP_THEN_CREDENTIAL_OP_ID)
    assert first.status == "awaiting_approval", first.error
    request_id = UUID(first.extras["approval_request_id"])
    async with get_sessionmaker()() as s_:
        await reject_request(s_, request_id, operator=h.operator(sub="op-approver"))
        await s_.commit()
    rejected = await h.request(request_id)
    assert rejected.status == ApprovalRequestStatus.REJECTED.value

    # A surface that tries to run the rejected request anyway gets a refusal.
    resumed = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=rejected, params=None
    )

    assert resumed.status != "ok"
    assert s.sent == [s.CHANGE_STEP]


# ---------------------------------------------------------------------------
# The step record
# ---------------------------------------------------------------------------


def test_result_with_a_secret_value_is_not_kept() -> None:
    """A result with a secret-named value stays "started", so it is never replayed."""
    journal = StepJournal(composite_op_id="vault.composite.x")
    kept = journal.start("vault-1.x", "POST:/a", {"name": "a"})
    journal.finish(kept, {"id": "a-1"})
    secret = journal.start("vault-1.x", "POST:/b", {"name": "b"})
    journal.finish(secret, {"id": "b-1", "token": "s.made-up-value"})
    not_json = journal.start("vault-1.x", "POST:/c", {"name": "c"})
    journal.finish(not_json, object())

    assert (kept["status"], kept["result"]) == (STEP_DONE, {"id": "a-1"})
    assert secret["status"] == STEP_STARTED
    assert "result" not in secret
    assert not_json["status"] == STEP_STARTED
    assert "made-up-value" not in str(journal.stored())


def test_same_step_twice_is_matched_in_order() -> None:
    """Two runs of one step with the same params are matched once each, in order."""
    first_run = StepJournal(composite_op_id="vault.composite.x")
    for value in ({"n": 1}, {"n": 2}):
        entry = first_run.start("vault-1.x", "POST:/a", {"name": "a"})
        first_run.finish(entry, value)
    resume = StepJournal(composite_op_id="vault.composite.x", replay=first_run.stored())

    one = resume.take("vault-1.x", "POST:/a", {"name": "a"})
    two = resume.take("vault-1.x", "POST:/a", {"name": "a"})
    three = resume.take("vault-1.x", "POST:/a", {"name": "a"})
    other = resume.take("vault-1.x", "POST:/a", {"name": "other"})

    assert one is not None and one["result"] == {"n": 1}
    assert two is not None and two["result"] == {"n": 2}
    assert three is None
    assert other is None
