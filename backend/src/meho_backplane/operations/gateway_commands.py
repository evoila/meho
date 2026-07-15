# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Single-use capability commands for the remote execution gateway (#2500).

Initiative #2415 (Remote execution gateway), Task #2500 — the authorization
keystone of the push-only satellite runner. This module mints, consumes,
and audits the durable ``gateway_command`` capability rows (#2498's queue
table, extended with the binding columns in migration ``0061``).

The three service functions:

* :func:`mint_gateway_command` — the **central mint gate**. It re-runs the
  dispatcher's pre-execution ladder (``lookup_descriptor`` →
  ``validate_params`` → **safe-only wall** → ``policy_gate``) and only on an
  explicit ``AUTO_EXECUTE`` verdict for a ``safety_level == 'safe'`` op does
  it write a command row (+ its synchronous mint audit row). Any other
  outcome is a structured refusal that writes **no** command row and does
  **not** park into the approval queue — change-ops-over-gateway is v2
  (#2415 out of scope). This is the v1 read-only guarantee: a non-``safe``
  op can never reach a runner because it is never minted.

* :func:`consume_command` — the one-way **consumption latch** (moulded on
  :func:`meho_backplane.operations.approval_queue.claim_resume`). A single
  conditional ``UPDATE ... SET consumed_at = now WHERE consumed_at IS NULL
  AND status = 'delivered'`` wins the right to accept a result exactly once;
  a replayed / already-consumed capability is centrally refused
  (``command_already_consumed`` + a ``gateway_command_replay_refused`` log).

* :func:`accept_command_result` — the result-ingest orchestration a
  runner-facing handler calls: win the consumption latch, record the
  terminal outcome (#2498's :func:`~meho_backplane.gateway.queue.record_result`),
  then write the result audit row stamped ``parent_audit_id = mint_audit_id``
  so a remote execution forms one audit subtree.

Token shape (binding decision, #2500): the capability *is* the command
row's opaque UUID primary key — not a JWT. At-most-once execution
inherently requires central state (the ``consumed_at`` latch), so a signed
stateless token would add a second trust artifact while enabling nothing:
verification is a DB lookup, revocation is a row update, replay refusal is
the conditional-UPDATE latch. Possession is not authorization — a command
is only ever delivered over the runner's authenticated, ``runner_id``-scoped
channel (#2498/#2502).

Transaction discipline: :func:`mint_gateway_command` and
:func:`accept_command_result` take an open session and flush; the **caller**
owns the commit so the mint (or consume + record + audit) lands atomically
(mould: :mod:`meho_backplane.operations.approval_queue`).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import (
    AuditLog,
    GatewayCommand,
    GatewayCommandStatus,
    PermissionVerdict,
)
from meho_backplane.gateway.queue import (
    GatewayCommandNotDeliveredError,
    GatewayCommandNotFoundError,
    enqueue_command,
    record_result,
)
from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
from meho_backplane.operations._validate import (
    compute_params_hash,
    policy_gate,
    validate_params,
)

__all__ = [
    "GatewayCommandAlreadyConsumedError",
    "MintRefusalCode",
    "MintResult",
    "accept_command_result",
    "consume_command",
    "mint_gateway_command",
]

# NOTE: the structlog logger is resolved per-call at each log site below
# rather than held as a module-level proxy. Production sets
# ``cache_logger_on_first_use=True`` in
# ``meho_backplane.logging.configure_logging``; a cached BoundLogger pins a
# reference to the processor chain it was built with, and later
# ``structlog.configure(...)`` calls *replace* (not mutate) that reference --
# so test fixtures using ``structlog.testing.capture_logs`` cannot observe
# events written through the orphaned cached proxy (the ``-n 3 --dist
# loadscope`` flake originally seen in #738). Same precedent + rationale as
# :mod:`meho_backplane.auth.jwt` and :mod:`meho_backplane.mcp.tools.memory`.

#: ``audit_log`` bookkeeping for the gateway command lifecycle. ``GATEWAY``
#: sits alongside ``DISPATCH`` / ``APPROVAL`` as the synthetic method for a
#: capability event; the synthetic ``202`` (accepted / pending) mirrors the
#: approval queue's ``_APPROVAL_STATUS_CODE`` convention for a durable park.
_AUDIT_METHOD = "GATEWAY"
_MINT_AUDIT_PATH = "gateway.command.mint"
_MINT_AUDIT_STATUS = 202
_RESULT_AUDIT_PATH = "gateway.command.result"
_RESULT_AUDIT_STATUS = 200


class GatewayCommandAlreadyConsumedError(Exception):
    """A result was reported for a command whose ``consumed_at`` latch is set.

    Raised by :func:`consume_command` when the one-way consumption latch is
    already claimed — a replayed result. The route layer maps this to 409
    (conflict), the same status as a duplicate report on a terminal row.
    """

    def __init__(self, command_id: uuid.UUID) -> None:
        self.command_id = command_id
        super().__init__(
            f"gateway_command {command_id} is already consumed; its result was "
            "accepted once and a replay is refused"
        )


class MintRefusalCode(StrEnum):
    """Closed vocabulary of central mint refusals.

    Every value is a fail-closed outcome: the command is **not** minted, so
    it never reaches a runner. ``OP_NOT_SAFE`` is the v1 read-only wall;
    ``POLICY_DENIED`` / ``NEEDS_APPROVAL`` are the policy-gate verdicts that
    are not ``AUTO_EXECUTE`` (change-ops-over-gateway is v2, so a
    ``needs-approval`` verdict is refused here rather than parked).
    """

    DESCRIPTOR_UNKNOWN = "descriptor_unknown"
    INVALID_PARAMS = "invalid_params"
    OP_NOT_SAFE = "op_not_safe"
    POLICY_DENIED = "policy_denied"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class MintResult:
    """The outcome of :func:`mint_gateway_command`.

    On a successful mint, :attr:`command` and :attr:`mint_audit_id` are set
    and :attr:`refusal_code` is ``None``. On a refusal, :attr:`command` is
    ``None`` and :attr:`refusal_code` / :attr:`refusal_reason` explain why
    — no command row and no approval row were written.
    """

    command: GatewayCommand | None = None
    mint_audit_id: uuid.UUID | None = None
    refusal_code: MintRefusalCode | None = None
    refusal_reason: str | None = None

    @property
    def minted(self) -> bool:
        """Whether a command row was minted (``True``) or refused (``False``)."""
        return self.command is not None


def _refused(code: MintRefusalCode, reason: str) -> MintResult:
    """Build a fail-closed refusal result (no rows written)."""
    return MintResult(refusal_code=code, refusal_reason=reason)


async def _write_gateway_audit_row(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    path: str,
    status_code: int,
    duration_ms: float,
    payload: dict[str, Any],
    target_id: uuid.UUID | None = None,
    parent_audit_id: uuid.UUID | None = None,
) -> None:
    """Insert one synchronous ``audit_log`` row for a gateway command event.

    Same-transaction (caller commits), ``method='GATEWAY'``. ``parent_audit_id``
    is set **directly** off the durable ``gateway_command.mint_audit_id`` for
    a result row (the lineage the ``parent_audit_id_var`` mechanism provides,
    read here off the row rather than a contextvar — same discipline as
    :func:`meho_backplane.operations.approval_queue._write_audit_row` reading
    ``request.request_audit_id``). Deliberately minimal (no redaction /
    runbook extras): a capability mint / result is not a connector-boundary
    response.
    """
    row = AuditLog(
        id=audit_id,
        occurred_at=datetime.now(UTC),
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        target_id=target_id,
        parent_audit_id=parent_audit_id,
        method=_AUDIT_METHOD,
        path=path,
        status_code=status_code,
        request_id=None,
        duration_ms=Decimal(str(round(duration_ms, 2))),
        payload=payload,
    )
    session.add(row)
    await session.flush()


async def mint_gateway_command(
    session: AsyncSession,
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
    runner_id: str,
    target_descriptor: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> MintResult:
    """Mint a single-use capability command, or refuse fail-closed.

    Re-runs the dispatcher's pre-execution ladder against the same
    descriptor metadata (:func:`meho_backplane.operations.dispatcher.dispatch`
    Steps 2-4), in order, and mints **only** on an explicit ``AUTO_EXECUTE``
    for a ``safety_level == 'safe'`` op:

    1. ``lookup_descriptor`` — unknown op → :attr:`MintRefusalCode.DESCRIPTOR_UNKNOWN`.
    2. ``validate_params`` — invalid params → :attr:`MintRefusalCode.INVALID_PARAMS`.
    3. **safe-only wall** — ``descriptor.safety_level != 'safe'`` →
       :attr:`MintRefusalCode.OP_NOT_SAFE`. Checked **before** the policy
       gate so a non-``safe`` op is refused without even consulting it (and
       so a ``requires_approval`` non-``safe`` op is never parked).
    4. ``policy_gate`` — any verdict other than ``AUTO_EXECUTE`` refuses
       (``DENY`` → :attr:`MintRefusalCode.POLICY_DENIED`, ``NEEDS_APPROVAL``
       → :attr:`MintRefusalCode.NEEDS_APPROVAL`); the defensive
       ``is not AUTO_EXECUTE`` branch denies any unexpected verdict.

    On success it stamps ``params_hash`` on the synchronous mint audit row,
    then enqueues the command bound to ``(runner_id, op_id, target,
    params_hash, expires_at)`` with ``mint_audit_id`` set. The session is
    flushed, **not** committed — the caller owns the mint+audit commit.

    A refusal writes **no** ``gateway_command`` row and **no**
    ``approval_request`` row; the command never reaches a runner.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        operator: The authorising principal (the policy gate's subject).
        connector_id: The ``product-version`` connector id (parsed for the
            descriptor lookup).
        op_id: The operation the runner will execute.
        target: The resolved target row the policy gate scopes to (or
            ``None`` for a targetless synthetic op).
        params: The op params — validated, hashed, and stored verbatim.
        runner_id: The runner principal **name** the capability is bound to.
        target_descriptor: The centrally-resolved target descriptor shipped
            in the command payload (the runner has no target table); ``None``
            for a targetless op.
        expires_at: An optional caller deadline, bounded down to the default
            TTL ceiling; the default TTL when omitted.

    Returns:
        A :class:`MintResult` — minted (``command`` + ``mint_audit_id`` set)
        or refused (``refusal_code`` set, no rows written).
    """
    started = time.monotonic()
    params_hash = compute_params_hash(params)
    product, version, impl_id = parse_connector_id(connector_id)

    # --- Step 2: descriptor lookup ---------------------------------------
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )
    if descriptor is None:
        structlog.get_logger(__name__).info(
            "gateway_command_mint_refused",
            reason=MintRefusalCode.DESCRIPTOR_UNKNOWN.value,
            op_id=op_id,
            connector_id=connector_id,
            operator_sub=operator.sub,
        )
        return _refused(
            MintRefusalCode.DESCRIPTOR_UNKNOWN,
            f"no enabled descriptor for op {op_id!r} on connector {connector_id!r}",
        )

    # --- Step 3: parameter_schema validation -----------------------------
    validation_errors = validate_params(descriptor.parameter_schema, params)
    if validation_errors:
        structlog.get_logger(__name__).info(
            "gateway_command_mint_refused",
            reason=MintRefusalCode.INVALID_PARAMS.value,
            op_id=op_id,
            operator_sub=operator.sub,
        )
        return _refused(
            MintRefusalCode.INVALID_PARAMS,
            f"params failed schema validation for op {op_id!r}",
        )

    # --- Safe-only wall (v1 read-only guarantee) -------------------------
    # Bound to the real op-identity metadata read straight off the
    # descriptor (dispatcher.py:1772), NOT a re-derived value. Checked
    # before the policy gate so a non-'safe' op is refused centrally and is
    # never parked — change-ops-over-gateway is a v2 follow-on (#2415).
    if descriptor.safety_level != "safe":
        structlog.get_logger(__name__).warning(
            "gateway_command_mint_refused_non_safe",
            reason=MintRefusalCode.OP_NOT_SAFE.value,
            op_id=op_id,
            safety_level=descriptor.safety_level,
            operator_sub=operator.sub,
            runner_id=runner_id,
        )
        return _refused(
            MintRefusalCode.OP_NOT_SAFE,
            f"op {op_id!r} has safety_level {descriptor.safety_level!r}; the gateway "
            "mints only safety_level='safe' ops in v1",
        )

    # --- Step 4: policy gate (only AUTO_EXECUTE mints) -------------------
    verdict, gate_reason = await policy_gate(
        operator=operator, descriptor=descriptor, target=target
    )
    if verdict is not PermissionVerdict.AUTO_EXECUTE:
        code = (
            MintRefusalCode.NEEDS_APPROVAL
            if verdict is PermissionVerdict.NEEDS_APPROVAL
            else MintRefusalCode.POLICY_DENIED
        )
        structlog.get_logger(__name__).info(
            "gateway_command_mint_refused",
            reason=code.value,
            verdict=verdict.value,
            op_id=op_id,
            operator_sub=operator.sub,
        )
        return _refused(
            code,
            gate_reason or f"policy gate returned {verdict.value!r}, not auto-execute",
        )

    # --- Mint: synchronous audit row + bound command row -----------------
    mint_audit_id = uuid.uuid4()
    command = await enqueue_command(
        session,
        tenant_id=operator.tenant_id,
        runner_id=runner_id,
        op_id=op_id,
        params=params,
        enqueued_by_sub=operator.sub,
        target_descriptor=target_descriptor,
        params_hash=params_hash,
        expires_at=expires_at,
        mint_audit_id=mint_audit_id,
    )
    await _write_gateway_audit_row(
        session,
        audit_id=mint_audit_id,
        operator=operator,
        path=_MINT_AUDIT_PATH,
        status_code=_MINT_AUDIT_STATUS,
        duration_ms=(time.monotonic() - started) * 1000,
        target_id=getattr(target, "id", None) if target is not None else None,
        payload={
            "command_id": str(command.id),
            "op_id": op_id,
            "connector_id": connector_id,
            "runner_id": runner_id,
            "params_hash": params_hash,
            "result_status": "minted",
        },
    )
    structlog.get_logger(__name__).info(
        "gateway_command_minted",
        command_id=str(command.id),
        mint_audit_id=str(mint_audit_id),
        op_id=op_id,
        runner_id=runner_id,
        tenant_id=str(operator.tenant_id),
        expires_at=command.expires_at.isoformat(),
    )
    return MintResult(command=command, mint_audit_id=mint_audit_id)


async def consume_command(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    runner_id: str,
    command_id: uuid.UUID,
) -> GatewayCommand:
    """Win the one-way consumption latch for a delivered command.

    A single conditional ``UPDATE ... SET consumed_at = now WHERE id = :id
    AND consumed_at IS NULL AND status = 'delivered'`` (mould:
    :func:`meho_backplane.operations.approval_queue.claim_resume`). The
    winner (one row touched) may accept the result; a loser is refused so a
    result is accepted **at most once**. Flushed, not committed — the caller
    commits to persist the latch (two independent callers each on their own
    committed transaction race correctly).

    Raises:
        GatewayCommandNotFoundError: no row in this runner's queue (404).
        GatewayCommandAlreadyConsumedError: the latch is already claimed —
            a replay (409); emits a ``gateway_command_replay_refused`` log.
        GatewayCommandNotDeliveredError: the row is not ``delivered`` (a
            never-claimed ``pending`` row) (409).
    """
    row = await session.get(GatewayCommand, command_id)
    if row is None or row.tenant_id != tenant_id or row.runner_id != runner_id:
        raise GatewayCommandNotFoundError(command_id)

    now = datetime.now(UTC)
    result = await session.execute(
        update(GatewayCommand)
        .where(
            GatewayCommand.id == command_id,
            GatewayCommand.consumed_at.is_(None),
            GatewayCommand.status == GatewayCommandStatus.DELIVERED.value,
        )
        .values(consumed_at=now)
    )
    await session.flush()
    rowcount: int = result.rowcount  # type: ignore[attr-defined]
    if rowcount == 0:
        # Lost the latch: reload to classify replay (consumed) vs. a
        # never-delivered row.
        await session.refresh(row)
        if row.consumed_at is not None:
            structlog.get_logger(__name__).warning(
                "gateway_command_replay_refused",
                command_id=str(command_id),
                tenant_id=str(tenant_id),
                runner_id=runner_id,
                op_id=row.op_id,
            )
            raise GatewayCommandAlreadyConsumedError(command_id)
        raise GatewayCommandNotDeliveredError(command_id, row.status)

    row.consumed_at = now
    return row


async def accept_command_result(
    session: AsyncSession,
    *,
    operator: Operator,
    runner_id: str,
    command_id: uuid.UUID,
    outcome: GatewayCommandStatus,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> GatewayCommand:
    """Accept a runner's result once: consume the latch, record it, audit it.

    Orchestrates the runner-facing result path (#2498's ``POST
    .../result`` handler calls this): win the :func:`consume_command`
    consumption latch **before** anything is recorded or audited (a replay
    is refused here), flip the row to its terminal outcome
    (:func:`meho_backplane.gateway.queue.record_result`), then write the
    result audit row stamped ``parent_audit_id = mint_audit_id`` so the
    remote execution links back to its mint under one audit subtree.

    Flushed, not committed — the caller owns the commit so consume + record
    + audit land atomically.
    """
    started = time.monotonic()
    # Win the consumption latch first — a replayed result never reaches the
    # record / audit writes (raises AlreadyConsumed).
    await consume_command(
        session, tenant_id=operator.tenant_id, runner_id=runner_id, command_id=command_id
    )
    row = await record_result(
        session,
        tenant_id=operator.tenant_id,
        runner_id=runner_id,
        command_id=command_id,
        outcome=outcome,
        result=result,
        error=error,
    )
    await _write_gateway_audit_row(
        session,
        audit_id=uuid.uuid4(),
        operator=operator,
        path=_RESULT_AUDIT_PATH,
        status_code=_RESULT_AUDIT_STATUS,
        duration_ms=(time.monotonic() - started) * 1000,
        parent_audit_id=row.mint_audit_id,
        payload={
            "command_id": str(command_id),
            "op_id": row.op_id,
            "runner_id": runner_id,
            "outcome": outcome.value,
            "result_status": outcome.value,
        },
    )
    return row
