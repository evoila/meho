# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stopping pod refuses new calls and lets running ones finish (#5024).

* After the stop signal, a new top-level call is refused with
  ``meho_shutting_down`` before anything is sent. A step of a running
  multi-step operation and an approved run whose claim was taken go on.
  An approved request is refused before its claim, also when the stop
  comes during the lookups before the claim.
* A call that already runs finishes, and the wait ends as soon as it did.
* A call that still runs at the limit, or that uvicorn cancels at its
  limit, is recorded as unfinished: a log line and one audit row each. The
  row carries the links of the call (agent session, parent row, runbook
  run), so the session replay finds it.
* ``/ready`` answers 503 ``draining``. The sensor runner and the scheduler
  start no new work; a scheduler batch stops at the trigger that is due
  when the stop comes, and that trigger stays due. The stop signal handler
  starts the drain and then calls the handler that was there before.
"""

from __future__ import annotations

import asyncio
import signal
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import FrameType
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from meho_backplane import main as main_module
from meho_backplane.agent.run import AgentRunStatus
from meho_backplane.audit_query.replay import replay_session
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.main import app
from meho_backplane.operations import approval_queue, dispatcher, shutdown_drain
from meho_backplane.operations._audit import (
    agent_session_id_var,
    parent_audit_id_var,
    run_id_var,
    work_ref_var,
)
from meho_backplane.operations.composite import composite_depth_var
from meho_backplane.scheduler import loop as scheduler_loop
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000d001")


@pytest.fixture(autouse=True)
def _fresh_drain_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    shutdown_drain.reset_drain_state()
    yield
    shutdown_drain.reset_drain_state()
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="drain-test-user",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


def _ok(op_id: str) -> OperationResult:
    return OperationResult(status="ok", op_id=op_id, duration_ms=1.0)


async def _call(op_id: str = "demo.thing.change", **kwargs: Any) -> OperationResult:
    return await dispatcher.dispatch(
        operator=_operator(),
        connector_id="demo-1.x",
        op_id=op_id,
        target=None,
        params={},
        **kwargs,
    )


def _block_body(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Make the dispatch body wait until the test lets it go."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _body(**kwargs: Any) -> OperationResult:
        entered.set()
        await release.wait()
        return _ok(kwargs["op_id"])

    monkeypatch.setattr(dispatcher, "_dispatch_now", _body)
    return entered, release


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return list(result.scalars().all())


# -- refusing new calls ------------------------------------------------------


async def test_new_call_is_refused_before_anything_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    body = MagicMock(side_effect=AssertionError("the body must not run"))
    monkeypatch.setattr(dispatcher, "_dispatch_now", body)
    shutdown_drain.begin_drain()

    result = await _call()

    assert result.status == "error"
    assert result.extras["error_code"] == shutdown_drain.SHUTTING_DOWN_ERROR_CODE
    assert result.extras["execution_outcome"] == "not_started"
    assert result.extras["retry"] is True
    body.assert_not_called()
    assert shutdown_drain.running_operations() == []


async def test_before_the_stop_signal_a_call_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _body(**kwargs: Any) -> OperationResult:
        return _ok(kwargs["op_id"])

    monkeypatch.setattr(dispatcher, "_dispatch_now", _body)

    result = await _call()

    assert result.status == "ok"
    assert shutdown_drain.is_draining() is False


async def test_approved_run_with_its_claim_taken_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _body(**kwargs: Any) -> OperationResult:
        assert kwargs["_approved"] is True
        return _ok(kwargs["op_id"])

    monkeypatch.setattr(dispatcher, "_dispatch_now", _body)
    shutdown_drain.begin_drain()

    result = await _call(_approved=True)

    assert result.status == "ok"


async def test_step_of_a_running_multi_step_operation_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _body(**kwargs: Any) -> OperationResult:
        return _ok(kwargs["op_id"])

    monkeypatch.setattr(dispatcher, "_dispatch_now", _body)
    shutdown_drain.begin_drain()
    token = composite_depth_var.set(1)
    try:
        result = await _call()
    finally:
        composite_depth_var.reset(token)

    assert result.status == "ok"
    # A step is part of its parent's call, not a call of its own.
    assert shutdown_drain.running_operations() == []


async def test_approved_resume_is_refused_before_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = MagicMock(side_effect=AssertionError("the claim must stay free"))
    monkeypatch.setattr(approval_queue, "claim_resume", claim)
    request = MagicMock(op_id="demo.thing.change", params={}, execution_handle=None)
    shutdown_drain.begin_drain()

    result = await approval_queue.resume_dispatch_after_approval(
        operator=_operator(), request=request
    )

    assert result.extras["error_code"] == shutdown_drain.SHUTTING_DOWN_ERROR_CODE
    claim.assert_not_called()


async def test_agent_approved_resume_is_refused_before_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
    from meho_backplane.operations import approval_handoff

    claim = MagicMock(side_effect=AssertionError("the claim must stay free"))
    custody = MagicMock(side_effect=AssertionError("the custody row must stay"))
    monkeypatch.setattr(approval_queue, "claim_resume", claim)
    monkeypatch.setattr(approval_handoff, "consume", custody)
    monkeypatch.setattr(approval_handoff, "discard", custody)
    shutdown_drain.begin_drain()

    result = await _resume_approved_or_already_resumed(
        operator=_operator(),
        call_arguments={"connector_id": "demo-1.0", "op_id": "demo.thing.change", "params": {}},
        awaiting_envelope={"op_id": "demo.thing.change", "extras": {}},
        approval_request_id=uuid.uuid4(),
    )

    assert result["op_id"] == "demo.thing.change"
    assert result["extras"]["error_code"] == shutdown_drain.SHUTTING_DOWN_ERROR_CODE
    claim.assert_not_called()
    custody.assert_not_called()


def _stop_comes_during_the_target_lookup(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The release check passes, then the stop signal comes while the target loads.

    Returns the claim mock, which must stay untouched.
    """
    claim = MagicMock(side_effect=AssertionError("the claim must stay free"))
    monkeypatch.setattr(approval_queue, "claim_resume", claim)

    async def _release_unchanged(*_args: Any) -> None:
        return None

    async def _target_then_stop(*_args: Any) -> tuple[None, None]:
        shutdown_drain.begin_drain()
        return None, None

    monkeypatch.setattr(approval_queue, "refuse_resume_if_release_changed", _release_unchanged)
    monkeypatch.setattr(approval_queue, "rehydrate_resume_target", _target_then_stop)
    return claim


async def test_approved_resume_is_refused_when_the_stop_comes_during_the_target_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _stop_comes_during_the_target_lookup(monkeypatch)
    request = MagicMock(
        op_id="demo.thing.change", params={}, execution_handle=None, reveal_secret=False
    )

    result = await approval_queue.resume_dispatch_after_approval(
        operator=_operator(), request=request
    )

    assert result.extras["error_code"] == shutdown_drain.SHUTTING_DOWN_ERROR_CODE
    claim.assert_not_called()


async def test_agent_approved_resume_is_refused_when_the_stop_comes_during_the_target_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed
    from meho_backplane.operations import approval_handoff, meta_tools

    claim = _stop_comes_during_the_target_lookup(monkeypatch)
    custody = MagicMock(side_effect=AssertionError("the custody row must stay"))
    monkeypatch.setattr(approval_handoff, "consume", custody)
    monkeypatch.setattr(approval_handoff, "discard", custody)
    request = MagicMock(
        op_id="demo.thing.change", params={}, execution_handle=None, resume_parent=None
    )

    async def _get_request(*_args: Any, **_kwargs: Any) -> MagicMock:
        return request

    monkeypatch.setattr(approval_queue, "get_request", _get_request)
    monkeypatch.setattr(meta_tools, "apply_target_fqdn_override", lambda *_args: None)

    result = await _resume_approved_or_already_resumed(
        operator=_operator(),
        call_arguments={"connector_id": "demo-1.0", "op_id": "demo.thing.change", "params": {}},
        awaiting_envelope={"op_id": "demo.thing.change", "extras": {}},
        approval_request_id=uuid.uuid4(),
    )

    assert result["op_id"] == "demo.thing.change"
    assert result["extras"]["error_code"] == shutdown_drain.SHUTTING_DOWN_ERROR_CODE
    claim.assert_not_called()
    custody.assert_not_called()


# -- letting running calls finish ---------------------------------------------


async def test_running_call_finishes_while_new_calls_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = _block_body(monkeypatch)
    running = asyncio.create_task(_call("demo.thing.slow"))
    await entered.wait()
    assert [e.op_id for e in shutdown_drain.running_operations()] == ["demo.thing.slow"]

    shutdown_drain.begin_drain()
    refused = await _call("demo.thing.new")
    assert refused.extras["error_code"] == shutdown_drain.SHUTTING_DOWN_ERROR_CODE

    waiter = asyncio.create_task(shutdown_drain.wait_for_running_operations(time.monotonic() + 30))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    release.set()

    assert (await running).status == "ok"
    assert await asyncio.wait_for(waiter, timeout=5) == []
    assert await shutdown_drain.record_unfinished_operations([]) == 0
    assert await _audit_rows("demo.thing.slow") == []


async def test_call_still_running_at_the_limit_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, _release = _block_body(monkeypatch)
    running = asyncio.create_task(_call("demo.thing.slow"))
    await entered.wait()
    shutdown_drain.begin_drain()

    still_running = await shutdown_drain.wait_for_running_operations(time.monotonic() + 0.2)
    assert [e.op_id for e in still_running] == ["demo.thing.slow"]

    assert await shutdown_drain.record_unfinished_operations(still_running) == 1
    rows = await _audit_rows("demo.thing.slow")
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == "drain-test-user"
    assert row.tenant_id == _TENANT
    assert row.status_code == 503
    assert row.payload["result_status"] == "unfinished"
    assert row.payload["execution_outcome"] == "uncertain"
    assert row.payload["retry"] is False
    assert row.payload["connector_id"] == "demo-1.x"

    # The loop teardown cancels the call later: it is not recorded twice.
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await shutdown_drain.record_unfinished_operations([]) == 0
    assert len(await _audit_rows("demo.thing.slow")) == 1


async def test_call_cancelled_at_the_server_limit_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, _release = _block_body(monkeypatch)
    running = asyncio.create_task(_call("demo.thing.cut"))
    await entered.wait()
    shutdown_drain.begin_drain()

    # What uvicorn does to an open request at UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN.
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert shutdown_drain.running_operations() == []
    assert await shutdown_drain.record_unfinished_operations([]) == 1
    rows = await _audit_rows("demo.thing.cut")
    assert [r.payload["result_status"] for r in rows] == ["unfinished"]


async def test_interrupted_agent_call_is_found_by_the_session_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unfinished row carries the agent session and its parent row."""
    session_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        # The agent's own tool call, which the cut-off operation belongs to.
        session.add(
            AuditLog(
                id=parent_id,
                event_class=AuditEventClass.OPERATION_EXECUTION,
                occurred_at=datetime.now(UTC),
                operator_sub="drain-test-user",
                tenant_id=_TENANT,
                agent_session_id=session_id,
                method="MCP",
                path="call_operation",
                status_code=200,
                payload={"op_id": "call_operation"},
            )
        )
        await session.commit()
    entered, _release = _block_body(monkeypatch)
    tokens = (
        agent_session_id_var.set(session_id),
        parent_audit_id_var.set(parent_id),
        run_id_var.set(run_id),
        work_ref_var.set("CHG-1"),
    )
    try:
        # The task takes the links with it, like an agent's tool call does.
        running = asyncio.create_task(_call("demo.thing.agent"))
    finally:
        work_ref_var.reset(tokens[3])
        run_id_var.reset(tokens[2])
        parent_audit_id_var.reset(tokens[1])
        agent_session_id_var.reset(tokens[0])
    await entered.wait()
    shutdown_drain.begin_drain()

    still_running = await shutdown_drain.wait_for_running_operations(time.monotonic() + 0.2)
    assert await shutdown_drain.record_unfinished_operations(still_running) == 1

    (row,) = await _audit_rows("demo.thing.agent")
    assert row.agent_session_id == session_id
    assert row.parent_audit_id == parent_id
    assert row.run_id == run_id
    assert row.work_ref == "CHG-1"
    assert row.payload["agent_session_id"] == str(session_id)
    assert row.payload["parent_audit_id"] == str(parent_id)
    async with get_sessionmaker()() as session:
        roots = await replay_session(session_id, tenant_id=_TENANT, session=session)
    assert [node.path for node in roots] == ["call_operation"]
    (child,) = roots[0].children
    assert child.path == "demo.thing.agent"
    assert child.payload["result_status"] == "unfinished"

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_cancel_without_a_stop_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, _release = _block_body(monkeypatch)
    running = asyncio.create_task(_call("demo.thing.cancelled"))
    await entered.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert await shutdown_drain.record_unfinished_operations([]) == 0


async def test_failed_audit_write_still_logs_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, _release = _block_body(monkeypatch)
    running = asyncio.create_task(_call("demo.thing.slow"))
    await entered.wait()
    shutdown_drain.begin_drain()

    async def _broken(*_args: Any) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr(shutdown_drain, "_write_audit_rows", _broken)
    still_running = shutdown_drain.running_operations()

    assert await shutdown_drain.record_unfinished_operations(still_running) == 1
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_lifespan_drain_waits_then_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0")
    get_settings.cache_clear()
    entered, _release = _block_body(monkeypatch)
    running = asyncio.create_task(_call("demo.thing.lifespan"))
    await entered.wait()

    await main_module._drain_running_operations()

    assert shutdown_drain.is_draining() is True
    assert len(await _audit_rows("demo.thing.lifespan")) == 1
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


def test_drain_deadline_counts_from_the_stop_signal() -> None:
    before = time.monotonic()
    shutdown_drain.begin_drain()
    first = shutdown_drain.drain_deadline(60)
    time.sleep(0.01)
    shutdown_drain.begin_drain()
    assert shutdown_drain.drain_deadline(60) == first
    assert before + 60 <= first <= time.monotonic() + 60


# -- no new work and no new traffic ---------------------------------------------


def test_ready_answers_503_draining() -> None:
    shutdown_drain.begin_drain()

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "draining"
    assert body["checks"] == []


async def test_sensor_runner_claims_nothing_while_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = MagicMock(side_effect=AssertionError("no claim while draining"))
    monkeypatch.setattr(sensor_runner, "advisory_lock", lock)
    shutdown_drain.begin_drain()

    assert await sensor_runner._claim_and_spawn_due() == (False, 0)
    lock.assert_not_called()


async def test_scheduler_fires_nothing_while_draining(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = MagicMock(side_effect=AssertionError("no fire while draining"))
    monkeypatch.setattr(scheduler_loop, "advisory_lock", lock)
    shutdown_drain.begin_drain()

    assert await scheduler_loop.run_one_tick(invoker=MagicMock()) == 0
    lock.assert_not_called()


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _scheduler_batch(
    monkeypatch: pytest.MonkeyPatch, rows: list[MagicMock], *, stop_during: str
) -> tuple[list[uuid.UUID], list[str]]:
    """Drive one tick over *rows* (one-off triggers) without a database.

    The stop signal comes during the first trigger's preparation or during
    its run (*stop_during*). Returns the ids of the triggers marked fired
    and the names of the agents that ran.
    """
    marked: list[uuid.UUID] = []
    ran: list[str] = []

    @asynccontextmanager
    async def _lock(*_args: Any, **_kwargs: Any) -> Any:
        yield True

    async def _claim(_session: Any, *, now: Any, limit: int) -> list[MagicMock]:
        return rows

    async def _prepare(row: Any) -> scheduler_loop._PreparedInvocation:
        if stop_during == "preparation":
            shutdown_drain.begin_drain()
        return scheduler_loop._PreparedInvocation(
            name=f"agent-{row.id}",
            identity_ref="demo",
            agent_client_id="demo",
            agent_client_secret=SecretStr("demo"),
            inputs_str="go",
            work_ref=None,
        )

    async def _mark(_session: Any, row: Any, *, fire_instant: Any) -> Any:
        marked.append(row.id)
        return row

    async def _run(name: str, *_args: Any, **_kwargs: Any) -> MagicMock:
        ran.append(name)
        if stop_during == "run":
            shutdown_drain.begin_drain()
        return MagicMock(status=AgentRunStatus.SUCCEEDED, run_id=uuid.uuid4(), error=None)

    monkeypatch.setattr(scheduler_loop, "advisory_lock", _lock)
    monkeypatch.setattr(scheduler_loop, "get_sessionmaker", lambda: _FakeSession)
    monkeypatch.setattr(scheduler_loop, "claim_due_triggers", _claim)
    monkeypatch.setattr(scheduler_loop, "_prepare_invocation", _prepare)
    monkeypatch.setattr(scheduler_loop, "mark_one_off_fired", _mark)
    invoker = MagicMock()
    invoker.run_scheduled = _run
    monkeypatch.setattr(scheduler_loop, "get_agent_invoker", lambda: invoker)
    return marked, ran


def _one_off_rows(count: int) -> list[MagicMock]:
    return [MagicMock(id=uuid.uuid4(), kind="one_off", skip_count=0) for _ in range(count)]


async def test_scheduler_leaves_the_rest_of_the_batch_when_the_stop_comes_during_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _one_off_rows(3)
    marked, ran = _scheduler_batch(monkeypatch, rows, stop_during="run")

    assert await scheduler_loop.run_one_tick() == 1

    # The first run went on. The other two triggers were neither marked
    # fired nor run: they stay due for the next pod.
    assert marked == [rows[0].id]
    assert ran == [f"agent-{rows[0].id}"]


async def test_scheduler_leaves_a_trigger_when_the_stop_comes_during_its_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _one_off_rows(2)
    marked, ran = _scheduler_batch(monkeypatch, rows, stop_during="preparation")

    assert await scheduler_loop.run_one_tick() == 0

    assert marked == []
    assert ran == []


# -- the stop signal -------------------------------------------------------------


def test_stop_signal_starts_the_drain_then_calls_the_server_handler() -> None:
    original = signal.getsignal(signal.SIGTERM)
    seen: list[int] = []

    def _server_handler(signum: int, _frame: FrameType | None) -> None:
        seen.append(signum)

    signal.signal(signal.SIGTERM, _server_handler)
    try:
        undo = shutdown_drain.install_stop_signal_hook()
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not _server_handler
        assert callable(handler)

        handler(signal.SIGTERM, None)

        assert shutdown_drain.is_draining() is True
        assert seen == [signal.SIGTERM]
        undo()
        assert signal.getsignal(signal.SIGTERM) is _server_handler
    finally:
        signal.signal(signal.SIGTERM, original)


def test_no_hook_when_no_server_handler_is_there() -> None:
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        undo = shutdown_drain.install_stop_signal_hook()
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        undo()
    finally:
        signal.signal(signal.SIGTERM, original)
