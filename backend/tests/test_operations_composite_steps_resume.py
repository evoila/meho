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
* a step with no stored end (checked only by the gate, or its call failed),
  then a step that would wait: MEHO asks for no approval and stops at once
  with ``composite_resume_step_outcome_unknown``;
* a rejected approval: nothing more runs;
* a re-run of the approved request after its run failed is refused, so no
  step is sent again;
* a credential write that waits: the change before it is sent once;
* a handler that changes a replayed result: the next wait still stores the
  result as the step returned it;
* the step record itself: a result with a secret-named value (also a
  number) is not kept, the result of a credential step is not kept under any
  field name, and a step is matched only on its own target.

Credential steps before a wait: ``test_operations_composite_credential_steps_resume.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import _preview, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    reject_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.approval_rerun import (
    RerunNeedsNewApprovalError,
    RerunRefusal,
    rerun_failed_request,
)
from meho_backplane.operations.composite_steps import (
    STEP_DONE,
    STEP_DONE_NO_RESULT,
    STEP_STARTED,
    StepJournal,
)
from meho_backplane.settings import get_settings
from tests._people_behind_helpers import give_agent_a_known_person, give_service_a_known_person

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


async def _requester(kind: str) -> Operator:
    if kind == "scheduled":
        await give_service_a_known_person(h.TENANT_ID, "svc-scheduler")
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

    first = await h.run(s.CHILD_THEN_WAIT_OP_ID, await _requester(who))
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

    first = await h.run(s.STEP_THEN_CREDENTIAL_OP_ID, await _requester(who))
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
    await give_agent_a_known_person(h.TENANT_ID, "agent-requester")
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


async def test_handler_that_changes_a_replayed_result_keeps_the_stored_result(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """The handler pops ``id`` from the change's result on every run; the third run still has it."""
    await _setup(
        stub_embedding_service,
        session,
        s.POP_RESULT_OP_ID,
        "change_pop_result_wait_wait_composite",
    )

    first = await h.run(s.POP_RESULT_OP_ID)
    second = await _approve_and_resume(first)
    assert second.status == "awaiting_approval", second.error
    third = await _approve_and_resume(second)

    assert third.status == "ok", third.error
    assert third.result == {"done": True, "id": "t-1"}
    assert s.sent == [s.CHANGE_STEP, s.CREDENTIAL_STEP, s.SECOND_WAIT_STEP]
    steps = ((await _requests())[1].resume_parent or {}).get("steps")
    assert steps[0]["result"] == {"id": "t-1"}


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
    """A later step would wait, but its approval could only stop the run: MEHO stops at once.

    MEHO does not know if the earlier step made its change, so a run after an
    approval would stop there. So no person is asked, and nothing is sent again.
    """
    await _setup(stub_embedding_service, session, op_id, handler)

    first = await h.run(op_id)

    assert first.status == "error"
    assert first.extras["error_code"] == "composite_resume_step_outcome_unknown"
    assert first.extras["blocked_op_id"] == blocked
    assert first.extras["execution_outcome"] == "uncertain"
    assert s.CREDENTIAL_STEP in (first.error or "")
    assert await _requests() == [], "no approval request is made"
    assert s.sent == [s.CHANGE_STEP], "the step that would wait is not sent"


# ---------------------------------------------------------------------------
# A re-run of the approved request after its run failed
# ---------------------------------------------------------------------------


async def test_rerun_of_a_failed_multi_step_run_is_refused(
    stub_embedding_service: AsyncMock, session: AsyncSession
) -> None:
    """Change, wait, change, a failing step; approve; re-run: refused, each change sent once."""
    await _setup(
        stub_embedding_service,
        session,
        s.FAILS_AFTER_WAIT_OP_ID,
        "change_wait_change_fail_composite",
    )
    first = await h.run(s.FAILS_AFTER_WAIT_OP_ID)
    second = await _approve_and_resume(first)
    assert second.status == "error", second
    sent_once = [s.CHANGE_STEP, s.CREDENTIAL_STEP, s.SECOND_CHANGE_STEP, s.FAILING_STEP]
    assert s.sent == sent_once

    with pytest.raises(RerunNeedsNewApprovalError, match="start a new run") as refused:
        await rerun_failed_request(
            operator=h.operator(), request_id=UUID(first.extras["approval_request_id"])
        )

    assert refused.value.reason is RerunRefusal.MULTI_STEP_RUN
    assert s.sent == sent_once, "the re-run sends no step again"


# ---------------------------------------------------------------------------
# A credential write that waits
# ---------------------------------------------------------------------------


async def test_change_before_a_waiting_credential_write_is_sent_once(
    stub_embedding_service: AsyncMock, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``credential_write`` step waits; its request keeps the run input encrypted.

    The run after the approval reads the stored steps from the encrypted
    copy, so the change before the wait is not sent again.
    """
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    # No Vault here: skip the step's Vault permission check before the wait.
    monkeypatch.delitem(_preview._PERMISSION_PREFLIGHTS, s.CREDENTIAL_WRITE_STEP, raising=False)
    await _setup(
        stub_embedding_service,
        session,
        s.CREDENTIAL_WRITE_WAIT_OP_ID,
        "change_then_credential_write_composite",
    )

    first = await h.run(s.CREDENTIAL_WRITE_WAIT_OP_ID)
    assert first.status == "awaiting_approval", first.error
    assert s.sent == [s.CHANGE_STEP]
    (waiting,) = await _requests()
    assert waiting.op_id == s.CREDENTIAL_WRITE_STEP
    assert waiting.execution_handle is not None
    # The row holds no params, only that a step was sent before the wait.
    assert waiting.resume_parent == {
        "op_id": s.CREDENTIAL_WRITE_WAIT_OP_ID,
        "request_sent": True,
    }

    second = await _approve_and_resume(first)

    assert second.status == "ok", second.error
    assert second.result == {"done": True, "id": "t-1"}
    assert s.sent == [s.CHANGE_STEP, s.CREDENTIAL_WRITE_STEP]


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
    kept = journal.start("vault-1.x", "POST:/a", None, {"name": "a"})
    journal.finish(kept, {"id": "a-1"})
    secret = journal.start("vault-1.x", "POST:/b", None, {"name": "b"})
    journal.finish(secret, {"id": "b-1", "token": "s.made-up-value"})
    not_json = journal.start("vault-1.x", "POST:/c", None, {"name": "c"})
    journal.finish(not_json, object())

    assert (kept["status"], kept["result"]) == (STEP_DONE, {"id": "a-1"})
    assert secret["status"] == STEP_STARTED
    assert "result" not in secret
    assert not_json["status"] == STEP_STARTED
    assert "made-up-value" not in str(journal.stored())


@pytest.mark.parametrize(
    ("op_id", "status"),
    [
        ("vault.kv.read", STEP_STARTED),
        ("vault.kv.put", STEP_DONE_NO_RESULT),
        ("vault.token.create", STEP_DONE_NO_RESULT),
    ],
    ids=["read", "write", "make"],
)
def test_result_of_a_credential_step_is_not_kept(op_id: str, status: str) -> None:
    """A credential step's result is not kept, also when the value is under a neutral name."""
    journal = StepJournal(composite_op_id="vault.composite.x")
    entry = journal.start("vault-1.x", op_id, None, {"path": "app"})
    journal.finish(entry, {"data": {"value": "made-up-value"}})

    assert entry["status"] == status
    assert "result" not in entry
    assert "made-up-value" not in str(journal.stored())


@pytest.mark.parametrize("value", [123456, 12.5], ids=["int", "float"])
def test_result_with_a_numeric_secret_value_is_not_kept(value: float) -> None:
    """A number under a secret name is a secret too: the step stays "started"."""
    journal = StepJournal(composite_op_id="vault.composite.x")
    entry = journal.start("vault-1.x", "POST:/a", None, {"name": "a"})
    journal.finish(entry, {"id": "a-1", "password": value})

    assert entry["status"] == STEP_STARTED
    assert "result" not in entry
    assert str(value) not in str(journal.stored())


def test_step_is_matched_only_on_its_own_target() -> None:
    """The same step with the same params on another target does not get this target's result."""
    target_a = SimpleNamespace(id=uuid4(), name="target-a")
    target_b = SimpleNamespace(id=uuid4(), name="target-b")
    first_run = StepJournal(composite_op_id="vault.composite.x")
    entry = first_run.start("vault-1.x", "POST:/a", target_a, {"name": "a"})
    first_run.finish(entry, {"id": "on-a"})
    resume = StepJournal(composite_op_id="vault.composite.x", replay=first_run.stored())

    assert resume.take("vault-1.x", "POST:/a", target_b, {"name": "a"}) is None
    assert resume.take("vault-1.x", "POST:/a", "target-a", {"name": "a"}) is None
    taken = resume.take("vault-1.x", "POST:/a", target_a, {"name": "a"})
    assert taken is not None and taken["result"] == {"id": "on-a"}


def test_taken_entry_is_a_copy_of_the_stored_one() -> None:
    """Changing a replayed result does not change what the next wait stores."""
    first_run = StepJournal(composite_op_id="vault.composite.x")
    entry = first_run.start("vault-1.x", "POST:/a", None, {"name": "a"})
    first_run.finish(entry, {"id": "a-1", "tags": ["x"]})
    resume = StepJournal(composite_op_id="vault.composite.x", replay=first_run.stored())

    taken = resume.take("vault-1.x", "POST:/a", None, {"name": "a"})
    assert taken is not None
    taken["result"].pop("id")
    taken["result"]["tags"].append("y")

    assert resume.stored()[0]["result"] == {"id": "a-1", "tags": ["x"]}


def test_same_step_twice_is_matched_in_order() -> None:
    """Two runs of one step with the same params are matched once each, in order."""
    first_run = StepJournal(composite_op_id="vault.composite.x")
    for value in ({"n": 1}, {"n": 2}):
        entry = first_run.start("vault-1.x", "POST:/a", None, {"name": "a"})
        first_run.finish(entry, value)
    resume = StepJournal(composite_op_id="vault.composite.x", replay=first_run.stored())

    one = resume.take("vault-1.x", "POST:/a", None, {"name": "a"})
    two = resume.take("vault-1.x", "POST:/a", None, {"name": "a"})
    three = resume.take("vault-1.x", "POST:/a", None, {"name": "a"})
    other = resume.take("vault-1.x", "POST:/a", None, {"name": "other"})

    assert one is not None and one["result"] == {"n": 1}
    assert two is not None and two["result"] == {"n": 2}
    assert three is None
    assert other is None
