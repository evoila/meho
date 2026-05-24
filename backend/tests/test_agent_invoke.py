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
from pydantic_ai.usage import RunUsage

from meho_backplane.agent import (
    AGENT_INVOKE_DEPTH_TOP_LEVEL,
    AgentDefinition,
    AgentInvocationDepthExceeded,
    AgentRunError,
    agent_invoke_depth_var,
    current_agent_run_id_var,
    make_invoke_agent_tool,
)
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.operator import Operator, TenantRole
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
    ) -> Any:
        nonlocal runner_calls
        runner_calls += 1
        return "should-not-run"

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
    ) -> Any:
        # During the child loop, the current-run contextvar is the child's id.
        seen_run_id_in_child.append(current_agent_run_id_var.get())
        return "child answer"

    async def recorder(
        *,
        operator: Operator,
        definition: AgentDefinition,
        parent_run_id: UUID | None,
    ) -> UUID:
        recorded.append(
            {
                "tenant_id": operator.tenant_id,
                "definition_name": definition.name,
                "parent_run_id": parent_run_id,
            }
        )
        return child_run_id

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


async def test_unknown_child_agent_is_a_model_retry() -> None:
    """An unresolvable agent name yields a recoverable ``ModelRetry``."""

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return None  # nothing resolves (typo / disabled / cross-tenant)

    async def child_runner(
        *, definition: AgentDefinition, operator: Operator, inputs: str, usage: RunUsage
    ) -> Any:
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
