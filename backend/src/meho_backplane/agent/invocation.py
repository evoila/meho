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
import contextlib
import os
import socket
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

import structlog
from pydantic import SecretStr

from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.agent.run import (
    AgentDefinition,
    AgentRun,
    AgentRunError,
    AgentRunEvent,
    AgentRunEventKind,
    BudgetExceededError,
    PydanticAgentRun,
    ScheduledRunNoInputError,
    prompt_is_effectively_empty,
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
from meho_backplane.operations import identity_budget
from meho_backplane.operations._audit import (
    AgentRunAuditMeta,
    agent_run_audit_meta_var,
    agent_session_id_var,
    work_ref_var,
)
from meho_backplane.operations.budget_enforcement import (
    BudgetDecision,
    BudgetDecisionKind,
    EnforcementContext,
    evaluate_pre_run_budget,
)
from meho_backplane.operations.identity_budget import TokenUsage
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "AgentDisabledError",
    "AgentInvocationError",
    "AgentInvoker",
    "AgentNotFoundError",
    "AgentRunNotFoundError",
    "AgentRunOutcome",
    "AgentRunStatusView",
    "AgentRunSummary",
    "BudgetExceededError",
    "ScheduledRunNoInputError",
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
class AgentRunSummary:
    """A list-row view of a run, read from the durable ``agent_run`` row.

    The projection the agent-run list surface (work_ref I3-T2 #1662)
    returns per row: identity, lifecycle state, the resolved model
    coordinates, timestamps, and the ``work_ref`` change-ticket
    reference the list filters on. The full ``output`` blob is *not*
    carried -- the list is a scannable index; a caller wanting the
    result polls :meth:`AgentInvoker.poll` for the specific run.
    """

    run_id: uuid.UUID
    status: AgentRunStatus
    trigger: str
    model_tier: str
    provider: str | None
    model: str | None
    turns: int
    work_ref: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


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


def _lease_owner() -> str:
    """Compute a stable per-process identifier for the run's ``lease_owner``.

    ``"<hostname>:<pid>"`` -- the same shape
    :func:`meho_backplane.events.drain._claimer_identity` uses for the
    outbox-drain claimer. Visible from PG diagnostics, no PII, no secret
    material; an operator chasing a reaped run can map the
    ``prior_lease_owner`` the reaper records straight back to the pod /
    process whose worker died.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


async def _heartbeat_loop(run_id: uuid.UUID, owner: str) -> None:
    """Extend *run_id*'s lease on a cadence until cancelled or the lease is lost.

    Wiring the lease lifecycle into the fire path (#1501) closes the
    G11.3-T4 #825 gap: :func:`run_lifecycle.claim_lease` stamps the lease
    at run-start, but without a heartbeat the lease expires under any run
    that outlives one TTL window and the reaper would reclaim a perfectly
    healthy worker. This sidecar bumps ``lease_expires_at`` forward every
    ``ttl_seconds / 2`` so a live worker keeps its lease, while a worker
    that has died (its event loop gone) stops heartbeating and the reaper
    reclaims the run after the TTL lapses.

    Each heartbeat opens its own short-lived committed transaction -- the
    conditional ``UPDATE`` in :func:`run_lifecycle.heartbeat` is atomic, so
    no longer-held session is needed. A :class:`run_lifecycle.LeaseLostError`
    means the reaper (or an operator cancel) already took the run over: the
    loop logs and returns so it stops touching a row it no longer owns.
    :class:`asyncio.CancelledError` (the run finished, the sidecar is being
    torn down) propagates verbatim.
    """
    settings = get_settings()
    ttl_seconds = settings.agent_run_lease_ttl_seconds
    # Heartbeat at half the TTL so a single missed beat (a transient DB
    # blip, a short GC pause) still leaves one full window of slack before
    # the reaper reclaims -- the same two-window margin the reaper's
    # default tick/TTL pairing documents.
    interval = max(1.0, ttl_seconds / 2.0)
    sessionmaker = get_sessionmaker()
    while True:
        await asyncio.sleep(interval)
        try:
            async with sessionmaker() as session:
                await run_lifecycle.heartbeat(
                    session,
                    run_id=run_id,
                    owner=owner,
                    ttl_seconds=ttl_seconds,
                )
                await session.commit()
        except run_lifecycle.LeaseLostError:
            # The reaper reclaimed the row or an operator cancelled it
            # out from under us. Stop heartbeating -- the run is no
            # longer ours to keep alive.
            _log.info("agent_run_lease_lost", run_id=str(run_id), owner=owner)
            return


def _log_decision(
    decision: BudgetDecision,
    *,
    operator: Operator,
    agent: str,
) -> None:
    """Emit one structured log line per pre-execution budget decision.

    Three log shapes so an operator's ``grep`` / Loki query distinguishes
    them at a glance:

    * ``agent_run_refused_*`` -- already emitted by the enforcement
      service itself (kill switch + cap-breach paths); this function
      adds nothing in the REFUSE branch.
    * ``agent_run_tier_downgraded`` / ``agent_run_threshold_no_cheaper_tier``
      -- already emitted by the enforcement service.
    * ``agent_run_budget_allowed`` -- the no-op happy-path line this
      function emits when nothing fired, gated to ``debug`` so the
      hot path stays quiet by default.

    Centralising the post-decision logging here (rather than threading
    it through every invocation entry point) keeps the audit trail
    consistent across :meth:`AgentInvoker.run` /
    :meth:`AgentInvoker.run_scheduled` /
    :meth:`AgentInvoker.stream_events`.
    """
    if decision.kind is BudgetDecisionKind.ALLOW and not decision.downgraded:
        _log.debug(
            "agent_run_budget_allowed",
            agent=agent,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
            tier=decision.tier.value if decision.tier is not None else None,
        )


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


def _full_model_id(provider: str | None, model: str | None) -> str | None:
    """Rebuild the provider-prefixed model id from a split ``(provider, model)``.

    The inverse of :func:`_split_model_id`. The
    :class:`~meho_backplane.db.models.AgentRun` row stores the two
    pieces separately (``provider``, ``model``); the
    :data:`~meho_backplane.operations.identity_budget.MODEL_PRICING`
    table keys on the rejoined ``provider:model`` form that
    :attr:`Settings.agent_default_model` emits. ``None`` either side
    surfaces as ``None`` so the cost lookup falls through to the
    *"unknown model -> Decimal(0)"* branch.
    """
    if provider is None or model is None:
        return None
    return f"{provider}:{model}"


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
        not-found / disabled / budget-refused error surfaces as a clean
        HTTP status rather than a torn ``text/event-stream`` connection
        (which an ``EventSource`` client would auto-reconnect into a
        hot loop). The budget gate runs here too (G11.5-T6 #1080) for
        the same reason; the SSE generator's own re-check inside
        :meth:`stream_events` is the source of truth for the resolved
        tier the loop actually runs against, but the pre-stream call
        gives the boundary a clean 4xx path.

        Raises:
            AgentNotFoundError: no such definition in the tenant.
            AgentDisabledError: the definition is disabled.
            BudgetExceededError: the per-identity / per-tenant /
                global pre-execution budget gate refused this run.
        """
        entry = await self._load_definition(operator, name)
        definition = self._to_agent_definition(entry)
        # Discard the (possibly degraded) return value here: the gate
        # is re-evaluated inside :meth:`stream_events` once the
        # generator is actually entered, and that re-evaluation is
        # the canonical one (it's the gate whose result the loop
        # runs against). The pre-stream call just trips a 4xx ahead
        # of the StreamingResponse.
        await self._enforce_pre_run_budget(operator, definition)

    async def _enforce_pre_run_budget(
        self,
        operator: Operator,
        definition: AgentDefinition,
    ) -> AgentDefinition:
        """Run the G11.5-T6 pre-execution budget gate; return the (maybe degraded) definition.

        Calls
        :func:`~meho_backplane.operations.budget_enforcement.evaluate_pre_run_budget`
        against the operator's tenant + sub, with the definition's
        :attr:`AgentDefinition.tier` as the requested tier. On
        :attr:`BudgetDecisionKind.REFUSE` raises
        :class:`BudgetExceededError` (which the public surface
        propagates as a 4xx / MCP elicitation refusal / terminal
        ``ERROR`` SSE event); on :attr:`BudgetDecisionKind.ALLOW`
        returns either the unchanged definition or a frozen-model
        copy with ``tier`` replaced by the one-rung-cheaper tier the
        policy picked (the runtime then resolves against that tier
        via :class:`~meho_backplane.agent.run.PydanticAgentRun`'s
        normal resolver path).

        The check uses a short-lived session because the consumption
        service is read-only here and the gate must commit no rows
        (so a refused run leaves no DB footprint per the
        :class:`BudgetExceededError` contract).
        """
        settings = get_settings()
        context = EnforcementContext.from_settings(settings)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            decision = await evaluate_pre_run_budget(
                session,
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
                requested_tier=definition.tier,
                context=context,
            )
        _log_decision(decision, operator=operator, agent=definition.name)
        if decision.kind is BudgetDecisionKind.REFUSE:
            raise BudgetExceededError(decision.reason)
        if decision.downgraded and decision.tier is not None:
            # The definition is frozen (Pydantic ConfigDict(frozen=True)),
            # so the tier swap goes through ``model_copy`` rather than
            # in-place mutation — keeps the original value object
            # untouched for any concurrent reader and matches the
            # frozen-data convention.
            return definition.model_copy(update={"tier": decision.tier})
        return definition

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

        ``tier`` is deliberately *not* threaded from ``entry.model_tier``
        in G11.5-T1 (#1075): the persisted
        :class:`~meho_backplane.agents.schemas.AgentModelTier`
        vocabulary (``standard`` / ``fast`` / ``deep``) and the resolver's
        :class:`~meho_backplane.agent.models.AgentTier` vocabulary
        (``triage`` / ``investigate`` / ``summarize``) are orthogonal:
        the first is a cost/capability ladder, the second is a workflow
        role. A naive equate-by-position mapping would be semantically
        wrong, and the API-surface
        :class:`~meho_backplane.agents.schemas.AgentDefinitionCreate.model_tier`
        is a public contract a rename would break. The reconciliation
        (single enum / explicit mapping table + Alembic retag of stored
        values) is queued behind the concrete-backend tasks
        (#1076 / #1077 / #1078), which exercise the resolver via direct
        programmatic construction in v0.2; once a concrete non-Anthropic
        backend ships the persisted vocabulary needs to grow, and that
        is the right moment to unify. Until then, every persisted
        definition resolves through :data:`ModelFactory` (the legacy
        single-tenant path) and the resolver branch in
        :meth:`~meho_backplane.agent.run.PydanticAgentRun._resolve_model`
        is reachable only via direct programmatic construction.
        """
        # TODO(G11.5-T2 — follow up to #1075): map entry.model_tier ->
        # definition.tier once AgentModelTier (standard/fast/deep) and
        # AgentTier (triage/investigate/summarize) are reconciled.
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
    ) -> tuple[uuid.UUID, str]:
        """Insert a ``pending`` run row, claim a lease, go ``running``, commit.

        Done in its own committed transaction *before* the loop starts so
        the run is pollable the instant :meth:`run` returns a handle — even
        in async mode where the background task has not made progress yet.
        Returns ``(run_id, lease_owner)``: the run id is the durable handle
        + audit-lineage key; the lease owner is the per-process stamp the
        heartbeat sidecar reuses so its conditional ``UPDATE`` matches the
        row this worker claimed. A human-initiated :meth:`run` records
        ``DIRECT``; an autonomous :meth:`run_scheduled` records
        ``SCHEDULED``.

        The lease is stamped (#1501 / G11.3-T4 #825) in the *same*
        transaction as the ``pending`` -> ``running`` transition, so a
        committed run is never ``running`` without a lease: the reaper's
        claim query (``status='running' AND lease_expires_at < now()``)
        can therefore reclaim this run once its lease lapses, instead of
        the row staying stuck ``running`` forever. The in-flight policy
        is left at the row default (``fail_into_audit``) -- a direct /
        scheduled run has no firing-trigger row to snapshot, and
        ``fail_into_audit`` is the conservative reclaim outcome the
        reaper applies.

        work_ref I3-T2 (#1662): the run's external change-ticket
        reference is read here off the shared
        :data:`~meho_backplane.operations._audit.work_ref_var` ContextVar
        -- the same boundary every transport (REST / MCP / scheduler)
        binds it on, so all three call sites (:meth:`run`,
        :meth:`stream_events`, :meth:`_launch_scheduled_run`) inherit it
        without per-caller plumbing. ``None`` when no ticket is bound;
        set-at-create-only on the row.
        """
        owner = _lease_owner()
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await run_lifecycle.create_run(
                session,
                tenant_id=operator.tenant_id,
                identity_sub=operator.sub,
                trigger=trigger,
                model_tier=entry.model_tier,
                agent_definition_id=entry.id,
                work_ref=work_ref_var.get(),
            )
            await run_lifecycle.start_run(session, row, provider=provider, model=model)
            await run_lifecycle.claim_lease(
                session,
                row,
                owner=owner,
                ttl_seconds=get_settings().agent_run_lease_ttl_seconds,
            )
            run_id = row.id
            await session.commit()
        return run_id, owner

    @staticmethod
    async def _finalize_run(
        run_id: uuid.UUID,
        *,
        output: dict[str, object] | None,
        error: str | None,
        usage: TokenUsage | None = None,
    ) -> None:
        """Record a run's terminal state on its durable row, committed.

        Loads the row fresh (the create transaction is long closed), then
        applies ``succeed_run`` or ``fail_run`` through the lifecycle
        state-machine guard. A row already in a terminal state (e.g. an
        operator cancelled it mid-flight) is left untouched —
        :class:`~meho_backplane.operations.agent_run.IllegalTransitionError`
        is swallowed because the cancel already wrote the terminal state.

        When *usage* is supplied (the success path), the function also:

        1. Rebuilds the provider-prefixed model id from the row's
           ``provider`` + ``model`` columns (the inverse of
           :func:`_split_model_id`) so the
           :data:`~meho_backplane.operations.identity_budget.MODEL_PRICING`
           lookup hits.
        2. Computes the run's USD cost via
           :func:`~meho_backplane.operations.identity_budget.compute_cost`.
        3. Stamps that cost on ``agent_run.cost`` through the lifecycle
           ``succeed_run`` helper (which already accepts the parameter
           the v0.2 stub-out reserved for this slice).
        4. Applies one increment per active budget bucket (daily +
           weekly + monthly) for the principal via
           :func:`~meho_backplane.operations.identity_budget.apply_consumption`.

        The two writes (``succeed_run`` and ``apply_consumption``) share
        the same :class:`AsyncSession` and the same commit, so the budget
        increments are atomic with the run's terminal transition: either
        both land or neither does. A failure path (``error is not None``)
        skips consumption entirely -- a failed run has no cost stamp by
        the v0.2 contract.
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
                    cost: Decimal | None = None
                    if usage is not None:
                        model_id = _full_model_id(row.provider, row.model)
                        cost = identity_budget.compute_cost(usage, model_id)
                    await run_lifecycle.succeed_run(session, row, output=output or {}, cost=cost)
                    if usage is not None and cost is not None:
                        await identity_budget.apply_consumption(
                            session,
                            tenant_id=row.tenant_id,
                            principal_sub=row.identity_sub,
                            tokens=usage.total_tokens,
                            cost=cost,
                        )
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
        *,
        meta: AgentRunAuditMeta,
        lease_owner: str,
    ) -> None:
        """Background coroutine: run the loop and record its terminal state.

        Wraps the seam's :meth:`~meho_backplane.agent.run.AgentRun.start` /
        :meth:`~meho_backplane.agent.run.AgentRun.result`; on success records
        the output, on :class:`AgentRunError` records the failure. Never
        re-raises — a failed run is a recorded ``failed`` row, not a crashed
        background task (an unhandled task exception would surface only as a
        log warning at GC time).

        A heartbeat sidecar (:func:`_heartbeat_loop`) runs alongside the
        loop for its whole lifetime (#1501): it keeps the run's lease fresh
        so the reaper does not reclaim a healthy long-running worker, and is
        cancelled in the ``finally`` once the loop terminates (success,
        failure, or cancel). If *this* task dies (worker recycle, unhandled
        crash), the sidecar dies with it -- the lease then lapses and the
        reaper drives the row to ``failed`` rather than leaving it stuck
        ``running``. The lifecycle's terminal-transition helpers already
        clear the lease, so a cleanly-finished run drops its lease before
        the sidecar is even cancelled; the cancel is belt-and-suspenders.

        Binds three contextvars around the loop, each scoped to this run:

        * :data:`~meho_backplane.agent.invoke.current_agent_run_id_var` --
          the parent id for any ``invoke_agent`` call the loop makes
          (the run-lineage key on the child ``agent_run`` row).
        * :data:`~meho_backplane.operations._audit.agent_session_id_var`
          (G11.4-T5 #1074) -- the session lineage key the dispatcher
          writes onto every per-tool-call ``audit_log`` row, so the
          G8.2-T3 #1011 reconstruct-sense replay can rebuild the
          agent's full session graph by ``agent_session_id``.
        * :data:`~meho_backplane.operations._audit.agent_run_audit_meta_var`
          (G11.4-T5 #1074) -- the model / provider / cost snapshot
          attribution-stamped onto every per-tool-call audit row's
          payload (per the C2 acceptance criteria).

        The task carries its own contextvar copy (``asyncio.create_task``
        snapshots the context), so the binds are isolated to this run.
        """
        run_token = current_agent_run_id_var.set(run_id)
        session_token = agent_session_id_var.set(run_id)
        meta_token = agent_run_audit_meta_var.set(meta)
        heartbeat = asyncio.create_task(
            _heartbeat_loop(run_id, lease_owner),
            name=f"agent-heartbeat-{run_id}",
        )
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
            usage = TokenUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
            )
            await self._finalize_run(
                run_id,
                output=_project_output(result.output),
                error=None,
                usage=usage,
            )
        finally:
            # Stop the heartbeat sidecar before resetting the contextvars:
            # the run has reached its terminal state (or is being cancelled),
            # so the lease no longer needs extending. Awaiting the cancel
            # keeps a stray "Task was destroyed but it is pending!" warning
            # from surfacing at GC under pytest-asyncio shutdown.
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            agent_run_audit_meta_var.reset(meta_token)
            agent_session_id_var.reset(session_token)
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
            BudgetExceededError: the per-identity / per-tenant /
                global pre-execution budget gate refused this run
                (G11.5-T6 #1080).
        """
        entry = await self._load_definition(operator, name)
        definition = self._to_agent_definition(entry)
        # G11.5-T6 #1080 — pre-execution budget gate. Refused runs
        # short-circuit *before* the durable row is created so a
        # kill-switched deploy doesn't fill the runs table with
        # ``failed`` rows on every retry. A degraded tier comes back as
        # a modified definition copy; the runtime picks the cheaper
        # backend via its normal resolver path.
        definition = await self._enforce_pre_run_budget(operator, definition)
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
            run_id, lease_owner = await self._create_run_row(
                operator, entry, provider=provider, model=model
            )
            task = self._launch_run(
                run_id,
                definition,
                operator,
                inputs,
                meta=AgentRunAuditMeta(model=model, provider=provider),
                lease_owner=lease_owner,
            )

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

    async def _authenticate_scheduled_agent(
        self,
        name: str,
        *,
        agent_client_id: str,
        agent_client_secret: SecretStr,
        settings: Settings,
    ) -> tuple[Operator, AgentDefinitionRead]:
        """Authenticate the agent's own identity and resolve its owned definition.

        Obtains a ``client_credentials`` token for *agent_client_id*, verifies
        the issued JWT, loads the named definition, and asserts the
        authenticated client owns it. Returns the verified ``operator`` and the
        ``entry`` for the caller to launch. Split out of :meth:`run_scheduled`
        so that method stays focused on the run-lifecycle (create row → launch
        → bounded wait) rather than the auth-and-bind preamble.

        ``agent_client_secret`` is a :class:`~pydantic.SecretStr` so it can
        never be rendered into a log line as a frame local on any exception
        path -- the value is unwrapped via ``.get_secret_value()`` only at
        the single :func:`get_client_credentials_token` call below.

        Raises:
            AgentTokenError: the ``client_credentials`` grant failed.
            AgentInvocationError: the authenticated client does not own the
                named definition (cross-agent launch refused).
            AgentNotFoundError / AgentDisabledError: no enabled definition
                named *name* in the agent's tenant.
        """
        # Request the audience we then verify, so the token carries it even on
        # realms without a default audience mapper.
        token = await get_client_credentials_token(
            issuer_url=str(settings.keycloak_issuer_url),
            client_id=agent_client_id,
            client_secret=agent_client_secret.get_secret_value(),
            audience=settings.keycloak_audience,
        )
        operator = await verify_jwt_for_audience(
            f"Bearer {token}",
            expected_audience=settings.keycloak_audience,
        )
        entry = await self._load_definition(operator, name)
        # Bind the run to the authenticating agent: the definition's
        # ``identity_ref`` must name the client whose credentials authenticated
        # this call (``agent_client_id`` — the grant only succeeds if its secret
        # matches, so it is proven). Without this, agent A's credentials could
        # launch agent B's definition (any enabled one in the tenant) and
        # misattribute the audit trail. ``identity_ref`` is the ``agent:<name>``
        # client-id reference set at definition-create time, so the comparison
        # is against ``agent_client_id`` — not ``operator.sub`` (the service-
        # account UUID), which lives in a different identifier space.
        if entry.identity_ref != agent_client_id:
            raise AgentInvocationError(
                f"scheduled run rejected: agent credentials for {agent_client_id!r} "
                f"do not own definition {name!r} (identity_ref={entry.identity_ref!r})"
            )
        return operator, entry

    async def run_scheduled(
        self,
        name: str,
        inputs: str,
        *,
        agent_client_id: str,
        agent_client_secret: SecretStr,
        work_ref: str | None = None,
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
        audit-shape seam it calls. The wait on the loop is **bounded** by
        :attr:`Settings.agent_sync_timeout_seconds` (mirroring :meth:`run`):
        a run still executing at the deadline keeps going in the background
        (it is shielded from the wait's cancellation) and the call returns a
        :class:`AgentRunOutcome` flagged ``converted_to_async``. Without this
        bound a single hung or approval-gated run would block the serial
        scheduler tick — and strand the tick's advisory lock — until a pod
        restart (#1502); the lifecycle/reaper, not this wait, owns reclaiming
        an abandoned background run.

        Authentication + identity binding (token grant, JWT verify,
        owns-definition guard) is delegated to
        :meth:`_authenticate_scheduled_agent`.

        work_ref I3-T3 (#1663): when the firing trigger carries a
        *work_ref* (its change-ticket reference), it is bound onto the
        shared
        :data:`~meho_backplane.operations._audit.work_ref_var` ContextVar
        for the duration of this call. :meth:`_create_run_row` reads that
        ContextVar at run-create time, so the dispatched
        ``agent_run.work_ref`` lands the trigger's ref; the background
        loop task snapshots the ContextVar at
        :func:`asyncio.create_task` time (in :meth:`_launch_run`), so
        every per-tool-call audit row the run produces inherits it too.
        ``None`` leaves the binding untouched (the run lands ``NULL``
        work_ref, the pre-#1663 behaviour). This is the inheritance hop
        the Initiative #1654 builds across the previously-severed
        trigger -> dispatched-run seam.

        A trigger fired with no usable user prompt (empty / whitespace-only
        *inputs*, the common cause being a trigger created without
        ``inputs``) does **not** reach the model: the run row is finalised
        ``failed`` with a :data:`~meho_backplane.agent.run.SCHEDULED_RUN_NO_INPUT_CLASS`-tagged
        error and the returned :class:`AgentRunOutcome` carries
        ``status=FAILED`` (#1505). This is a returned terminal outcome, not
        a raised exception -- the scheduler treats it as a fired-but-failed
        trigger (a permanent misconfiguration), not a transient retry.

        Raises:
            AgentTokenError: the ``client_credentials`` grant failed.
            AgentInvocationError: the authenticated client does not own the
                named definition.
            AgentNotFoundError / AgentDisabledError: no enabled definition
                named *name* in the agent's tenant.
        """
        settings = get_settings()
        operator, entry = await self._authenticate_scheduled_agent(
            name,
            agent_client_id=agent_client_id,
            agent_client_secret=agent_client_secret,
            settings=settings,
        )
        # work_ref I3-T3 #1663: bind the firing trigger's change-ticket ref
        # onto the shared ContextVar so the dispatched run inherits it.
        # _create_run_row reads it at create time (-> agent_run.work_ref);
        # the background loop task snapshots it at create_task time (->
        # the run's audit rows). A blank/None ref binds nothing -- the run
        # lands NULL work_ref (the pre-#1663 behaviour).
        cleaned_work_ref = work_ref.strip() if work_ref else None
        work_ref_token = work_ref_var.set(cleaned_work_ref) if cleaned_work_ref else None
        try:
            return await self._launch_scheduled_run(
                name, inputs, operator=operator, entry=entry, settings=settings
            )
        finally:
            if work_ref_token is not None:
                work_ref_var.reset(work_ref_token)

    async def _refuse_scheduled_no_input(
        self,
        run_id: uuid.UUID,
        name: str,
        operator: Operator,
    ) -> AgentRunOutcome:
        """Finalise an already-created scheduled run ``failed`` for no input (#1505).

        The run row exists (so the refusal is auditable in the runs table)
        but the loop is never launched: the row is finalised ``failed``
        with a :data:`~meho_backplane.agent.run.SCHEDULED_RUN_NO_INPUT_CLASS`-tagged
        error and no model call is made. Returns the terminal
        ``status=FAILED`` outcome the scheduler surfaces as
        ``scheduler_fired_run_failed``.
        """
        error = str(ScheduledRunNoInputError(agent=name))
        _log.warning(
            "agent_scheduled_no_input",
            run_id=str(run_id),
            agent=name,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        await self._finalize_run(run_id, output=None, error=error)
        return AgentRunOutcome(
            run_id=run_id,
            status=AgentRunStatus.FAILED,
            error=error,
        )

    async def _launch_scheduled_run(
        self,
        name: str,
        inputs: str,
        *,
        operator: Operator,
        entry: AgentDefinitionRead,
        settings: Settings,
    ) -> AgentRunOutcome:
        """Budget-gate, persist, launch, and bounded-wait a scheduled run.

        The run-lifecycle half of :meth:`run_scheduled`, kept separate from the
        auth-and-bind preamble. The wait on the background loop is bounded by
        ``settings.agent_sync_timeout_seconds``; on timeout the still-running
        handle is returned (``converted_to_async``) so the serial scheduler tick
        is not blocked and its advisory lock is released each cadence (#1502).
        """
        definition = self._to_agent_definition(entry)
        # G11.5-T6 #1080 — same pre-execution gate as :meth:`run`.
        # A scheduler-fired run is the most common cost-runaway shape
        # (a misconfigured cron firing every minute), so the gate is
        # critical here even when the operator is a service account
        # rather than a human.
        definition = await self._enforce_pre_run_budget(operator, definition)
        provider, model = _split_model_id(settings.agent_default_model)
        run_id, lease_owner = await self._create_run_row(
            operator,
            entry,
            provider=provider,
            model=model,
            trigger=AgentRunTrigger.SCHEDULED,
        )
        # No-input guard (#1505): a scheduled trigger fired with no usable
        # user prompt (the common cause: created without ``inputs``) would
        # reach the provider as a system-prompt-only request with an empty
        # ``messages`` array and come back as an opaque provider 400. Fail
        # the run typed *before* launching the doomed loop. The row is
        # created first so the refusal is visible in the runs table the
        # same as any other terminal scheduled run.
        if prompt_is_effectively_empty(inputs):
            return await self._refuse_scheduled_no_input(run_id, name, operator)
        # No actor_delegation: the agent is the subject, not an actor on behalf
        # of a human, so actor_sub stays NULL.
        task = self._launch_run(
            run_id,
            definition,
            operator,
            inputs,
            meta=AgentRunAuditMeta(model=model, provider=provider),
            lease_owner=lease_owner,
        )
        _log.info(
            "agent_scheduled_started",
            run_id=str(run_id),
            agent=name,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        try:
            # Bound the wait so a hung/approval-gated run cannot block the
            # serial scheduler tick (and strand its advisory lock) until a
            # pod restart (#1502). Shield so the timeout abandons the *wait*,
            # not the run — the loop keeps going in the background and stays
            # pollable / reapable; the reaper owns reclaiming an abandoned run.
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=settings.agent_sync_timeout_seconds,
            )
        except TimeoutError:
            _log.info(
                "agent_scheduled_converted_to_async",
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

    def _launch_run(
        self,
        run_id: uuid.UUID,
        definition: AgentDefinition,
        operator: Operator,
        inputs: str,
        *,
        meta: AgentRunAuditMeta,
        lease_owner: str,
    ) -> asyncio.Task[None]:
        """Launch the loop as a background task anchored in the run store.

        The store holds a strong reference so the task is not GC'd
        mid-flight (asyncio weakly references bare tasks) and survives the
        request that created it; a done-callback evicts the entry on
        completion so a long-lived worker does not accumulate finished runs.

        *meta* is the agent-run audit snapshot threaded into every
        per-tool-call audit row's payload (G11.4-T5 #1074) -- carried
        into the background task via
        :data:`~meho_backplane.operations._audit.agent_run_audit_meta_var`
        in :meth:`_run_loop_to_completion`. *lease_owner* is the
        per-process stamp :meth:`_create_run_row` wrote with the lease;
        the loop's heartbeat sidecar reuses it (#1501).
        """
        task = asyncio.create_task(
            self._run_loop_to_completion(
                run_id,
                definition,
                operator,
                inputs,
                meta=meta,
                lease_owner=lease_owner,
            ),
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

    async def list_runs(
        self,
        operator: Operator,
        *,
        work_ref: str | None = None,
        status: AgentRunStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentRunSummary]:
        """List the operator's tenant's runs, newest first (work_ref I3-T2).

        Reads the durable ``agent_run`` rows, tenant-isolated to the
        operator's tenant. ``work_ref`` (when not ``None``) narrows to
        runs whose external change-ticket reference matches exactly;
        ``status`` (when not ``None``) narrows to one lifecycle state.
        The slice is bounded server-side -- the operation clamps the page
        size -- so a single call never scans an unbounded table.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = await run_lifecycle.list_runs(
                session,
                tenant_id=operator.tenant_id,
                work_ref=work_ref,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [_row_to_summary(r) for r in rows]

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
            BudgetExceededError: the per-identity / per-tenant /
                global pre-execution budget gate refused this run
                (G11.5-T6 #1080). The boundary maps it to a 4xx
                response before the SSE connection is opened.
        """
        entry = await self._load_definition(operator, name)
        definition = self._to_agent_definition(entry)
        # G11.5-T6 #1080 — pre-execution budget gate. Raised
        # *before* the durable row is created so the SSE consumer
        # gets a clean 4xx rather than a stream that opens and
        # immediately emits a terminal ERROR event (which an
        # ``EventSource`` client would auto-reconnect into).
        definition = await self._enforce_pre_run_budget(operator, definition)
        settings = get_settings()
        provider, model = _split_model_id(settings.agent_default_model)
        run_id, lease_owner = await self._create_run_row(
            operator, entry, provider=provider, model=model
        )

        terminal_output: dict[str, object] | None = None
        terminal_error: str | None = None
        # Bind the lineage contextvar so an ``invoke_agent`` call inside the
        # streamed run records its child against this run (G11.1-T7 #1067). The
        # stream runs inline in the SSE response coroutine, so the token reset
        # in ``finally`` keeps the bind from leaking past the stream.
        #
        # G11.4-T5 #1074 -- also bind the session id + audit meta
        # contextvars so the dispatcher's per-tool-call audit rows
        # carry the run's lineage key + the model/provider snapshot.
        # Same finally-token discipline; identical inline scope.
        run_token = current_agent_run_id_var.set(run_id)
        session_token = agent_session_id_var.set(run_id)
        meta_token = agent_run_audit_meta_var.set(
            AgentRunAuditMeta(model=model, provider=provider),
        )
        # Heartbeat sidecar (#1501): the streamed run executes inline in the
        # SSE coroutine, so a hung tool call or a slow model would let the
        # lease lapse and the reaper reclaim a live stream. Keep the lease
        # fresh for the stream's lifetime; cancel it once the stream
        # terminates (final/error event, client disconnect, cancel).
        heartbeat = asyncio.create_task(
            _heartbeat_loop(run_id, lease_owner),
            name=f"agent-heartbeat-{run_id}",
        )
        try:
            async for event in self._runtime.stream_events(definition, operator, inputs, run_id):
                if event.kind is AgentRunEventKind.FINAL:
                    terminal_output = _project_output(event.data.get("output"))
                elif event.kind is AgentRunEventKind.ERROR:
                    terminal_error = str(event.data.get("error"))
                yield run_id, event
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            agent_run_audit_meta_var.reset(meta_token)
            agent_session_id_var.reset(session_token)
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
) -> tuple[uuid.UUID, str]:
    """Persist a child ``agent_run`` row linked to its parent; return ``(id, owner)``.

    The :class:`~meho_backplane.agent.invoke.ChildRunRecorder` the live invoker
    injects (G11.1-T7 #1067). The seam value object carries neither the
    persisted ``agent_definition_id`` nor the ``model_tier`` the run row
    records, so the definition is re-resolved by ``(tenant, name)`` to recover
    them; the row is then created with ``trigger=agent-invoked`` + the parent
    linkage and transitioned to ``running``, committed in its own transaction
    so the child run is inspectable while it executes (mirrors
    :meth:`AgentInvoker._create_run_row`).

    Like the top-level path (#1501), the row is leased in the same
    transaction as the ``running`` transition; the lease owner is returned so
    the ``invoke_agent`` tool can heartbeat the child for its loop's lifetime
    and the reaper can reclaim a child whose worker died mid-flight.

    work_ref I3-T2 (#1662): the child run inherits the parent's external
    change-ticket reference off the shared
    :data:`~meho_backplane.operations._audit.work_ref_var` ContextVar -- the
    same boundary the top-level path reads in
    :meth:`AgentInvoker._create_run_row`. Children are recorded inside the
    parent's ``invoker.run`` call, so the var is still bound at child-creation
    time and the child lands the same ``work_ref`` as its parent instead of
    ``None``.

    The row's *terminal* state is closed by the companion
    :func:`_finalize_child_run` (G11.1-T8 #1087) after the child loop returns,
    so the child reaches ``succeeded`` / ``failed``. A definition deleted
    between resolution and recording raises
    :class:`~meho_backplane.agent.run.AgentRunError`, surfaced by
    ``invoke_agent`` as a ``ModelRetry``.
    """
    service = AgentDefinitionService()
    entry = await service.get(operator.tenant_id, definition.name)
    if entry is None:
        raise AgentRunError(f"agent definition {definition.name!r} no longer exists")
    settings = get_settings()
    provider, model = _split_model_id(settings.agent_default_model)
    owner = _lease_owner()
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
            work_ref=work_ref_var.get(),
        )
        await run_lifecycle.start_run(session, row, provider=provider, model=model)
        await run_lifecycle.claim_lease(
            session,
            row,
            owner=owner,
            ttl_seconds=settings.agent_run_lease_ttl_seconds,
        )
        child_run_id = row.id
        await session.commit()
    return child_run_id, owner


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


def _row_to_summary(row: AgentRunRow) -> AgentRunSummary:
    """Project an ``agent_run`` row onto the list-row summary (no output blob)."""
    return AgentRunSummary(
        run_id=row.id,
        status=AgentRunStatus(row.status),
        trigger=row.trigger,
        model_tier=row.model_tier,
        provider=row.provider,
        model=row.model,
        turns=row.turns,
        work_ref=row.work_ref,
        created_at=row.created_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
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
