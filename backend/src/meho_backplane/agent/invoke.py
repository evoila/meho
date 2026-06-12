# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-invokes-agent composition (G11.1-T5 / #812).

This module gives a running agent's loop one extra meta-tool, ``invoke_agent``,
that runs **another** agent definition in the same tenant as a child run. From
MEHO's view a child agent run is just another governed call: the child resolves
through the same identity, the same RBAC-filtered toolset
(:func:`~meho_backplane.agent.toolset.resolve_agent_tools`), the same dispatch +
audit machinery. There is no "tier" concept here -- the *consumer's* harness may
escalate a cheap-tier agent to a deep-tier agent, but MEHO only sees one agent
run invoking another.

Two independent bounds keep a cascade from escaping
====================================================

A naive agent-invokes-agent surface is the textbook runaway-cost foot-gun
(ai_engineering best practices, "no LLM calls from inside a tool ... if a tool
needs reasoning, it's a sub-agent -- *name it as such*"): a definition that
invokes itself, directly or transitively, spawns an unbounded chain of LLM runs.
This module bounds it on two axes, and a cascade terminates on whichever it
hits first:

* **Depth** -- the *height* of the invocation tree. A per-task contextvar
  (:data:`agent_invoke_depth_var`) tracks how many ``invoke_agent`` frames the
  current :mod:`asyncio` task is nested inside. :func:`make_invoke_agent_tool`'s
  tool pre-increments + checks it against :attr:`Settings.agent_invoke_max_depth`
  *before* the child run starts, so an over-depth invocation never spends. This
  mirrors the composite-recursion cap exactly
  (:data:`~meho_backplane.operations.composite.composite_depth_var` +
  :attr:`Settings.composite_max_depth`).
* **Budget** -- the *total turn count* across the whole cascade. The child run
  is driven with ``usage=ctx.usage`` (Pydantic AI's budget-propagation knob),
  so the parent's :class:`~pydantic_ai.usage.RunUsage` accumulator is shared
  with the child. The shared ``UsageLimits(request_limit=...)`` is enforced
  against the running total, so a deep-but-narrow cascade and a shallow-but-wide
  one both trip the same budget. The framework raises
  :class:`~pydantic_ai.exceptions.UsageLimitExceeded`, which the seam surfaces
  as a failed run (:class:`~meho_backplane.agent.run.AgentRunError`).

Why a structured retry, not a crash
===================================

An over-depth invocation raises :class:`~pydantic_ai.ModelRetry`, the same
agent-reasonable error shape the connector-allow-list check in
:mod:`meho_backplane.agent.toolset` uses. The model receives a tool-level retry
prompt ("you've reached the maximum invocation depth; answer directly or stop")
it can reason about -- it is not a tool-execution crash that fails the whole run.
The depth ceiling is a deterministic termination condition the *model never
controls* (ai_engineering best practices, "termination is deterministic ...
never rely on the model to stop").

Lineage -- the cascade tree is reconstructable
==============================================

The child run is recorded as a child of the parent in two parallel lineages,
both threaded by this module so the cascade tree is walkable after the fact:

* **Run lineage** -- the child ``agent_run`` row's
  :attr:`~meho_backplane.db.models.AgentRun.parent_run_id` points at the parent
  run's id, and its
  :attr:`~meho_backplane.db.models.AgentRun.trigger` is
  :attr:`~meho_backplane.db.models.AgentRunTrigger.AGENT_INVOKED`. Recording the
  child row is delegated to an injected :class:`ChildRunRecorder` callback (the
  T4 #811 invocation surface / T6 #813 lifecycle service own the DB session);
  when no recorder is wired (the pure in-process T1 path) the depth + budget
  bounds still apply, the lineage row is simply not persisted.
* **Session lineage** -- :data:`current_agent_run_id_var` carries the *current*
  run's id for the duration of a child invocation, so a nested ``invoke_agent``
  reads it as the next child's ``parent_run_id`` and every per-tool-call audit
  row the child writes can be correlated to its run. Bound + reset with a token
  so siblings see clean state even if the child raises.

Why this is a tool factory, not a standalone service function
=============================================================

The acceptance criterion is that a *running agent* can invoke another -- so the
mechanism has to be reachable from inside the loop, i.e. a registered tool. The
factory shape (rather than a module-level tool) lets the caller inject the
``child_runner`` (which owns the framework ``Agent`` construction + the
``usage=ctx.usage`` call, keeping ``pydantic_ai`` loop-driving confined to
:mod:`meho_backplane.agent.run`) and the optional ``recorder`` without this
module reaching into either.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic_ai import ModelRetry, RunContext, Tool

from meho_backplane.auth.operator import Operator
from meho_backplane.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from pydantic_ai.usage import RunUsage

    # Imported under TYPE_CHECKING to break the import cycle with
    # ``meho_backplane.agent.run`` (which imports this module at module scope
    # to wire the ``invoke_agent`` tool into its agent builder). The runtime
    # ``except AgentRunError`` in the tool body does a local import instead.
    from meho_backplane.agent.run import AgentDefinition

__all__ = [
    "AGENT_INVOKE_DEPTH_TOP_LEVEL",
    "AgentInvocationDepthExceeded",
    "ChildAgentResolver",
    "ChildRunFinalizer",
    "ChildRunRecorder",
    "ChildRunner",
    "agent_invoke_depth_var",
    "current_agent_run_id_var",
    "make_invoke_agent_tool",
]

_log = structlog.get_logger(__name__)


#: Sentinel depth for a top-level run -- nothing has entered an
#: ``invoke_agent`` frame yet. The first ``invoke_agent`` call advances
#: depth to ``1``; an agent invoked *by* that child (invoke-inside-invoke)
#: advances to ``2``; and so on, until :attr:`Settings.agent_invoke_max_depth`.
AGENT_INVOKE_DEPTH_TOP_LEVEL: int = 0


#: ContextVar tracking how deep the current :mod:`asyncio` task is into an
#: agent-invokes-agent cascade. Top-level runs see the default (``0``). The
#: ``invoke_agent`` tool pre-increments this before each child run and compares
#: the result against :attr:`Settings.agent_invoke_max_depth`, raising
#: :class:`AgentInvocationDepthExceeded` *before* the child loop starts when the
#: next level would breach the cap. Per the asyncio contextvar contract the
#: value is per-task, not per-process -- two concurrent cascades see independent
#: counters. Mirrors
#: :data:`~meho_backplane.operations.composite.composite_depth_var`.
agent_invoke_depth_var: ContextVar[int] = ContextVar(
    "agent_invoke_depth",
    default=AGENT_INVOKE_DEPTH_TOP_LEVEL,
)


#: ContextVar carrying the *current* agent run's id for the duration of a child
#: invocation. The ``invoke_agent`` tool reads it as the parent_run_id when it
#: records a child run, then binds it to the child's id around the child loop so
#: a nested ``invoke_agent`` sees the right parent. ``None`` at the top level (a
#: run not yet associated with a durable ``agent_run`` row -- the pure
#: in-process T1 path). The T4/T6 surface sets it to the run's id at start.
current_agent_run_id_var: ContextVar[uuid.UUID | None] = ContextVar(
    "current_agent_run_id",
    default=None,
)


class AgentInvocationDepthExceeded(RuntimeError):  # noqa: N818 -- parallels CompositeRecursionLimitExceeded
    """Raised when an ``invoke_agent`` call would breach the depth cap.

    Carries the attempted depth, the configured cap, and the chain of agent
    names that led to the violation, so the failure is actionable when it
    surfaces (the ``invoke_agent`` tool catches it and re-raises as a
    :class:`~pydantic_ai.ModelRetry` the model can reason about). The attempted
    child run never starts -- no LLM spend, no child ``agent_run`` row.
    """

    def __init__(
        self,
        *,
        attempted_depth: int,
        max_depth: int,
        agent_name_chain: tuple[str, ...],
    ) -> None:
        self.attempted_depth = attempted_depth
        self.max_depth = max_depth
        self.agent_name_chain = agent_name_chain
        chain_repr = " -> ".join(agent_name_chain) if agent_name_chain else "(empty)"
        super().__init__(
            f"agent invocation depth limit exceeded: attempted depth "
            f"{attempted_depth} > max_depth {max_depth}; "
            f"agent chain: {chain_repr}"
        )


#: ContextVar accumulating the chain of agent names the current cascade has
#: descended through, so :class:`AgentInvocationDepthExceeded` can name *which*
#: chain blew the cap. Bound + reset around each child invocation, same token
#: discipline as :data:`agent_invoke_depth_var`.
_agent_name_chain_var: ContextVar[tuple[str, ...]] = ContextVar(
    "agent_invoke_name_chain",
    default=(),
)


class ChildAgentResolver(Protocol):
    """Resolve a child agent name to a runnable definition in the same tenant.

    The ``invoke_agent`` tool calls this with the *parent* operator and the
    requested agent name; the implementation looks the definition up scoped to
    ``operator.tenant_id`` (so cross-tenant invocation is structurally
    impossible -- a name in another tenant simply does not resolve) and returns
    a :class:`~meho_backplane.agent.run.AgentDefinition` ready to run, or
    ``None`` when no such definition exists / is enabled for the tenant.
    """

    async def __call__(
        self,
        operator: Operator,
        agent_name: str,
    ) -> AgentDefinition | None: ...


class ChildRunner(Protocol):
    """Drive one child agent loop, threading the parent's usage budget.

    Implemented in :mod:`meho_backplane.agent.run` (which owns the framework
    ``Agent`` construction + the ``usage=ctx.usage`` call), so ``pydantic_ai``
    loop-driving stays confined there. Returns the child's free-text / structured
    output. Raises :class:`~meho_backplane.agent.run.AgentRunError` when the
    child loop fails -- including when the shared budget (``usage``) trips the
    :class:`~pydantic_ai.exceptions.UsageLimitExceeded` the framework raises.
    """

    async def __call__(
        self,
        *,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
        usage: RunUsage,
    ) -> Any: ...


class ChildRunRecorder(Protocol):
    """Persist a child ``agent_run`` row linked to its parent; return ``(id, lease_owner)``.

    Optional -- injected by the T4 #811 invocation surface / T6 #813 lifecycle
    service, which own the DB session. Called *after* the depth check passes and
    *before* the child loop starts, so the child run is inspectable while it
    runs. The returned id becomes the child's ``current_agent_run_id_var`` for
    the duration of its loop (so a grand-child invocation links to it). When no
    recorder is wired, the in-process bounds still hold; the lineage row is just
    not written.

    The recorder stamps a lease on the child row at creation (#1501) and
    returns its ``lease_owner`` alongside the id so the ``invoke_agent`` tool
    can heartbeat the child for the duration of its loop -- a child that
    outlives its lease TTL, or whose worker dies mid-flight, is then reclaimed
    by the reaper instead of staying stuck ``running``, the same contract the
    top-level run path enforces.
    """

    async def __call__(
        self,
        *,
        operator: Operator,
        definition: AgentDefinition,
        parent_run_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID, str]: ...


class ChildRunFinalizer(Protocol):
    """Record a recorded child ``agent_run`` row's terminal state.

    The companion to :class:`ChildRunRecorder`: the recorder creates the child
    row and transitions it to ``running``; this hook closes the lifecycle when
    the child loop returns or fails. Optional -- injected by the same T4 #811 /
    T6 #813 surface that owns the DB session. Called only when a child run was
    recorded (so there is a row to finalize), *after* the child loop: on
    success with the child's loop ``output`` (``error=None``), on
    :class:`~meho_backplane.agent.run.AgentRunError` with the ``error``
    (``output=None``). The ``output`` is the child loop's raw result; the
    implementation projects it onto the run row's JSON column (the same
    ``_project_output`` contract the parent run uses), so this seam stays free
    of the row's storage shape. Mirrors :meth:`AgentInvoker._finalize_run`: load
    the row fresh, apply ``succeed_run`` / ``fail_run``, and swallow
    :class:`~meho_backplane.operations.agent_run.IllegalTransitionError` when a
    terminal state already landed (e.g. the row was cancelled mid-flight). When
    no finalizer is wired the child row is simply not finalized -- the in-process
    bounds and the recorder's lineage row are unaffected.
    """

    async def __call__(
        self,
        run_id: uuid.UUID,
        *,
        output: Any,
        error: str | None,
    ) -> None: ...


async def _stop_child_heartbeat(task: asyncio.Task[None] | None) -> None:
    """Cancel a child run's heartbeat sidecar and await its unwind.

    A no-op when *task* is ``None`` (no recorder wired, or the heartbeat was
    already stopped on this code path). Swallows the expected
    :class:`asyncio.CancelledError` so tearing the sidecar down never surfaces
    as a stray task exception, mirroring the disposal shape the lifespan-owned
    sweepers use (#1501).
    """
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _check_invoke_depth(child_agent_name: str) -> int:
    """Read + check the per-task invocation depth; return the next depth.

    Pre-increments the would-be depth from :data:`agent_invoke_depth_var` and
    compares against :attr:`Settings.agent_invoke_max_depth`. Raises
    :class:`AgentInvocationDepthExceeded` when the next call would breach the
    cap, with the agent-name chain that led there. Returns the validated next
    depth so the caller can pass it to :func:`agent_invoke_depth_var.set`.
    """
    current_depth = agent_invoke_depth_var.get()
    attempted_depth = current_depth + 1
    max_depth = get_settings().agent_invoke_max_depth
    if attempted_depth > max_depth:
        current_chain = _agent_name_chain_var.get()
        raise AgentInvocationDepthExceeded(
            attempted_depth=attempted_depth,
            max_depth=max_depth,
            agent_name_chain=(*current_chain, child_agent_name),
        )
    return attempted_depth


def make_invoke_agent_tool(
    *,
    resolver: ChildAgentResolver,
    child_runner: ChildRunner,
    recorder: ChildRunRecorder | None = None,
    finalizer: ChildRunFinalizer | None = None,
) -> Tool[Operator]:
    """Build the ``invoke_agent`` meta-tool for a running agent's loop.

    The returned :class:`pydantic_ai.Tool` lets the loop invoke another agent
    definition in the same tenant as a depth-capped, budget-aware, audited child
    run. Register it alongside the discovery + execution meta-tools (it is the
    composition surface on top of them).

    Args:
        resolver: Resolves the requested child agent name to a runnable
            :class:`~meho_backplane.agent.run.AgentDefinition`, scoped to the
            parent operator's tenant.
        child_runner: Drives the child loop with the parent's shared usage
            budget (owns the ``pydantic_ai`` ``Agent`` + ``usage=`` call).
        recorder: Optional persistence of the child ``agent_run`` lineage row.
            ``None`` for the pure in-process path (bounds still apply).
        finalizer: Optional terminal-state recorder for a recorded child row.
            Called after the child loop only when ``recorder`` returned a run
            id: on success with the child's output, on
            :class:`~meho_backplane.agent.run.AgentRunError` with the error.
            ``None`` leaves the child row un-finalized (the recorder's lineage
            row and the in-process bounds are unaffected).

    Returns:
        The ``invoke_agent`` :class:`pydantic_ai.Tool`.
    """

    # Local import (function scope) breaks the module-level cycle with
    # ``meho_backplane.agent.run`` -- resolved once at factory-build time, not
    # per child invocation.
    from meho_backplane.agent.run import AgentRunError

    async def _invoke_agent(  # code-quality-allow: function-size — one cohesive control flow
        ctx: RunContext[Operator],
        agent_name: str,
        inputs: str,
    ) -> dict[str, Any]:
        # The tool body is a single top-to-bottom path the two bounds + the
        # lineage/finalize wiring must read in order (depth check -> resolve ->
        # record -> run-with-shared-budget -> finalize -> reset contextvars).
        # Splitting it fragments that ordering for no readability gain.
        operator = ctx.deps

        # Bound 1 -- depth. Checked BEFORE any resolution / spend, so an
        # over-depth invocation never starts a child loop. Surfaced to the
        # model as a ModelRetry it can recover from (answer directly / stop),
        # never the model's call to make.
        try:
            attempted_depth = _check_invoke_depth(agent_name)
        except AgentInvocationDepthExceeded as exc:
            _log.warning(
                "agent_invoke_depth_exceeded",
                agent_name=agent_name,
                attempted_depth=exc.attempted_depth,
                max_depth=exc.max_depth,
                operator_sub=operator.sub,
                tenant_id=str(operator.tenant_id),
            )
            raise ModelRetry(
                f"cannot invoke agent {agent_name!r}: maximum invocation depth "
                f"{exc.max_depth} reached (chain: "
                f"{' -> '.join(exc.agent_name_chain)}). Answer directly or stop."
            ) from exc

        # Resolve the child in the parent's tenant. A name that does not
        # resolve (typo, disabled, or another tenant's definition) is a
        # ModelRetry -- the model picks a real agent or stops, it does not
        # crash the run.
        definition = await resolver(operator, agent_name)
        if definition is None:
            raise ModelRetry(
                f"no agent definition named {agent_name!r} is available in this "
                f"tenant. Pick an existing agent or answer directly."
            )

        parent_run_id = current_agent_run_id_var.get()
        child_run_id: uuid.UUID | None = None
        child_lease_owner: str | None = None
        if recorder is not None:
            child_run_id, child_lease_owner = await recorder(
                operator=operator,
                definition=definition,
                parent_run_id=parent_run_id,
            )

        # Bind depth + name-chain + the child run id for the duration of the
        # child loop, so a nested invoke_agent sees the right depth and parent.
        # Tokens make the resets exception-safe.
        depth_token = agent_invoke_depth_var.set(attempted_depth)
        chain_token = _agent_name_chain_var.set(
            (*_agent_name_chain_var.get(), definition.name),
        )
        run_id_token = current_agent_run_id_var.set(
            child_run_id if child_run_id is not None else parent_run_id,
        )
        _log.info(
            "agent_invoke_child_started",
            child_agent=definition.name,
            depth=attempted_depth,
            parent_run_id=str(parent_run_id) if parent_run_id is not None else None,
            child_run_id=str(child_run_id) if child_run_id is not None else None,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        # Heartbeat the child's lease for the duration of its loop (#1501).
        # The child runs inline under the parent task; if the parent task
        # dies mid-child both stop, the child's lease lapses, and the reaper
        # reclaims the child row instead of leaving it stuck ``running``. The
        # sidecar is cancelled in the ``finally`` once the child loop ends.
        # A local import avoids the module-scope cycle with
        # ``meho_backplane.agent.invocation`` (which imports this module to
        # wire the ``invoke_agent`` tool) -- the same lazy-import discipline
        # the ``AgentRunError`` handler below uses.
        child_heartbeat: asyncio.Task[None] | None = None
        if child_run_id is not None and child_lease_owner is not None:
            from meho_backplane.agent.invocation import _heartbeat_loop

            child_heartbeat = asyncio.create_task(
                _heartbeat_loop(child_run_id, child_lease_owner),
                name=f"agent-heartbeat-{child_run_id}",
            )
        try:
            # Bound 2 -- budget. Sharing ctx.usage threads the child's turns
            # into the parent's running total; the shared UsageLimits the loop
            # carries is enforced against that total, so the whole cascade
            # terminates when the budget trips (UsageLimitExceeded surfaces as
            # AgentRunError from the child_runner).
            output = await child_runner(
                definition=definition,
                operator=operator,
                inputs=inputs,
                usage=ctx.usage,
            )
        except AgentRunError as exc:
            _log.warning(
                "agent_invoke_child_failed",
                child_agent=definition.name,
                depth=attempted_depth,
                error=str(exc),
                operator_sub=operator.sub,
            )
            # Stop the heartbeat before finalizing: the child's terminal
            # transition clears the lease, so a still-beating sidecar would
            # only race to a no-op LeaseLostError. Cancelling first keeps the
            # ordering clean.
            await _stop_child_heartbeat(child_heartbeat)
            child_heartbeat = None
            # Close the recorded child row to ``failed`` before re-raising, so a
            # failed / over-budget child does not stay stuck ``running``. Only a
            # recorded child has a row to finalize (no recorder -> child_run_id
            # is None -> nothing to close).
            if child_run_id is not None and finalizer is not None:
                await finalizer(child_run_id, output=None, error=str(exc))
            # A failed child (budget exhausted, tool raised, model errored) is
            # a tool-level outcome the parent model can reason about, not a
            # crash that aborts the parent run.
            raise ModelRetry(
                f"child agent {definition.name!r} failed: {exc}. "
                f"Try a different approach or answer directly."
            ) from exc
        finally:
            # Belt-and-suspenders: stop the heartbeat on every exit path
            # (success, cancel, the ModelRetry re-raise above) so the sidecar
            # never outlives the child loop. ``_stop_child_heartbeat`` is a
            # no-op when it was already stopped in the except branch.
            await _stop_child_heartbeat(child_heartbeat)
            current_agent_run_id_var.reset(run_id_token)
            _agent_name_chain_var.reset(chain_token)
            agent_invoke_depth_var.reset(depth_token)

        # Close the recorded child row to ``succeeded`` with its output (the
        # finalizer projects the raw loop output onto the row's JSON column).
        if child_run_id is not None and finalizer is not None:
            await finalizer(child_run_id, output=output, error=None)

        return {"agent": definition.name, "output": output}

    return Tool.from_schema(
        _invoke_agent,
        name="invoke_agent",
        description=(
            "Invoke another agent definition in your tenant as a child run when "
            "the task needs a different agent's system prompt or toolset (e.g. "
            "escalating a hard sub-problem to a more capable agent). The child "
            "runs under your identity with its own RBAC-filtered tools; its "
            "turns count against your shared budget and the call is "
            "depth-capped, so a runaway chain terminates with a structured "
            "error rather than unbounded spend. Arguments: `agent_name` (the "
            "name of the agent definition to invoke) and `inputs` (the task / "
            "prompt to hand the child). Returns the child's `output`. Prefer "
            "answering directly when you can -- invoke another agent only when "
            "it is genuinely better suited."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "minLength": 1},
                "inputs": {"type": "string", "minLength": 1},
            },
            "required": ["agent_name", "inputs"],
            "additionalProperties": False,
        },
        takes_ctx=True,
    )
