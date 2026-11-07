# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run approvals: a person approves the operation list of one automation run once (#4126).

The automation add-on runs as a service principal. Without help, every
change it makes waits for approval, one call at a time. A run approval
lets a person approve the whole list of one run once:

1. The add-on asks (:meth:`RunApprovalService.request`): its run reference
   (``work_ref``) and the list of ``(connector_id, op_id, target)`` entries.
   The request is ``pending``.
2. A person approves it in MEHO (:meth:`RunApprovalService.approve`), on
   REST or the CLI. Only a person: the routes refuse a machine token, and
   the approve needs a fresh approval sign-in. There is no MCP tool.
3. The add-on reads the status (``GET``) and runs. A call of that principal
   with that ``Meho-Work-Ref`` that matches an entry runs without a new
   approval (:func:`~meho_backplane.operations.run_approval_gate.consult_and_record_run_approval`,
   called by the policy gate). The gate writes one ``approval.decision`` audit row that names
   the run approval, and the call's own audit row carries
   ``run_approval_id`` too. Every call still goes through the policy gate
   and the audit log.
4. It ends when the add-on ends the run (:meth:`RunApprovalService.end`),
   when a person revokes it, or at ``expires_at``.

Rules that never change:

* The approval is keyed on the token subject (``sub``) of the principal
  that asked, plus the run reference, inside one tenant. Never on a
  client name.
* A call outside the list waits for approval, as before.
* An operation with a delete sign (the ``destructive`` level, the HTTP verb
  ``DELETE``, a delete name; the same rule as the standing grants,
  :func:`~meho_backplane.operations.service_grants.delete_grant_reason`)
  is never covered. A request that lists one is refused, and the gate
  never lets a run approval clear one.
* An operation that connects to an address the caller gives is never
  covered either (a person approves each call).
* A target kind entry (``target_product``) works only when the tenant
  setting ``run_approval_target_kinds_enabled`` is on.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RunApproval, Tenant
from meho_backplane.operations.role_floors import role_floor_denial
from meho_backplane.operations.run_approval_schemas import (
    RunApprovalCreate,
    RunApprovalEntry,
    RunApprovalRead,
    RunApprovalStatus,
)
from meho_backplane.operations.service_grants import (
    caller_address_grant_reason,
    delete_grant_reason,
)

__all__ = [
    "DEFAULT_RUN_APPROVAL_HOURS",
    "MAX_RUN_APPROVAL_DAYS",
    "RunApprovalConflictError",
    "RunApprovalError",
    "RunApprovalNotFoundError",
    "RunApprovalRoleFloorError",
    "RunApprovalService",
    "RunApprovalValidationError",
    "as_utc",
    "target_kinds_enabled",
]

_log = structlog.get_logger(__name__)

#: How long a run approval lasts when the request names no ``expires_at``.
DEFAULT_RUN_APPROVAL_HOURS = 24
#: The longest a run approval may last, counted from the request.
MAX_RUN_APPROVAL_DAYS = 7


class RunApprovalError(Exception):
    """Base error; ``message`` is the plain text for the caller."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class RunApprovalValidationError(RunApprovalError):
    """The request is well-formed but not allowed (HTTP 422)."""


class RunApprovalNotFoundError(RunApprovalError):
    """No such run approval in the caller's tenant (HTTP 404)."""


class RunApprovalConflictError(RunApprovalError):
    """The run approval is not in a state that allows the change (HTTP 409)."""


class RunApprovalRoleFloorError(RunApprovalError):
    """The approver's role is below the role an entry needs (HTTP 403)."""


def as_utc(value: datetime) -> datetime:
    """Treat a naive time (SQLite gives those back) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _status(row: RunApproval, now: datetime) -> RunApprovalStatus:
    if row.revoked_at is not None:
        return "revoked"
    if row.ended_at is not None:
        return "ended"
    if as_utc(row.expires_at) <= now:
        return "expired"
    return "approved" if row.approved_at is not None else "pending"


def _read(row: RunApproval, now: datetime) -> RunApprovalRead:
    return RunApprovalRead(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_sub=row.principal_sub,
        work_ref=row.work_ref,
        entries=[RunApprovalEntry.model_validate(entry) for entry in row.entries],
        reason=row.reason,
        status=_status(row, now),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        approved_at=row.approved_at,
        approved_by_sub=row.approved_by_sub,
        revoked_at=row.revoked_at,
        revoked_by_sub=row.revoked_by_sub,
        revoked_reason=row.revoked_reason,
        ended_at=row.ended_at,
    )


def _expires_at(requested: datetime, expires_at: datetime | None) -> datetime:
    if expires_at is None:
        return requested + timedelta(hours=DEFAULT_RUN_APPROVAL_HOURS)
    if expires_at.tzinfo is None:
        raise RunApprovalValidationError("expires_at must carry a time zone (UTC preferred)")
    if expires_at <= requested:
        raise RunApprovalValidationError("expires_at must be in the future")
    if expires_at > requested + timedelta(days=MAX_RUN_APPROVAL_DAYS):
        raise RunApprovalValidationError(
            f"a run approval lasts at most {MAX_RUN_APPROVAL_DAYS} days; set an earlier expires_at"
        )
    return expires_at


async def target_kinds_enabled(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    value = await session.scalar(
        select(Tenant.run_approval_target_kinds_enabled).where(Tenant.id == tenant_id)
    )
    return bool(value)


async def _entry_refusal(tenant_id: uuid.UUID, entry: RunApprovalEntry) -> str | None:
    """Why *entry* can never be covered by a run approval, or ``None``."""
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
    from meho_backplane.settings import get_settings

    for field, value in (
        ("op_id", entry.op_id),
        ("connector_id", entry.connector_id),
        ("target_product", entry.target_product or ""),
    ):
        if "*" in value:
            return f"{field} must be an exact value, not a pattern (got {value!r})"
    product, version, impl_id = parse_connector_id(entry.connector_id)
    descriptor = await lookup_descriptor(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=entry.op_id
    )
    reason = caller_address_grant_reason(descriptor) or delete_grant_reason(
        entry.op_id, get_settings().service_grant_delete_shaped_patterns, descriptor
    )
    if reason is None:
        return None
    return (
        f"{reason}. A run approval never covers it: leave it out of the list, "
        "and a person approves each call"
    )


async def _load(
    session: AsyncSession, tenant_id: uuid.UUID, run_approval_id: uuid.UUID
) -> RunApproval:
    row = await session.scalar(
        select(RunApproval).where(
            RunApproval.id == run_approval_id, RunApproval.tenant_id == tenant_id
        )
    )
    if row is None:
        raise RunApprovalNotFoundError("run_approval_not_found")
    return row


class RunApprovalService:
    """Tenant-scoped request / read / approve / revoke / end of run approvals.

    Stateless and async; each method opens its own session. The REST routes
    own the role, person and fresh sign-in checks.
    """

    async def request(self, operator: Operator, payload: RunApprovalCreate) -> RunApprovalRead:
        """Store a ``pending`` request of *operator* (a service principal)."""
        now = datetime.now(UTC)
        expires_at = _expires_at(now, payload.expires_at)
        for index, entry in enumerate(payload.entries):
            refusal = await _entry_refusal(operator.tenant_id, entry)
            if refusal is not None:
                raise RunApprovalValidationError(f"entries[{index}]: {refusal}")
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            if any(entry.target_product for entry in payload.entries) and not (
                await target_kinds_enabled(session, operator.tenant_id)
            ):
                raise RunApprovalValidationError(
                    "an entry names a target kind (target_product), but this tenant does not "
                    "allow target kinds in run approvals; name each target, or ask a tenant "
                    "admin to turn on run_approval_target_kinds in the settings file"
                )
            await self._refuse_second_live(session, operator, payload.work_ref, now)
            row = RunApproval(
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
                work_ref=payload.work_ref,
                entries=[e.model_dump(mode="json", exclude_none=True) for e in payload.entries],
                reason=payload.reason,
                requested_at=now,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            read = _read(row, now)
        _log.info(
            "run_approval_requested",
            tenant_id=str(operator.tenant_id),
            run_approval_id=str(read.id),
            principal_sub=operator.sub,
            work_ref=payload.work_ref,
            entry_count=len(payload.entries),
        )
        return read

    @staticmethod
    async def _refuse_second_live(
        session: AsyncSession, operator: Operator, work_ref: str, now: datetime
    ) -> None:
        rows = (
            await session.scalars(
                select(RunApproval).where(
                    RunApproval.tenant_id == operator.tenant_id,
                    RunApproval.principal_sub == operator.sub,
                    RunApproval.work_ref == work_ref,
                    RunApproval.revoked_at.is_(None),
                    RunApproval.ended_at.is_(None),
                )
            )
        ).all()
        live = [row for row in rows if as_utc(row.expires_at) > now]
        if live:
            raise RunApprovalConflictError(
                f"run approval {live[0].id} for this run is still {_status(live[0], now)}; "
                "end it or wait until a person revokes it before you ask again"
            )

    async def list_(
        self,
        tenant_id: uuid.UUID,
        *,
        principal_sub: str | None = None,
        status: RunApprovalStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunApprovalRead]:
        """List the tenant's run approvals, newest first."""
        now = datetime.now(UTC)
        stmt = select(RunApproval).where(RunApproval.tenant_id == tenant_id)
        if principal_sub is not None:
            stmt = stmt.where(RunApproval.principal_sub == principal_sub)
        stmt = stmt.order_by(RunApproval.requested_at.desc(), RunApproval.id)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows: Sequence[RunApproval] = (await session.scalars(stmt)).all()
        reads = [_read(row, now) for row in rows]
        if status is not None:
            reads = [read for read in reads if read.status == status]
        return reads[offset : offset + limit]

    async def get(self, tenant_id: uuid.UUID, run_approval_id: uuid.UUID) -> RunApprovalRead:
        """Return one run approval; :exc:`RunApprovalNotFoundError` across tenants."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await _load(session, tenant_id, run_approval_id)
            return _read(row, datetime.now(UTC))

    async def approve(self, operator: Operator, run_approval_id: uuid.UUID) -> RunApprovalRead:
        """A person approves a ``pending`` request; the whole list at once."""
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await _load(session, operator.tenant_id, run_approval_id)
            status = _status(row, now)
            if status != "pending":
                raise RunApprovalConflictError(
                    f"run approval {row.id} is {status}; only a pending one can be approved"
                )
            for entry in row.entries:
                denial = role_floor_denial(str(entry["op_id"]), operator.tenant_role)
                if denial is not None:
                    raise RunApprovalRoleFloorError(f"role_floor_required: {denial}")
            row.approved_at = now
            row.approved_by_sub = operator.sub
            await session.commit()
            read = _read(row, now)
        _log.info(
            "run_approval_approved",
            tenant_id=str(operator.tenant_id),
            run_approval_id=str(run_approval_id),
            approved_by_sub=operator.sub,
        )
        return read

    async def revoke(
        self, operator: Operator, run_approval_id: uuid.UUID, reason: str | None
    ) -> RunApprovalRead:
        """A person revokes (or turns down) a ``pending`` or ``approved`` run approval."""
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await _load(session, operator.tenant_id, run_approval_id)
            status = _status(row, now)
            if status not in ("pending", "approved"):
                raise RunApprovalConflictError(f"run approval {row.id} is already {status}")
            row.revoked_at = now
            row.revoked_by_sub = operator.sub
            row.revoked_reason = reason
            await session.commit()
            read = _read(row, now)
        _log.info(
            "run_approval_revoked",
            tenant_id=str(operator.tenant_id),
            run_approval_id=str(run_approval_id),
            revoked_by_sub=operator.sub,
        )
        return read

    async def end(self, operator: Operator, run_approval_id: uuid.UUID) -> RunApprovalRead:
        """The principal that asked ends its run; the approval covers nothing more."""
        now = datetime.now(UTC)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            row = await _load(session, operator.tenant_id, run_approval_id)
            if row.principal_sub != operator.sub:
                raise RunApprovalNotFoundError("run_approval_not_found")
            status = _status(row, now)
            if status not in ("pending", "approved"):
                raise RunApprovalConflictError(f"run approval {row.id} is already {status}")
            row.ended_at = now
            await session.commit()
            read = _read(row, now)
        _log.info(
            "run_approval_ended",
            tenant_id=str(operator.tenant_id),
            run_approval_id=str(run_approval_id),
            principal_sub=operator.sub,
        )
        return read
