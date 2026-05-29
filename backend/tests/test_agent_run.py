# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``AgentRun`` seam (G11.1-T1 / #808).

These exercise the seam against a deterministic
:class:`~pydantic_ai.models.function.FunctionModel` so no real LLM is hit
(python_best_practices §14 — no network in unit tests). The acceptance
criteria from #808 map onto the tests as:

* ``test_loop_calls_call_operation_tool_against_seeded_op`` — a real
  in-process loop drives the ``call_operation`` tool against a seeded typed
  op and returns its result. Proves the dispatch path end to end.
* ``test_turn_budget_caps_the_loop`` — a model that loops forever is
  stopped by the ``request_limit`` turn budget, surfacing as a failed run.
* ``test_structured_output_is_validated`` — an ``output_type`` model is the
  run's typed output.
* ``test_operator_is_threaded_into_tool_calls`` — the operator handed to
  ``start`` reaches the tool (RBAC/audit see the right principal).

The ``FunctionModel`` callback decides each turn from the message history:
turn 1 emits a ``call_operation`` tool call; once the tool result is in the
history it emits either a final text answer or the structured-output tool
call, depending on whether the agent was configured with an ``output_type``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meho_backplane.agent import (
    AgentDefinition,
    AgentRunError,
    AgentRunStatus,
    PydanticAgentRun,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.retrieval.embedding import EMBEDDING_DIMENSION
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires for this module.

    ``get_settings`` is :func:`functools.lru_cache`-wrapped, so the cache is
    cleared around each test to pick up the pinned values (and not leak a
    stale ``Settings`` into the next module).
    """
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
    """A stub embedding service returning a fixed-dimension vector.

    The seeded descriptor needs an embedding column; the value is irrelevant
    to these tests (no semantic search runs), so a deterministic vector
    keeps registration fast and offline. The shape mirrors
    :class:`~meho_backplane.retrieval.embedding.EmbeddingService` —
    ``encode_one`` / ``encode`` / ``dimension``.
    """
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


#: Records the operator sub each handler invocation saw, so a test can assert
#: the seam threaded the right principal all the way to the dispatch handler.
_seen_operator_subs: list[str] = []


async def _echo_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Typed handler that echoes its params and records the operator sub."""
    _seen_operator_subs.append(operator.sub)
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


def _call_op_then(final_part: TextPart | ToolCallPart):
    """Build a FunctionModel callback: call the op once, then emit *final_part*.

    Turn 1 (only a system + user request in history) emits a
    ``call_operation_tool`` tool call. Once the tool's return is in the
    message history, the callback emits *final_part* — either a final text
    answer or the structured-output tool call.
    """

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        has_tool_return = any(
            part.part_kind == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_tool_return:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "call_operation_tool",
                        {
                            "connector_id": "vault-1.x",
                            "op_id": "vault.kv.read",
                            "params": {"path": "secret/foo"},
                        },
                    )
                ]
            )
        return ModelResponse(parts=[final_part])

    return fn


async def test_loop_calls_call_operation_tool_against_seeded_op(
    stub_embedding_service: AsyncMock,
) -> None:
    """A real loop drives ``call_operation`` against a seeded op and returns."""
    await _seed_echo_op(stub_embedding_service)
    _seen_operator_subs.clear()

    model = FunctionModel(_call_op_then(TextPart("done: secret read")))
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(
        name="reader",
        system_prompt="You read secrets via MEHO operations.",
        request_limit=5,
    )

    handle = runtime.start(definition, _make_operator(), "read secret/foo")
    result = await runtime.result(handle)

    assert runtime.poll(handle) is AgentRunStatus.SUCCEEDED
    assert result.output == "done: secret read"
    # The loop made >=2 model requests (tool call, then final) and 1 tool call.
    assert result.request_count >= 2
    assert result.tool_call_count == 1


async def test_turn_budget_caps_the_loop(
    stub_embedding_service: AsyncMock,
) -> None:
    """A runaway model is stopped by the ``request_limit`` turn budget.

    The op is seeded so the tool call *succeeds* every turn — the loop is
    stopped by the budget, not by a tool error, which is what the turn-budget
    contract must guarantee.
    """
    await _seed_echo_op(stub_embedding_service)

    def loop_forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Always call a tool that succeeds — never terminate on its own, so
        # only the turn budget can end the loop.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "call_operation_tool",
                    {
                        "connector_id": "vault-1.x",
                        "op_id": "vault.kv.read",
                        "params": {"path": "secret/foo"},
                    },
                )
            ]
        )

    model = FunctionModel(loop_forever)
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(
        name="runaway",
        system_prompt="loop",
        request_limit=3,
    )

    handle = runtime.start(definition, _make_operator(), "go")
    with pytest.raises(AgentRunError, match="turn budget exhausted"):
        await runtime.result(handle)
    assert runtime.poll(handle) is AgentRunStatus.FAILED


class _SecretSummary(BaseModel):
    """Structured-output schema for the validated-output test."""

    path: str
    found: bool


async def test_structured_output_is_validated(
    stub_embedding_service: AsyncMock,
) -> None:
    """An ``output_type`` model is the run's typed, validated output."""
    await _seed_echo_op(stub_embedding_service)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        has_tool_return = any(
            part.part_kind == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_tool_return:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "call_operation_tool",
                        {
                            "connector_id": "vault-1.x",
                            "op_id": "vault.kv.read",
                            "params": {"path": "secret/foo"},
                        },
                    )
                ]
            )
        # Emit the framework's final-result tool with the structured payload.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"path": "secret/foo", "found": True},
                )
            ]
        )

    model = FunctionModel(fn)
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(
        name="structured-reader",
        system_prompt="Return a structured summary.",
        request_limit=5,
        output_type=_SecretSummary,
    )

    handle = runtime.start(definition, _make_operator(), "read secret/foo")
    result = await runtime.result(handle)

    assert isinstance(result.output, _SecretSummary)
    assert result.output.path == "secret/foo"
    assert result.output.found is True


async def test_operator_is_threaded_into_tool_calls(
    stub_embedding_service: AsyncMock,
) -> None:
    """The operator handed to ``start`` reaches the dispatched handler."""
    await _seed_echo_op(stub_embedding_service)
    _seen_operator_subs.clear()

    model = FunctionModel(_call_op_then(TextPart("ok")))
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(name="reader", system_prompt="read", request_limit=5)

    operator = _make_operator(sub="op-distinct-principal")
    handle = runtime.start(definition, operator, "read secret/foo")
    await runtime.result(handle)

    assert _seen_operator_subs == ["op-distinct-principal"]


async def test_stream_yields_final_output(
    stub_embedding_service: AsyncMock,
) -> None:
    """``stream`` yields the loop's final answer (T1 single-chunk contract)."""
    await _seed_echo_op(stub_embedding_service)

    model = FunctionModel(_call_op_then(TextPart("streamed answer")))
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(name="reader", system_prompt="read", request_limit=5)

    handle = runtime.start(definition, _make_operator(), "read secret/foo")
    chunks: list[str] = []
    stream: AsyncIterator[str] = runtime.stream(handle)
    async for chunk in stream:
        chunks.append(chunk)

    assert chunks == ["streamed answer"]


async def test_default_model_factory_fail_closed_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default model factory fails closed when no API key is configured."""
    from meho_backplane.agent.run import default_model_factory
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(AgentRunError, match="no ANTHROPIC_API_KEY"):
            default_model_factory()
    finally:
        get_settings.cache_clear()


async def test_toolset_definition_drives_resolved_call_operation(
    stub_embedding_service: AsyncMock,
) -> None:
    """A definition with a toolset spec runs the resolved ``call_operation`` end to end.

    Proves T3 (#810) integrates into the live seam: the loop registers the
    toolset-resolved tools (here just ``call_operation``) and dispatches a
    seeded op under the run's operator — the same dispatch path REST + MCP
    use (#810 ACs 1 + 2).
    """
    await _seed_echo_op(stub_embedding_service)
    _seen_operator_subs.clear()

    # The resolved meta-tool is named ``call_operation`` (matching the MCP
    # surface), not the T1 hand-wired ``call_operation_tool``; call it once
    # then finish.
    def call_op_then_done(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        has_tool_return = any(
            part.part_kind == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_tool_return:
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
        return ModelResponse(parts=[TextPart("done via toolset")])

    model = FunctionModel(call_op_then_done)
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(
        name="scoped-reader",
        system_prompt="read via the scoped toolset",
        request_limit=5,
        toolset={"meta_tools": ["call_operation"], "connectors": ["vault-1.x"]},
    )

    operator = _make_operator(sub="op-toolset-principal")
    handle = runtime.start(definition, operator, "read secret/foo")
    result = await runtime.result(handle)

    assert runtime.poll(handle) is AgentRunStatus.SUCCEEDED
    assert result.output == "done via toolset"
    assert result.tool_call_count == 1
    # The operator threaded through the resolved tool to the dispatch handler.
    assert _seen_operator_subs == ["op-toolset-principal"]


async def test_toolset_omitting_call_operation_makes_it_absent_from_surface(
    stub_embedding_service: AsyncMock,
) -> None:
    """A meta-tool omitted from the toolset spec is not on the model's surface.

    The intersection means the model literally cannot see / call a tool the
    spec excluded. The ``FunctionModel`` callback inspects ``info.function_tools``
    to assert ``call_operation`` is absent when the spec lists only
    ``list_operation_groups`` (#810 AC 1: disallowed ops are absent).
    """
    await _seed_echo_op(stub_embedding_service)
    captured: dict[str, list[str]] = {}

    def inspect_surface(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["tool_names"] = sorted(td.name for td in info.function_tools)
        return ModelResponse(parts=[TextPart("inspected")])

    model = FunctionModel(inspect_surface)
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(
        name="discovery-only",
        system_prompt="discover only",
        request_limit=3,
        toolset={"meta_tools": ["list_operation_groups"]},
    )

    handle = runtime.start(definition, _make_operator(), "what groups exist?")
    await runtime.result(handle)

    assert captured["tool_names"] == ["list_operation_groups"]
    assert "call_operation" not in captured["tool_names"]


async def test_read_only_identity_gets_no_tools_in_loop(
    stub_embedding_service: AsyncMock,
) -> None:
    """A read_only identity ranks below every meta-tool floor -> empty surface.

    The seam-level mirror of the unit intersection test: even with all
    meta-tools listed in the spec, a ``read_only`` operator's loop registers
    no tools, so the agent cannot dispatch anything (#810 AC: an op outside
    the identity's perms is not callable).
    """
    await _seed_echo_op(stub_embedding_service)
    captured: dict[str, list[str]] = {}

    def inspect_surface(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["tool_names"] = sorted(td.name for td in info.function_tools)
        return ModelResponse(parts=[TextPart("no tools for me")])

    model = FunctionModel(inspect_surface)
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(
        name="read-only-agent",
        system_prompt="read only",
        request_limit=3,
        toolset={"meta_tools": ["list_operation_groups", "search_operations", "call_operation"]},
    )

    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-ro")
    handle = runtime.start(definition, read_only, "do something")
    await runtime.result(handle)

    assert captured["tool_names"] == []
