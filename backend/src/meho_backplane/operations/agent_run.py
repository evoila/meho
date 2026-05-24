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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import AgentRun, AgentRunStatus, AgentRunTrigger

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "AgentRunError",
    "AgentRunNotFoundError",
    "IllegalTransitionError",
    "UnauthorizedCancellationError",
    "cancel_run",
    "create_run",
    "fail_run",
    "get_run",
    "increment_turns",
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

    row.status = to_status.value
    await session.flush()
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
