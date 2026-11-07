# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for agent-invokes-agent composition (G11.1-T5 / #812).

These exercise the ``invoke_agent`` meta-tool against a deterministic
:class:`~pydantic_ai.models.function.FunctionModel` (no real LLM hit;
python_best_practices §14 -- no network in unit tests). The #812 acceptance
criteria map onto the tests as:

* ``test_running_agent_invokes_child_under_same_identity`` -- a running agent
  invokes another definition; the child runs under the same operator and its
  output flows back. Proves the composition path end to end.
* ``test_cascade_terminates_on_depth_cap`` -- a self-invoking cascade is stopped
  by ``agent_invoke_max_depth``; the over-depth call never starts a child and
  the model receives a structured retry, not unbounded spend.
* ``test_cascade_terminates_on_budget`` -- the child's turns count against the
  shared ``usage`` budget, so a cascade exceeding the turn budget trips
  ``UsageLimitExceeded`` (surfaced as ``AgentRunError``), terminating the chain.
* ``test_child_run_linked_to_parent_in_lineage`` -- the recorder is called with
  the parent run id, so the cascade tree (``parent_run_id`` lineage) is
  reconstructable.
* ``test_finalizer_closes_child_on_success`` /
  ``test_finalizer_closes_child_on_failure`` -- a recorded child row is closed
  via the finalizer after the child loop (G11.1-T8 #1087): on success with the
  output, on ``AgentRunError`` with the error before re-raising ``ModelRetry``.
* ``test_finalizer_not_called_without_recorder`` -- no recorder means no child
  run id, so the finalizer is never reached (nothing to close).
* ``test_unknown_child_agent_is_a_model_retry`` -- an unresolvable agent name is
  a recoverable ``ModelRetry``, not a crash.

The ``FunctionModel`` callback decides each turn from the message history: a
parent that has not yet seen a tool return emits an ``invoke_agent`` call; once
the child's result is in history it emits a final answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage

from meho_backplane.agent import (
    AGENT_INVOKE_DEPTH_TOP_LEVEL,
    AgentDefinition,
    AgentInvocationDepthExceeded,
    AgentRunError,
    ChildAgentResolver,
    ChildRunRecorder,
    ChildRunResult,
    agent_invoke_depth_var,
    current_agent_run_id_var,
    make_invoke_agent_tool,
)
from meho_backplane.agent.run import AgentRunEventKind, PydanticAgentRun
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations.identity_budget import TokenUsage
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars :class:`Settings` requires + reset the cache per test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_invoke_contextvars() -> Iterator[None]:
    """Guard against contextvar leakage between tests (per-task by contract, but
    the test runner shares a task)."""
    yield
    assert agent_invoke_depth_var.get() == AGENT_INVOKE_DEPTH_TOP_LEVEL
    assert current_agent_run_id_var.get() is None


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


def _always_invoke(child_name: str):
    """FunctionModel callback that invokes a child every turn (never stops).

    Used to build a self-recursive cascade: each level invokes the same child,
    so only the depth cap (or budget) can terminate it.
    """

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "invoke_agent",
                    {"agent_name": child_name, "inputs": "recurse"},
                )
            ]
        )

    return fn


async def test_running_agent_invokes_child_under_same_identity() -> None:
    """A running agent invokes another definition; the child runs + returns.

    Exercises the real wiring: a ``PydanticAgentRun`` with a
    ``child_agent_resolver`` injected carries the ``invoke_agent`` tool, so a
    started run can invoke a child. The child runs under the same operator.
    """
    seen_child_operators: list[Operator] = []

    child_def = AgentDefinition(
        name="deep-agent",
        system_prompt="You answer hard sub-questions.",
        request_limit=5,
        # The child uses the FunctionModel below; an empty toolset keeps its
        # surface to just whatever the runtime appends (none here).
        toolset={"meta_tools": []},
    )

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        if agent_name == "deep-agent":
            seen_child_operators.append(operator)
            return child_def
        return None

    # One FunctionModel serves both the parent (invoke once, then finish) and
    # the child (no tool return possible -> emit final text immediately, since
    # the child's first turn has no prior tool return either). Disambiguate by
    # system prompt content in the message history.
    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        is_child = any(
            part.part_kind == "system-prompt" and "hard sub-questions" in part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if is_child:
            return ModelResponse(parts=[TextPart("child answer")])
        # parent: invoke once, then finish
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
                        "invoke_agent",
                        {"agent_name": "deep-agent", "inputs": "sub-task"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("parent done")])

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(model_fn),
        child_agent_resolver=resolver,
    )
    parent_def = AgentDefinition(
        name="cheap-agent",
        system_prompt="You delegate hard sub-tasks.",
        request_limit=5,
        toolset={"meta_tools": []},
    )

    operator = _make_operator()
    handle = runtime.start(parent_def, operator, "delegate this")
    result = await runtime.result(handle)

    assert result.output == "parent done"
    assert len(seen_child_operators) == 1
    assert seen_child_operators[0].tenant_id == operator.tenant_id
    assert seen_child_operators[0].sub == operator.sub


async def test_cascade_terminates_on_depth_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-invoking cascade is stopped by ``agent_invoke_max_depth``.

    Cap is set to 2: depth-1 and depth-2 invocations start children; the
    depth-3 invocation never starts -- it raises
    :class:`AgentInvocationDepthExceeded`, surfaced to the model as a
    ``ModelRetry``. The recursive model only ever calls ``invoke_agent`` (it
    never produces a final answer), so once the cap denies the deepest call the
    framework exhausts the tool's retries and the run fails -- the seam
    surfaces that as :class:`AgentRunError`. That is the deterministic
    termination we want: the chain stopped at the cap (a structured error), it
    did not spend unbounded. The ``child_starts == 2`` assertion proves the
    over-depth child never resolved or ran.
    """
    monkeypatch.setenv("AGENT_INVOKE_MAX_DEPTH", "2")
    get_settings.cache_clear()

    child_starts = 0

    recursive_def = AgentDefinition(
        name="recursive-agent",
        system_prompt="loop",
        request_limit=20,
        toolset={"meta_tools": []},
    )

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        nonlocal child_starts
        if agent_name == "recursive-agent":
            child_starts += 1
            return recursive_def
        return None

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(_always_invoke("recursive-agent")),
        child_agent_resolver=resolver,
    )

    # The top-level agent recurses too; the cascade is depth-bounded.
    handle = runtime.start(recursive_def, _make_operator(), "start")
    with pytest.raises(AgentRunError):
        await runtime.result(handle)

    # depth-1 + depth-2 children resolved + started; depth-3 was rejected at
    # the depth check, before the resolver was reached.
    assert child_starts == 2


async def test_depth_check_rejects_before_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit-level: an over-depth invoke raises before resolving / running.

    Isolates the depth guard from the framework: pre-set the depth contextvar to
    the cap, then assert the tool's resolver + child_runner are never reached and
    the model sees a ``ModelRetry``.
    """
    monkeypatch.setenv("AGENT_INVOKE_MAX_DEPTH", "1")
    get_settings.cache_clear()

    resolver_calls = 0
    runner_calls = 0

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        nonlocal resolver_calls
        resolver_calls += 1
        return AgentDefinition(name=agent_name, system_prompt="x", request_limit=3)

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> ChildRunResult:
        nonlocal runner_calls
        runner_calls += 1
        return ChildRunResult(output="should-not-run")

    invoke_tool = make_invoke_agent_tool(resolver=resolver, child_runner=child_runner)
    fn = invoke_tool.function

    # Pre-set depth to the cap so the next invoke breaches it.
    token = agent_invoke_depth_var.set(1)
    try:
        ctx = _FakeCtx(deps=_make_operator(), usage=RunUsage())
        with pytest.raises(ModelRetry, match="maximum invocation depth"):
            await fn(ctx, agent_name="anything", inputs="go")
    finally:
        agent_invoke_depth_var.reset(token)

    assert resolver_calls == 0  # rejected before resolution
    assert runner_calls == 0  # never spent


async def test_cascade_terminates_on_budget() -> None:
    """The child's turns share the parent budget, so the cascade trips it.

    The child model loops forever (only tool-less text would stop it; here it
    keeps emitting an op-less response that the FunctionModel turns into model
    requests). Driven with a shared usage at ``request_limit=1`` via
    ``run_child``, the child trips ``UsageLimitExceeded`` -> ``AgentRunError``.
    """

    def loop_forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Emit a tool call so the loop never self-terminates on a final answer.
        return ModelResponse(parts=[ToolCallPart("noop_tool", {})])

    child_def = AgentDefinition(
        name="runaway-child",
        system_prompt="loop",
        request_limit=1,  # shared budget exhausts immediately
    )

    runtime = PydanticAgentRun(model_factory=lambda: FunctionModel(loop_forever))

    # Pre-load the shared usage near/at the limit so the child run trips it.
    shared = RunUsage(requests=1)
    with pytest.raises(AgentRunError, match="turn budget exhausted"):
        await runtime.run_child(
            definition=child_def,
            operator=_make_operator(),
            inputs="go",
            usage=shared,
        )


async def test_child_run_linked_to_parent_in_lineage() -> None:
    """The recorder is called with the parent run id -> cascade tree walkable."""
    parent_run_id = uuid4()
    child_run_id = uuid4()
    recorded: list[dict[str, Any]] = []

    child_def = AgentDefinition(name="deep-agent", system_prompt="answer", request_limit=5)

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return child_def

    seen_run_id_in_child: list[UUID | None] = []

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> ChildRunResult:
        # During the child loop, the current-run contextvar is the child's id.
        seen_run_id_in_child.append(current_agent_run_id_var.get())
        return ChildRunResult(output="child answer")

    async def recorder(
        *,
        operator: Operator,
        definition: AgentDefinition,
        parent_run_id: UUID | None,
    ) -> tuple[UUID, str]:
        recorded.append(
            {
                "tenant_id": operator.tenant_id,
                "definition_name": definition.name,
                "parent_run_id": parent_run_id,
            }
        )
        return child_run_id, "test-worker:0"

    invoke_tool = make_invoke_agent_tool(
        resolver=resolver, child_runner=child_runner, recorder=recorder
    )
    fn = invoke_tool.function

    # Simulate a parent run already associated with a durable agent_run row.
    run_token = current_agent_run_id_var.set(parent_run_id)
    try:
        ctx = _FakeCtx(deps=_make_operator(), usage=RunUsage())
        out = await fn(ctx, agent_name="deep-agent", inputs="sub-task")
    finally:
        current_agent_run_id_var.reset(run_token)

    assert out == {"agent": "deep-agent", "output": "child answer"}
    assert len(recorded) == 1
    assert recorded[0]["parent_run_id"] == parent_run_id
    assert recorded[0]["definition_name"] == "deep-agent"
    assert recorded[0]["tenant_id"] == _TENANT_A
    # Inside the child loop the lineage contextvar was the recorded child id.
    assert seen_run_id_in_child == [child_run_id]


def _resolver_for(child_def: AgentDefinition) -> ChildAgentResolver:
    """A resolver that always returns *child_def* (single-child cascade tests)."""

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return child_def

    return resolver


async def _recorder_returning(child_run_id: UUID) -> ChildRunRecorder:
    """A recorder that records the requested child and returns ``(id, owner)``."""

    async def recorder(
        *,
        operator: Operator,
        definition: AgentDefinition,
        parent_run_id: UUID | None,
    ) -> tuple[UUID, str]:
        return child_run_id, "test-worker:0"

    return recorder


async def test_finalizer_closes_child_on_success() -> None:
    """A successful child loop calls the finalizer with its output, ``error=None``.

    The seam-level invariant behind #1087: the recorded child row must reach a
    terminal state, so the tool calls the finalizer after the child loop returns.
    The finalizer also gets the child's own token counts (#5853).
    """
    child_run_id = uuid4()
    child_usage = TokenUsage(input_tokens=7, output_tokens=3, cache_read_tokens=2)
    finalized: list[tuple[UUID, Any, str | None, TokenUsage | None]] = []
    child_def = AgentDefinition(name="closer", system_prompt="answer", request_limit=5)

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> ChildRunResult:
        return ChildRunResult(output="child answer", usage=child_usage)

    async def finalizer(
        run_id: UUID, *, output: Any, error: str | None, usage: TokenUsage | None = None
    ) -> None:
        finalized.append((run_id, output, error, usage))

    invoke_tool = make_invoke_agent_tool(
        resolver=_resolver_for(child_def),
        child_runner=child_runner,
        recorder=await _recorder_returning(child_run_id),
        finalizer=finalizer,
    )

    ctx = _FakeCtx(deps=_make_operator(), usage=RunUsage())
    out = await invoke_tool.function(ctx, agent_name="closer", inputs="go")

    assert out == {"agent": "closer", "output": "child answer"}
    assert finalized == [(child_run_id, "child answer", None, child_usage)]


async def test_finalizer_closes_child_on_failure() -> None:
    """A failed child loop calls the finalizer with the error, ``output=None``,
    *before* the tool re-raises the recoverable ``ModelRetry``."""
    child_run_id = uuid4()
    finalized: list[tuple[UUID, Any, str | None]] = []
    child_def = AgentDefinition(name="faulter", system_prompt="answer", request_limit=5)

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> ChildRunResult:
        raise AgentRunError("turn budget exhausted: boom")

    async def finalizer(
        run_id: UUID, *, output: Any, error: str | None, usage: TokenUsage | None = None
    ) -> None:
        finalized.append((run_id, output, error))

    invoke_tool = make_invoke_agent_tool(
        resolver=_resolver_for(child_def),
        child_runner=child_runner,
        recorder=await _recorder_returning(child_run_id),
        finalizer=finalizer,
    )

    ctx = _FakeCtx(deps=_make_operator(), usage=RunUsage())
    with pytest.raises(ModelRetry, match="child agent 'faulter' failed"):
        await invoke_tool.function(ctx, agent_name="faulter", inputs="go")

    assert len(finalized) == 1
    run_id, output, error = finalized[0]
    assert run_id == child_run_id
    assert output is None
    assert error is not None
    assert "turn budget exhausted" in error


async def test_finalizer_not_called_without_recorder() -> None:
    """No recorder -> no child run id -> the finalizer is never called (there is
    no row to close); the in-process path is unchanged."""
    finalized: list[UUID] = []
    child_def = AgentDefinition(name="orphan", system_prompt="answer", request_limit=5)

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> ChildRunResult:
        return ChildRunResult(output="child answer")

    async def finalizer(
        run_id: UUID, *, output: Any, error: str | None, usage: TokenUsage | None = None
    ) -> None:
        finalized.append(run_id)

    invoke_tool = make_invoke_agent_tool(
        resolver=_resolver_for(child_def),
        child_runner=child_runner,
        finalizer=finalizer,  # wired, but no recorder -> never reached
    )

    ctx = _FakeCtx(deps=_make_operator(), usage=RunUsage())
    out = await invoke_tool.function(ctx, agent_name="orphan", inputs="go")

    assert out == {"agent": "orphan", "output": "child answer"}
    assert finalized == []


async def test_unknown_child_agent_is_a_model_retry() -> None:
    """An unresolvable agent name yields a recoverable ``ModelRetry``."""

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return None  # nothing resolves (typo / disabled / cross-tenant)

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> ChildRunResult:
        raise AssertionError("child_runner must not run when resolution fails")

    invoke_tool = make_invoke_agent_tool(resolver=resolver, child_runner=child_runner)
    fn = invoke_tool.function

    ctx = _FakeCtx(deps=_make_operator(), usage=RunUsage())
    with pytest.raises(ModelRetry, match="no agent definition named"):
        await fn(ctx, agent_name="ghost", inputs="go")


async def test_depth_exceeded_carries_chain() -> None:
    """The exception names the agent chain that blew the cap (actionable)."""
    exc = AgentInvocationDepthExceeded(
        attempted_depth=3,
        max_depth=2,
        agent_name_chain=("a", "b", "c"),
    )
    assert exc.attempted_depth == 3
    assert exc.max_depth == 2
    assert exc.agent_name_chain == ("a", "b", "c")
    assert "a -> b -> c" in str(exc)


class _FakeCtx:
    """Minimal stand-in for ``RunContext`` carrying just ``deps`` + ``usage``.

    The ``invoke_agent`` tool reads only ``ctx.deps`` (the operator) and
    ``ctx.usage`` (the shared budget), so a tiny duck-typed context lets the
    unit-level tests drive the tool's function directly without a full
    framework run.
    """

    def __init__(self, *, deps: Operator, usage: RunUsage) -> None:
        self.deps = deps
        self.usage = usage


_CHILD_INPUT, _PARENT_FIRST_INPUT, _PARENT_FINAL_INPUT = 100_001, 1_000, 2_000


def _delegating_runtime() -> tuple[PydanticAgentRun, AgentDefinition]:
    """A parent run that calls one child run, each request with known usage."""
    child_def = AgentDefinition(
        name="deep-agent",
        system_prompt="You answer hard sub-questions.",
        request_limit=5,
        toolset={"meta_tools": []},
    )

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return child_def if agent_name == "deep-agent" else None

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        is_child = any(
            part.part_kind == "system-prompt" and "hard sub-questions" in part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if is_child:
            return ModelResponse(
                parts=[TextPart("child answer")],
                usage=RequestUsage(input_tokens=_CHILD_INPUT, output_tokens=10),
            )
        has_tool_return = any(
            part.part_kind == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if not has_tool_return:
            return ModelResponse(
                parts=[ToolCallPart("invoke_agent", {"agent_name": "deep-agent", "inputs": "x"})],
                usage=RequestUsage(input_tokens=_PARENT_FIRST_INPUT, output_tokens=20),
            )
        return ModelResponse(
            parts=[TextPart("parent done")],
            usage=RequestUsage(input_tokens=_PARENT_FINAL_INPUT, output_tokens=30),
        )

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(model_fn),
        child_agent_resolver=resolver,
    )
    parent_def = AgentDefinition(
        name="cheap-agent",
        system_prompt="You delegate hard sub-tasks.",
        request_limit=5,
        toolset={"meta_tools": []},
    )
    return runtime, parent_def


async def test_result_lists_the_usage_of_each_request_including_the_child_run() -> None:
    """A run's per-request usage holds the parent's requests and the child's.

    The child's messages are not in the parent's message history, while its
    tokens are in the shared total. So the result lists the child's requests
    too, and the per-request list adds up to the run's totals.
    """
    runtime, parent_def = _delegating_runtime()

    handle = runtime.start(parent_def, _make_operator(), "delegate this")
    result = await runtime.result(handle)

    assert result.output == "parent done"
    assert sorted(u.input_tokens for u in result.request_usages) == sorted(
        [_CHILD_INPUT, _PARENT_FIRST_INPUT, _PARENT_FINAL_INPUT]
    )
    assert sum(u.input_tokens for u in result.request_usages) == result.input_tokens
    assert sum(u.output_tokens for u in result.request_usages) == result.output_tokens
    assert len(result.request_usages) == result.request_count


async def test_streamed_final_event_carries_the_usage_including_the_child_run() -> None:
    """The final event of a streamed run carries the same usage as a run that is not streamed.

    Only the final event has usage. The usage is not in the event's
    ``data``, so the SSE frame does not carry it.
    """
    runtime, parent_def = _delegating_runtime()

    events = [
        event
        async for event in runtime.stream_events(
            parent_def, _make_operator(), "delegate this", uuid4()
        )
    ]

    final = events[-1]
    assert final.kind is AgentRunEventKind.FINAL
    assert final.data == {"output": "parent done"}
    assert final.usage is not None
    assert sorted(u.input_tokens for u in final.request_usages) == sorted(
        [_CHILD_INPUT, _PARENT_FIRST_INPUT, _PARENT_FINAL_INPUT]
    )
    assert final.usage.input_tokens == _CHILD_INPUT + _PARENT_FIRST_INPUT + _PARENT_FINAL_INPUT
    assert final.usage.output_tokens == 10 + 20 + 30
    assert all(e.usage is None and e.request_usages == () for e in events[:-1])


async def test_result_keeps_the_requests_of_a_child_that_ran_out_of_budget() -> None:
    """A child that spends tokens and then trips its limit still counts in the cost.

    The child's first request completes; its second trips the shared
    request limit, so the child fails. The parent gets that failure as a
    tool error and answers by itself. The child's completed request is
    in the shared token total, so it must be in the per-request list too,
    or the run's cost would leave it out.
    """
    child_def = AgentDefinition(
        name="deep-agent",
        system_prompt="You answer hard sub-questions.",
        request_limit=2,  # the parent's first request plus one child request
        toolset={"meta_tools": []},
    )

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return child_def if agent_name == "deep-agent" else None

    child_input, parent_first_input, parent_final_input = 100_001, 1_000, 2_000

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        is_child = any(
            part.part_kind == "system-prompt" and "hard sub-questions" in part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if is_child:
            # A tool call keeps the child loop going until its budget trips.
            return ModelResponse(
                parts=[ToolCallPart("noop_tool", {})],
                usage=RequestUsage(input_tokens=child_input, output_tokens=10),
            )
        answered_before = any(isinstance(message, ModelResponse) for message in messages)
        if not answered_before:
            return ModelResponse(
                parts=[ToolCallPart("invoke_agent", {"agent_name": "deep-agent", "inputs": "x"})],
                usage=RequestUsage(input_tokens=parent_first_input, output_tokens=20),
            )
        return ModelResponse(
            parts=[TextPart("parent done")],
            usage=RequestUsage(input_tokens=parent_final_input, output_tokens=30),
        )

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(model_fn),
        child_agent_resolver=resolver,
    )
    parent_def = AgentDefinition(
        name="cheap-agent",
        system_prompt="You delegate hard sub-tasks.",
        request_limit=5,
        toolset={"meta_tools": []},
    )

    handle = runtime.start(parent_def, _make_operator(), "delegate this")
    result = await runtime.result(handle)

    assert result.output == "parent done"
    assert result.request_count == 3
    assert sorted(u.input_tokens for u in result.request_usages) == sorted(
        [child_input, parent_first_input, parent_final_input]
    )
    assert sum(u.input_tokens for u in result.request_usages) == result.input_tokens
    assert sum(u.output_tokens for u in result.request_usages) == result.output_tokens
    assert len(result.request_usages) == result.request_count


async def test_parallel_child_runs_each_get_their_own_token_counts() -> None:
    """Two child runs called in the same model turn each get only their own counts.

    The parent calls ``invoke_agent`` twice in one response, so both child
    loops run at the same time and add to the same shared usage total. Each
    child's counts come from its own model responses, so they do not mix.
    """
    child_def = AgentDefinition(
        name="helper", system_prompt="You help.", request_limit=5, toolset={"meta_tools": []}
    )
    child_inputs = {"first": 111, "second": 222}

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(
            part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if part.part_kind == "user-prompt"
        )
        if prompt in child_inputs:
            return ModelResponse(
                parts=[TextPart(f"{prompt} done")],
                usage=RequestUsage(input_tokens=child_inputs[prompt], output_tokens=1),
            )
        if any(
            part.part_kind == "tool-return"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        ):
            return ModelResponse(parts=[TextPart("parent done")])
        return ModelResponse(
            parts=[
                ToolCallPart("invoke_agent", {"agent_name": "helper", "inputs": name})
                for name in child_inputs
            ]
        )

    recorded: list[UUID] = []
    finalized: dict[str, TokenUsage | None] = {}

    async def recorder(
        *, operator: Operator, definition: AgentDefinition, parent_run_id: UUID | None
    ) -> tuple[UUID, str]:
        recorded.append(uuid4())
        return recorded[-1], "test-worker:0"

    async def finalizer(
        run_id: UUID, *, output: Any, error: str | None, usage: TokenUsage | None = None
    ) -> None:
        finalized[str(output)] = usage

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(model_fn),
        child_agent_resolver=_resolver_for(child_def),
        child_run_recorder=recorder,
        child_run_finalizer=finalizer,
    )
    parent_def = AgentDefinition(
        name="parent", system_prompt="You delegate.", request_limit=5, toolset={"meta_tools": []}
    )

    result = await runtime.result(runtime.start(parent_def, _make_operator(), "go"))

    assert result.output == "parent done"
    assert len(recorded) == 2
    assert finalized == {
        "first done": TokenUsage(input_tokens=111, output_tokens=1),
        "second done": TokenUsage(input_tokens=222, output_tokens=1),
    }
