# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agent-run record lifecycle + cancellation service.

Initiative #802 (G11.1 Agent runtime), Task #813 (T6). The runtime hosts
an LLM tool-use loop in MEHO's process (G11.1-T1); each invocation is one
durable ``agent_run`` row (:class:`meho_backplane.db.models.AgentRun`).
This module owns the **lifecycle**: creating the row, walking its
``status`` through an explicit, enforced state machine, recording turns /
output / failure, and the operator-authorized cancellation path. The row
id doubles as the ``agent_session_id`` lineage key G11.4/C2 binds into
per-tool-call audit rows; this service hands the caller that id at create
time.

Why an explicit state machine
------------------------------

The ``status`` column is a closed enum
(:class:`~meho_backplane.db.models.AgentRunStatus`) backed by a DB
``CHECK`` constraint, but a ``CHECK`` only enforces the *set* of legal
values -- not the legal *transitions* between them. Without a transition
guard, a bug in the runtime could write ``succeeded`` -> ``running`` (a
finished run "restarting") or ``cancelled`` -> ``succeeded`` (a cancelled
run reporting success), corrupting the audit lineage and any cost / replay
view built on top. :data:`ALLOWED_TRANSITIONS` is the single source of
truth for the legal edges; :func:`transition` rejects every edge not on
the map with :class:`IllegalTransitionError` *before* it touches the DB.

The state machine::

    pending ──> running ──> succeeded   (terminal)
       │           │  ▲         │
       │           │  │         └─ output recorded
       │           ▼  │
       │     awaiting_approval          (resumable: ──> running)
       │           │  │
       │           ▼  ▼
       ├────────> failed                (terminal)
       │
       └──┐
          ▼
       cancelled                        (terminal; from any
                                         non-terminal state, by an
                                         authorized operator)

The four non-terminal states (``pending``, ``running``,
``awaiting_approval``) can all be cancelled; the three terminal states
(``succeeded``, ``failed``, ``cancelled``) accept no further transition.

Transaction discipline
-----------------------

Every mutating function takes an open
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, flushes its changes, and
returns -- the **caller** owns the commit (the same contract
:mod:`meho_backplane.ui.auth.session_store` follows). This lets the
runtime compose a status transition with other writes (e.g. the audit row
for the cancellation) inside one transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import select, update
from sqlalchemy.engine.cursor import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    ScheduledTriggerInFlightPolicy,
)
from meho_backplane.events.outbox import publish as publish_event

#: Event kind the agent-run terminal-transition emits onto the outbox.
#: Subscribers (``scheduled_trigger`` rows of ``kind='event'``) match
#: against this discriminator. v0.2 ships the producer; the
#: subscription matcher follows in T5 #826's admin surface. The
#: ``<resource>.<action>`` shape mirrors the audit-trail convention.
AGENT_RUN_COMPLETED_EVENT_KIND: Final[str] = "agent_run.completed"

__all__ = [
    "AGENT_RUN_COMPLETED_EVENT_KIND",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "AgentRunError",
    "AgentRunNotFoundError",
    "IllegalTransitionError",
    "LeaseLostError",
    "UnauthorizedCancellationError",
    "cancel_run",
    "claim_lease",
    "create_run",
    "fail_run",
    "get_run",
    "heartbeat",
    "increment_turns",
    "list_runs",
    "release_lease",
    "snapshot_in_flight_policy",
    "start_run",
    "succeed_run",
    "transition",
]


#: The terminal lifecycle states -- a run in any of these accepts no
#: further transition. Derived once so :func:`transition`,
#: :func:`cancel_run`, and the read-side "is this run still active?"
#: checks all agree.
TERMINAL_STATUSES: Final[frozenset[AgentRunStatus]] = frozenset(
    {
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }
)


#: The single source of truth for legal ``status`` edges. Maps each
#: state to the set of states it may transition *to*. Terminal states
#: map to an empty set. :func:`transition` consults this map and rejects
#: any edge not present, so an illegal jump never reaches the DB.
#:
#: Cancellation is modelled as an ordinary edge here (every non-terminal
#: state -> ``cancelled``); :func:`cancel_run` layers the operator
#: authorization check on top of the plain :func:`transition` it calls.
ALLOWED_TRANSITIONS: Final[dict[AgentRunStatus, frozenset[AgentRunStatus]]] = {
    AgentRunStatus.PENDING: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.AWAITING_APPROVAL,
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.AWAITING_APPROVAL: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.SUCCEEDED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.CANCELLED: frozenset(),
}


#: Minimum tenant role authorized to cancel a running agent run.
#: Cancelling in-flight work is a control action, not a read -- a
#: ``read_only`` operator must not be able to stop another principal's
#: run. ``OPERATOR`` (and, by the linear ranking, ``TENANT_ADMIN``)
#: clears the gate; the same ranking
#: :mod:`meho_backplane.auth.rbac` uses for HTTP routes.
_MIN_CANCEL_ROLE: Final[TenantRole] = TenantRole.OPERATOR

#: Linear role ranking -- index = rank. Mirrors
#: :data:`meho_backplane.auth.rbac._ROLE_ORDER`; duplicated rather than
#: imported because that one is a private module-level constant and the
#: service layer must not depend on the HTTP-RBAC module's internals.
_ROLE_RANK: Final[tuple[TenantRole, ...]] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)


class AgentRunError(Exception):
    """Base class for agent-run lifecycle failures."""


class AgentRunNotFoundError(AgentRunError):
    """No ``agent_run`` row exists for the requested id.

    Raised by :func:`cancel_run` (and any future mutate-by-id helper)
    when the id does not resolve. The caller maps it to a 404; the
    service does not silently no-op so a cancel against a typo'd /
    cross-tenant id surfaces rather than appearing to succeed.
    """

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(f"no agent_run row for id {run_id}")


class IllegalTransitionError(AgentRunError):
    """A requested ``status`` transition is not on :data:`ALLOWED_TRANSITIONS`.

    Raised by :func:`transition` before any DB write. Carries the
    ``from``/``to`` pair so the caller's error response (and the audit
    trail) can name the rejected edge precisely.
    """

    def __init__(self, *, from_status: AgentRunStatus, to_status: AgentRunStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"illegal agent_run transition {from_status.value!r} -> {to_status.value!r}"
        )


class UnauthorizedCancellationError(AgentRunError):
    """The operator lacks the role required to cancel a run.

    Raised by :func:`cancel_run` when ``operator.tenant_role`` ranks
    below :data:`_MIN_CANCEL_ROLE`. Distinct from
    :class:`IllegalTransitionError` (which is about run *state*) so the
    caller can map authorization failures to 403 and state failures to
    409.
    """

    def __init__(self, *, operator_sub: str, role: TenantRole) -> None:
        self.operator_sub = operator_sub
        self.role = role
        super().__init__(
            f"operator {operator_sub!r} with role {role.value!r} may not cancel an agent run "
            f"(requires at least {_MIN_CANCEL_ROLE.value!r})"
        )


class LeaseLostError(AgentRunError):
    """The lease this worker thought it held has been reassigned.

    Initiative #804 (G11.3 Scheduler), Task #825 (T4). Raised by
    :func:`heartbeat` when the conditional update touches zero rows --
    the row's ``lease_owner`` no longer matches the heartbeating
    worker (the reaper or another claimer has taken over) or the row
    has reached a terminal status while the worker was off-CPU. The
    worker must stop its work immediately on this signal: any further
    side-effects would be at-least-twice (the reaper has handed the
    work to someone else or recorded it as failed).

    Distinct from :class:`IllegalTransitionError` (which is about
    *state*) so the runtime can map this to a clean abort path rather
    than a 409: a lost lease is a coordination event, not a caller
    bug.
    """

    def __init__(self, *, run_id: uuid.UUID, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        super().__init__(
            f"agent_run {run_id} lease no longer held by {owner!r} "
            f"(reaper reclaimed or run terminated)"
        )


def _coerce_status(value: AgentRunStatus | str) -> AgentRunStatus:
    """Normalise a status to :class:`AgentRunStatus`.

    ``AgentRun.status`` is stored as ``str`` (the column type), so a row
    read back from the DB carries the string value. Callers may pass
    either the enum or the raw string; this coerces to the enum so the
    transition lookup is type-safe. An unknown value raises
    :class:`ValueError` -- a row whose stored status is outside the
    closed enum is a corruption the service must not paper over.
    """
    if isinstance(value, AgentRunStatus):
        return value
    return AgentRunStatus(value)


async def create_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity_sub: str,
    trigger: AgentRunTrigger,
    model_tier: str,
    identity_act: str | None = None,
    agent_definition_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    work_ref: str | None = None,
) -> AgentRun:
    """Insert a fresh ``agent_run`` row in the ``pending`` state.

    Called by the invocation surface (G11.1-T4) at the start of a run.
    The returned row's :attr:`AgentRun.id` is the ``agent_session_id``
    lineage key the caller threads through the runtime so every
    per-tool-call audit row (G11.4/C2) shares it.

    Args:
        session: Open :class:`AsyncSession`. The function flushes (so
            ``id`` / ``created_at`` / defaults are populated) but does
            not commit -- the caller's transaction owns the commit.
        tenant_id: The tenant the run belongs to (real FK to
            ``tenant.id``).
        identity_sub: RFC 8693 ``sub`` -- the principal the agent acts
            for. Required.
        trigger: What initiated the run (:class:`AgentRunTrigger`).
        model_tier: The logical model tier requested; the resolver
            (G11.5) maps it to a concrete provider + model later.
        identity_act: RFC 8693 ``act`` -- the agent principal acting on
            the subject's behalf. ``None`` for a direct human run.
        agent_definition_id: The ``agent_definition`` row the run
            executes (soft-FK; ``None`` for an ad-hoc run).
        parent_run_id: The parent run's id when this is an
            agent-invoked child (G11.1-T5); ``None`` otherwise.
        work_ref: The external change-ticket reference the run works
            under (work_ref I3-T2 #1662) -- an opaque cross-system
            string (``"gh:evoila/meho#11"`` / a Jira key / a CR id),
            normally the request-time
            :data:`meho_backplane.operations._audit.work_ref_var`
            binding. ``None`` (the default) when no ticket is bound.
            Set-at-create-only -- the lifecycle never re-writes it.
            Distinct from the run's own ``id``.

    Returns:
        The inserted :class:`AgentRun`, flushed so its server / ORM
        defaults are populated.
    """
    row = AgentRun(
        id=uuid.uuid4(),
        agent_definition_id=agent_definition_id,
        tenant_id=tenant_id,
        identity_sub=identity_sub,
        identity_act=identity_act,
        trigger=trigger.value,
        model_tier=model_tier,
        status=AgentRunStatus.PENDING.value,
        turns=0,
        parent_run_id=parent_run_id,
        work_ref=work_ref,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> AgentRun | None:
    """Load an ``agent_run`` row by id, or ``None`` if absent.

    The read side of the inspection surface (status, turns, model +
    provider, output). Returns ``None`` rather than raising so the
    caller can shape a 404 itself; mutate-by-id helpers
    (:func:`cancel_run`) raise :class:`AgentRunNotFoundError` instead,
    because a missing row on a *write* is an error the service must
    surface.
    """
    return await session.get(AgentRun, run_id)


#: Server-side cap on the agent-run list page size -- mirrors the bounded
#: paging discipline of the runbook-run / approval list surfaces so a
#: single request can never scan an unbounded slice of the table.
_LIST_RUNS_MAX_LIMIT: Final[int] = 500
_LIST_RUNS_DEFAULT_LIMIT: Final[int] = 100


async def list_runs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    work_ref: str | None = None,
    status: AgentRunStatus | None = None,
    agent_definition_id: uuid.UUID | None = None,
    limit: int = _LIST_RUNS_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[AgentRun]:
    """Page through agent runs in *tenant_id*, newest first.

    The read substrate for the agent-run list surface (work_ref I3-T2
    #1662) -- ``GET /api/v1/agents/runs``, ``meho.agents.list_runs``, and
    ``meho agent run-list``. Tenant-isolated by the WHERE clause:
    cross-tenant rows are invisible, so the list cannot leak another
    tenant's runs.

    Args:
        session: Open :class:`AsyncSession` (read-only; no commit).
        tenant_id: The tenant whose runs to list.
        work_ref: When supplied, narrows to runs whose ``work_ref``
            matches this external change ticket exactly
            (``"gh:evoila/meho#11"``). ``None`` applies no work_ref
            filter (runs with and without a ticket are returned). An
            empty string is a legitimate exact-match value, distinct
            from ``None`` -- so the filter is applied whenever the
            argument is not ``None``.
        status: When supplied, narrows to runs in this lifecycle state.
            ``None`` returns every state.
        agent_definition_id: When supplied, narrows to runs produced by
            this agent definition (matched against the run row's soft-FK
            ``agent_definition_id``). ``None`` applies no agent filter.
            The name-to-id resolution lives at the caller
            (:meth:`AgentInvoker.list_runs`); this operation filters on
            the resolved id so an unknown name never reaches the DB.
        limit: Max rows per page. Clamped to ``[1, 500]``.
        offset: Rows to skip (paging). Negative offsets are clamped to 0.

    Returns:
        The matching :class:`AgentRun` rows ordered ``created_at DESC``.
    """
    bounded_limit = max(1, min(limit, _LIST_RUNS_MAX_LIMIT))
    bounded_offset = max(0, offset)
    stmt = select(AgentRun).where(AgentRun.tenant_id == tenant_id)
    if work_ref is not None:
        stmt = stmt.where(AgentRun.work_ref == work_ref)
    if status is not None:
        stmt = stmt.where(AgentRun.status == status.value)
    if agent_definition_id is not None:
        stmt = stmt.where(AgentRun.agent_definition_id == agent_definition_id)
    stmt = stmt.order_by(AgentRun.created_at.desc()).limit(bounded_limit).offset(bounded_offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def resolve_agent_definition_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
) -> uuid.UUID | None:
    """Resolve an agent definition name to its id within *tenant_id*.

    Backs the ``agent_name`` filter on the run-list surfaces: the caller
    resolves the name to an id here, then passes the id to
    :func:`list_runs`. Returns ``None`` when no definition in the tenant
    carries *name* (an unknown name), so the filter yields an empty run
    list rather than an error and cannot probe definition existence
    beyond what the run list already reveals. The ``(tenant_id, name)``
    lookup rides the unique ``agent_definition_tenant_name_idx``.
    """
    result = await session.execute(
        select(AgentDefinition.id).where(
            AgentDefinition.tenant_id == tenant_id,
            AgentDefinition.name == name,
        )
    )
    return result.scalar_one_or_none()


async def resolve_agent_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    definition_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Batch-resolve agent definition ids to their names within *tenant_id*.

    The read-time back-fill for the run projections: a run row carries only
    the soft-FK ``agent_definition_id`` (no denormalized name), so the list
    and status surfaces resolve names here in one query rather than per-row.
    Ids that match no live definition (an ad-hoc run's ``None`` is filtered
    by the caller before it gets here; a dangling soft-FK after the
    definition was deleted) are simply absent from the returned mapping, so
    the projection renders ``agent_name=None`` for them.
    """
    ids = set(definition_ids)
    if not ids:
        return {}
    result = await session.execute(
        select(AgentDefinition.id, AgentDefinition.name).where(
            AgentDefinition.tenant_id == tenant_id,
            AgentDefinition.id.in_(ids),
        )
    )
    return dict(result.tuples().all())


async def transition(
    session: AsyncSession,
    row: AgentRun,
    to_status: AgentRunStatus,
) -> AgentRun:
    """Move *row* to *to_status*, enforcing the legal state machine.

    The single mutation point for ``status``. Looks up the current
    status's legal successors in :data:`ALLOWED_TRANSITIONS` and raises
    :class:`IllegalTransitionError` if *to_status* is not among them --
    before any DB write, so an illegal edge never lands. Stamps
    ``started_at`` on the first move into ``running`` and ``ended_at``
    on any move into a terminal state, so the timestamps are a
    by-product of the transition rather than a separate caller
    responsibility.

    Higher-level helpers (:func:`start_run`, :func:`succeed_run`,
    :func:`fail_run`, :func:`cancel_run`) wrap this with the
    state-specific side effects (output, error, authorization); call
    this directly only for transitions without extra payload (e.g.
    ``running`` -> ``awaiting_approval`` and back).

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The attached :class:`AgentRun` to mutate.
        to_status: The desired next status.

    Returns:
        The same *row*, mutated and flushed.

    Raises:
        IllegalTransitionError: *to_status* is not a legal successor of
            the row's current status.
    """
    from_status = _coerce_status(row.status)
    if to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise IllegalTransitionError(from_status=from_status, to_status=to_status)

    now = datetime.now(UTC)
    # Stamp started_at only on the first entry into running. The
    # awaiting_approval -> running resume re-enters running but must not
    # reset the original start time, so guard on the column being unset.
    if to_status is AgentRunStatus.RUNNING and row.started_at is None:
        row.started_at = now
    if to_status in TERMINAL_STATUSES:
        row.ended_at = now
        # T4 #825 -- clear the lease on terminal transitions so the
        # ``agent_run_lease_expires_at_idx`` does not retain stale
        # metadata, and so a future reader sees "no worker holds
        # this" rather than "the worker that ran this once held a
        # lease here". The columns are nullable; clearing is
        # idempotent.
        row.lease_owner = None
        row.lease_expires_at = None

    row.status = to_status.value
    await session.flush()

    # G11.3-T3 #824: emit ``agent_run.completed`` onto the transactional
    # outbox in the same session as the status write. The outbox row
    # commits with the status change so a producer rollback discards
    # both. Subscribers (``scheduled_trigger`` rows of ``kind='event'``)
    # consume the event via the drain loop; v0.2 ships the producer
    # only -- the subscription matcher lands in T5 #826's admin
    # surface follow-up. The payload carries the fields a subscriber's
    # JSONB filter needs to match against: the run id (so a
    # cheap-to-deep escalation pattern can fire the next agent against
    # a specific prior run), the tenant id (subscribers are
    # tenant-scoped), the terminal status (subscribers filter on
    # success / failure), and the agent definition id (subscribers
    # can target a specific upstream agent).
    if to_status in TERMINAL_STATUSES:
        await publish_event(
            session,
            tenant_id=row.tenant_id,
            event_kind=AGENT_RUN_COMPLETED_EVENT_KIND,
            payload={
                "run_id": str(row.id),
                "tenant_id": str(row.tenant_id),
                "status": to_status.value,
                "agent_definition_id": (
                    str(row.agent_definition_id) if row.agent_definition_id is not None else None
                ),
                # work_ref I3-T2 #1662: the change ticket the run worked
                # under, so a subscriber's JSONB filter can route the
                # follow-up against runs of a specific change record.
                # NULL when the run carried no ticket.
                "work_ref": row.work_ref,
            },
        )

    return row


async def start_run(
    session: AsyncSession,
    row: AgentRun,
    *,
    provider: str,
    model: str,
) -> AgentRun:
    """Transition a ``pending`` run to ``running`` and record the resolved model.

    Called by the runtime once the multi-provider resolver (G11.5) has
    mapped the logical ``model_tier`` to a concrete provider + model.
    Records both before the transition so a reader that observes the run
    as ``running`` always sees the resolved pair.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The ``pending`` :class:`AgentRun`.
        provider: The resolved provider (e.g. ``anthropic``).
        model: The resolved model id.

    Returns:
        The mutated, flushed row.

    Raises:
        IllegalTransitionError: The row is not in a state from which
            ``running`` is reachable.
    """
    row.provider = provider
    row.model = model
    return await transition(session, row, AgentRunStatus.RUNNING)


async def claim_lease(
    session: AsyncSession,
    row: AgentRun,
    *,
    owner: str,
    ttl_seconds: int,
) -> AgentRun:
    """Stamp a lease on *row* and record the owning worker.

    Initiative #804 (G11.3 Scheduler), Task #825 (T4). Called by the
    trigger-firing path (T2 #823 / T3 #824) when a worker begins
    executing a run; called by the reaper's ``resume`` policy when it
    re-dispatches a run whose previous worker died. The lease + the
    owner are written together so a reader that observes the lease
    always sees who holds it.

    Storage discipline
    ------------------

    The lease columns are pure side-effect: they do not change
    ``status``. The caller threads :func:`claim_lease` and
    :func:`start_run` (which transitions ``pending`` -> ``running``)
    together inside the same transaction so a partial commit cannot
    leave the row ``running`` without a lease (or with a lease but
    still ``pending``).

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The :class:`AgentRun` to claim.
        owner: A stable identifier for the worker (e.g. pod name +
            pid). The reaper uses this for diagnostics; the *expiry*
            column drives reclaim.
        ttl_seconds: Wall-clock seconds the lease is valid for. The
            worker must heartbeat within this window or the reaper
            will reclaim the row. Typical values: 60-180s, with a
            heartbeat at ~1/2 the TTL.

    Returns:
        The mutated, flushed row with ``lease_owner`` /
        ``lease_expires_at`` populated.
    """
    row.lease_owner = owner
    row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    await session.flush()
    return row


async def heartbeat(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    owner: str,
    ttl_seconds: int,
) -> AgentRun:
    """Extend the lease on *run_id* iff this worker still holds it.

    Initiative #804 (G11.3 Scheduler), Task #825 (T4). The healthy
    worker calls this on a periodic cadence (≈ ``ttl_seconds / 2``)
    to keep the reaper from reclaiming the run. The update is
    conditional on ``lease_owner = owner`` AND ``status = 'running'``
    so a worker whose lease has been stolen by the reaper (or whose
    run has been cancelled out from under it by an operator) gets a
    :class:`LeaseLostError` and stops cleanly -- at-least-once
    semantics depend on the worker honouring this signal.

    Why a conditional ``UPDATE`` rather than a Python ``if`` + edit
    -----------------------------------------------------------------

    The Python-side check would race the reaper: between reading the
    row and writing the new ``lease_expires_at`` the reaper could
    reclaim, and the worker's later write would silently overwrite
    the reaper's clear (or another claimer's owner). A single
    conditional ``UPDATE`` is atomic at the DB layer -- the predicate
    and the write commit together, so either the worker keeps the
    lease (one row touched) or it has already lost it (zero rows
    touched) and we raise.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        run_id: The run whose lease this worker is extending.
        owner: The worker identifier (must match the row's current
            ``lease_owner`` or the update touches zero rows).
        ttl_seconds: The new lease window (same shape as
            :func:`claim_lease`).

    Returns:
        The :class:`AgentRun` with its lease extended. Newly-loaded
        from the DB after the update so the caller sees the latest
        server-side values (the conditional update bypasses the ORM's
        change tracking).

    Raises:
        LeaseLostError: The conditional update touched zero rows --
            this worker no longer holds the lease.
    """
    new_expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    # ``session.execute()`` on an ``UPDATE`` returns a
    # :class:`~sqlalchemy.engine.cursor.CursorResult` (which carries
    # the DBAPI ``rowcount``) at runtime; the static stub return type
    # is the generic :class:`Result` so mypy needs the explicit cast
    # to resolve ``rowcount``. ``synchronize_session=False`` is also
    # correct semantically -- we are about to reload the row from the
    # DB anyway, so synchronising the identity map before that reload
    # would be wasted work.
    raw_result = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.lease_owner == owner,
            AgentRun.status == AgentRunStatus.RUNNING.value,
        )
        .values(lease_expires_at=new_expires_at)
        .execution_options(synchronize_session=False)
    )
    cursor_result = cast(CursorResult[Any], raw_result)
    if cursor_result.rowcount == 0:
        raise LeaseLostError(run_id=run_id, owner=owner)
    await session.flush()
    # Reload so the caller sees server-side state (the conditional
    # UPDATE bypasses the ORM's identity map change tracking).
    row = await session.get(AgentRun, run_id)
    if row is None:
        # Defensive: the rowcount>0 branch means the row existed at
        # update time; deletion mid-flight would be a substrate-level
        # event we do not currently model. Surface it as
        # LeaseLostError so the worker stops.
        raise LeaseLostError(run_id=run_id, owner=owner)
    return row


async def release_lease(session: AsyncSession, row: AgentRun) -> AgentRun:
    """Clear the lease on *row* without changing its status.

    Initiative #804 (G11.3 Scheduler), Task #825 (T4). Used by:

    * The reaper's ``resume`` policy after marking a row eligible for
      re-dispatch (the next worker claim populates the columns
      fresh).
    * The lifecycle service's terminal-transition helpers
      (:func:`succeed_run`, :func:`fail_run`, :func:`cancel_run`)
      after the row reaches a terminal state so the indexes do not
      retain stale lease metadata.

    Idempotent: clearing an already-cleared lease is a no-op (both
    fields are set to ``None`` regardless of their prior value).

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The :class:`AgentRun` whose lease to clear.

    Returns:
        The mutated, flushed row.
    """
    row.lease_owner = None
    row.lease_expires_at = None
    await session.flush()
    return row


async def snapshot_in_flight_policy(
    session: AsyncSession,
    row: AgentRun,
    policy: ScheduledTriggerInFlightPolicy,
) -> AgentRun:
    """Copy the firing trigger's :attr:`in_flight_policy` onto the run row.

    Initiative #804 (G11.3 Scheduler), Task #825 (T4). T2 #823 / T3
    #824 wire this call when they fire a run: the trigger's policy is
    snapshotted onto the run row so a definition edit mid-flight
    cannot flip behavior on a run that's already executing. The
    runtime (G11.1) and the reaper (T4) both read the snapshot, not
    the trigger.

    The runtime helper here is a thin wrapper around the
    column assignment because copying the policy is *part of* the
    run-start handshake -- threading a separate caller path for the
    snapshot risks the trigger / run rows committing in different
    transactions and the run executing under the wrong policy.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The :class:`AgentRun` being started.
        policy: The trigger's current :class:`ScheduledTriggerInFlightPolicy`.

    Returns:
        The mutated, flushed row.
    """
    row.in_flight_policy = policy.value
    await session.flush()
    return row


async def increment_turns(session: AsyncSession, row: AgentRun) -> AgentRun:
    """Increment the run's observable tool-use turn counter.

    The runtime calls this once per loop turn. ``turns`` is purely
    observational -- the actual turn *budget* is enforced by the loop
    (``UsageLimits.request_limit``, G11.1-T1), not this counter. No
    status change; flushed, not committed.
    """
    row.turns += 1
    await session.flush()
    return row


async def succeed_run(
    session: AsyncSession,
    row: AgentRun,
    *,
    output: dict[str, object],
    cost: Decimal | None = None,
) -> AgentRun:
    """Transition a run to ``succeeded`` and record its output.

    Records ``output`` before the transition so a reader observing the
    run as ``succeeded`` always sees the result. ``cost`` is accepted
    but **stubbed in v0.2** -- the runtime passes ``None`` and the
    column stays NULL until G11.5/C3 wires per-identity cost attribution;
    the parameter exists now so C3 lands without a service-signature
    change.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The ``running`` :class:`AgentRun`.
        output: The run's structured final result.
        cost: Computed cost (G11.5/C3). ``None`` in v0.2.

    Returns:
        The mutated, flushed row.

    Raises:
        IllegalTransitionError: ``succeeded`` is not reachable from the
            row's current status (e.g. the run already terminated).
    """
    row.output = output
    if cost is not None:
        row.cost = cost
    return await transition(session, row, AgentRunStatus.SUCCEEDED)


async def fail_run(
    session: AsyncSession,
    row: AgentRun,
    *,
    error: str,
) -> AgentRun:
    """Transition a run to ``failed`` and record the failure reason.

    Records ``error`` (the exception class + message the loop surfaced)
    before the transition. ``error`` is kept distinct from ``output`` so
    a failed run's diagnostics never masquerade as a result.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The non-terminal :class:`AgentRun`.
        error: Human-readable failure reason.

    Returns:
        The mutated, flushed row.

    Raises:
        IllegalTransitionError: ``failed`` is not reachable from the
            row's current status.
    """
    row.error = error
    return await transition(session, row, AgentRunStatus.FAILED)


async def cancel_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    operator: Operator,
) -> AgentRun:
    """Cancel a running (or pending / awaiting-approval) run.

    The operator-authorized cancellation path. Loads the run by id,
    checks the operator holds at least :data:`_MIN_CANCEL_ROLE`, then
    transitions it to ``cancelled`` via the same :func:`transition`
    guard every other status change uses -- so a cancel against an
    already-terminal run surfaces as :class:`IllegalTransitionError`
    (409), not a silent no-op.

    The actual interruption of the in-flight async loop is the
    runtime's responsibility (the AgentRun seam, G11.1-T1): this
    function records the *durable intent* (status + ``ended_at``); the
    loop observes the cancelled status on its next turn boundary and
    stops. Recording the intent durably first is what makes
    cancellation survive a process restart.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        run_id: The run to cancel (its id == its ``agent_session_id``).
        operator: The authenticated operator requesting the cancel.

    Returns:
        The cancelled, flushed row.

    Raises:
        AgentRunNotFoundError: No row for *run_id*.
        UnauthorizedCancellationError: The operator's role ranks below
            :data:`_MIN_CANCEL_ROLE`.
        IllegalTransitionError: The run is already terminal.
    """
    if _ROLE_RANK.index(operator.tenant_role) < _ROLE_RANK.index(_MIN_CANCEL_ROLE):
        raise UnauthorizedCancellationError(
            operator_sub=operator.sub,
            role=operator.tenant_role,
        )

    row = await session.get(AgentRun, run_id)
    if row is None:
        raise AgentRunNotFoundError(run_id)

    return await transition(session, row, AgentRunStatus.CANCELLED)
