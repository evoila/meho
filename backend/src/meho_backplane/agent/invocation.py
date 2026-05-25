# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent invocation service + run-handle store (G11.1-T4 / #811).

This module is the orchestration layer behind the public invocation
surface (REST + MCP + CLI). It ties three pieces the sibling Tasks ship
into one durable, pollable run:

* the :class:`~meho_backplane.agent.run.AgentRun` seam (T1 #808) that runs
  the bounded tool-use loop in-process,
* the persisted :class:`~meho_backplane.agents.service.AgentDefinitionService`
  (T2 #809) the run loads its definition from, and
* the durable ``agent_run`` row + lifecycle state machine (T6 #813) that
  makes a run inspectable and pollable after the request that started it
  has returned.

Two invocation modes, one loop
==============================

A single :class:`AgentInvoker.run` call serves both shapes the issue
specifies:

* **Sync** — the call blocks until the loop finishes (or fails) and
  returns the terminal :class:`AgentRunOutcome`, *unless* a server-side
  timeout (:attr:`Settings.agent_sync_timeout_seconds`) elapses first. On
  timeout the loop keeps running in the background and the call returns a
  :class:`AgentRunOutcome` flagged ``converted_to_async`` carrying the run
  handle — a long sync run degrades cleanly to a pollable async run rather
  than holding the HTTP connection open indefinitely.
* **Async** — the call returns the run handle immediately; the loop runs
  in the background and the caller polls / streams.

Why a run-handle store
======================

The seam's :class:`~meho_backplane.agent.run.AgentRunHandle` wraps an
in-memory :class:`asyncio.Task`. A FastAPI request's task tree is torn
down when the request returns, so a fire-and-forget run launched on the
request's loop would be cancelled the moment the async-mode call returns.
The :class:`AgentInvoker`'s run store (a ``{run_id: _RunState}`` map) keeps
the background tasks on the **application** event loop, anchored by a
strong reference, so they outlive the request that created them (asyncio
only weakly references bare tasks — an unreferenced task can be GC'd
mid-flight). The store is the in-process liveness anchor;
the durable ``agent_run`` row is the source of truth for *status* and
*output*, so :meth:`AgentInvoker.poll` reads the DB row and works even for
a run whose in-memory task is gone (e.g. after a worker restart).

Tenant scoping + RBAC
=====================

The caller (REST route / MCP tool / CLI verb) authenticates the
:class:`~meho_backplane.auth.operator.Operator` and gates the surface on
the ``operator`` role before calling in. This service additionally
enforces that (a) the named definition belongs to the operator's tenant
(a cross-tenant name is a 404-shaped :class:`AgentNotFoundError`), (b) the
definition is ``enabled`` (a disabled agent is a
:class:`AgentDisabledError`), and (c) a poll / stream only resolves a run
the operator's tenant owns (cross-tenant run ids surface as
:class:`AgentRunNotFoundError`).
"""

# code-quality-allow: file-size — this is the agent-runtime orchestration
# module. The T7 #1067 composition wiring (_resolve_child_definition /
# _record_child_run) and the T8 #1087 finalizer (_finalize_child_run) reuse the
# invoker's _to_agent_definition / _finalize_run / _project_output + the module's
# _split_model_id, so they belong here; pulling them (or the pre-existing error
# classes / dataclasses) into a separate file fragments a cohesive unit or
# introduces an import cycle with no readability gain.

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import structlog

from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.agent.run import (
    AgentDefinition,
    AgentRun,
    AgentRunError,
    AgentRunEvent,
    AgentRunEventKind,
    PydanticAgentRun,
)
from meho_backplane.agents.schemas import AgentDefinitionRead
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.agent_token import get_client_credentials_token
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentRun as AgentRunRow
from meho_backplane.db.models import AgentRunStatus, AgentRunTrigger
from meho_backplane.operations import agent_run as run_lifecycle
from meho_backplane.settings import get_settings

__all__ = [
    "AgentDisabledError",
    "AgentInvocationError",
    "AgentInvoker",
    "AgentNotFoundError",
    "AgentRunNotFoundError",
    "AgentRunOutcome",
    "AgentRunStatusView",
    "get_agent_invoker",
    "reset_agent_invoker_for_testing",
]

_log = structlog.get_logger(__name__)


class AgentInvocationError(Exception):
    """Base class for invocation-surface failures."""


class AgentNotFoundError(AgentInvocationError):
    """No enabled definition named *name* exists in the operator's tenant.

    Raised before any run is created. The boundary maps it to 404 — a
    cross-tenant probe lands here too (the definition lookup is
    tenant-scoped), so existence is not leaked across tenants.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"no agent definition {name!r} in this tenant")


class AgentDisabledError(AgentInvocationError):
    """The named definition exists but is ``enabled=False``.

    A disabled agent must not run; the boundary maps this to 409. Distinct
    from :class:`AgentNotFoundError` so an operator who owns the definition
    gets an actionable message ("enable it first") rather than a misleading
    404.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"agent definition {name!r} is disabled")


class AgentRunNotFoundError(AgentInvocationError):
    """No ``agent_run`` row for *run_id* in the operator's tenant.

    Raised by :meth:`AgentInvoker.poll` / :meth:`AgentInvoker.stream_events`
    for an unknown or cross-tenant run id. The boundary maps it to 404.
    """

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(f"no agent run {run_id} in this tenant")


@dataclass(frozen=True, slots=True)
class AgentRunStatusView:
    """A poll-time view of a run's durable state, read from the DB row.

    Mirrors the inspectable columns of :class:`~meho_backplane.db.models.AgentRun`
    the poll surface exposes. ``output`` / ``error`` are populated only once
    the run reaches a terminal state.
    """

    run_id: uuid.UUID
    status: AgentRunStatus
    turns: int
    provider: str | None
    model: str | None
    output: dict[str, object] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AgentRunOutcome:
    """The result of an :meth:`AgentInvoker.run` call.

    ``run_id`` is always the durable run handle. ``status`` is the run's
    state at the moment the call returns: a terminal state for a completed
    sync run, or :attr:`AgentRunStatus.RUNNING` for an async run (or a sync
    run that exceeded the server-side timeout and converted to async).
    ``output`` / ``error`` are set only for a terminal sync outcome.
    ``converted_to_async`` is ``True`` when a sync call timed out and handed
    back the still-running handle — the caller renders it as a 202-shaped
    response so the operator knows to poll.
    """

    run_id: uuid.UUID
    status: AgentRunStatus
    output: dict[str, object] | None = None
    error: str | None = None
    converted_to_async: bool = False


@dataclass(slots=True)
class _RunState:
    """In-process liveness anchor for one background run.

    Holds a strong reference to the loop's :class:`asyncio.Task` so it is
    not garbage-collected mid-flight (asyncio keeps only a weak reference to
    a bare task). The store evicts the entry on completion via the task's
    done-callback so a long-lived worker does not accumulate finished runs.
    """

    task: asyncio.Task[None]
    operator_sub: str
    tenant_id: uuid.UUID


#: Provider id parsed from a ``"<provider>:<model>"`` model id. The G11.1
#: initiative resolves the logical model tier to this concrete pair through
#: the settings default; the multi-provider resolver is G11.5.
_DEFAULT_PROVIDER: Final[str] = "anthropic"


def _split_model_id(model_id: str) -> tuple[str, str]:
    """Split ``"anthropic:claude-..."`` into ``(provider, model)``.

    A model id without a ``":"`` separator falls back to the default
    provider with the whole string as the model name, so an operator who
    pins a bare model id does not break the run record.
    """
    provider, sep, model = model_id.partition(":")
    if not sep:
        return _DEFAULT_PROVIDER, model_id
    return provider, model


class AgentInvoker:
    """Drives agent runs and tracks their durable, pollable state.

    Stateless beyond the injected seam + the process-wide
    :class:`_RunStore`; instantiate once (the module singleton via
    :func:`get_agent_invoker`) and call freely. Tests inject a
    deterministic :class:`~meho_backplane.agent.run.AgentRun` (a
    :class:`~meho_backplane.agent.run.PydanticAgentRun` over a
    ``FunctionModel``) so no real LLM is hit.
    """

    def __init__(self, *, runtime: AgentRun | None = None) -> None:
        # The default runtime wires agent-invokes-agent composition (G11.1-T7
        # #1067): the live surface owns the tenant-scoped child resolver + the
        # child-run recorder, so a run started here can invoke another agent. The
        # finalizer (G11.1-T8 #1087) closes each recorded child row to its
        # terminal state, so an invoked child reaches ``succeeded`` / ``failed``
        # rather than staying stuck ``running``. An injected runtime (tests)
        # controls its own wiring.
        self._runtime: AgentRun = (
            runtime
            if runtime is not None
            else PydanticAgentRun(
                child_agent_resolver=_resolve_child_definition,
                child_run_recorder=_record_child_run,
                child_run_finalizer=_finalize_child_run,
            )
        )
        self._store: dict[uuid.UUID, _RunState] = {}

    # -- definition resolution -------------------------------------------

    async def _load_definition(
        self,
        operator: Operator,
        name: str,
    ) -> AgentDefinitionRead:
        """Load an enabled definition for the operator's tenant or raise."""
        service = AgentDefinitionService()
        entry = await service.get(operator.tenant_id, name)
        if entry is None:
            raise AgentNotFoundError(name)
        if not entry.enabled:
            raise AgentDisabledError(name)
        return entry

    async def ensure_runnable(self, operator: Operator, name: str) -> None:
        """Validate the named agent is runnable for the tenant, run nothing.

        The SSE events route calls this *before* opening the stream so a
        not-found / disabled error surfaces as a clean HTTP status rather
        than a torn ``text/event-stream`` connection (which an
        ``EventSource`` client would auto-reconnect into a hot loop).

        Raises:
            AgentNotFoundError: no such definition in the tenant.
            AgentDisabledError: the definition is disabled.
        """
        await self._load_definition(operator, name)

    @staticmethod
    def _to_agent_definition(entry: AgentDefinitionRead) -> AgentDefinition:
        """Map a persisted definition row onto the seam's value object.

        The persisted ``output_schema`` (a JSON Schema dict) is *not* mapped
        onto the seam's ``output_type`` (a Pydantic class): synthesising a
        runtime model from JSON Schema is the seam's contract, not this
        surface's, so a run with a stored output schema returns its loop's
        free-text answer in v0.2 (recorded as ``{"text": ...}``). The
        toolset spec passes through verbatim — the T3 resolver intersects it
        with the operator's permissions at run time.
        """
        return AgentDefinition(
            name=entry.name,
            system_prompt=entry.system_prompt,
            request_limit=entry.turn_budget,
            toolset=entry.toolset,
        )

    # -- run creation -----------------------------------------------------

    async def _create_run_row(
        self,
        operator: Operator,
        entry: AgentDefinitionRead,
        *,
        provider: str,
        model: str,
        trigger: AgentRunTrigger = AgentRunTrigger.DIRECT,
    ) -> uuid.UUID:
        """Insert a ``pending`` run row, transition it to ``running``, commit.

        Done in its own committed transaction *before* the loop starts so
        the run is pollable the instant :meth:`run` returns a handle — even
        in async mode where the background task has not made progress yet.
        Returns the run id (the durable handle + audit-lineage key). A
        human-initiated :meth:`run` records ``DIRECT``; an autonomous
        :meth:`run_scheduled` records ``SCHEDULED``.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.create_run(
                session,
                tenant_id=operator.tenant_id,
                identity_sub=operator.sub,
                trigger=trigger,
                model_tier=entry.model_tier,
                agent_definition_id=entry.id,
            )
            await run_lifecycle.start_run(session, row, provider=provider, model=model)
            run_id = row.id
            await session.commit()
        return run_id

    @staticmethod
    async def _finalize_run(
        run_id: uuid.UUID,
        *,
        output: dict[str, object] | None,
        error: str | None,
    ) -> None:
        """Record a run's terminal state on its durable row, committed.

        Loads the row fresh (the create transaction is long closed), then
        applies ``succeed_run`` or ``fail_run`` through the lifecycle
        state-machine guard. A row already in a terminal state (e.g. an
        operator cancelled it mid-flight) is left untouched —
        :class:`~meho_backplane.operations.agent_run.IllegalTransitionError`
        is swallowed because the cancel already wrote the terminal state.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None:  # pragma: no cover - row created moments earlier
                return
            try:
                if error is not None:
                    await run_lifecycle.fail_run(session, row, error=error)
                else:
                    await run_lifecycle.succeed_run(session, row, output=output or {})
            except run_lifecycle.IllegalTransitionError:
                await session.rollback()
                return
            await session.commit()

    async def _run_loop_to_completion(
        self,
        run_id: uuid.UUID,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
    ) -> None:
        """Background coroutine: run the loop and record its terminal state.

        Wraps the seam's :meth:`~meho_backplane.agent.run.AgentRun.start` /
        :meth:`~meho_backplane.agent.run.AgentRun.result`; on success records
        the output, on :class:`AgentRunError` records the failure. Never
        re-raises — a failed run is a recorded ``failed`` row, not a crashed
        background task (an unhandled task exception would surface only as a
        log warning at GC time).

        Binds :data:`~meho_backplane.agent.invoke.current_agent_run_id_var` to
        this run's id for the loop's duration, so the first ``invoke_agent``
        call records its child with this run as the parent (the lineage key).
        The task carries its own contextvar copy (``asyncio.create_task``
        snapshots the context), so the bind is isolated to this run.
        """
        run_token = current_agent_run_id_var.set(run_id)
        try:
            try:
                handle = self._runtime.start(definition, operator, inputs)
                result = await self._runtime.result(handle)
            except AgentRunError as exc:
                await self._finalize_run(run_id, output=None, error=str(exc))
                return
            except asyncio.CancelledError:
                # A cancel writes its own terminal row via cancel_run; leave it.
                raise
            except Exception as exc:
                _log.warning("agent_invoke_unexpected_failure", run_id=str(run_id), error=str(exc))
                await self._finalize_run(run_id, output=None, error=str(exc))
                return
            await self._finalize_run(run_id, output=_project_output(result.output), error=None)
        finally:
            current_agent_run_id_var.reset(run_token)

    # -- public surface ---------------------------------------------------

    async def run(
        self,
        operator: Operator,
        name: str,
        inputs: str,
        *,
        async_mode: bool = False,
    ) -> AgentRunOutcome:
        """Invoke an agent; block for short sync runs, hand back a handle else.

        Resolves the named, enabled definition for the operator's tenant,
        creates the durable run row, and launches the loop as a background
        task anchored in the run store. In ``async_mode`` the handle returns
        immediately. Otherwise the call awaits the run up to the
        server-side timeout; on timeout the still-running handle is returned
        with ``converted_to_async=True``.

        Raises:
            AgentNotFoundError: no such definition in the tenant.
            AgentDisabledError: the definition is disabled.
        """
        entry = await self._load_definition(operator, name)
        definition = self._to_agent_definition(entry)
        settings = get_settings()
        provider, model = _split_model_id(settings.agent_default_model)
        # Resource-server delegation (G11.2-T2 #816): a human triggered this
        # run, so bind the acting agent's principal as the RFC 8693 actor
        # while the loop task is created. ``asyncio.create_task`` snapshots
        # the contextvars, so the background task inherits actor_sub for its
        # whole life; every audit row its in-process tool calls produce records
        # operator_sub=human + actor_sub=agent. Keycloak has no delegation
        # token exchange, so MEHO synthesises the sub+act binding here.
        #
        # Persist the durable run row *inside* the binding so a fail-closed
        # actor_delegation (empty identity_ref) raises before the row is
        # committed — never a ``running`` row with no backing task.
        with actor_delegation(entry.identity_ref):
            run_id = await self._create_run_row(operator, entry, provider=provider, model=model)
            task = self._launch_run(run_id, definition, operator, inputs)

        _log.info(
            "agent_invoke_started",
            run_id=str(run_id),
            agent=name,
            async_mode=async_mode,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )

        if async_mode:
            return AgentRunOutcome(run_id=run_id, status=AgentRunStatus.RUNNING)

        try:
            # Shield so the timeout abandons the *wait*, not the run — the
            # loop keeps going in the background and stays pollable.
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=settings.agent_sync_timeout_seconds,
            )
        except TimeoutError:
            _log.info(
                "agent_invoke_converted_to_async",
                run_id=str(run_id),
                timeout=settings.agent_sync_timeout_seconds,
            )
            return AgentRunOutcome(
                run_id=run_id,
                status=AgentRunStatus.RUNNING,
                converted_to_async=True,
            )

        view = await self.poll(operator, run_id)
        return AgentRunOutcome(
            run_id=run_id,
            status=view.status,
            output=view.output,
            error=view.error,
        )

    async def run_scheduled(
        self,
        name: str,
        inputs: str,
        *,
        agent_client_id: str,
        agent_client_secret: str,
    ) -> AgentRunOutcome:
        """Run an agent autonomously under its own ``client_credentials`` identity.

        No human initiator: the agent authenticates as itself via the
        ``client_credentials`` grant (a single
        :func:`~meho_backplane.auth.agent_token.get_client_credentials_token`
        call), so it is the *subject* (``operator_sub``=agent) and there is no
        separate actor — ``actor_sub`` stays ``NULL`` on every audit row the
        run produces (this method deliberately does **not** bind
        :func:`~meho_backplane.auth.delegation.actor_delegation`, unlike the
        human-initiated :meth:`run`).

        The scheduler that decides *when* to fire a run and supplies the agent
        credentials (from Vault / config) is G11.3's
        ``SCHEDULED``-trigger scope; this method is the authentication +
        audit-shape seam it calls. Blocks until the loop completes (autonomous
        runs have no client waiting on a sync timeout).

        Raises:
            AgentTokenError: the ``client_credentials`` grant failed.
            AgentNotFoundError / AgentDisabledError: no enabled definition
                named *name* in the agent's tenant.
        """
        settings = get_settings()
        # Request the audience we then verify, so the token carries it even on
        # realms without a default audience mapper.
        token = await get_client_credentials_token(
            issuer_url=str(settings.keycloak_issuer_url),
            client_id=agent_client_id,
            client_secret=agent_client_secret,
            audience=settings.keycloak_audience,
        )
        operator = await verify_jwt_for_audience(
            f"Bearer {token}",
            expected_audience=settings.keycloak_audience,
        )
        entry = await self._load_definition(operator, name)
        # Bind the run to the authenticating agent: the definition's
        # ``identity_ref`` must be the agent principal's own sub. Without this,
        # agent A's credentials could launch agent B's definition (any enabled
        # one in the tenant) and misattribute the audit trail. The contract —
        # ``identity_ref`` == the agent principal's Keycloak sub — also keeps a
        # user-initiated run's ``actor_sub`` (= ``identity_ref``) in the same
        # identifier space as an autonomous run's ``operator_sub``.
        if entry.identity_ref != operator.sub:
            raise AgentInvocationError(
                f"scheduled run rejected: agent credentials (sub={operator.sub!r}) "
                f"do not own definition {name!r} (identity_ref={entry.identity_ref!r})"
            )
        definition = self._to_agent_definition(entry)
        provider, model = _split_model_id(settings.agent_default_model)
        run_id = await self._create_run_row(
            operator,
            entry,
            provider=provider,
            model=model,
            trigger=AgentRunTrigger.SCHEDULED,
        )
        # No actor_delegation: the agent is the subject, not an actor on behalf
        # of a human, so actor_sub stays NULL.
        task = self._launch_run(run_id, definition, operator, inputs)
        _log.info(
            "agent_scheduled_started",
            run_id=str(run_id),
            agent=name,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        await task
        view = await self.poll(operator, run_id)
        return AgentRunOutcome(
            run_id=run_id,
            status=view.status,
            output=view.output,
            error=view.error,
        )

    def _launch_run(
        self,
        run_id: uuid.UUID,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
    ) -> asyncio.Task[None]:
        """Launch the loop as a background task anchored in the run store.

        The store holds a strong reference so the task is not GC'd
        mid-flight (asyncio weakly references bare tasks) and survives the
        request that created it; a done-callback evicts the entry on
        completion so a long-lived worker does not accumulate finished runs.
        """
        task = asyncio.create_task(
            self._run_loop_to_completion(run_id, definition, operator, inputs),
            name=f"agent-invoke-{run_id}",
        )
        self._store[run_id] = _RunState(
            task=task,
            operator_sub=operator.sub,
            tenant_id=operator.tenant_id,
        )

        def _evict(_task: asyncio.Task[None], rid: uuid.UUID = run_id) -> None:
            self._store.pop(rid, None)

        task.add_done_callback(_evict)
        return task

    async def poll(self, operator: Operator, run_id: uuid.UUID) -> AgentRunStatusView:
        """Return the durable state of a run the operator's tenant owns.

        Reads the ``agent_run`` row (the durable source of truth), so it
        works after the creating request has returned and even after the
        in-memory task is gone. A cross-tenant / unknown id raises
        :class:`AgentRunNotFoundError`.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.get_run(session, run_id)
            if row is None or row.tenant_id != operator.tenant_id:
                raise AgentRunNotFoundError(run_id)
            return _row_to_view(row)

    async def stream_events(
        self,
        operator: Operator,
        name: str,
        inputs: str,
    ) -> AsyncIterator[tuple[uuid.UUID, AgentRunEvent]]:
        """Run an agent inline and yield ``(run_id, event)`` for SSE.

        Drives the seam's :meth:`~meho_backplane.agent.run.AgentRun.stream_events`
        inline in the calling coroutine (the SSE response task), recording a
        durable run row first so the stream's events share a poll-able run
        handle. The terminal event (``final`` / ``error``) is also written
        to the run row, so a consumer that streamed a run can still poll its
        recorded outcome afterward.

        Raises:
            AgentNotFoundError / AgentDisabledError: same as :meth:`run`.
        """
        entry = await self._load_definition(operator, name)
        definition = self._to_agent_definition(entry)
        settings = get_settings()
        provider, model = _split_model_id(settings.agent_default_model)
        run_id = await self._create_run_row(operator, entry, provider=provider, model=model)

        terminal_output: dict[str, object] | None = None
        terminal_error: str | None = None
        # Bind the lineage contextvar so an ``invoke_agent`` call inside the
        # streamed run records its child against this run (G11.1-T7 #1067). The
        # stream runs inline in the SSE response coroutine, so the token reset
        # in ``finally`` keeps the bind from leaking past the stream.
        run_token = current_agent_run_id_var.set(run_id)
        try:
            async for event in self._runtime.stream_events(definition, operator, inputs, run_id):
                if event.kind is AgentRunEventKind.FINAL:
                    terminal_output = _project_output(event.data.get("output"))
                elif event.kind is AgentRunEventKind.ERROR:
                    terminal_error = str(event.data.get("error"))
                yield run_id, event
        finally:
            current_agent_run_id_var.reset(run_token)
            await self._finalize_run(run_id, output=terminal_output, error=terminal_error)


async def _resolve_child_definition(
    operator: Operator,
    agent_name: str,
) -> AgentDefinition | None:
    """Resolve a child agent name to a runnable seam definition, or ``None``.

    The :class:`~meho_backplane.agent.invoke.ChildAgentResolver` the live
    invoker injects into the seam (G11.1-T7 #1067). Looks the definition up
    scoped to the operator's tenant via :class:`AgentDefinitionService`, so a
    cross-tenant or unknown name simply does not resolve; a ``enabled=False``
    definition also returns ``None``. ``invoke_agent`` surfaces a ``None`` as a
    structured :class:`~pydantic_ai.ModelRetry`. Mirrors
    :meth:`AgentInvoker._load_definition` but returns ``None`` instead of
    raising, per the resolver protocol.
    """
    service = AgentDefinitionService()
    entry = await service.get(operator.tenant_id, agent_name)
    if entry is None or not entry.enabled:
        return None
    return AgentInvoker._to_agent_definition(entry)


async def _record_child_run(
    *,
    operator: Operator,
    definition: AgentDefinition,
    parent_run_id: uuid.UUID | None,
) -> uuid.UUID:
    """Persist a child ``agent_run`` row linked to its parent; return its id.

    The :class:`~meho_backplane.agent.invoke.ChildRunRecorder` the live invoker
    injects (G11.1-T7 #1067). The seam value object carries neither the
    persisted ``agent_definition_id`` nor the ``model_tier`` the run row
    records, so the definition is re-resolved by ``(tenant, name)`` to recover
    them; the row is then created with ``trigger=agent-invoked`` + the parent
    linkage and transitioned to ``running``, committed in its own transaction
    so the child run is inspectable while it executes (mirrors
    :meth:`AgentInvoker._create_run_row`).

    The row's *terminal* state is deliberately not written here: the
    ``ChildRunRecorder`` protocol returns only the new id, and the child loop's
    success/failure surfaces through the parent run. Finalizing child rows to
    ``succeeded`` / ``failed`` is a follow-up — it needs a protocol extension
    (a finalizer hook), out of #1067's "wire the existing mechanism" scope. A
    definition deleted between resolution and recording raises
    :class:`~meho_backplane.agent.run.AgentRunError`, surfaced by
    ``invoke_agent`` as a ``ModelRetry``.
    """
    service = AgentDefinitionService()
    entry = await service.get(operator.tenant_id, definition.name)
    if entry is None:
        raise AgentRunError(f"agent definition {definition.name!r} no longer exists")
    settings = get_settings()
    provider, model = _split_model_id(settings.agent_default_model)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await run_lifecycle.create_run(
            session,
            tenant_id=operator.tenant_id,
            identity_sub=operator.sub,
            trigger=AgentRunTrigger.AGENT_INVOKED,
            model_tier=entry.model_tier,
            agent_definition_id=entry.id,
            parent_run_id=parent_run_id,
        )
        await run_lifecycle.start_run(session, row, provider=provider, model=model)
        child_run_id = row.id
        await session.commit()
    return child_run_id


async def _finalize_child_run(
    run_id: uuid.UUID,
    *,
    output: object,
    error: str | None,
) -> None:
    """Close a recorded child ``agent_run`` row to its terminal state.

    The :class:`~meho_backplane.agent.invoke.ChildRunFinalizer` the live invoker
    injects (G11.1-T8 #1087), the companion to :func:`_record_child_run`. After
    the child loop returns or fails, ``invoke_agent`` calls this with the child's
    loop ``output`` (on success) or the ``error`` (on
    :class:`~meho_backplane.agent.run.AgentRunError`), so the child row reaches
    ``succeeded`` / ``failed`` instead of staying stuck ``running``. Reuses
    :meth:`AgentInvoker._finalize_run`'s shape verbatim -- load the row fresh
    (the create transaction is long closed), project the raw output with
    :func:`_project_output`, apply ``succeed_run`` / ``fail_run``, and swallow
    :class:`~meho_backplane.operations.agent_run.IllegalTransitionError` when a
    terminal state already landed (e.g. an operator cancelled the child
    mid-flight -- the cancel already wrote the terminal row).
    """
    await AgentInvoker._finalize_run(
        run_id,
        output=None if error is not None else _project_output(output),
        error=error,
    )


def _row_to_view(row: AgentRunRow) -> AgentRunStatusView:
    """Project an ``agent_run`` row onto the poll-time view."""
    return AgentRunStatusView(
        run_id=row.id,
        status=AgentRunStatus(row.status),
        turns=row.turns,
        provider=row.provider,
        model=row.model,
        output=row.output,
        error=row.error,
    )


def _project_output(output: object) -> dict[str, object]:
    """Project a loop output onto the run row's JSON ``output`` column.

    The column is JSON-object shaped, so a free-text answer is wrapped as
    ``{"text": ...}`` and a structured dict passes through. Mirrors the
    ``output`` contract :class:`~meho_backplane.db.models.AgentRun`
    documents ("structured output when the agent declared an output_type,
    or a ``{"text": ...}`` projection otherwise").
    """
    if isinstance(output, dict):
        return output
    return {"text": output if isinstance(output, str) else str(output)}


#: Process-wide singleton. The in-memory run store must be shared across
#: every request handler in the worker (a per-request invoker would lose
#: the background-task anchor the moment the request returned), so the
#: invoker is a module singleton — the same shape the broadcast client
#: uses.
_INVOKER: AgentInvoker | None = None


def get_agent_invoker() -> AgentInvoker:
    """Return the process-wide :class:`AgentInvoker`, creating on first call.

    Every REST route / MCP tool / CLI-backing handler shares one invoker so
    the in-memory run store (the background-task liveness anchor) is shared.
    """
    global _INVOKER
    if _INVOKER is None:
        _INVOKER = AgentInvoker()
    return _INVOKER


def reset_agent_invoker_for_testing(invoker: AgentInvoker | None = None) -> None:
    """Replace (or clear) the singleton — test seam only.

    Passing an *invoker* installs it (so a test can inject a deterministic
    seam); passing ``None`` clears the cache so the next
    :func:`get_agent_invoker` builds a fresh default.
    """
    global _INVOKER
    _INVOKER = invoker
