# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable command-queue service for the outbound gateway command plane.

Initiative #2415 (Remote execution gateway), Task #2498. The central side
of the push-only command plane: a durable ``gateway_command`` queue
(:class:`~meho_backplane.db.models.GatewayCommand`) plus the three service
functions the runner-facing routes (:mod:`meho_backplane.api.v1.gateway`)
call through.

* :func:`enqueue_command` — central code parks a pre-authorized operation
  for a runner. No HTTP enqueue endpoint exists: an enqueue surface not
  fronted by the policy gate would bypass the initiative's "all
  authorization stays central" principle. #2500's capability-minting path
  (post-policy-gate) is the production caller; tests enqueue directly.
* :func:`claim_next_command` — one FIFO claim attempt: the oldest
  ``pending`` row for ``(tenant_id, runner_id)`` flips to ``delivered``.
  Multi-replica-safe via ``SELECT ... FOR UPDATE SKIP LOCKED`` on
  PostgreSQL (moulded on :func:`meho_backplane.scheduler.repository.claim_due_triggers`,
  #804) with a conditional-``UPDATE`` fallback on the SQLite test path so
  two in-process claimers still deliver a row at most once.
* :func:`record_result` — the runner reports an outcome, flipping
  ``delivered -> succeeded|failed``. Distinguishes an unknown / foreign
  command (404) from a non-``delivered`` row (409) for the route's
  status-code split.

Transaction discipline
----------------------

Every mutating function takes an open
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, flushes its changes, and
returns — the **caller** owns the commit (mould:
:mod:`meho_backplane.operations.approval_queue`). The long-poll handler
opens a fresh short-lived session per claim attempt (never holding a DB
transaction across its ``asyncio.sleep``) and commits a won claim itself.

Hold-window ceiling
-------------------

:data:`GATEWAY_LONGPOLL_MAX_WAIT_SECONDS` is the exported cap on the
long-poll hold. It lives here (not buried in the route closure) as an
importable seam: #2501's dead-man threshold is a multiple of it. The
route clamps the caller's ``wait`` to it via :func:`clamp_longpoll_wait`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import GatewayCommand, GatewayCommandStatus
from meho_backplane.operations._validate import compute_params_hash

__all__ = [
    "GATEWAY_COMMAND_DEFAULT_TTL",
    "GATEWAY_LONGPOLL_DEFAULT_WAIT_SECONDS",
    "GATEWAY_LONGPOLL_MAX_WAIT_SECONDS",
    "GatewayCommandNotDeliveredError",
    "GatewayCommandNotFoundError",
    "bound_capability_expiry",
    "claim_next_command",
    "clamp_longpoll_wait",
    "enqueue_command",
    "record_result",
]

# NOTE: the structlog logger is resolved per-call at each log site below
# rather than held as a module-level proxy -- production's
# ``cache_logger_on_first_use=True`` orphans a cached BoundLogger's
# processor chain from ``structlog.testing.capture_logs`` (the #738
# ``-n 3 --dist loadscope`` flake). Same convention as
# :mod:`meho_backplane.auth.jwt` / :mod:`meho_backplane.operations.gateway_commands`.

#: Default capability-command TTL (#2500). A minted command stays
#: claimable for this window; the mint caller may only *shorten* it
#: (:func:`bound_capability_expiry`), never extend it — the single default
#: doubles as the ceiling, exactly like the approval queue's
#: ``_bounded_expires_at`` (#2322). A module constant, not a settings knob:
#: substrate minimalism (#1177), and the bound is a security property that
#: should not be operator-tunable per environment. Sized well above the
#: long-poll hold + a few runner ticks so a briefly-offline runner can
#: still claim a fresh command, but short enough to bound the replay window.
GATEWAY_COMMAND_DEFAULT_TTL: Final[timedelta] = timedelta(minutes=5)


def bound_capability_expiry(requested: datetime | None, *, now: datetime | None = None) -> datetime:
    """Resolve the ``expires_at`` a minted capability command is stamped with.

    ``None`` defaults to ``now + GATEWAY_COMMAND_DEFAULT_TTL``. An explicit
    caller deadline is honoured but **capped** at that same ceiling, so no
    caller can mint an unbounded-lived capability while a shorter (probe /
    already-past) deadline is respected as-is. No lower bound (substrate
    minimalism). Moulded on
    :func:`meho_backplane.operations.approval_queue._bounded_expires_at`.
    """
    current = now if now is not None else datetime.now(UTC)
    ceiling = current + GATEWAY_COMMAND_DEFAULT_TTL
    if requested is None:
        return ceiling
    return min(requested, ceiling)


#: Ceiling on a single long-poll hold, in seconds. Sizing mirrors the SSE
#: feed's intermediary-idle-timeout rationale (nginx ``proxy_read_timeout``
#: / ALB idle default 60 s -- hold well below it so an idle-timeout kill
#: never races a claim): the runner re-polls immediately on a ``204``.
#: Exported (not a route-local literal) because #2501's dead-man threshold
#: is a multiple of this constant.
GATEWAY_LONGPOLL_MAX_WAIT_SECONDS: Final[int] = 30

#: Default ``wait`` when the runner does not specify one. Below the ceiling
#: so the common poll self-bounds without relying on the clamp.
GATEWAY_LONGPOLL_DEFAULT_WAIT_SECONDS: Final[int] = 25


def clamp_longpoll_wait(requested: int) -> int:
    """Clamp a caller-requested ``wait`` into ``[0, ceiling]``.

    ``wait=0`` means a single immediate claim attempt (no hold); a value
    above :data:`GATEWAY_LONGPOLL_MAX_WAIT_SECONDS` is clamped **down** to
    the ceiling rather than rejected — a runner asking for a longer hold
    gets a bounded hold, not a 422 (binding decision #2498-3). A negative
    value (shouldn't reach here past the ``Query(ge=0)`` bound) floors at 0.
    """
    return min(max(requested, 0), GATEWAY_LONGPOLL_MAX_WAIT_SECONDS)


class GatewayCommandNotFoundError(Exception):
    """No :class:`GatewayCommand` matches ``(command_id, tenant_id, runner_id)``.

    Raised by :func:`record_result` when the id does not resolve, belongs
    to a different tenant, or was enqueued for a different runner — the
    three cases are indistinguishable to the caller (no cross-tenant /
    cross-runner existence oracle). The route layer maps this to 404.
    """

    def __init__(self, command_id: uuid.UUID) -> None:
        self.command_id = command_id
        super().__init__(f"no gateway_command row for id {command_id} in this runner's queue")


class GatewayCommandNotDeliveredError(Exception):
    """The command exists but is not in the ``delivered`` state.

    Raised by :func:`record_result` when the row is still ``pending``
    (never claimed) or already terminal (``succeeded`` / ``failed`` — a
    duplicate report). The route layer maps this to 409 (conflict).
    """

    def __init__(self, command_id: uuid.UUID, status: str) -> None:
        self.command_id = command_id
        self.status = status
        super().__init__(
            f"gateway_command {command_id} is {status!r}, not 'delivered'; "
            "only a delivered command accepts a result"
        )


async def enqueue_command(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    op_id: str,
    params: dict[str, Any],
    enqueued_by_sub: str,
    target_descriptor: dict[str, Any] | None = None,
    params_hash: str | None = None,
    expires_at: datetime | None = None,
    mint_audit_id: uuid.UUID | None = None,
) -> GatewayCommand:
    """Insert a ``pending`` :class:`GatewayCommand` for a runner.

    The single central enqueue path (no HTTP surface). Flushes, not
    committed — the caller owns the commit so the enqueue can compose with
    the minting transaction (#2500).

    Every row carries its capability binding (#2500): ``params_hash``
    (defaulting to ``compute_params_hash(params)`` so it always matches the
    stored params), ``expires_at`` (bounded by
    :func:`bound_capability_expiry`), and the optional ``mint_audit_id``
    lineage. The mint path (:func:`meho_backplane.operations.gateway_commands.mint_gateway_command`)
    passes an explicit ``params_hash`` (the one it also stamped on the mint
    audit row) and ``mint_audit_id``; direct enqueues (tests) get the
    computed hash and a default TTL.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        tenant_id: The owning tenant.
        runner_id: The runner principal **name** (wire identity) the
            command is queued for.
        op_id: The operation the runner will execute.
        params: The validated op params. Stored verbatim.
        enqueued_by_sub: The ``sub`` of the principal whose central
            dispatch enqueued the command (audit provenance).
        target_descriptor: The centrally-resolved target descriptor a
            handler duck-reads, or ``None`` for a targetless synthetic op
            (``net.*``).
        params_hash: The args-hash binding; computed from *params* when
            omitted so it always matches the stored params.
        expires_at: A caller deadline (bounded down to the default-TTL
            ceiling); the default TTL when omitted.
        mint_audit_id: The id of the synchronous ``gateway.command.mint``
            audit row, stamped for result → mint audit lineage.

    Returns:
        The flushed :class:`GatewayCommand` row (with its generated ``id``).
    """
    command = GatewayCommand(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        runner_id=runner_id,
        op_id=op_id,
        params=params,
        target_descriptor=target_descriptor,
        status=GatewayCommandStatus.PENDING.value,
        enqueued_by_sub=enqueued_by_sub,
        enqueued_at=datetime.now(UTC),
        params_hash=params_hash if params_hash is not None else compute_params_hash(params),
        expires_at=bound_capability_expiry(expires_at),
        mint_audit_id=mint_audit_id,
    )
    session.add(command)
    await session.flush()
    structlog.get_logger(__name__).info(
        "gateway_command_enqueued",
        command_id=str(command.id),
        tenant_id=str(tenant_id),
        runner_id=runner_id,
        op_id=op_id,
        expires_at=command.expires_at.isoformat(),
    )
    return command


async def claim_next_command(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
) -> GatewayCommand | None:
    """Claim the oldest ``pending`` command for a runner; flip it ``delivered``.

    One FIFO claim attempt. On PostgreSQL the candidate row is selected
    ``FOR UPDATE SKIP LOCKED`` so a second claimer in another transaction
    (another replica / pod) sees zero rows for a row this transaction has
    locked — the row lock releases on the caller's commit / rollback. On
    SQLite (the test path) the locking clause no-ops; the conditional
    ``UPDATE ... WHERE status='pending'`` is what enforces claim-at-most-once
    across two in-process claimers sharing the connection pool (mould:
    :func:`meho_backplane.scheduler.repository.claim_due_triggers` /
    :func:`~meho_backplane.scheduler.repository.advance_cron_trigger`, #804).

    Returns the delivered row (``status='delivered'``, ``delivered_at``
    stamped) on a win, or ``None`` when the queue is empty **or** a
    concurrent claimer won the row between the SELECT and the UPDATE.

    Capability predicate (#2500): only an **unexpired** (``expires_at >
    now``), **unconsumed** (``consumed_at IS NULL``) ``pending`` row is
    claimable, so an expired or already-consumed capability is never
    delivered. Before flipping the row the stored ``params`` are re-hashed
    against ``params_hash``; a mismatch (post-mint substitution) refuses
    delivery (returns ``None`` + an error log) — the same swap defence
    ``approve_request`` runs, here guarding the params column.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed. The
            caller commits to persist the ``pending -> delivered`` flip.
        tenant_id: The runner's tenant (every query is tenant-scoped).
        runner_id: The runner principal name whose queue to claim from.
    """
    conn = await session.connection()
    now = datetime.now(UTC)
    stmt = (
        select(GatewayCommand)
        .where(
            GatewayCommand.tenant_id == tenant_id,
            GatewayCommand.runner_id == runner_id,
            GatewayCommand.status == GatewayCommandStatus.PENDING.value,
            # Capability binding (#2500): never deliver an expired or an
            # already-consumed command.
            GatewayCommand.expires_at > now,
            GatewayCommand.consumed_at.is_(None),
        )
        .order_by(GatewayCommand.enqueued_at.asc())
        .limit(1)
    )
    if conn.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None

    # Post-mint substitution defence (#2500): the stored params must still
    # hash to the bound params_hash, else refuse delivery. A mismatch means
    # the params column was mutated after mint — a tamper signal, not a race
    # — so it is fail-closed (the row stays pending, undelivered) and logged
    # at error level for an operator to investigate.
    if compute_params_hash(row.params) != row.params_hash:
        structlog.get_logger(__name__).error(
            "gateway_command_params_hash_mismatch",
            command_id=str(row.id),
            tenant_id=str(tenant_id),
            runner_id=runner_id,
            op_id=row.op_id,
        )
        return None

    # Conditional flip: the predicate + write commit together, so a second
    # in-process claimer that SELECTed the same pending row loses (0 rows).
    result = await session.execute(
        update(GatewayCommand)
        .where(
            GatewayCommand.id == row.id,
            GatewayCommand.status == GatewayCommandStatus.PENDING.value,
        )
        .values(status=GatewayCommandStatus.DELIVERED.value, delivered_at=now)
    )
    await session.flush()
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        return None
    # Reflect the flip on the local row so the caller sees it without a
    # re-query (mould: advance_cron_trigger).
    row.status = GatewayCommandStatus.DELIVERED.value
    row.delivered_at = now
    structlog.get_logger(__name__).info(
        "gateway_command_delivered",
        command_id=str(row.id),
        tenant_id=str(tenant_id),
        runner_id=runner_id,
        op_id=row.op_id,
    )
    return row


async def record_result(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    command_id: uuid.UUID,
    outcome: GatewayCommandStatus,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> GatewayCommand:
    """Record a runner's outcome on a ``delivered`` command.

    Flips ``delivered -> succeeded|failed``, stamps ``result`` / ``error``
    and ``completed_at``. The precondition ladder gives the route its
    404/409 split:

    1. The row must resolve within ``(tenant_id, runner_id)`` — else
       :class:`GatewayCommandNotFoundError` (404). Unknown id, a command
       enqueued for another runner, and a cross-tenant id are all the same
       404 (no existence oracle).
    2. The row must be ``delivered`` — else
       :class:`GatewayCommandNotDeliveredError` (409). A duplicate report
       (already terminal) and a never-claimed ``pending`` row both 409.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        tenant_id: The runner's tenant.
        runner_id: The runner principal name the command was queued for.
        command_id: The command row's id.
        outcome: Terminal state to record — ``SUCCEEDED`` or ``FAILED``.
        result: The success payload (for ``SUCCEEDED``); may be ``None``.
        error: The failure summary (for ``FAILED``); may be ``None``.

    Returns:
        The updated, flushed :class:`GatewayCommand`.

    Raises:
        ValueError: *outcome* is not a terminal state.
        GatewayCommandNotFoundError: No row in this runner's queue.
        GatewayCommandNotDeliveredError: Row is not ``delivered``.
    """
    if outcome not in (GatewayCommandStatus.SUCCEEDED, GatewayCommandStatus.FAILED):
        raise ValueError(f"outcome must be 'succeeded' or 'failed'; got {outcome!r}")

    row = await session.get(GatewayCommand, command_id)
    if row is None or row.tenant_id != tenant_id or row.runner_id != runner_id:
        raise GatewayCommandNotFoundError(command_id)
    if row.status != GatewayCommandStatus.DELIVERED.value:
        raise GatewayCommandNotDeliveredError(command_id, row.status)

    now = datetime.now(UTC)
    # Conditional on status='delivered' so a concurrent identical report
    # loses the race (0 rows) rather than double-writing the outcome.
    upd = await session.execute(
        update(GatewayCommand)
        .where(
            GatewayCommand.id == command_id,
            GatewayCommand.status == GatewayCommandStatus.DELIVERED.value,
        )
        .values(status=outcome.value, result=result, error=error, completed_at=now)
    )
    await session.flush()
    rowcount: int = upd.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        # A concurrent report flipped it terminal between our load + UPDATE.
        await session.refresh(row)
        raise GatewayCommandNotDeliveredError(command_id, row.status)

    row.status = outcome.value
    row.result = result
    row.error = error
    row.completed_at = now
    structlog.get_logger(__name__).info(
        "gateway_command_reported",
        command_id=str(command_id),
        tenant_id=str(tenant_id),
        runner_id=runner_id,
        outcome=outcome.value,
    )
    return row
