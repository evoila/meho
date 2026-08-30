# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operation-run record lifecycle + cancellation service (async dispatch).

Async governed dispatch (#3079). A governed operation submitted in async
mode is one durable ``operation_run`` row
(:class:`meho_backplane.db.models.OperationRun`). This module owns the
**lifecycle**: creating the row, walking its ``status`` through an
explicit, enforced state machine, recording the result envelope / failure
reason, the lease/heartbeat the reaper reclaims against, and the
operator-authorized cancellation path.

It is a deliberate trim of :mod:`meho_backplane.operations.agent_run` --
the same durable-row + lease + state-machine discipline, without the
LLM-loop concepts (model tier, turns, cost, agent definition) and without
a ``resume`` in-flight policy. A governed op can wrap a non-idempotent
vendor write, so an orphaned run is **never** re-dispatched; the reaper
drives it to ``failed`` instead. That single-policy simplification is why
this module carries no ``in_flight_policy`` / ``snapshot_in_flight_policy``.

The state machine::

    pending ──> running ──> succeeded   (terminal; result envelope recorded)
       │           │           │
       │           ▼           └─────────
       ├────────> failed                  (terminal; run crashed / reaped)
       │
       └──┐
          ▼
       cancelled                          (terminal; from any non-terminal
                                           state, by an authorized operator)

``succeeded`` means the *run* completed and its
:class:`~meho_backplane.connectors.schemas.OperationResult` envelope is
durable -- even when that envelope's own ``status`` is ``error`` /
``denied`` / ``needs-approval``. ``failed`` is reserved for a run that
never produced an envelope (the worker died, or the dispatch raised).

Transaction discipline
----------------------

Every mutating function takes an open :class:`AsyncSession`, flushes its
changes, and returns -- the **caller** owns the commit, the same contract
:mod:`meho_backplane.operations.agent_run` follows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import select, update
from sqlalchemy.engine.cursor import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import (
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATUSES",
    "IllegalOperationRunTransitionError",
    "OperationRunError",
    "OperationRunLeaseLostError",
    "OperationRunNotFoundError",
    "UnauthorizedOperationRunCancellationError",
    "cancel_run",
    "claim_lease",
    "create_run",
    "fail_run",
    "get_run",
    "heartbeat",
    "list_runs",
    "mark_running",
    "release_lease",
    "succeed_run",
    "transition",
]


#: The terminal lifecycle states -- a run in any of these accepts no
#: further transition.
TERMINAL_STATUSES: Final[frozenset[OperationRunStatus]] = frozenset(
    {
        OperationRunStatus.SUCCEEDED,
        OperationRunStatus.FAILED,
        OperationRunStatus.CANCELLED,
    }
)


#: The single source of truth for legal ``status`` edges. Cancellation is
#: modelled as an ordinary edge (every non-terminal state -> ``cancelled``);
#: :func:`cancel_run` layers the operator authorization on top of the plain
#: :func:`transition` it calls.
ALLOWED_TRANSITIONS: Final[dict[OperationRunStatus, frozenset[OperationRunStatus]]] = {
    OperationRunStatus.PENDING: frozenset(
        {
            OperationRunStatus.RUNNING,
            OperationRunStatus.CANCELLED,
        }
    ),
    OperationRunStatus.RUNNING: frozenset(
        {
            OperationRunStatus.SUCCEEDED,
            OperationRunStatus.FAILED,
            OperationRunStatus.CANCELLED,
        }
    ),
    OperationRunStatus.SUCCEEDED: frozenset(),
    OperationRunStatus.FAILED: frozenset(),
    OperationRunStatus.CANCELLED: frozenset(),
}


#: Minimum tenant role authorized to cancel an operation run. Cancelling
#: in-flight work is a control action, not a read; mirrors the agent-run
#: cancel floor.
_MIN_CANCEL_ROLE: Final[TenantRole] = TenantRole.OPERATOR

#: Linear role ranking -- index = rank. Mirrors
#: :data:`meho_backplane.operations.agent_run._ROLE_RANK`.
_ROLE_RANK: Final[tuple[TenantRole, ...]] = (
    TenantRole.READ_ONLY,
    TenantRole.OPERATOR,
    TenantRole.TENANT_ADMIN,
)

_LIST_RUNS_MAX_LIMIT: Final[int] = 500
_LIST_RUNS_DEFAULT_LIMIT: Final[int] = 100


class OperationRunError(Exception):
    """Base class for operation-run lifecycle failures."""


class OperationRunNotFoundError(OperationRunError):
    """No ``operation_run`` row exists for the requested id.

    Raised by :func:`cancel_run` when the id does not resolve. The caller
    maps it to a 404; the service does not silently no-op so a cancel
    against a typo'd / cross-tenant id surfaces.
    """

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(f"no operation_run row for id {run_id}")


class IllegalOperationRunTransitionError(OperationRunError):
    """A requested ``status`` transition is not on :data:`ALLOWED_TRANSITIONS`.

    Raised by :func:`transition` before any DB write. Carries the
    ``from``/``to`` pair so the caller's error response names the rejected
    edge precisely (a cancel against an already-terminal run maps to 409).
    """

    def __init__(self, *, from_status: OperationRunStatus, to_status: OperationRunStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"illegal operation_run transition {from_status.value!r} -> {to_status.value!r}"
        )


class UnauthorizedOperationRunCancellationError(OperationRunError):
    """The operator lacks the role required to cancel a run.

    Raised by :func:`cancel_run` when ``operator.tenant_role`` ranks below
    :data:`_MIN_CANCEL_ROLE`. Distinct from
    :class:`IllegalOperationRunTransitionError` so the caller maps
    authorization failures to 403 and state failures to 409.
    """

    def __init__(self, *, operator_sub: str, role: TenantRole) -> None:
        self.operator_sub = operator_sub
        self.role = role
        super().__init__(
            f"operator {operator_sub!r} with role {role.value!r} may not cancel an "
            f"operation run (requires at least {_MIN_CANCEL_ROLE.value!r})"
        )


class OperationRunLeaseLostError(OperationRunError):
    """The lease this worker thought it held has been reassigned.

    Raised by :func:`heartbeat` when the conditional update touches zero
    rows -- the row's ``lease_owner`` no longer matches the heartbeating
    worker (the reaper reclaimed it) or the row reached a terminal status
    (an operator cancelled it). The worker must stop cleanly on this
    signal.
    """

    def __init__(self, *, run_id: uuid.UUID, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        super().__init__(
            f"operation_run {run_id} lease no longer held by {owner!r} "
            f"(reaper reclaimed or run terminated)"
        )


def _coerce_status(value: OperationRunStatus | str) -> OperationRunStatus:
    """Normalise a status to :class:`OperationRunStatus`.

    ``OperationRun.status`` is stored as ``str``; a row read from the DB
    carries the string value. An unknown value raises :class:`ValueError`
    -- a row whose stored status is outside the closed enum is a
    corruption the service must not paper over.
    """
    if isinstance(value, OperationRunStatus):
        return value
    return OperationRunStatus(value)


async def create_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity_sub: str,
    origin: OperationRunOrigin,
    connector_id: str,
    op_id: str,
    identity_act: str | None = None,
    target_name: str | None = None,
    params_hash: str | None = None,
    approval_request_id: uuid.UUID | None = None,
) -> OperationRun:
    """Insert a fresh ``operation_run`` row in the ``pending`` state.

    The returned row's :attr:`OperationRun.id` is the durable handle the
    202 response hands back. The caller (the run service) claims a lease
    and launches the background task in the same request.

    Args:
        session: Open :class:`AsyncSession`; flushed (so ``id`` /
            ``created_at`` are populated), not committed.
        tenant_id: The tenant the run belongs to (real FK to ``tenant.id``).
        identity_sub: RFC 8693 ``sub`` -- the submitting operator.
        origin: What created the run (:class:`OperationRunOrigin`).
        connector_id: The connector implementation id the dispatch targets.
        op_id: The operation id being dispatched.
        identity_act: RFC 8693 ``act`` -- the delegated actor, if any.
        target_name: The submitted target name (``None`` for a target-less op).
        params_hash: Hex digest of the submitted params (secret-safe
            correlation), or ``None`` for a param-less op.
        approval_request_id: The parked request id for an
            ``approval_resume`` run; ``None`` for a ``direct`` run.

    Returns:
        The inserted :class:`OperationRun`, flushed.
    """
    row = OperationRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        identity_sub=identity_sub,
        identity_act=identity_act,
        origin=origin.value,
        connector_id=connector_id,
        op_id=op_id,
        target_name=target_name,
        params_hash=params_hash,
        approval_request_id=approval_request_id,
        status=OperationRunStatus.PENDING.value,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> OperationRun | None:
    """Load an ``operation_run`` row by id, or ``None`` if absent."""
    return await session.get(OperationRun, run_id)


async def list_runs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: OperationRunStatus | None = None,
    limit: int = _LIST_RUNS_DEFAULT_LIMIT,
    offset: int = 0,
) -> list[OperationRun]:
    """Page through operation runs in *tenant_id*, newest first.

    Tenant-isolated by the WHERE clause: cross-tenant rows are invisible.

    Args:
        session: Open :class:`AsyncSession` (read-only).
        tenant_id: The tenant whose runs to list.
        status: When supplied, narrows to runs in this lifecycle state.
        limit: Max rows per page. Clamped to ``[1, 500]``.
        offset: Rows to skip (paging). Negative offsets clamp to 0.

    Returns:
        The matching :class:`OperationRun` rows ordered ``created_at DESC``.
    """
    bounded_limit = max(1, min(limit, _LIST_RUNS_MAX_LIMIT))
    bounded_offset = max(0, offset)
    stmt = select(OperationRun).where(OperationRun.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(OperationRun.status == status.value)
    stmt = stmt.order_by(OperationRun.created_at.desc()).limit(bounded_limit).offset(bounded_offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def transition(
    session: AsyncSession,
    row: OperationRun,
    to_status: OperationRunStatus,
) -> OperationRun:
    """Move *row* to *to_status*, enforcing the legal state machine.

    The single mutation point for ``status``. Raises
    :class:`IllegalOperationRunTransitionError` for any edge not on
    :data:`ALLOWED_TRANSITIONS` -- before any DB write. Stamps
    ``started_at`` on the first move into ``running`` and ``ended_at`` on
    any move into a terminal state; clears the lease on terminal
    transitions so the reaper index does not retain stale metadata.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The attached :class:`OperationRun` to mutate.
        to_status: The desired next status.

    Returns:
        The same *row*, mutated and flushed.

    Raises:
        IllegalOperationRunTransitionError: *to_status* is not a legal
            successor of the row's current status.
    """
    from_status = _coerce_status(row.status)
    if to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise IllegalOperationRunTransitionError(from_status=from_status, to_status=to_status)

    now = datetime.now(UTC)
    if to_status is OperationRunStatus.RUNNING and row.started_at is None:
        row.started_at = now
    if to_status in TERMINAL_STATUSES:
        row.ended_at = now
        row.lease_owner = None
        row.lease_expires_at = None

    row.status = to_status.value
    await session.flush()
    return row


async def mark_running(session: AsyncSession, row: OperationRun) -> OperationRun:
    """Transition a ``pending`` run to ``running`` (no extra payload)."""
    return await transition(session, row, OperationRunStatus.RUNNING)


async def claim_lease(
    session: AsyncSession,
    row: OperationRun,
    *,
    owner: str,
    ttl_seconds: int,
) -> OperationRun:
    """Stamp a lease on *row* and record the owning worker.

    The lease columns are pure side-effect (they do not change ``status``).
    The caller threads :func:`claim_lease` and the ``pending`` insert
    together so a reader always sees who holds the lease.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The :class:`OperationRun` to claim.
        owner: A stable worker identifier (e.g. ``"<hostname>:<pid>"``).
        ttl_seconds: Wall-clock seconds the lease is valid for; the worker
            must heartbeat within this window or the reaper reclaims.

    Returns:
        The mutated, flushed row.
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
) -> OperationRun:
    """Extend the lease on *run_id* iff this worker still holds it.

    A single conditional ``UPDATE`` gated on ``lease_owner = owner AND
    status = 'running'`` -- atomic at the DB layer, so either the worker
    keeps the lease (one row touched) or it has already lost it (zero rows
    touched) and we raise. Mirrors
    :func:`meho_backplane.operations.agent_run.heartbeat`.

    Raises:
        OperationRunLeaseLostError: The conditional update touched zero
            rows -- this worker no longer holds the lease.
    """
    new_expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    raw_result = await session.execute(
        update(OperationRun)
        .where(
            OperationRun.id == run_id,
            OperationRun.lease_owner == owner,
            OperationRun.status == OperationRunStatus.RUNNING.value,
        )
        .values(lease_expires_at=new_expires_at)
        .execution_options(synchronize_session=False)
    )
    cursor_result = cast(CursorResult[Any], raw_result)
    if cursor_result.rowcount == 0:
        raise OperationRunLeaseLostError(run_id=run_id, owner=owner)
    await session.flush()
    row = await session.get(OperationRun, run_id)
    if row is None:
        raise OperationRunLeaseLostError(run_id=run_id, owner=owner)
    return row


async def release_lease(session: AsyncSession, row: OperationRun) -> OperationRun:
    """Clear the lease on *row* without changing its status. Idempotent."""
    row.lease_owner = None
    row.lease_expires_at = None
    await session.flush()
    return row


async def succeed_run(
    session: AsyncSession,
    row: OperationRun,
    *,
    result: dict[str, Any],
) -> OperationRun:
    """Transition a run to ``succeeded`` and persist its result envelope.

    Records ``result`` (the ``OperationResult`` envelope, already
    ``model_dump(mode="json")``-shaped) before the transition so a reader
    observing the run as ``succeeded`` always sees the durable envelope.
    This is the dropped-response-class fix: the envelope survives the
    submitting connection.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        row: The ``running`` :class:`OperationRun`.
        result: The dispatch's ``OperationResult`` envelope as a JSON dict.

    Returns:
        The mutated, flushed row.

    Raises:
        IllegalOperationRunTransitionError: ``succeeded`` is not reachable
            from the row's current status (e.g. the run already terminated).
    """
    row.result = result
    return await transition(session, row, OperationRunStatus.SUCCEEDED)


async def fail_run(
    session: AsyncSession,
    row: OperationRun,
    *,
    error: str,
) -> OperationRun:
    """Transition a run to ``failed`` and record the failure reason.

    Reserved for a run that never produced an envelope -- the worker died
    (reaped) or the dispatch raised unexpectedly. ``error`` is kept
    distinct from ``result`` so a crashed run never masquerades as an op
    result.

    Raises:
        IllegalOperationRunTransitionError: ``failed`` is not reachable
            from the row's current status.
    """
    row.error = error
    return await transition(session, row, OperationRunStatus.FAILED)


async def cancel_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    operator: Operator,
) -> OperationRun:
    """Cancel a running (or pending) run for an authorized operator.

    Loads the run by id, checks the operator holds at least
    :data:`_MIN_CANCEL_ROLE`, then transitions it to ``cancelled`` via the
    same :func:`transition` guard every other status change uses -- so a
    cancel against an already-terminal run surfaces as
    :class:`IllegalOperationRunTransitionError` (409), not a silent no-op.

    The actual interruption of the in-flight task is best-effort in the run
    service (it observes the durable ``cancelled`` status / loses its
    lease); recording the intent durably first is what makes cancellation
    survive a process restart.

    Raises:
        OperationRunNotFoundError: No row for *run_id*.
        UnauthorizedOperationRunCancellationError: The operator's role
            ranks below :data:`_MIN_CANCEL_ROLE`.
        IllegalOperationRunTransitionError: The run is already terminal.
    """
    if _ROLE_RANK.index(operator.tenant_role) < _ROLE_RANK.index(_MIN_CANCEL_ROLE):
        raise UnauthorizedOperationRunCancellationError(
            operator_sub=operator.sub,
            role=operator.tenant_role,
        )

    row = await session.get(OperationRun, run_id)
    if row is None:
        raise OperationRunNotFoundError(run_id)

    return await transition(session, row, OperationRunStatus.CANCELLED)
