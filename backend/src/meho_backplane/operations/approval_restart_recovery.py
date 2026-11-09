# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Close the approved runs that a restart cut short (#5025).

MEHO marks an approved request as started (``resumed_at``) before it calls
the vendor, and it stores the result when the call returns. If MEHO stops in
between (a restart, a crash), the request stays ``started`` with no result,
for ever. The vendor may have done the work, so nobody can call it failed.

At start-up, :func:`recover_interrupted_approvals` gives each such request a
stored ``uncertain`` result and one audit row. It never runs anything: an
approved request starts only once, so the request cannot run again, and the
approver is told to check the target.

This is safe because, on a one-machine install, the old process has stopped
before the new one starts (the old pod is stopped first and its running work
drained), so a request that is ``started`` with no result cannot still be
running. An install with several pods needs a lease (a time-limited claim
that shows a run is alive) before it may use this step.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditEventClass, Tenant
from meho_backplane.operations.approval_queue import (
    _write_audit_row,
    expiry_operator,
)
from meho_backplane.operations.uncertain_outcome import result_interrupted_by_restart

__all__ = [
    "mark_interrupted_approvals_uncertain",
    "recover_interrupted_approvals",
]

_log = structlog.get_logger(__name__)

#: Status code of the audit row. The run did not end in a known way.
_RECOVERY_STATUS_CODE = 500


async def mark_interrupted_approvals_uncertain(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[ApprovalRequest]:
    """Store an ``uncertain`` result on every started approved request of a tenant.

    Rows another transaction holds are skipped. The caller commits.
    """
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.tenant_id == tenant_id)
        .where(ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value)
        .where(ApprovalRequest.resumed_at.is_not(None))
        .where(ApprovalRequest.resume_result.is_(None))
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    operator = expiry_operator(tenant_id)
    for request in rows:
        audit_id = uuid.uuid4()
        await _write_audit_row(
            session,
            audit_id=audit_id,
            event_class=AuditEventClass.APPROVAL_DECISION,
            operator=operator,
            request=request,
            path="approval.recovery",
            status_code=_RECOVERY_STATUS_CODE,
            duration_ms=0.0,
            extra_payload={
                "execution_outcome": "uncertain",
                "reason": "interrupted_by_restart",
                "resumed_at": request.resumed_at.isoformat() if request.resumed_at else None,
            },
        )
        envelope = result_interrupted_by_restart(request.op_id).model_dump(mode="json")
        envelope["audit_id"] = str(audit_id)
        request.resume_result = envelope
    return rows


async def recover_interrupted_approvals() -> int:
    """Close every approved run a restart cut short; return how many (#5025).

    Run once at start-up. Each tenant is handled in its own transaction, and
    a failure for one tenant is logged and does not stop the others.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())

    recovered = 0
    for tenant_id in tenant_ids:
        async with sessionmaker() as session:
            try:
                rows = await mark_interrupted_approvals_uncertain(session, tenant_id=tenant_id)
                await session.commit()
            except Exception:
                await session.rollback()
                _log.exception("approval_restart_recovery_tenant_failed", tenant_id=str(tenant_id))
                continue
        recovered += len(rows)
    if recovered:
        _log.warning("approval_restart_recovery_done", uncertain_total=recovered)
    return recovered
