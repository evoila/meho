# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approved runs whose outcome MEHO does not know, for operators to check (#5026).

An approved request runs once. MEHO sets ``resumed_at`` before it calls the
vendor and stores the result when the call returns. Two kinds of approved
request leave MEHO without a known outcome:

* ``uncertain`` -- the stored result says MEHO does not know if the change
  happened (#5025): a lost answer, a failed audit write, a restart in the
  middle of the run.
* ``started_no_result`` -- the run started more than
  ``APPROVAL_OUTCOME_UNKNOWN_AFTER_MINUTES`` minutes ago (default 15) and
  stored no result yet.

These requests form the "outcome unknown" list. REST, MCP, the CLI and the
console all read it with :func:`list_outcome_unknown`, so they show the same
rows. A person checks the target and records what they found with
:func:`mark_outcome_checked`: "it happened" or "it did not happen", with a
note. That writes one audit row and fills the ``outcome_check*`` columns.
The stored result stays as it was, and nothing runs. A checked request
leaves the list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditEventClass
from meho_backplane.operations.approval_queue import (
    ApprovalError,
    ApprovalListOrder,
    _check_reviewer_role,
    _load_for_tenant,
    _write_audit_row,
)
from meho_backplane.operations.uncertain_outcome import (
    UNCERTAIN_ADVICE,
    UNCERTAIN_OUTCOME,
    envelope_is_uncertain,
)
from meho_backplane.settings import get_settings

__all__ = [
    "STARTED_NO_RESULT_ADVICE",
    "OutcomeAlreadyCheckedError",
    "OutcomeCheck",
    "OutcomeCheckVerdict",
    "OutcomeNotUnknownError",
    "OutcomeUnknown",
    "OutcomeUnknownReason",
    "list_outcome_unknown",
    "mark_outcome_checked",
    "outcome_check",
    "outcome_unknown",
    "outcome_unknown_after",
]


class OutcomeCheckVerdict(StrEnum):
    """What the person found on the target."""

    HAPPENED = "happened"
    DID_NOT_HAPPEN = "did_not_happen"


class OutcomeUnknownReason(StrEnum):
    """Why a request is on the "outcome unknown" list."""

    UNCERTAIN = "uncertain"
    STARTED_NO_RESULT = "started_no_result"


#: What to check for a run that started and stored no result.
STARTED_NO_RESULT_ADVICE: Final[str] = (
    "The run started and stored no result. MEHO does not know if the change "
    "happened. Do not run it again. Check the target first."
)

#: Status code of the audit row of a check. Recording the check worked.
_CHECK_STATUS_CODE: Final[int] = 200


@dataclass(frozen=True)
class OutcomeUnknown:
    """Why a request is on the list, and what the person should check."""

    reason: OutcomeUnknownReason
    advice: str


@dataclass(frozen=True)
class OutcomeCheck:
    """What a person recorded after they checked the target."""

    verdict: OutcomeCheckVerdict
    note: str
    checked_by: str
    checked_by_name: str | None
    checked_at: str


class OutcomeNotUnknownError(ApprovalError):
    """The request is not on the "outcome unknown" list, so it cannot be checked."""

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(f"approval_request {request_id} has no unknown outcome to check")


class OutcomeAlreadyCheckedError(ApprovalError):
    """Someone already marked the request as checked."""

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(f"approval_request {request_id} is already checked")


def outcome_unknown_after() -> timedelta:
    """How long a started run may go without a result before it is listed."""
    return timedelta(minutes=get_settings().approval_outcome_unknown_after_minutes)


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime (the SQLite test driver) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def outcome_unknown(
    request: ApprovalRequest,
    *,
    now: datetime | None = None,
    after: timedelta | None = None,
) -> OutcomeUnknown | None:
    """Return why *request* is on the list, or ``None`` when it is not.

    The Python twin of :func:`_outcome_unknown_clause`, for one row.
    """
    if request.status != ApprovalRequestStatus.APPROVED.value:
        return None
    if request.outcome_checked_at is not None:
        return None
    if request.resume_result is not None:
        if envelope_is_uncertain(request.resume_result):
            return OutcomeUnknown(OutcomeUnknownReason.UNCERTAIN, UNCERTAIN_ADVICE)
        return None
    if request.resumed_at is None:
        return None
    cutoff = (now or datetime.now(UTC)) - (after if after is not None else outcome_unknown_after())
    if _as_utc(request.resumed_at) > cutoff:
        return None
    return OutcomeUnknown(OutcomeUnknownReason.STARTED_NO_RESULT, STARTED_NO_RESULT_ADVICE)


def _outcome_unknown_clause(cutoff: datetime) -> Any:
    """The WHERE clause of the list. *cutoff* is ``now`` minus the setting.

    The same rule as :func:`envelope_is_uncertain`: a stored result whose
    ``status`` is not ``ok`` and whose ``extras.execution_outcome`` is
    ``uncertain``.
    """
    result = ApprovalRequest.resume_result
    result_status = func.coalesce(result["status"].as_string(), "")
    execution_outcome = result[("extras", "execution_outcome")].as_string()
    uncertain = and_(
        result_status != "ok",
        execution_outcome == UNCERTAIN_OUTCOME,
    )
    # "No result" is SQL NULL, or a JSON ``null`` (a row written with an
    # explicit ``None``); both load as ``None`` in Python.
    no_result = or_(result.is_(None), cast(result, Text) == "null")
    started_no_result = and_(
        no_result,
        ApprovalRequest.resumed_at.is_not(None),
        ApprovalRequest.resumed_at <= cutoff,
    )
    return and_(
        ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
        ApprovalRequest.outcome_checked_at.is_(None),
        or_(uncertain, started_no_result),
    )


async def list_outcome_unknown(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    work_ref: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    order: ApprovalListOrder = "newest",
    now: datetime | None = None,
) -> list[ApprovalRequest]:
    """List the approved requests of *tenant_id* whose outcome is unknown.

    ``limit=None`` returns every match. Sorted by ``created_at`` like the
    other approval lists.
    """
    cutoff = (now or datetime.now(UTC)) - outcome_unknown_after()
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.tenant_id == tenant_id)
        .where(_outcome_unknown_clause(cutoff))
    )
    if work_ref is not None:
        stmt = stmt.where(ApprovalRequest.work_ref == work_ref)
    if order == "oldest":
        stmt = stmt.order_by(ApprovalRequest.created_at.asc(), ApprovalRequest.id.asc())
    else:
        stmt = stmt.order_by(ApprovalRequest.created_at.desc())
    stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def outcome_check(request: ApprovalRequest) -> OutcomeCheck | None:
    """Return what a person recorded for *request*, or ``None`` when unchecked."""
    if request.outcome_checked_at is None or request.outcome_check_verdict is None:
        return None
    return OutcomeCheck(
        verdict=OutcomeCheckVerdict(request.outcome_check_verdict),
        note=request.outcome_check_note or "",
        checked_by=request.outcome_checked_by or "",
        checked_by_name=request.outcome_checked_by_name,
        checked_at=_as_utc(request.outcome_checked_at).isoformat(),
    )


async def mark_outcome_checked(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    verdict: OutcomeCheckVerdict,
    note: str,
) -> ApprovalRequest:
    """Record what a person found on the target of an approved run.

    Only a person who may decide approvals may record a check (the same rule
    as approve and reject). The request must be on the "outcome unknown"
    list right now. The row is locked, the five ``outcome_check*`` columns
    are set, and one ``approval.outcome_check`` audit row is written. The
    stored result (``resume_result``) is not changed, and nothing runs.

    Args:
        session: Open session; flushed, not committed.
        request_id: The approved request.
        operator: The person who checked.
        verdict: What they found.
        note: What they checked. Must not be empty.

    Raises:
        NonHumanApprovalError: The caller is an agent or a service.
        UnauthorizedApprovalError: The caller may not decide approvals.
        ValueError: *note* is empty.
        ApprovalNotFoundError: No such request in this tenant.
        OutcomeAlreadyCheckedError: Someone already checked it.
        OutcomeNotUnknownError: The request is not on the list.
    """
    _check_reviewer_role(operator)
    note = note.strip()
    if not note:
        raise ValueError("note must not be empty")
    request = await _load_for_tenant(session, request_id, operator.tenant_id, for_update=True)
    if request.outcome_checked_at is not None:
        raise OutcomeAlreadyCheckedError(request_id)
    unknown = outcome_unknown(request)
    if unknown is None:
        raise OutcomeNotUnknownError(request_id)

    checked_at = datetime.now(UTC)
    request.outcome_checked_at = checked_at
    request.outcome_checked_by = operator.sub
    request.outcome_checked_by_name = operator.name or None
    request.outcome_check_verdict = verdict.value
    request.outcome_check_note = note
    await session.flush()

    audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=audit_id,
        event_class=AuditEventClass.APPROVAL_DECISION,
        operator=operator,
        request=request,
        path="approval.outcome_check",
        status_code=_CHECK_STATUS_CODE,
        duration_ms=0.0,
        extra_payload={
            "verdict": verdict.value,
            "note": note,
            "outcome_unknown_reason": unknown.reason.value,
            "resumed_at": _as_utc(request.resumed_at).isoformat() if request.resumed_at else None,
        },
    )
    request._audit_id = audit_id  # type: ignore[attr-defined]
    return request
