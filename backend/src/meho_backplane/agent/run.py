# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``AgentRun`` seam — a bounded in-process tool-use loop (G11.1-T1).

This module is the only place in the backplane that imports ``pydantic_ai``.
It wraps the framework's :class:`~pydantic_ai.Agent` behind a narrow Protocol
(:class:`AgentRun`) and a pair of value objects (:class:`AgentDefinition`,
:class:`AgentRunHandle`) so the rest of MEHO depends on the seam, never the
library. Swapping the loop framework (a Goal #800 design constraint) touches
this file alone.

What the seam does
==================

One :meth:`AgentRun.start` call kicks off a single bounded tool-use loop as
an in-process :class:`asyncio.Task`:

* **System prompt** — taken from the :class:`AgentDefinition`.
* **Turn budget** — :attr:`AgentDefinition.request_limit` becomes a
  ``UsageLimits(request_limit=...)``; the framework raises
  :class:`~pydantic_ai.exceptions.UsageLimitExceeded` once the loop would
  exceed it, which the seam surfaces as a failed :class:`AgentRunHandle`.
* **Structured output** — :attr:`AgentDefinition.output_type`, when set,
  is passed as the framework's ``output_type`` so the final answer is a
  validated Pydantic model rather than free text.
* **Operator injection** — the :class:`~meho_backplane.auth.operator.Operator`
  travels as the framework dependency (``deps_type`` / ``RunContext``), so
  every tool call dispatches under the right principal and the existing
  RBAC + audit machinery sees the real identity.

Tools
=====

The loop's tools are MEHO's own meta-tools, adapted from their
``(operator, arguments) -> dict`` handler shape onto the framework's tool
interface. The handler *is* the dispatch path REST + MCP use; the agent gets
no special surface (CLAUDE.md postulate 5).

Which meta-tools register is decided by T3's toolset resolver
(:func:`~meho_backplane.agent.toolset.resolve_agent_tools`): given the
definition's :attr:`AgentDefinition.toolset` spec and the run's operator, it
returns exactly the meta-tools that are in the **intersection** of (the
spec's allow-list) ∩ (the meta-tools the operator's role admits). A tool the
identity may not call is not registered. When :attr:`AgentDefinition.toolset`
is ``None`` the seam falls back to the original two hand-wired meta-tools
(:func:`_register_default_meta_tools`) — the T1 path, kept so a definition
constructed without a toolset (and the T1 test corpus) still runs.

Why a model factory, not the ``LlmClient`` seam
===============================================

The existing ``LlmClient`` Protocol
(:mod:`meho_backplane.operations.ingest`) is shaped for one-shot JSON
completion (``generate_json(system_prompt, user_prompt, ...) -> str``) — the
right shape for the spec-ingestion grouping pass, the wrong shape for a
multi-turn tool-use loop, which needs the full Messages API (tool calls,
tool results, repeated turns). Pydantic AI drives its loop through a
:class:`~pydantic_ai.models.Model`, so the seam mirrors the *pattern* of
``LlmClientFactory`` — an injected, fail-closed factory — rather than the
one-shot method. :func:`default_model_factory` builds an Anthropic model
from settings (the G11 initiative ships against Anthropic; multi-provider
routing is G11.5). Tests inject a deterministic
:class:`~pydantic_ai.models.function.FunctionModel` instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext, Tool, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import RunUsage

from meho_backplane.agent.invoke import (
    ChildAgentResolver,
    ChildRunRecorder,
    make_invoke_agent_tool,
)
from meho_backplane.agent.toolset import resolve_agent_tools
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.meta_tools import call_operation, list_operation_groups

if TYPE_CHECKING:
    from pydantic_ai.models import Model

__all__ = [
    "AgentDefinition",
    "AgentRun",
    "AgentRunError",
    "AgentRunHandle",
    "AgentRunResult",
    "AgentRunStatus",
    "ModelFactory",
    "PydanticAgentRun",
    "default_model_factory",
]

_log = structlog.get_logger(__name__)


#: A factory that builds the framework :class:`~pydantic_ai.models.Model`
#: the loop runs against. A factory (not a singleton instance) so the seam
#: can lazy-build the model after settings change and so tests can inject a
#: deterministic ``FunctionModel`` per run — the same indirection the
#: spec-ingestion ``LlmClientFactory`` uses.
ModelFactory = Callable[[], "Model"]


class AgentRunError(RuntimeError):
    """Raised when an agent run cannot start or its result is unavailable.

    A domain exception so callers (the T4 invocation surface, the T6 run
    record) can distinguish a seam-level failure from an
    :class:`~meho_backplane.connectors.schemas.OperationResult` error
    inside the loop. The framework's own loop failures (the turn budget
    tripping, a tool raising) are captured on the
    :class:`AgentRunHandle` as a :attr:`AgentRunStatus.FAILED` status with
    the error message attached — they do not propagate as this exception.
    """


class AgentRunStatus(StrEnum):
    """Lifecycle state of one :class:`AgentRunHandle`.

    A closed enum (not free strings) so the T4 poll surface and the T6 run
    record can switch exhaustively. ``RUNNING`` is the only non-terminal
    state; ``SUCCEEDED`` and ``FAILED`` are terminal.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentDefinition(BaseModel):
    """The static shape of one agent run — system prompt, budget, output.

    Frozen so a definition handed to :meth:`AgentRun.start` cannot mutate
    mid-flight (the same posture as :class:`Operator`). For T1 the
    definition is constructed in-process by the caller; persistence +
    admin CRUD is T2 (#809), which will materialise rows into this shape.

    ``output_type`` is the optional structured-output schema. When set, the
    framework constrains the loop's final answer to a validated instance of
    the given Pydantic model; when ``None``, the run returns the model's
    free-text answer. It is excluded from equality/serialisation
    comparisons because a class object is not JSON-serialisable — it is a
    runtime wiring detail, not persisted state.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    #: Per-run turn budget. Becomes ``UsageLimits(request_limit=...)``; the
    #: loop is stopped once it would exceed this many model requests.
    request_limit: int = Field(default=8, gt=0)
    #: Optional model-id override (``"anthropic:claude-..."``). When unset,
    #: the seam's :class:`ModelFactory` decides (settings default).
    model: str | None = None
    #: Optional structured-output schema (a Pydantic ``BaseModel`` subclass).
    output_type: type[BaseModel] | None = Field(default=None, exclude=True)
    #: Optional toolset spec — the allowed meta-tools / connectors, resolved
    #: against the run's identity by
    #: :func:`~meho_backplane.agent.toolset.resolve_agent_tools` (T3 #810).
    #: ``None`` selects the T1 default surface (the two hand-wired meta-tools
    #: in :func:`_register_default_meta_tools`); a dict (even ``{}``) routes
    #: through the resolver. See :mod:`meho_backplane.agent.toolset` for the
    #: shape. Persisted definitions (T2 #809) materialise their stored
    #: ``toolset`` JSON into this field.
    toolset: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """The terminal outcome of a finished run.

    ``output`` is the loop's final answer — a validated
    :attr:`AgentDefinition.output_type` instance when structured output was
    requested, otherwise the model's free-text string. ``request_count`` /
    ``tool_call_count`` are lifted from the framework's usage accounting so
    the T6 run record and cost attribution (G11.5) have the turn + tool
    totals without re-deriving them from the message log.
    """

    output: Any
    request_count: int
    tool_call_count: int


@dataclass(slots=True)
class AgentRunHandle:
    """A reference to one in-flight or finished run.

    Returned by :meth:`AgentRun.start` and passed back to
    :meth:`AgentRun.poll` / :meth:`AgentRun.result` / :meth:`AgentRun.stream`.
    For T1 the handle wraps the backing :class:`asyncio.Task` directly; T6
    (#813) replaces the in-memory task with a durable ``agent_run`` row and
    a session-id lineage key, but the handle's public shape — ``run_id`` +
    the task-derived status + terminal accessors — stays the contract.

    The :class:`asyncio.Task` is the single source of truth for lifecycle
    state, so :meth:`AgentRun.poll` derives the status from it rather than
    from a separately-maintained field that could drift. The task is private
    (``_task``) so callers go through :meth:`AgentRun.poll` /
    :meth:`AgentRun.result` / :meth:`AgentRun.stream` rather than awaiting
    it directly.
    """

    run_id: UUID
    _task: asyncio.Task[AgentRunResult]


@runtime_checkable
class AgentRun(Protocol):
    """The narrow seam every consumer depends on instead of the framework.

    Four methods mirror the G11.1-T1 contract: :meth:`start` kicks off a
    bounded loop and returns a handle; :meth:`poll` reports lifecycle state
    without blocking; :meth:`result` blocks until the run finishes and
    returns its :class:`AgentRunResult`; :meth:`stream` yields the loop's
    events as they happen. A structural Protocol (not an ABC) so the T4
    surface can hold the interface while a test or an alternate framework
    adapter supplies the implementation.
    """

    def start(
        self,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
    ) -> AgentRunHandle:
        """Begin a bounded run; return immediately with a live handle."""
        ...

    def poll(self, handle: AgentRunHandle) -> AgentRunStatus:
        """Return the run's current lifecycle state without blocking."""
        ...

    async def result(self, handle: AgentRunHandle) -> AgentRunResult:
        """Block until the run finishes; return its terminal result.

        Raises :class:`AgentRunError` if the run failed.
        """
        ...

    def stream(self, handle: AgentRunHandle) -> AsyncIterator[str]:
        """Yield the loop's textual output events as they are produced."""
        ...


def default_model_factory() -> Model:
    """Build the Anthropic model the loop runs against, from settings.

    Fail-closed: a deployment with no ``ANTHROPIC_API_KEY`` configured
    raises :class:`AgentRunError` here rather than surfacing an opaque
    framework error mid-loop — mirroring
    :func:`~meho_backplane.operations.ingest.default_llm_client_factory`'s
    posture. Multi-provider routing (Bedrock, on-prem OpenAI-compatible,
    VCF Private AI Foundation) is G11.5; this Task ships against Anthropic
    only, so the factory is intentionally single-provider.
    """
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    from meho_backplane.settings import get_settings

    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise AgentRunError(
            "no ANTHROPIC_API_KEY configured for the agent runtime; "
            "set it to run against Anthropic. Multi-provider routing is G11.5.",
        )
    provider = AnthropicProvider(anthropic_client=AsyncAnthropic(api_key=api_key))
    return AnthropicModel(settings.agent_default_model, provider=provider)


def _register_default_meta_tools(agent: Agent[Operator, Any]) -> None:
    """Wire the T1 default two-meta-tool surface onto *agent*.

    Adapts the existing ``(operator, arguments) -> dict`` handler shape
    onto the framework's ``RunContext``-first tool signature: the operator
    comes from ``ctx.deps`` (so RBAC + audit see the right principal), and
    the tool's typed parameters are repacked into the ``arguments`` dict the
    handler expects. The handler docstrings double as the model-facing tool
    descriptions, so the agent picks tools from the same prose operators
    read.

    This is the fallback path used when an :class:`AgentDefinition` carries
    no ``toolset`` spec (``toolset is None``): exactly the two meta-tools T1
    hand-wired (discovery + execution), enough to run a definition that never
    asked for a specific surface. A definition *with* a toolset routes
    through :func:`~meho_backplane.agent.toolset.resolve_agent_tools`
    instead, which enforces the spec ∩ identity-permissions intersection.
    """

    @agent.tool
    async def call_operation_tool(
        ctx: RunContext[Operator],
        connector_id: str,
        op_id: str,
        params: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke a MEHO operation through the governed dispatch path.

        Use this to *execute* an operation once you know its
        ``connector_id`` and ``op_id``. ``params`` carries the operation's
        arguments; ``target`` is an optional ``{"name": "<slug>"}`` for
        operations that act on a specific managed target.
        """
        return await call_operation(
            ctx.deps,
            {
                "connector_id": connector_id,
                "op_id": op_id,
                "params": params or {},
                "target": target,
            },
        )

    @agent.tool
    async def list_operation_groups_tool(
        ctx: RunContext[Operator],
        connector_id: str,
    ) -> dict[str, Any]:
        """List a connector's operation groups to scope an operation search.

        Use this first to discover which group of operations is relevant
        before searching for a specific operation to call.
        """
        return await list_operation_groups(ctx.deps, {"connector_id": connector_id})


@dataclass
class PydanticAgentRun:
    """The Pydantic AI-backed :class:`AgentRun` implementation.

    Holds an injected :class:`ModelFactory` (defaulting to
    :func:`default_model_factory`). Each :meth:`start` builds a fresh
    framework :class:`~pydantic_ai.Agent` from the
    :class:`AgentDefinition`, wires the meta-tools, and launches the bounded
    loop as an :class:`asyncio.Task`. State lives on the returned
    :class:`AgentRunHandle`; the implementation itself is stateless beyond
    the factory, so a single instance is safe to share across runs.
    """

    model_factory: ModelFactory = field(default=default_model_factory)
    #: Optional child-agent resolver (G11.1-T5 #812). When set, every built
    #: agent additionally carries the ``invoke_agent`` meta-tool, so a running
    #: agent can invoke another definition in its tenant as a depth-capped,
    #: budget-aware, audited child run. ``None`` (the default) means
    #: composition is off -- the T1/T3 surface is unchanged. Injected at the
    #: edge by the T4 invocation surface, which owns the
    #: :class:`~meho_backplane.agent.invoke.ChildAgentResolver` (the
    #: tenant-scoped definition lookup).
    child_agent_resolver: ChildAgentResolver | None = None
    #: Optional persistence of the child ``agent_run`` lineage row (G11.1-T5 /
    #: T6 #813). Passed straight to
    #: :func:`~meho_backplane.agent.invoke.make_invoke_agent_tool`. ``None``
    #: keeps the in-process bounds without writing the lineage row.
    child_run_recorder: ChildRunRecorder | None = None

    def _build_agent(
        self,
        definition: AgentDefinition,
        operator: Operator,
    ) -> Agent[Operator, Any]:
        """Construct the framework agent for *definition* under *operator*.

        When *definition* carries a ``toolset`` spec, the tools registered
        are the intersection of (spec) ∩ (operator's permissions), resolved
        by :func:`~meho_backplane.agent.toolset.resolve_agent_tools` and
        passed to the framework via the ``tools=`` constructor argument. A
        definition with no toolset (``toolset is None``) falls back to the T1
        default two-meta-tool surface.

        When this runtime carries a :attr:`child_agent_resolver` (G11.1-T5),
        the ``invoke_agent`` composition tool is appended to whichever surface
        the definition selected, so a running agent can invoke another
        definition. The tool is bound to :meth:`run_child` as its
        :class:`~meho_backplane.agent.invoke.ChildRunner`, so the child loop
        runs with the parent's shared usage budget.
        """
        model = self.model_factory()
        invoke_tool = self._maybe_build_invoke_tool()
        if definition.toolset is not None:
            tools = resolve_agent_tools(definition.toolset, operator)
            if invoke_tool is not None:
                tools = [*tools, invoke_tool]
            agent: Agent[Operator, Any] = Agent(
                model,
                deps_type=Operator,
                system_prompt=definition.system_prompt,
                output_type=definition.output_type if definition.output_type is not None else str,
                tools=tools,
            )
            return agent
        agent = Agent(
            model,
            deps_type=Operator,
            system_prompt=definition.system_prompt,
            output_type=definition.output_type if definition.output_type is not None else str,
            tools=[invoke_tool] if invoke_tool is not None else [],
        )
        _register_default_meta_tools(agent)
        return agent

    def _maybe_build_invoke_tool(self) -> Tool[Operator] | None:
        """Build the ``invoke_agent`` tool when composition is wired, else ``None``.

        Composition is on exactly when a :attr:`child_agent_resolver` is
        injected. The tool's :class:`~meho_backplane.agent.invoke.ChildRunner`
        is :meth:`run_child` (bound to this instance), so the child loop shares
        the parent's usage budget and the framework stays confined here.
        """
        if self.child_agent_resolver is None:
            return None
        return make_invoke_agent_tool(
            resolver=self.child_agent_resolver,
            child_runner=self.run_child,
            recorder=self.child_run_recorder,
        )

    async def _run_loop(
        self,
        agent: Agent[Operator, Any],
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
        run_id: UUID,
    ) -> AgentRunResult:
        """Drive one bounded loop and return its result.

        The coroutine's return value / raised exception *is* the run's
        terminal state — the :class:`asyncio.Task` wrapping it is the single
        source of truth read by :meth:`poll` / :meth:`result`. A tripped
        turn budget surfaces as :class:`AgentRunError` so the seam's failure
        type is uniform regardless of which framework exception fired.
        """
        limits = UsageLimits(request_limit=definition.request_limit)
        try:
            run_result = await agent.run(inputs, deps=operator, usage_limits=limits)
        except UsageLimitExceeded as exc:
            _log.warning(
                "agent_run_budget_exhausted",
                run_id=str(run_id),
                agent=definition.name,
                request_limit=definition.request_limit,
                operator_sub=operator.sub,
            )
            raise AgentRunError(f"turn budget exhausted: {exc}") from exc
        usage = run_result.usage
        result = AgentRunResult(
            output=run_result.output,
            request_count=usage.requests,
            tool_call_count=usage.tool_calls,
        )
        _log.info(
            "agent_run_succeeded",
            run_id=str(run_id),
            agent=definition.name,
            request_count=result.request_count,
            tool_call_count=result.tool_call_count,
            operator_sub=operator.sub,
        )
        return result

    async def run_child(
        self,
        *,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
        usage: RunUsage,
    ) -> Any:
        """Drive one child loop budget-aware, sharing the parent's usage.

        The :class:`~meho_backplane.agent.invoke.ChildRunner` the
        ``invoke_agent`` tool (G11.1-T5 #812) calls. Builds the child agent
        from *definition* under the parent's *operator* (so the child's tools
        are resolved + RBAC-filtered exactly like a top-level run), then drives
        the loop with ``usage=usage`` -- the parent's
        :class:`~pydantic_ai.usage.RunUsage` accumulator -- so the child's turns
        count against the parent's running total. The shared
        ``UsageLimits(request_limit=...)`` (the child's own
        :attr:`AgentDefinition.request_limit`) is enforced against that shared
        total, so a cascade that would exceed the budget trips
        :class:`~pydantic_ai.exceptions.UsageLimitExceeded` -- surfaced here as
        :class:`AgentRunError`, the seam's uniform failure type.

        Unlike :meth:`start`, this awaits the child loop inline (it runs inside
        the parent loop's ``invoke_agent`` tool call, on the parent's event
        loop) and returns the child's output directly rather than a handle: the
        parent tool needs the child's answer to continue, and the child's
        lifecycle is bounded by the parent tool call.
        """
        child = self._build_agent(definition, operator)
        limits = UsageLimits(request_limit=definition.request_limit)
        try:
            run_result = await child.run(
                inputs,
                deps=operator,
                usage=usage,
                usage_limits=limits,
            )
        except UsageLimitExceeded as exc:
            _log.warning(
                "agent_child_run_budget_exhausted",
                agent=definition.name,
                request_limit=definition.request_limit,
                shared_requests=usage.requests,
                operator_sub=operator.sub,
            )
            raise AgentRunError(f"turn budget exhausted: {exc}") from exc
        return run_result.output

    def start(
        self,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
    ) -> AgentRunHandle:
        """Begin a bounded run; return immediately with a live handle.

        The loop runs as an :class:`asyncio.Task` on the current event
        loop, so :meth:`start` is non-blocking — callers either
        :meth:`poll` for status, await :meth:`result`, or consume
        :meth:`stream`. A :class:`RuntimeError` from
        :func:`asyncio.get_running_loop` (no running loop) surfaces as
        :class:`AgentRunError`: the seam is async-only by design (the T4
        sync surface bridges via ``asyncio.run`` at the edge).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            raise AgentRunError(
                "AgentRun.start requires a running event loop; "
                "the sync invocation surface (T4) bridges at the edge",
            ) from exc
        agent = self._build_agent(definition, operator)
        run_id = uuid4()
        task = asyncio.create_task(
            self._run_loop(agent, definition, operator, inputs, run_id),
            name=f"agent-run-{run_id}",
        )
        _log.info(
            "agent_run_started",
            run_id=str(run_id),
            agent=definition.name,
            request_limit=definition.request_limit,
            structured_output=definition.output_type is not None,
            operator_sub=operator.sub,
        )
        return AgentRunHandle(run_id=run_id, _task=task)

    def poll(self, handle: AgentRunHandle) -> AgentRunStatus:
        """Return the run's current lifecycle state without blocking.

        Derived from the backing task: still pending → ``RUNNING``;
        finished cleanly → ``SUCCEEDED``; finished with an exception (or
        cancelled) → ``FAILED``. The task is the single source of truth, so
        there is no status field to drift out of sync with it.
        """
        if not handle._task.done():
            return AgentRunStatus.RUNNING
        if handle._task.cancelled() or handle._task.exception() is not None:
            return AgentRunStatus.FAILED
        return AgentRunStatus.SUCCEEDED

    async def result(self, handle: AgentRunHandle) -> AgentRunResult:
        """Block until the run finishes; return its terminal result.

        Raises :class:`AgentRunError` if the loop failed (turn budget
        exhausted, a tool raised, or the model errored). The
        already-:class:`AgentRunError` case from :meth:`_run_loop` (the
        budget path) propagates unchanged; any other exception is wrapped so
        callers only ever catch :class:`AgentRunError`.
        """
        try:
            return await handle._task
        except AgentRunError:
            raise
        except Exception as exc:
            _log.warning(
                "agent_run_failed",
                run_id=str(handle.run_id),
                error=str(exc),
            )
            raise AgentRunError(f"agent run {handle.run_id} failed: {exc}") from exc

    async def stream(self, handle: AgentRunHandle) -> AsyncIterator[str]:
        """Yield the loop's terminal textual output.

        T1 ships a minimal stream — it awaits the run and yields the final
        answer as a single chunk — so the seam's four-method surface is
        complete and the T4 SSE surface has a contract to build against.
        Token-by-token streaming (the framework's ``run_stream`` /
        ``iter`` node events) is wired in T4 (#811) where the SSE transport
        lives. Yielding only on success keeps the failure path on
        :meth:`result`'s :class:`AgentRunError`.
        """
        result = await self.result(handle)
        yield str(result.output)
