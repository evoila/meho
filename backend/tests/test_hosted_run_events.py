# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hosted agent runs keep their events in the session store.

A run that MEHO starts gets one session record (id = run id). It saves each
turn, tool call, tool result and its final answer (or error) in order, after
the secret check. A run that stops without a final event shows
``unfinished`` and its last saved event. Reading the events writes an audit
row, and stays in the caller's tenant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentRunNotFoundError
from meho_backplane.agent.reaper import _run_one_tick
from meho_backplane.agent.run_events import read_run_events
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.db.models import AgentRunStatus, AgentRunTrigger, AuditLog
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.session_store.hosted_runs import (
    HOSTED_RUN_APP,
    RunEventLog,
    check_event,
)
from meho_backplane.session_store.models import AgentSessionEvent, AgentSessionRecord
from meho_backplane.session_store.service import delete_record_rows
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.agents.runs import events as events_stream
from tests._hosted_run_helpers import end_hosted_run, record_hosted_run
from tests.test_agent_invocation import (
    _TENANT_A,
    _TENANT_B,
    _invoker_with,
    _make_operator,
    _seed_definition,
    _seed_echo_op,
    _seed_tenants,
)

pytestmark = pytest.mark.asyncio

#: A made-up bearer token. It is not a real credential.
_FAKE_TOKEN = "madeupTokenValue0123456789abcdef"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """A stub embedding service returning a fixed-dimension vector."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * EMBEDDING_DIMENSION
    service.encode.return_value = [[0.1] * EMBEDDING_DIMENSION]
    service.dimension = EMBEDDING_DIMENSION
    return service


def _call_op_with_secret_then_text(text: str) -> FunctionModel:
    """A model that calls ``call_operation`` with a planted secret, then answers."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        has_return = any(
            getattr(part, "part_kind", "") == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_return:
            params = {"path": "secret/foo", "note": f"Authorization: Bearer {_FAKE_TOKEN}"}
            call = {"connector_id": "vault-1.x", "op_id": "vault.kv.read", "params": params}
            return ModelResponse(parts=[ToolCallPart("call_operation", call)])
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


async def _saved(run_id: UUID) -> list[AgentSessionEvent]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(
                select(AgentSessionEvent)
                .where(AgentSessionEvent.record_id == run_id)
                .order_by(AgentSessionEvent.seq)
            )
        )


async def _record(run_id: UUID) -> AgentSessionRecord | None:
    async with get_sessionmaker()() as session:
        return await session.get(AgentSessionRecord, run_id)


async def _wait_done(run_id: UUID) -> None:
    for _ in range(200):
        async with get_sessionmaker()() as session:
            row = await session.get(AgentRunRow, run_id)
            assert row is not None
            if row.status in ("succeeded", "failed", "cancelled"):
                return
        await asyncio.sleep(0.05)
    raise AssertionError("run did not end")


# ---------------------------------------------------------------------------
# Saving the events
# ---------------------------------------------------------------------------


async def test_background_run_saves_events_in_order_and_ends(
    stub_embedding_service: AsyncMock,
) -> None:
    """A background run saves turn, tool_call, tool_result and final, then ``ended``."""
    await _seed_echo_op(stub_embedding_service)
    await _seed_definition(toolset={"meta_tools": ["call_operation"]})
    invoker = _invoker_with(_call_op_with_secret_then_text("answer"))

    outcome = await invoker.run(_make_operator(), "reader", "go", async_mode=True)
    await _wait_done(outcome.run_id)

    events = await _saved(outcome.run_id)
    kinds = [event.kind for event in events]
    assert kinds[0] == "turn"
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "final"
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[-1].data == {"output": "answer"}
    record = await _record(outcome.run_id)
    assert record is not None
    assert record.app == HOSTED_RUN_APP
    assert record.session_id == str(outcome.run_id)
    assert record.project == "reader"
    assert record.state == "ended"
    assert record.outcome == "succeeded"


async def test_planted_secret_in_tool_result_is_not_saved_in_clear_text(
    stub_embedding_service: AsyncMock,
) -> None:
    """The secret check replaces a known secret in the tool call and the tool result."""
    await _seed_echo_op(stub_embedding_service)
    await _seed_definition(toolset={"meta_tools": ["call_operation"]})
    invoker = _invoker_with(_call_op_with_secret_then_text("answer"))

    outcome = await invoker.run(_make_operator(), "reader", "go", async_mode=True)
    await _wait_done(outcome.run_id)

    events = await _saved(outcome.run_id)
    result = next(event for event in events if event.kind == "tool_result")
    call = next(event for event in events if event.kind == "tool_call")
    for event in (result, call):
        assert _FAKE_TOKEN not in str(event.data)
        assert "[REDACTED:" in str(event.data)
    assert call.redactions >= 1


async def test_secret_check_runs_on_a_tool_result_before_it_is_saved() -> None:
    """A tool result with a known secret is saved with the secret replaced."""
    await _seed_tenants()
    # Split, so the repository's key scanners do not flag a made-up key.
    key = (
        "-----BEGIN " + "PRIVATE KEY-----\nMadeUpKeyBody0123456789\n-----END " + "PRIVATE KEY-----"
    )
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[
            ("tool_result", {"tool_name": "a", "content": f"Authorization: Bearer {_FAKE_TOKEN}"}),
            ("tool_result", {"tool_name": "b", "content": {"nested": [key]}}),
        ],
    )
    events = await _saved(run_id)
    assert _FAKE_TOKEN not in str(events[0].data)
    assert "MadeUpKeyBody" not in str(events[1].data)
    assert all(event.redactions >= 1 for event in events)
    assert all(not event.quarantined for event in events)


async def test_streamed_run_saves_the_events_it_yields(
    stub_embedding_service: AsyncMock,
) -> None:
    """The inline stream saves each event before the client gets it."""
    await _seed_echo_op(stub_embedding_service)
    await _seed_definition(toolset={"meta_tools": ["call_operation"]})
    invoker = _invoker_with(_call_op_with_secret_then_text("answer"))

    yielded: list[str] = []
    run_id: UUID | None = None
    async for rid, event in invoker.stream_events(_make_operator(), "reader", "go"):
        run_id = rid
        yielded.append(event.kind.value)
    assert run_id is not None

    events = await _saved(run_id)
    assert [event.kind for event in events] == yielded
    record = await _record(run_id)
    assert record is not None
    assert (record.state, record.outcome) == ("ended", "succeeded")


async def test_refused_run_saves_its_error_event() -> None:
    """A run refused before the loop (a runbook it cannot run) saves its error."""
    await _seed_definition()
    invoker = _invoker_with(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])))
    outcome = await invoker.run(_make_operator(), "reader", "Execute runbook `restart-web`.")
    assert outcome.status is AgentRunStatus.FAILED

    events = await _saved(outcome.run_id)
    assert [event.kind for event in events] == ["error"]
    assert events[0].data["error"] == outcome.error
    record = await _record(outcome.run_id)
    assert record is not None
    assert (record.state, record.outcome) == ("ended", "failed")


async def test_killed_run_shows_unfinished_and_its_last_event() -> None:
    """A run whose process died keeps its saved events; the reaper marks it unfinished."""
    await _seed_definition()
    invoker = _invoker_with(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])))
    entry = await AgentDefinitionService().get(_TENANT_A, "reader")
    assert entry is not None
    run_id, _owner = await invoker._create_run_row(
        _make_operator(), entry, provider="anthropic", model="claude-sonnet-4-6"
    )
    log = RunEventLog(run_id, _TENANT_A)
    await log.save("turn", {})
    await log.save("tool_call", {"tool_name": "call_operation", "args": {}})

    # The process dies: no final event, and the lease runs out.
    async with get_sessionmaker()() as session:
        row = await session.get(AgentRunRow, run_id)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=120)
        await session.commit()
    await _run_one_tick()

    view = await read_run_events(_make_operator(), run_id)
    assert view.run_status == "failed"
    assert view.state == "unfinished"
    assert [event.kind for event in view.events] == ["turn", "tool_call"]
    assert view.events[-1].seq == 2


async def test_events_survive_a_backend_restart() -> None:
    """A finished run's events are read back after the engine is built again."""
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("turn", {}), ("final", {"output": "done"})],
    )
    reset_engine_for_testing()

    view = await read_run_events(_make_operator(), run_id)
    assert [event.kind for event in view.events] == ["turn", "final"]
    assert view.events[-1].data == {"output": "done"}
    assert view.state == "ended"


async def test_cancelled_run_without_final_event_is_unfinished() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A, sub="op-agent", events=[("turn", {})], end=AgentRunStatus.CANCELLED
    )
    record = await _record(run_id)
    assert record is not None
    assert (record.state, record.outcome) == ("unfinished", "cancelled")


async def test_a_failed_save_stops_the_log_and_never_leaves_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one save fails, later events are not saved, so the log has no gap."""
    await _seed_tenants()
    run_id = await record_hosted_run(tenant_id=_TENANT_A, sub="op-agent", end=None)
    log = RunEventLog(run_id, _TENANT_A)
    await log.save("turn", {})

    real_check = check_event
    calls = {"n": 0}

    def flaky(data: dict[str, Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database gone")
        return real_check(data)

    monkeypatch.setattr("meho_backplane.session_store.hosted_runs.check_event", flaky)
    await log.save("tool_call", {"tool_name": "a"})
    await log.save("tool_result", {"tool_name": "a"})
    await end_hosted_run(run_id, AgentRunStatus.SUCCEEDED)

    assert [event.kind for event in await _saved(run_id)] == ["turn"]
    record = await _record(run_id)
    assert record is not None
    assert record.state == "unfinished"


async def test_a_run_without_a_session_saves_nothing() -> None:
    """A run id with no hosted-run session (a child run) gets no event rows."""
    await _seed_tenants()
    log = RunEventLog(UUID("33333333-3333-3333-3333-333333333333"), _TENANT_A)
    await log.save("turn", {})
    assert await _saved(UUID("33333333-3333-3333-3333-333333333333")) == []


async def test_deleting_the_session_deletes_its_events() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A, sub="op-agent", events=[("turn", {}), ("final", {"output": 1})]
    )
    async with get_sessionmaker()() as session:
        await delete_record_rows(session, run_id)
        await session.commit()
    assert await _saved(run_id) == []
    assert await _record(run_id) is None


# ---------------------------------------------------------------------------
# Reading the events
# ---------------------------------------------------------------------------


async def test_reading_events_writes_an_audit_row() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A, sub="op-agent", events=[("turn", {}), ("final", {"output": 1})]
    )
    view = await read_run_events(_make_operator(), run_id, audit_path="/ui/agents/runs/x")
    assert len(view.events) == 2

    async with get_sessionmaker()() as session:
        rows = list(await session.scalars(select(AuditLog)))
    reads = [row for row in rows if row.payload.get("op_id") == "sessions.run_events.read"]
    assert len(reads) == 1
    assert reads[0].payload["run_id"] == str(run_id)
    assert reads[0].payload["count"] == 2
    assert reads[0].operator_sub == "op-agent"


async def test_another_tenant_cannot_read_the_events() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(tenant_id=_TENANT_A, sub="op-agent", events=[("turn", {})])
    with pytest.raises(AgentRunNotFoundError):
        await read_run_events(_make_operator(tenant_id=_TENANT_B), run_id)


async def test_direct_run_data_is_withheld_from_another_person() -> None:
    """Only the person who started a direct run reads its event data."""
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A, sub="op-agent", events=[("final", {"output": "private"})]
    )
    view = await read_run_events(_make_operator(sub="someone-else"), run_id)
    assert view.events[0].withheld is True
    assert view.events[0].data == {}

    scheduled = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="agent:reader",
        events=[("final", {"output": "shared"})],
        trigger=AgentRunTrigger.SCHEDULED,
    )
    shared = await read_run_events(_make_operator(sub="someone-else"), scheduled)
    assert shared.events[0].data == {"output": "shared"}


async def test_quarantined_event_is_shown_only_to_a_tenant_admin() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("tool_result", {"tool_name": "x", "content": {"password": "made-up-pass"}})],
    )
    events = await _saved(run_id)
    assert events[0].quarantined is True

    operator_view = await read_run_events(_make_operator(), run_id)
    assert operator_view.events[0].data == {}
    assert operator_view.events[0].quarantined is True
    admin_view = await read_run_events(_make_operator(role=TenantRole.TENANT_ADMIN), run_id)
    assert admin_view.events[0].data["tool_name"] == "x"


async def test_read_after_returns_only_later_events() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("turn", {}), ("tool_call", {"tool_name": "a"}), ("final", {"output": 1})],
    )
    view = await read_run_events(_make_operator(), run_id, after=1)
    assert [event.seq for event in view.events] == [2, 3]


# ---------------------------------------------------------------------------
# Following a live run
# ---------------------------------------------------------------------------


async def test_tail_shows_events_so_far_then_live_ones_without_gaps_or_doubles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that joins mid-run gets the saved events, then each new one once."""
    monkeypatch.setattr(events_stream, "POLL_SECONDS", 0.01)
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("turn", {}), ("tool_call", {"tool_name": "a"})],
        end=None,
    )
    log = RunEventLog(run_id, _TENANT_A)
    operator = _make_operator()
    first = await events_stream.first_read(operator, run_id, after=0, path="/p")

    async def finish_the_run() -> None:
        await asyncio.sleep(0.05)
        await log.save("tool_result", {"tool_name": "a", "content": "ok"})
        await asyncio.sleep(0.05)
        await log.save("final", {"output": "done"})
        await end_hosted_run(run_id, AgentRunStatus.SUCCEEDED)

    writer = asyncio.create_task(finish_the_run())
    frames = [frame async for frame in events_stream.tail_frames(operator, first)]
    await writer

    ids = [int(frame.split("\n")[0][4:]) for frame in frames if frame.startswith("id: ")]
    assert ids == [1, 2, 3, 4]
    assert frames[-1].startswith("event: end\n")
    assert '"state":"ended"' in frames[-1]
    assert '"status":"succeeded"' in frames[-1]


async def test_tail_after_last_event_id_continues_without_doubles() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("turn", {}), ("tool_call", {"tool_name": "a"}), ("final", {"output": 1})],
    )
    operator = _make_operator()
    first = await events_stream.first_read(operator, run_id, after=2, path="/p")
    frames = [frame async for frame in events_stream.tail_frames(operator, first, after=2)]
    assert frames[0].startswith("id: 3\nevent: final\n")
    assert frames[-1].startswith("event: end\n")
    assert '"last_seq":3' in frames[-1]


async def test_tail_of_a_killed_run_ends_unfinished_with_its_last_event() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("turn", {}), ("tool_call", {"tool_name": "a"})],
        end=AgentRunStatus.FAILED,
    )
    operator = _make_operator()
    first = await events_stream.first_read(operator, run_id, after=0, path="/p")
    frames = [frame async for frame in events_stream.tail_frames(operator, first)]
    assert len(frames) == 3
    assert '"state":"unfinished"' in frames[-1]
    assert '"last_seq":2' in frames[-1]
    assert "event: final" not in "".join(frames)


async def test_withheld_frame_has_no_data() -> None:
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A, sub="op-agent", events=[("final", {"output": "private"})]
    )
    operator = _make_operator(sub="someone-else")
    first = await events_stream.first_read(operator, run_id, after=0, path="/p")
    frames = [frame async for frame in events_stream.tail_frames(operator, first)]
    assert "private" not in frames[0]
    assert '"withheld":true' in frames[0]


async def test_tail_reads_every_page_of_a_long_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with more events than one read returns is streamed whole."""
    monkeypatch.setattr(events_stream, "PAGE_SIZE", 2)
    await _seed_tenants()
    run_id = await record_hosted_run(
        tenant_id=_TENANT_A,
        sub="op-agent",
        events=[("turn", {})] * 4 + [("final", {"output": "done"})],
    )
    operator = _make_operator()
    first = await events_stream.first_read(operator, run_id, after=0, path="/p")
    frames = [frame async for frame in events_stream.tail_frames(operator, first)]
    ids = [int(frame.split("\n")[0][4:]) for frame in frames if frame.startswith("id: ")]
    assert ids == [1, 2, 3, 4, 5]
    assert '"last_seq":5' in frames[-1]
