# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The call-time check of run approvals (#4126).

The policy gate calls :func:`consult_and_record_run_approval` for a service
principal whose call would wait for approval and that no standing grant
covers. The rules are in :mod:`meho_backplane.operations.run_approvals`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select

from meho_backplane.auth.current_tenant import tenant_role_source_payload
from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog, EndpointDescriptor, RunApproval
from meho_backplane.operations.run_approval_schemas import RunApprovalEntry
from meho_backplane.operations.run_approvals import as_utc, target_kinds_enabled
from meho_backplane.operations.service_grants import (
    caller_address_grant_reason,
    delete_grant_reason,
)

__all__ = ["consult_and_record_run_approval"]

_log = structlog.get_logger(__name__)


def _match(
    entries: list[dict[str, Any]],
    *,
    connector_id: str,
    op_id: str,
    target_id: uuid.UUID | None,
    target_product: str | None,
) -> str | None:
    """How the list covers the call: ``target``, ``no_target``, ``kind`` or ``None``.

    An exact target (or no target) wins over a target kind.
    """
    kind_match = False
    for raw in entries:
        entry = RunApprovalEntry.model_validate(raw)
        if entry.connector_id != connector_id or entry.op_id != op_id:
            continue
        if target_id is None:
            if entry.target_id is None and entry.target_product is None:
                return "no_target"
            continue
        if entry.target_id == target_id:
            return "target"
        if entry.target_product is not None and entry.target_product == target_product:
            kind_match = True
    return "kind" if kind_match else None


def _target_fields(target: Any) -> tuple[uuid.UUID | None, str | None]:
    raw_id = getattr(target, "id", None) if target is not None else None
    product = getattr(target, "product", None) if target is not None else None
    return (
        raw_id if isinstance(raw_id, uuid.UUID) else None,
        product if isinstance(product, str) else None,
    )


def _never_covered(descriptor: EndpointDescriptor) -> bool:
    from meho_backplane.settings import get_settings

    if caller_address_grant_reason(descriptor) is not None:
        return True
    patterns = get_settings().service_grant_delete_shaped_patterns
    return delete_grant_reason(descriptor.op_id, patterns, descriptor) is not None


async def _find_covering(
    operator: Operator,
    *,
    work_ref: str,
    connector_id: str,
    op_id: str,
    target: Any,
) -> tuple[RunApproval, str] | None:
    target_id, target_product = _target_fields(target)
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(RunApproval)
                .where(
                    RunApproval.tenant_id == operator.tenant_id,
                    RunApproval.principal_sub == operator.sub,
                    RunApproval.work_ref == work_ref,
                    RunApproval.approved_at.is_not(None),
                    RunApproval.revoked_at.is_(None),
                    RunApproval.ended_at.is_(None),
                )
                .order_by(RunApproval.approved_at.desc())
            )
        ).all()
        for row in rows:
            if as_utc(row.expires_at) <= now:
                continue
            matched_by = _match(
                row.entries,
                connector_id=connector_id,
                op_id=op_id,
                target_id=target_id,
                target_product=target_product,
            )
            if matched_by == "kind" and not await target_kinds_enabled(session, operator.tenant_id):
                matched_by = None
            if matched_by is not None:
                return row, matched_by
    return None


async def consult_and_record_run_approval(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str,
    record: bool = True,
) -> uuid.UUID | None:
    """Return the id of the live run approval that covers this call, or ``None``.

    Called by the policy gate for a service principal whose call would wait
    for approval and that no standing grant covers. The run reference is
    the request's ``Meho-Work-Ref``; a call without one is never covered.
    An operation with a delete sign, or one that connects to an address the
    caller gives, is never covered.

    On a hit with ``record=True`` it writes one ``approval.decision`` audit
    row (same shape as a standing grant use) in its own committed
    transaction before the call runs, and binds ``audit_run_approval_id``
    so the call's own audit row names the approval too. ``record=False``
    (the preview) only looks and writes nothing.
    """
    from meho_backplane.operations._audit import work_ref_var

    work_ref = work_ref_var.get()
    if not work_ref or _never_covered(descriptor):
        return None
    found = await _find_covering(
        operator,
        work_ref=work_ref,
        connector_id=connector_id,
        op_id=descriptor.op_id,
        target=target,
    )
    if found is None:
        return None
    row, matched_by = found
    if not record:
        return row.id
    target_id, _ = _target_fields(target)
    await _record_use(
        operator=operator,
        row=row,
        connector_id=connector_id,
        op_id=descriptor.op_id,
        target_id=target_id,
        matched_by=matched_by,
    )
    structlog.contextvars.bind_contextvars(audit_run_approval_id=str(row.id))
    _log.info(
        "run_approval_auto_approved",
        run_approval_id=str(row.id),
        op_id=descriptor.op_id,
        connector_id=connector_id,
        principal_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        matched_by=matched_by,
    )
    return row.id


async def _record_use(
    *,
    operator: Operator,
    row: RunApproval,
    connector_id: str,
    op_id: str,
    target_id: uuid.UUID | None,
    matched_by: str,
) -> None:
    """Write the ``approval.decision`` audit row of one covered call.

    The same shape as a standing grant use
    (:func:`meho_backplane.operations.service_grants._record_grant_use`):
    ``method='APPROVAL'``, ``path='approval.decision'``, status ``200``,
    ``decision='auto-approved'``. ``reviewed_by`` reads
    ``run_approval:<id>`` and the payload names the run approval, the
    person who approved it and how the list matched the call.
    """
    from meho_backplane.operations._audit import (
        parent_audit_id_var,
        resolve_agent_session_id,
    )
    from meho_backplane.request_usage.collector import current_request_id

    payload: dict[str, Any] = {
        "decision": "auto-approved",
        "reviewed_by": f"run_approval:{row.id}",
        "run_approval_id": str(row.id),
        "approved_by_sub": row.approved_by_sub,
        "op_id": op_id,
        "connector_id": connector_id,
        "principal_sub": operator.sub,
        "reason": f"auto-approved by run approval {row.id}",
        "result_status": "decision",
        "matched_by": matched_by,
    }
    payload.update(tenant_role_source_payload(operator))
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                event_class=AuditEventClass.APPROVAL_DECISION,
                occurred_at=datetime.now(UTC),
                operator_sub=operator.sub,
                tenant_id=operator.tenant_id,
                target_id=target_id,
                parent_audit_id=parent_audit_id_var.get(),
                agent_session_id=resolve_agent_session_id(),
                method="APPROVAL",
                path="approval.decision",
                status_code=200,
                request_id=current_request_id(),
                duration_ms=Decimal("0.00"),
                payload=payload,
                work_ref=row.work_ref,
            )
        )
        await session.commit()
