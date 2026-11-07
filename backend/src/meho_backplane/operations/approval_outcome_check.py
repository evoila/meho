# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approved runs whose outcome MEHO does not know, for operators to check (#5026).

MEHO sets ``resumed_at`` before it calls the vendor for the first run and
stores the result when the call returns. A re-run after a failed run
(#5417) clears the stored result and sets ``rerun_started_at``. So the
latest run started at ``rerun_started_at``, or at ``resumed_at`` when there
was no re-run. Two kinds of approved request leave MEHO without a known
outcome:

* ``uncertain`` -- the stored result says MEHO does not know if the change
  happened (#5025): a lost answer, a failed audit write, a restart in the
  middle of the run.
* ``started_no_result`` -- the latest run started more than
  ``APPROVAL_OUTCOME_UNKNOWN_AFTER_MINUTES`` minutes ago (default 15) and
  stored no result yet.

These requests form the "outcome unknown" list. REST, MCP, the CLI and the
console all read it with :func:`list_outcome_unknown`, so they show the same
rows. A person checks the target and records what they found with
:func:`mark_outcome_checked`: "it happened" or "it did not happen", with a
note. That writes one audit row and fills the ``outcome_check*`` columns.
The stored result stays as it was, and nothing runs. A checked request
leaves the list. A check covers only the reason the request had when it
was checked, and only the latest run at that time. So the request comes
back on the list when a later re-run has no result after the set minutes,
or when the run that was checked as ``started_no_result`` later stores an
``uncertain`` result.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Text, and_, cast, func, not_, or_, select
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


def _run_started_at(request: ApprovalRequest) -> datetime | None:
    """When the latest run started: the latest re-run, else the first run."""
    started = request.rerun_started_at or request.resumed_at
    return _as_utc(started) if started is not None else None


def _unknown_reason(
    request: ApprovalRequest, *, now: datetime | None, after: timedelta | None
) -> OutcomeUnknownReason | None:
    """Why the outcome of *request* is unknown, ignoring any check."""
    if request.status != ApprovalRequestStatus.APPROVED.value:
        return None
    if request.resume_result is not None:
        if envelope_is_uncertain(request.resume_result):
            return OutcomeUnknownReason.UNCERTAIN
        return None
    started = _run_started_at(request)
    if started is None:
        return None
    cutoff = (now or datetime.now(UTC)) - (after if after is not None else outcome_unknown_after())
    if started > cutoff:
        return None
    return OutcomeUnknownReason.STARTED_NO_RESULT


def _check_covers(request: ApprovalRequest, reason: OutcomeUnknownReason) -> bool:
    """True when a check recorded *reason* during the latest run."""
    if request.outcome_checked_at is None or request.outcome_check_reason != reason.value:
        return False
    started = _run_started_at(request)
    return started is None or _as_utc(request.outcome_checked_at) >= started


def outcome_unknown(
    request: ApprovalRequest,
    *,
    now: datetime | None = None,
    after: timedelta | None = None,
) -> OutcomeUnknown | None:
    """Return why *request* is on the list, or ``None`` when it is not.

    The Python twin of :func:`_outcome_unknown_clause`, for one row.
    """
    reason = _unknown_reason(request, now=now, after=after)
    if reason is None or _check_covers(request, reason):
        return None
    if reason is OutcomeUnknownReason.UNCERTAIN:
        return OutcomeUnknown(reason, UNCERTAIN_ADVICE)
    return OutcomeUnknown(reason, STARTED_NO_RESULT_ADVICE)


def _outcome_unknown_clause(cutoff: datetime) -> Any:
    """The WHERE clause of the list. *cutoff* is ``now`` minus the setting.

    The same rule as :func:`envelope_is_uncertain`: a stored result whose
    ``status`` is not ``ok`` and whose ``extras.execution_outcome`` is
    ``uncertain``. The same check rule as :func:`_check_covers`.
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
    run_started_at = func.coalesce(ApprovalRequest.rerun_started_at, ApprovalRequest.resumed_at)
    started_no_result = and_(
        no_result,
        run_started_at.is_not(None),
        run_started_at <= cutoff,
    )

    def covered(reason: OutcomeUnknownReason) -> Any:
        return and_(
            ApprovalRequest.outcome_checked_at.is_not(None),
            func.coalesce(ApprovalRequest.outcome_check_reason, "") == reason.value,
            or_(
                run_started_at.is_(None),
                ApprovalRequest.outcome_checked_at >= run_started_at,
            ),
        )

    return and_(
        ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
        or_(
            and_(uncertain, not_(covered(OutcomeUnknownReason.UNCERTAIN))),
            and_(started_no_result, not_(covered(OutcomeUnknownReason.STARTED_NO_RESULT))),
        ),
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
    list right now. The row is locked, the ``outcome_check*`` columns are
    set (an older check of an earlier run or another reason is replaced),
    and one ``approval.outcome_check`` audit row is written. The stored
    result (``resume_result``) is not changed, and nothing runs.

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
        OutcomeAlreadyCheckedError: Someone already checked it for its
            current reason and its latest run.
        OutcomeNotUnknownError: The request is not on the list.
    """
    _check_reviewer_role(operator)
    note = note.strip()
    if not note:
        raise ValueError("note must not be empty")
    request = await _load_for_tenant(session, request_id, operator.tenant_id, for_update=True)
    reason = _unknown_reason(request, now=None, after=None)
    if reason is None:
        raise OutcomeNotUnknownError(request_id)
    if _check_covers(request, reason):
        raise OutcomeAlreadyCheckedError(request_id)

    checked_at = datetime.now(UTC)
    request.outcome_checked_at = checked_at
    request.outcome_checked_by = operator.sub
    request.outcome_checked_by_name = operator.name or None
    request.outcome_check_verdict = verdict.value
    request.outcome_check_note = note
    request.outcome_check_reason = reason.value
    await session.flush()

    started = _run_started_at(request)
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
            "outcome_unknown_reason": reason.value,
            "resumed_at": _as_utc(request.resumed_at).isoformat() if request.resumed_at else None,
            "rerun_count": request.rerun_count,
            "run_started_at": started.isoformat() if started else None,
        },
    )
    request._audit_id = audit_id  # type: ignore[attr-defined]
    return request
