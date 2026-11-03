# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stopping pod refuses new calls and lets running ones finish (#5024).

* After the stop signal, a new top-level call is refused with
  ``meho_shutting_down`` before anything is sent. A step of a running
  multi-step operation and an approved run whose claim was taken go on.
* A call that already runs finishes, and the wait ends as soon as it did.
* A call that still runs at the limit, or that uvicorn cancels at its
  limit, is recorded as unfinished: a log line and one audit row each.
* ``/ready`` answers 503 ``draining``. The sensor runner and the scheduler
  start no new work. The stop signal handler starts the drain and then
  calls the handler that was there before.
"""

from __future__ import annotations

import asyncio
import signal
import time
import uuid
from collections.abc import Iterator
from types import FrameType
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane import main as main_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.operations import approval_queue, dispatcher, shutdown_drain
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
