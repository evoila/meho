# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the agent invocation service + seam event stream.

G11.1-T4 (#811). Covers the orchestration layer
(:mod:`meho_backplane.agent.invocation`) and the seam's richer event
stream (:meth:`meho_backplane.agent.run.PydanticAgentRun.stream_events`)
against a deterministic :class:`~pydantic_ai.models.function.FunctionModel`
so no real LLM is hit (python_best_practices §14 — no network in unit
tests).

Acceptance criteria from #811 map onto the tests as:

* ``test_sync_run_blocks_and_returns_final_output`` — a short sync run
  blocks and returns the final answer recorded on the durable run row.
* ``test_long_sync_run_converts_to_async`` — a run that exceeds the
  server-side timeout returns a still-running handle flagged
  ``converted_to_async``; the run keeps going and later succeeds.
* ``test_async_run_returns_handle_then_poll_succeeds`` — async mode hands
  back a handle immediately; polling later shows the terminal state.
* ``test_poll_after_request_returns_reads_durable_row`` — poll works for a
  run whose in-memory store entry has been evicted (durability).
* ``test_stream_events_emits_turn_tool_and_final`` — the event stream
  surfaces turn / tool-call / tool-result / final events end to end.
* ``test_run_rejects_unknown_and_disabled_definitions`` — a missing /
  cross-tenant name and a disabled definition raise the typed errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import (
    AgentDisabledError,
    AgentInvocationError,
    AgentInvoker,
    AgentNotFoundError,
    AgentRunNotFoundError,
    _finalize_child_run,
    _record_child_run,
    _resolve_child_definition,
)
from meho_backplane.agent.reaper import (
    AGENT_RUN_REAPER_INTERRUPTION_REASON,
    _run_one_tick,
)
from meho_backplane.agent.run import (
    UNEXECUTABLE_RUNBOOK_CLASS,
    AgentDefinition,
    AgentRunEventKind,
    PydanticAgentRun,
)
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    AgentRunStatus,
    AgentRunTrigger,
    ScheduledTriggerInFlightPolicy,
    Tenant,
)
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires; reset the lru cache."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Clear the connector registry + dispatcher caches around each test."""
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


def _make_operator(
    *,
    tenant_id: UUID = _TENANT_A,
    role: TenantRole = TenantRole.OPERATOR,
    sub: str = "op-agent",
) -> Operator:
    return Operator(
        sub=sub,
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


async def _seed_tenants() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        for tid, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
            existing = await session.get(Tenant, tid)
            if existing is None:
                session.add(Tenant(id=tid, slug=slug, name=f"Tenant {slug}"))
        await session.commit()


async def _seed_definition(
    *,
    name: str = "reader",
    tenant_id: UUID = _TENANT_A,
    enabled: bool = True,
    toolset: dict[str, Any] | None = None,
    system_prompt: str = "You read secrets via MEHO operations.",
    turn_budget: int = 5,
) -> None:
    """Insert an agent definition for *tenant_id* via the CRUD service.

    G11.2-T8 (#1099): the service now rejects an ``identity_ref`` that
    doesn't resolve to a registered principal, so seed the matching
    ``agent_principal`` first. Idempotent under multiple calls with the
    same (tenant_id, name).
    """
    await _seed_tenants()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        existing = await session.execute(
            select(AgentPrincipal).where(
                AgentPrincipal.tenant_id == tenant_id,
                AgentPrincipal.keycloak_client_id == f"agent:{name}",
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                AgentPrincipal(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    keycloak_client_id=f"agent:{name}",
                    keycloak_internal_id=f"kc-internal-{tenant_id}-{name}",
                    owner_sub="seed-admin",
                    revoked=False,
                    created_by_sub="seed-admin",
                )
            )
            await session.commit()
    service = AgentDefinitionService()
    await service.create(
        tenant_id=tenant_id,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=name,
            identity_ref=f"agent:{name}",
            model_tier=AgentModelTier.STANDARD,
            system_prompt=system_prompt,
            toolset=toolset or {},
            turn_budget=turn_budget,
            enabled=enabled,
        ),
    )


class _NoOpVaultConnector(Connector):
    """Connector class registered so resolver/dispatch lookups succeed."""

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler echoing its params — proves the dispatch path runs."""
    return {"echo": params, "operator_sub": operator.sub}


async def _seed_echo_op(stub_embedding_service: AsyncMock) -> None:
    """Register the ``vault.kv.read`` typed op the agent tool will dispatch."""
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.kv.read",
        handler=_echo_handler,
        summary="Read a secret.",
        description="reads.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )


def _final_text(text: str) -> FunctionModel:
    """A model that answers immediately with *text* (no tool calls)."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _call_op_then_text(text: str) -> FunctionModel:
    """A model that calls ``call_operation`` once, then answers with *text*."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        has_return = any(
            getattr(part, "part_kind", "") == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_return:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "call_operation",
                        {
                            "connector_id": "vault-1.x",
                            "op_id": "vault.kv.read",
                            "params": {"path": "secret/foo"},
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _invoker_with(model: FunctionModel) -> AgentInvoker:
    """Build an invoker over a deterministic seam (no real LLM)."""
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))


# ---------------------------------------------------------------------------
# Sync invocation
# ---------------------------------------------------------------------------


async def test_sync_run_blocks_and_returns_final_output() -> None:
    """A short sync run blocks and returns the final answer, recorded durably."""
    await _seed_definition()
    invoker = _invoker_with(_final_text("done reading"))

    outcome = await invoker.run(_make_operator(), "reader", "read secret/foo")

    assert outcome.status is AgentRunStatus.SUCCEEDED
    assert outcome.converted_to_async is False
    assert outcome.output == {"text": "done reading"}

    # The durable row carries the same terminal state.
    view = await invoker.poll(_make_operator(), outcome.run_id)
    assert view.status is AgentRunStatus.SUCCEEDED
    assert view.output == {"text": "done reading"}
    assert view.provider == "anthropic"


async def test_long_sync_run_converts_to_async(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync run past the server-side timeout returns a still-running handle."""
    await _seed_definition()
    invoker = _invoker_with(_final_text("eventually"))

    # Drive the timeout near zero so the wait abandons before the background
    # task is scheduled — exercising the converted-to-async path
    # deterministically without a real long-running loop.
    monkeypatch.setenv("AGENT_SYNC_TIMEOUT_SECONDS", "0.0000001")
    get_settings.cache_clear()

    outcome = await invoker.run(_make_operator(), "reader", "go")

    assert outcome.converted_to_async is True
    assert outcome.status is AgentRunStatus.RUNNING

    # The run keeps going in the background and eventually succeeds; poll
    # until the durable row reaches a terminal state.
    for _ in range(200):
        view = await invoker.poll(_make_operator(), outcome.run_id)
        if view.status is AgentRunStatus.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    assert view.status is AgentRunStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Async invocation + poll
# ---------------------------------------------------------------------------


async def test_async_run_returns_handle_then_poll_succeeds() -> None:
    """Async mode hands back a handle immediately; polling shows the terminal state."""
    await _seed_definition()
    invoker = _invoker_with(_final_text("async done"))

    outcome = await invoker.run(_make_operator(), "reader", "go", async_mode=True)
    assert outcome.status is AgentRunStatus.RUNNING

    for _ in range(200):
        view = await invoker.poll(_make_operator(), outcome.run_id)
        if view.status is AgentRunStatus.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    assert view.status is AgentRunStatus.SUCCEEDED
    assert view.output == {"text": "async done"}


async def test_poll_after_store_eviction_reads_durable_row() -> None:
    """Poll works for a run whose in-memory store entry has been evicted."""
    await _seed_definition()
    invoker = _invoker_with(_final_text("durable"))

    outcome = await invoker.run(_make_operator(), "reader", "go")
    # Simulate the worker dropping the in-memory anchor (restart / GC).
    invoker._store.clear()

    view = await invoker.poll(_make_operator(), outcome.run_id)
    assert view.status is AgentRunStatus.SUCCEEDED
    assert view.output == {"text": "durable"}


async def test_poll_cross_tenant_run_is_not_found() -> None:
    """A run id owned by tenant A is invisible to tenant B's poll."""
    await _seed_definition()
    invoker = _invoker_with(_final_text("x"))
    outcome = await invoker.run(_make_operator(tenant_id=_TENANT_A), "reader", "go")

    with pytest.raises(AgentRunNotFoundError):
        await invoker.poll(_make_operator(tenant_id=_TENANT_B, sub="op-b"), outcome.run_id)


async def test_poll_unknown_handle_is_not_found() -> None:
    """An unknown run id raises AgentRunNotFoundError."""
    await _seed_tenants()
    invoker = _invoker_with(_final_text("x"))
    with pytest.raises(AgentRunNotFoundError):
        await invoker.poll(_make_operator(), uuid4())


# ---------------------------------------------------------------------------
# Lease/heartbeat wiring into the fire path (#1501)
# ---------------------------------------------------------------------------


async def test_create_run_row_stamps_lease_so_run_is_reapable() -> None:
    """#1501: ``_create_run_row`` claims a lease, so a live run has a non-NULL
    ``lease_expires_at`` and the reaper's claim query can reach it.

    The pre-#1501 defect was that ``_create_run_row`` only called
    ``create_run`` + ``start_run`` -- never ``claim_lease`` -- so every run
    committed with ``lease_expires_at = NULL`` and the reaper
    (``WHERE lease_expires_at IS NOT NULL AND < now``) could never reclaim a
    hung/crashed run. Here we drive the real run-creation transaction and
    assert the lease + owner landed.
    """
    await _seed_definition()
    invoker = _invoker_with(_final_text("x"))
    entry = await AgentDefinitionService().get(_TENANT_A, "reader")
    assert entry is not None

    run_id, lease_owner = await invoker._create_run_row(
        _make_operator(), entry, provider="anthropic", model="claude-sonnet-4-6"
    )

    # The owner is the per-process worker stamp ("<hostname>:<pid>" shape).
    assert ":" in lease_owner

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(AgentRunRow, run_id)
        assert row is not None
        assert row.status == AgentRunStatus.RUNNING.value
        # The lease is stamped in the same committed transaction as the
        # pending -> running transition: a committed run is never
        # ``running`` without a lease.
        assert row.lease_owner == lease_owner
        assert row.lease_expires_at is not None
        # The default in-flight policy is the conservative reclaim outcome.
        assert row.in_flight_policy == ScheduledTriggerInFlightPolicy.FAIL_INTO_AUDIT.value


async def test_hung_run_with_expired_lease_is_reaped_to_failed() -> None:
    """#1501 acceptance: a run created through the real fire path whose lease
    has expired (simulated dead worker) is transitioned to ``failed`` by the
    reaper within one reap interval.

    Drives ``_create_run_row`` (the production lease-stamping path), back-dates
    the lease to simulate a worker that died without releasing it, then runs
    one reaper tick. With the default ``fail_into_audit`` policy the row lands
    terminal ``failed`` with the reaper's interruption reason -- the
    "no run silently lost" contract, end to end through the wired fire path.
    """
    await _seed_definition()
    invoker = _invoker_with(_final_text("x"))
    entry = await AgentDefinitionService().get(_TENANT_A, "reader")
    assert entry is not None

    run_id, _owner = await invoker._create_run_row(
        _make_operator(), entry, provider="anthropic", model="claude-sonnet-4-6"
    )

    sessionmaker = get_sessionmaker()
    # Simulate the worker dying: the lease lapsed two minutes ago and no
    # heartbeat extended it.
    async with sessionmaker() as session:
        row = await session.get(AgentRunRow, run_id)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=120)
        await session.commit()

    await _run_one_tick()

    async with sessionmaker() as session:
        row = await session.get(AgentRunRow, run_id)
        assert row is not None
        assert row.status == AgentRunStatus.FAILED.value
        assert row.error == AGENT_RUN_REAPER_INTERRUPTION_REASON
        assert row.ended_at is not None
        # Terminal transition cleared the lease.
        assert row.lease_owner is None
        assert row.lease_expires_at is None


# ---------------------------------------------------------------------------
# SSE event stream (seam + service)
# ---------------------------------------------------------------------------


async def test_stream_events_emits_turn_tool_and_final(
    stub_embedding_service: AsyncMock,
) -> None:
    """The event stream surfaces turn / tool-call / tool-result / final."""
    await _seed_echo_op(stub_embedding_service)
    await _seed_definition(toolset={"meta_tools": ["call_operation"]})
    invoker = _invoker_with(_call_op_then_text("answer"))

    kinds: list[AgentRunEventKind] = []
    run_ids: set[str] = set()
    final_payload: dict[str, Any] | None = None
    async for run_id, event in invoker.stream_events(_make_operator(), "reader", "go"):
        kinds.append(event.kind)
        run_ids.add(str(run_id))
        if event.kind is AgentRunEventKind.FINAL:
            final_payload = event.data

    assert AgentRunEventKind.TURN in kinds
    assert AgentRunEventKind.TOOL_CALL in kinds
    assert AgentRunEventKind.TOOL_RESULT in kinds
    assert kinds[-1] is AgentRunEventKind.FINAL
    assert final_payload == {"output": "answer"}
    # All events share one durable run handle.
    assert len(run_ids) == 1

    # The streamed run's terminal outcome is recorded durably.
    run_id = UUID(next(iter(run_ids)))
    view = await invoker.poll(_make_operator(), run_id)
    assert view.status is AgentRunStatus.SUCCEEDED
    assert view.output == {"text": "answer"}


async def test_seam_stream_events_surfaces_budget_error() -> None:
    """A runaway loop yields a terminal ERROR event, not a raised exception."""
    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(
            lambda messages, info: ModelResponse(parts=[ToolCallPart("nonexistent_tool", {})])
        )
    )
    op = _make_operator()
    # request_limit=1 trips the budget after one turn (the model keeps
    # calling a tool that does not exist, so the loop never terminates on
    # its own and only the budget can stop it).
    definition = AgentDefinition(name="runaway", system_prompt="loop", request_limit=1)
    events: list[Any] = []
    async for event in runtime.stream_events(definition, op, "go", uuid4()):
        events.append(event)
    assert events[-1].kind is AgentRunEventKind.ERROR


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_run_rejects_unknown_definition() -> None:
    """A missing / cross-tenant name raises AgentNotFoundError."""
    await _seed_tenants()
    invoker = _invoker_with(_final_text("x"))
    with pytest.raises(AgentNotFoundError):
        await invoker.run(_make_operator(), "nonexistent", "go")


async def test_run_rejects_cross_tenant_definition() -> None:
    """Tenant B cannot run tenant A's definition (surfaces as not-found)."""
    await _seed_definition(tenant_id=_TENANT_A)
    invoker = _invoker_with(_final_text("x"))
    with pytest.raises(AgentNotFoundError):
        await invoker.run(_make_operator(tenant_id=_TENANT_B, sub="op-b"), "reader", "go")


async def test_run_rejects_disabled_definition() -> None:
    """A disabled definition raises AgentDisabledError."""
    await _seed_definition(enabled=False)
    invoker = _invoker_with(_final_text("x"))
    with pytest.raises(AgentDisabledError):
        await invoker.run(_make_operator(), "reader", "go")


async def test_failed_loop_records_failed_run() -> None:
    """A loop that errors records a ``failed`` run with the error message."""
    await _seed_definition()
    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(
            lambda messages, info: ModelResponse(parts=[ToolCallPart("nonexistent", {})])
        )
    )
    invoker = AgentInvoker(runtime=runtime)
    # request_limit on the seeded definition is 5; a model that only calls a
    # missing tool will exhaust the budget and surface as a failed run.
    outcome = await invoker.run(_make_operator(), "reader", "go")
    assert outcome.status is AgentRunStatus.FAILED
    assert outcome.error is not None


# ---------------------------------------------------------------------------
# Composition wiring (G11.1-T7 #1067) — agent invokes agent via the live invoker
# ---------------------------------------------------------------------------


def _composing_invoker_with(model: FunctionModel) -> AgentInvoker:
    """An invoker whose seam carries the FunctionModel *and* the real (DB-backed)
    child resolver + recorder + finalizer — exercises the live composition wiring
    end to end (the default invoker wires the same three callables)."""
    return AgentInvoker(
        runtime=PydanticAgentRun(
            model_factory=lambda: model,
            child_agent_resolver=_resolve_child_definition,
            child_run_recorder=_record_child_run,
            child_run_finalizer=_finalize_child_run,
        )
    )


def _parent_invokes_child_once(child_name: str, *, child_marker: str) -> FunctionModel:
    """A model that, as the parent, invokes *child_name* once then answers; as the
    child (detected by *child_marker* in its system prompt) answers directly."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        is_child = any(
            getattr(part, "part_kind", "") == "system-prompt"
            and child_marker in getattr(part, "content", "")
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if is_child:
            return ModelResponse(parts=[TextPart("child done")])
        has_tool_return = any(
            getattr(part, "part_kind", "") == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_tool_return:
            return ModelResponse(
                parts=[ToolCallPart("invoke_agent", {"agent_name": child_name, "inputs": "sub"})]
            )
        return ModelResponse(parts=[TextPart("parent done")])

    return FunctionModel(fn)


def _always_invoke_model(child_name: str) -> FunctionModel:
    """A model that calls ``invoke_agent`` every turn — a self-recursive cascade
    only the depth cap or the shared budget can terminate."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("invoke_agent", {"agent_name": child_name, "inputs": "recurse"})]
        )

    return FunctionModel(fn)


async def _agent_invoked_rows(tenant_id: UUID) -> list[AgentRunRow]:
    """All ``agent_run`` rows with ``trigger=agent-invoked`` for *tenant_id*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AgentRunRow).where(
                AgentRunRow.tenant_id == tenant_id,
                AgentRunRow.trigger == AgentRunTrigger.AGENT_INVOKED.value,
            )
        )
        return list(result.scalars().all())


async def test_default_invoker_wires_composition() -> None:
    """The default invoker builds a runtime with composition wired (#1067).

    The gap #1067 closes: before this, ``AgentInvoker()`` built a bare
    ``PydanticAgentRun()`` with no child hooks, so ``invoke_agent`` was never
    registered for a run started via the live surface.
    """
    invoker = AgentInvoker()
    runtime = invoker._runtime
    assert isinstance(runtime, PydanticAgentRun)
    assert runtime.child_agent_resolver is _resolve_child_definition
    assert runtime.child_run_recorder is _record_child_run
    assert runtime.child_run_finalizer is _finalize_child_run


async def test_resolver_scopes_to_tenant_and_enabled() -> None:
    """The child resolver returns a definition only for an enabled, same-tenant
    name; disabled / unknown / cross-tenant names resolve to ``None``."""
    await _seed_definition(name="reader", tenant_id=_TENANT_A, enabled=True)
    await _seed_definition(name="parked", tenant_id=_TENANT_A, enabled=False)
    op_a = _make_operator(tenant_id=_TENANT_A)

    resolved = await _resolve_child_definition(op_a, "reader")
    assert resolved is not None
    assert resolved.name == "reader"

    assert await _resolve_child_definition(op_a, "parked") is None  # disabled
    assert await _resolve_child_definition(op_a, "ghost") is None  # unknown

    op_b = _make_operator(tenant_id=_TENANT_B, sub="op-b")
    assert await _resolve_child_definition(op_b, "reader") is None  # cross-tenant


async def test_invoker_composition_persists_child_run_with_lineage() -> None:
    """A parent run invoked through the live surface invokes a child; the child
    ``agent_run`` row is persisted with ``trigger=agent-invoked`` and a
    ``parent_run_id`` pointing at the parent run (cascade tree reconstructable)."""
    await _seed_definition(
        name="parent", toolset={"meta_tools": []}, system_prompt="You are the PARENT."
    )
    await _seed_definition(
        name="child", toolset={"meta_tools": []}, system_prompt="You are the CHILD-AGENT."
    )
    invoker = _composing_invoker_with(
        _parent_invokes_child_once("child", child_marker="CHILD-AGENT")
    )

    outcome = await invoker.run(_make_operator(), "parent", "delegate")

    assert outcome.status is AgentRunStatus.SUCCEEDED
    assert outcome.output == {"text": "parent done"}

    children = await _agent_invoked_rows(_TENANT_A)
    assert len(children) == 1
    child_row = children[0]
    assert child_row.parent_run_id == outcome.run_id
    assert child_row.trigger == AgentRunTrigger.AGENT_INVOKED.value
    assert child_row.agent_definition_id is not None
    # The finalizer (#1087) closed the child row to its terminal state with the
    # child loop's output — not left stuck ``running``.
    assert child_row.status == AgentRunStatus.SUCCEEDED.value
    assert child_row.output == {"text": "child done"}
    assert child_row.error is None


async def test_invoker_composition_depth_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-invoking cascade through the live surface is bounded by
    ``agent_invoke_max_depth`` — the over-depth call never reaches the recorder."""
    monkeypatch.setenv("AGENT_INVOKE_MAX_DEPTH", "2")
    get_settings.cache_clear()
    await _seed_definition(name="loop", toolset={"meta_tools": []})
    invoker = _composing_invoker_with(_always_invoke_model("loop"))

    outcome = await invoker.run(_make_operator(), "loop", "start")

    assert outcome.status is AgentRunStatus.FAILED
    children = await _agent_invoked_rows(_TENANT_A)
    # depth-1 + depth-2 invocations each recorded a child row; the depth-3
    # invocation was rejected at the depth check, before the recorder ran
    # (mirrors the seam-level invariant in test_agent_invoke.py).
    assert len(children) == 2
    assert all(c.parent_run_id is not None for c in children)


async def test_invoker_composition_budget_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a high depth cap, the shared turn budget terminates the cascade — the
    run fails and the cascade stops well before the depth cap."""
    monkeypatch.setenv("AGENT_INVOKE_MAX_DEPTH", "8")
    get_settings.cache_clear()
    await _seed_definition(name="loop", toolset={"meta_tools": []}, turn_budget=1)
    invoker = _composing_invoker_with(_always_invoke_model("loop"))

    outcome = await invoker.run(_make_operator(), "loop", "start")

    assert outcome.status is AgentRunStatus.FAILED
    children = await _agent_invoked_rows(_TENANT_A)
    # The shared budget (turn_budget=1) tripped far below the depth cap of 8 —
    # budget, not depth, bounded the cascade.
    assert len(children) < 8


# ---------------------------------------------------------------------------
# Child-run finalization (G11.1-T8 #1087) — invoked child rows reach a terminal
# status (succeeded / failed) through the live AgentInvoker, not stuck running.
# ---------------------------------------------------------------------------


def _parent_invokes_failing_child(child_name: str, *, child_marker: str) -> FunctionModel:
    """A model where the parent invokes *child_name* once then answers; the child
    (detected by *child_marker*) loops on an op-less tool call so it exhausts its
    own turn budget and the child loop fails with ``AgentRunError``."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        is_child = any(
            getattr(part, "part_kind", "") == "system-prompt"
            and child_marker in getattr(part, "content", "")
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if is_child:
            # Keep emitting an op-less tool call so the child never reaches a
            # final answer -> its own turn budget trips -> AgentRunError.
            return ModelResponse(parts=[ToolCallPart("noop_tool", {})])
        # The failed child surfaces to the parent as a ``retry-prompt`` (the
        # tool's ModelRetry). The parent invokes the child exactly once, then
        # recovers on seeing that prompt so the *parent* run still succeeds —
        # isolating the child's terminal status from the parent's outcome.
        already_invoked = any(
            getattr(part, "part_kind", "") == "retry-prompt"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not already_invoked:
            return ModelResponse(
                parts=[ToolCallPart("invoke_agent", {"agent_name": child_name, "inputs": "sub"})]
            )
        return ModelResponse(parts=[TextPart("parent recovered")])

    return FunctionModel(fn)


async def test_invoker_composition_finalizes_failed_child() -> None:
    """An over-budget child run reaches ``status='failed'`` with the error
    recorded — the finalizer closes a failed child, it is not stuck ``running``.

    The parent's ``invoke_agent`` of the failing child surfaces as a
    ``ModelRetry`` the parent recovers from, so the *parent* succeeds while the
    *child* row is ``failed`` — proving the child lifecycle is finalized
    independently of the parent's outcome.
    """
    await _seed_definition(
        name="parent", toolset={"meta_tools": []}, system_prompt="You are the PARENT."
    )
    await _seed_definition(
        name="child",
        toolset={"meta_tools": []},
        system_prompt="You are the CHILD-AGENT.",
        turn_budget=1,
    )
    invoker = _composing_invoker_with(
        _parent_invokes_failing_child("child", child_marker="CHILD-AGENT")
    )

    outcome = await invoker.run(_make_operator(), "parent", "delegate")

    assert outcome.status is AgentRunStatus.SUCCEEDED  # parent recovered
    children = await _agent_invoked_rows(_TENANT_A)
    assert len(children) == 1
    child_row = children[0]
    assert child_row.status == AgentRunStatus.FAILED.value
    assert child_row.error is not None
    assert "turn budget exhausted" in child_row.error
    assert child_row.output is None


async def test_finalize_child_run_swallows_illegal_transition() -> None:
    """A finalizer call against an already-terminal child row swallows the
    ``IllegalTransitionError`` (mirrors ``_finalize_run``) — e.g. the row was
    cancelled mid-flight before the finalizer ran."""
    await _seed_definition(name="child", toolset={"meta_tools": []})
    op = _make_operator()
    # Record a child row (pending -> running), then drive it to a terminal
    # state out-of-band, simulating a cancel landing before the finalizer runs.
    child_def = await _resolve_child_definition(op, "child")
    assert child_def is not None
    child_run_id, _lease_owner = await _record_child_run(
        operator=op, definition=child_def, parent_run_id=None
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await run_lifecycle.get_run(session, child_run_id)
        assert row is not None
        await run_lifecycle.cancel_run(session, child_run_id, operator=op)
        await session.commit()

    # The finalizer must not raise even though the row is already terminal.
    await _finalize_child_run(child_run_id, output="late answer", error=None)

    async with sessionmaker() as session:
        row = await run_lifecycle.get_run(session, child_run_id)
        assert row is not None
        # The cancel's terminal state stands; the finalizer did not overwrite it.
        assert row.status == AgentRunStatus.CANCELLED.value
        assert row.output is None


# ---------------------------------------------------------------------------
# work_ref I3-T2 (#1662): an agent-invoked child run inherits the parent's
# external change-ticket reference off the shared ``work_ref_var`` ContextVar —
# children are recorded inside the parent's invoker.run call while the var is
# still bound, so the child must land the same work_ref, not NULL.
# ---------------------------------------------------------------------------


async def test_record_child_run_inherits_bound_work_ref() -> None:
    """A child run recorded while ``work_ref_var`` is bound persists that
    work_ref — mirroring the top-level ``_create_run_row`` path. The defect this
    guards: child runs landing NULL even when the parent's ticket is bound."""
    await _seed_definition(name="child", toolset={"meta_tools": []})
    op = _make_operator()
    child_def = await _resolve_child_definition(op, "child")
    assert child_def is not None

    token = work_ref_var.set("gh:evoila/meho#1662")
    try:
        child_run_id, _lease_owner = await _record_child_run(
            operator=op, definition=child_def, parent_run_id=None
        )
    finally:
        work_ref_var.reset(token)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await run_lifecycle.get_run(session, child_run_id)
        assert row is not None
        assert row.work_ref == "gh:evoila/meho#1662"


async def test_record_child_run_leaves_work_ref_null_when_unbound() -> None:
    """With no ticket bound on ``work_ref_var``, a child run records work_ref as
    NULL — inheritance is opt-in via the ContextVar, never fabricated."""
    await _seed_definition(name="child", toolset={"meta_tools": []})
    op = _make_operator()
    child_def = await _resolve_child_definition(op, "child")
    assert child_def is not None

    # work_ref_var defaults to None; assert that to make the precondition explicit.
    assert work_ref_var.get() is None
    child_run_id, _lease_owner = await _record_child_run(
        operator=op, definition=child_def, parent_run_id=None
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await run_lifecycle.get_run(session, child_run_id)
        assert row is not None
        assert row.work_ref is None


# ---------------------------------------------------------------------------
# G11.2-T2 (#816): autonomous run_scheduled binds the run to the authenticating
# agent — agent A's credentials must not launch agent B's definition.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scheduled_rejects_cross_agent_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled run fails closed when the definition's identity_ref does not
    name the authenticated client (no DB run row is created)."""
    from types import SimpleNamespace

    # The runtime is never reached — the identity guard raises first.
    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _final_text("ok")))

    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(return_value=_make_operator(sub="sa-uuid-a")),
    )
    # Credentials authenticate client "agent:a" but the named definition
    # belongs to "agent:b" — cross-agent launch must be refused.
    monkeypatch.setattr(
        invoker,
        "_load_definition",
        AsyncMock(return_value=SimpleNamespace(name="other-bot", identity_ref="agent:b")),
    )
    create_spy = AsyncMock()
    monkeypatch.setattr(invoker, "_create_run_row", create_spy)

    with pytest.raises(AgentInvocationError, match="do not own definition"):
        await invoker.run_scheduled(
            "other-bot",
            "do the thing",
            agent_client_id="agent:a",
            agent_client_secret=SecretStr("s3cr3t"),
        )
    # Fail-closed before persisting anything.
    create_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_scheduled_allows_matching_agent_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When identity_ref names the authenticated client, the guard passes and
    the run is created (stopped at _create_run_row to avoid the full loop)."""
    from types import SimpleNamespace

    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _final_text("ok")))
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(return_value=_make_operator(sub="sa-uuid-a")),
    )
    monkeypatch.setattr(
        invoker,
        "_load_definition",
        AsyncMock(
            return_value=SimpleNamespace(
                name="a-bot", identity_ref="agent:a", model_tier="standard", id=uuid4()
            )
        ),
    )
    # Pass the guard, then stop the flow at run-row creation. The
    # stubbed definition needs the real ``AgentDefinition`` shape now
    # that the G11.5-T6 #1080 pre-execution budget gate reads
    # ``definition.tier``; the budget gate itself returns ALLOW
    # unchanged (no budget configured for this principal).
    from meho_backplane.agent.run import AgentDefinition

    monkeypatch.setattr(
        invoker,
        "_to_agent_definition",
        lambda entry: AgentDefinition(
            name="a-bot",
            system_prompt="stub",
            request_limit=1,
        ),
    )
    boom = RuntimeError("stop-after-guard")
    create_spy = AsyncMock(side_effect=boom)
    monkeypatch.setattr(invoker, "_create_run_row", create_spy)

    with pytest.raises(RuntimeError, match="stop-after-guard"):
        await invoker.run_scheduled(
            "a-bot",
            "do the thing",
            agent_client_id="agent:a",
            agent_client_secret=SecretStr("s3cr3t"),
        )
    # Guard passed — the run row creation was reached.
    create_spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# #1502: run_scheduled bounds its wait so a hung run cannot block the serial
# scheduler tick (and strand the advisory lock) until a pod restart.
# ---------------------------------------------------------------------------


def _blocking_model(gate: asyncio.Event) -> FunctionModel:
    """A model whose first turn awaits *gate* — simulates a hung HTTP call."""

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await gate.wait()
        return ModelResponse(parts=[TextPart("eventually")])

    return FunctionModel(fn)


async def test_run_scheduled_bounds_wait_and_converts_to_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled run still executing at the deadline returns a running handle.

    The wait abandons (``converted_to_async``) instead of blocking forever, so
    the serial scheduler tick can return and release its advisory lock; the
    background loop keeps running and is later reaped/finalised (#1502).
    """
    await _seed_definition(name="reporter")
    gate = asyncio.Event()
    invoker = AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _blocking_model(gate)))

    # The scheduled path obtains a client_credentials token and verifies the
    # JWT before launching; stub both seams (no Keycloak in unit tests). The
    # verified operator must own the seeded ``agent:reporter`` definition.
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(return_value=_make_operator(sub="sa-reporter")),
    )

    # Drive the bound near zero so the wait abandons deterministically while the
    # background loop is still parked on the gate — no real long-running call.
    monkeypatch.setenv("AGENT_SYNC_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()

    try:
        outcome = await asyncio.wait_for(
            invoker.run_scheduled(
                "reporter",
                "go",
                agent_client_id="agent:reporter",
                agent_client_secret=SecretStr("s3cr3t"),
            ),
            # Generous ceiling: the call must return on the 0.05s inner bound,
            # well under this; exceeding it means run_scheduled blocked on the
            # still-gated loop — the bug this guards against.
            timeout=5.0,
        )
    finally:
        # Release the background loop so it finalises cleanly regardless of
        # outcome (avoids a "Task was destroyed but it is pending" warning).
        gate.set()

    assert outcome.converted_to_async is True
    assert outcome.status is AgentRunStatus.RUNNING

    # The abandoned loop keeps running and reaches a terminal state once the
    # gate is released — the wait was abandoned, not the run.
    for _ in range(200):
        view = await invoker.poll(_make_operator(sub="sa-reporter"), outcome.run_id)
        if view.status is AgentRunStatus.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    assert view.status is AgentRunStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# #2077: unexecutable-runbook run-start guard — a zero-tool-call run that
# cannot satisfy its instruction fails typed instead of reporting a
# hallucinated ``succeeded``.
# ---------------------------------------------------------------------------


def _model_that_must_not_be_called() -> FunctionModel:
    """A model that fails the test if the loop ever consults it.

    The #2077 guard's contract is *pre-model* refusal: proving the model is
    never called also proves the run completed with zero tool-call
    dispatches (there is no other way for the loop to emit one).
    """

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the model must not be consulted for an unexecutable-runbook run")

    return FunctionModel(fn)


async def test_run_refuses_unexecutable_runbook_reference_in_prompt() -> None:
    """The #2077 repro: prompt "use runbook <slug>", default toolset, no tool.

    The regression test for both observed defects: (a) the zero-tool-call
    run reports a non-success terminal state (the model is never even
    consulted), and (b) the reason is machine-classifiable on the outcome
    object (``UNEXECUTABLE_RUNBOOK_CLASS`` prefix), not a free-text
    confabulation in ``output.text``.
    """
    await _seed_definition(
        name="runbook-bot",
        system_prompt="use runbook vcenter-9.0-snapshot-revert",
        toolset={},
        turn_budget=20,
    )
    invoker = _invoker_with(_model_that_must_not_be_called())

    outcome = await invoker.run(_make_operator(), "runbook-bot", "go")

    assert outcome.status is AgentRunStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.startswith(UNEXECUTABLE_RUNBOOK_CLASS)
    assert "no available tool to execute runbook" in outcome.error
    assert "'vcenter-9.0-snapshot-revert'" in outcome.error
    # The confirm-gated path is surfaced explicitly (issue AC 4): the reason
    # names the human-operator contract instead of silently degrading.
    assert "human operator" in outcome.error
    assert outcome.output is None

    # The refusal is durable + honest: failed row, no output, zero turns.
    view = await invoker.poll(_make_operator(), outcome.run_id)
    assert view.status is AgentRunStatus.FAILED
    assert view.error == outcome.error
    assert view.output is None
    assert view.turns == 0


async def test_run_refuses_runbook_instruction_in_inputs() -> None:
    """The unexecutable instruction may arrive via the run inputs, too."""
    await _seed_definition(name="helper")
    invoker = _invoker_with(_model_that_must_not_be_called())

    outcome = await invoker.run(_make_operator(), "helper", "please execute runbook disk-cleanup")

    assert outcome.status is AgentRunStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.startswith(UNEXECUTABLE_RUNBOOK_CLASS)
    assert "'disk-cleanup'" in outcome.error


async def test_run_mentioning_runbooks_without_instruction_still_runs() -> None:
    """A prompt that merely *mentions* runbooks is not refused (no false trip)."""
    await _seed_definition(
        name="runbook-faq",
        system_prompt="You answer questions about runbook authoring and history.",
    )
    invoker = _invoker_with(_final_text("a runbook is an operator procedure"))

    outcome = await invoker.run(_make_operator(), "runbook-faq", "what is a runbook?")

    assert outcome.status is AgentRunStatus.SUCCEEDED
    assert outcome.output == {"text": "a runbook is an operator procedure"}


async def test_stream_events_refuses_unexecutable_runbook() -> None:
    """The SSE path emits one terminal ``error`` frame and records the failed row."""
    await _seed_definition(
        name="runbook-bot-sse",
        system_prompt="use runbook vcenter-9.0-snapshot-revert",
    )
    invoker = _invoker_with(_model_that_must_not_be_called())

    events = [
        (run_id, event)
        async for run_id, event in invoker.stream_events(_make_operator(), "runbook-bot-sse", "go")
    ]

    assert len(events) == 1
    run_id, event = events[0]
    assert event.kind is AgentRunEventKind.ERROR
    error = str(event.data["error"])
    assert error.startswith(UNEXECUTABLE_RUNBOOK_CLASS)

    view = await invoker.poll(_make_operator(), run_id)
    assert view.status is AgentRunStatus.FAILED
    assert view.error is not None
    assert view.error.startswith(UNEXECUTABLE_RUNBOOK_CLASS)


async def test_run_scheduled_refuses_unexecutable_runbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduled path applies the same guard after the no-input check."""
    await _seed_definition(name="runbook-cron")
    invoker = _invoker_with(_model_that_must_not_be_called())

    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(return_value=_make_operator(sub="sa-runbook-cron")),
    )

    outcome = await invoker.run_scheduled(
        "runbook-cron",
        "execute runbook disk-cleanup",
        agent_client_id="agent:runbook-cron",
        agent_client_secret=SecretStr("s3cr3t"),
    )

    assert outcome.status is AgentRunStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.startswith(UNEXECUTABLE_RUNBOOK_CLASS)
    assert "'disk-cleanup'" in outcome.error
