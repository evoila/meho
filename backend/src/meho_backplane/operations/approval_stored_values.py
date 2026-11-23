# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stored approval request keeps the values of its call only while it can run.

Two parts:

* :func:`expire_unclaimed_approved_requests` -- an ``approved`` request that
  nobody started within ``APPROVAL_DEFAULT_TTL`` after its decision ends as
  ``expired``, the same way a waiting request ends at its deadline: one
  decision audit row, and
  :func:`~meho_backplane.operations.approval_queue.end_run_input` drops the
  encrypted handoff row and hides each secret-named value of the stored
  params. The TTL sweep (:mod:`meho_backplane.operations.approval_expiry`)
  runs it next to the expiry of waiting requests.
* :func:`hide_values_of_requests_that_cannot_run` -- run at start-up. Every
  request that is not waiting, was never started and cannot run gets
  the same clean-up, in small batches. An approved request of another
  release also ends as ``expired`` in the same transaction, so no MEHO of
  any release can start it after its inputs are gone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import structlog
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload
from meho_backplane.operations.approval_queue import (
    ApprovalExpiredReason,
    _as_utc,
    _check_reviewer_role,
    _deadline_has_passed,
    _expire_request,
    _now,
    _resolve_default_ttl,
    expiry_operator,
    params_with_secret_fields_hidden,
    record_approval_step_event,
    resume_parent_without_inputs,
)
from meho_backplane.version import running_release_id

__all__ = [
    "CLEAN_UP_BATCH_SIZE",
    "CleanUpCount",
    "expire_unclaimed_approved_requests",
    "hide_values_of_requests_that_cannot_run",
]

_log = structlog.get_logger(__name__)

#: How many requests one transaction of the start-up clean-up reads.
CLEAN_UP_BATCH_SIZE: Final[int] = 500

#: Statuses of a request that ended without a run.
_ENDED_STATUSES: Final[tuple[str, ...]] = (
    ApprovalRequestStatus.REJECTED.value,
    ApprovalRequestStatus.EXPIRED.value,
    ApprovalRequestStatus.WITHDRAWN.value,
)


async def expire_unclaimed_approved_requests(
    session: AsyncSession,
    *,
    operator: Operator,
    default_ttl: timedelta,
    now: datetime | None = None,
) -> list[ApprovalRequest]:
    """End each ``approved`` request of the tenant that nobody started in time.

    A request counts when it is ``approved``, nobody claimed its run
    (``resumed_at`` is empty), and its decision is *default_ttl* or more
    before *now*. It moves to ``expired`` with one decision audit row, and
    :func:`~meho_backplane.operations.approval_queue.end_run_input` drops
    what MEHO kept only to run it. Its ``expires_at`` becomes the end of
    the time it had to start, so every surface reads it as a request whose
    time ran out. A later claim of the run finds no ``approved`` row and is
    refused.

    Same contract as
    :func:`~meho_backplane.operations.approval_queue.expire_stale_requests`:
    the caller commits, rows a concurrent claim or decision holds are
    skipped, and each returned row carries ``_audit_id`` for the
    after-commit step event.
    """
    _check_reviewer_role(operator)
    cutoff = now or _now()
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.tenant_id == operator.tenant_id)
        .where(ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value)
        .where(ApprovalRequest.resumed_at.is_(None))
        .where(ApprovalRequest.decided_at.is_not(None))
        .where(ApprovalRequest.decided_at <= cutoff - default_ttl)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for request in rows:
        decided_at = request.decided_at
        assert decided_at is not None  # the query selects only decided rows
        request.expires_at = _as_utc(decided_at) + default_ttl
        await _expire_request(
            session, request, operator=operator, cutoff=cutoff, default_ttl=default_ttl
        )
    return rows


@dataclass
class CleanUpCount:
    """What one start-up clean-up did."""

    requests_read: int = 0
    #: Rows whose params or parent inputs were rewritten.
    params_hidden: int = 0
    handoffs_removed: int = 0
    #: Approved requests of another release that ended as ``expired``.
    approvals_ended: int = 0
    batches: int = 0


def _cannot_run_clause(running_release: str | None) -> Any:
    """Requests that are not waiting, were never started, and cannot run.

    An ``approved`` request runs only under the release it was made under
    (:func:`~meho_backplane.operations.approval_queue.refuse_resume_if_release_changed`).
    So an approved request of another release counts. Without a release
    identity (a local development run) no approved request counts.
    """
    ended: Any = ApprovalRequest.status.in_(_ENDED_STATUSES)
    if running_release is not None:
        ended = or_(
            ended,
            and_(
                ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
                or_(
                    ApprovalRequest.release_id.is_(None),
                    ApprovalRequest.release_id != running_release,
                ),
            ),
        )
    return and_(ended, ApprovalRequest.resumed_at.is_(None))


async def _end_approval(
    session: AsyncSession, request: ApprovalRequest, *, running_release: str | None
) -> None:
    """End an approved request of another release as ``expired``, with its audit row.

    Its time to start becomes the end of the TTL after its decision, as in
    :func:`expire_unclaimed_approved_requests`. A request that ends here
    before that time reads with the reason ``release_changed``, in its
    audit row too. A request whose time already ran out reads with the
    reason ``deadline_passed`` everywhere, as
    :func:`~meho_backplane.operations.approval_queue.expire_older_release_requests`
    does for a waiting request.
    """
    default_ttl = _resolve_default_ttl()
    cutoff = _now()
    if request.decided_at is not None:
        request.expires_at = _as_utc(request.decided_at) + default_ttl
    extra_payload: dict[str, Any] | None = None
    if not _deadline_has_passed(request, cutoff, default_ttl):
        extra_payload = {
            "reason": ApprovalExpiredReason.RELEASE_CHANGED.value,
            "request_release": request.release_id,
            "running_release": running_release,
        }
    await _expire_request(
        session,
        request,
        operator=expiry_operator(request.tenant_id),
        cutoff=cutoff,
        default_ttl=default_ttl,
        extra_payload=extra_payload,
    )


async def _clean_batch(
    session: AsyncSession,
    rows: list[ApprovalRequest],
    count: CleanUpCount,
    *,
    running_release: str | None = None,
) -> list[ApprovalRequest]:
    """Clean *rows*; return the approved requests that ended in this batch.

    Deletes their handoff rows, hides the secret-named values of their
    params and drops their parent inputs. An approved request ends as
    ``expired`` in the same transaction.
    """
    removed = await session.execute(
        delete(ApprovalExecutionPayload)
        .where(ApprovalExecutionPayload.request_id.in_([request.id for request in rows]))
        .returning(ApprovalExecutionPayload.handle)
    )
    count.handoffs_removed += len(removed.all())
    ended: list[ApprovalRequest] = []
    for request in rows:
        before = (request.params, request.resume_parent)
        if request.status == ApprovalRequestStatus.APPROVED.value:
            await _end_approval(session, request, running_release=running_release)
            ended.append(request)
        else:
            hidden = params_with_secret_fields_hidden(request.params, op_id=request.op_id)
            if hidden is not None and hidden != request.params:
                request.params = hidden
            parent = resume_parent_without_inputs(request.resume_parent)
            if parent is not None:
                request.resume_parent = parent
        if (request.params, request.resume_parent) != before:
            count.params_hidden += 1
    count.approvals_ended += len(ended)
    count.requests_read += len(rows)
    count.batches += 1
    return ended


async def hide_values_of_requests_that_cannot_run(
    *, batch_size: int = CLEAN_UP_BATCH_SIZE
) -> CleanUpCount:
    """Clean every stored request that cannot run; return the counts.

    Run at start-up. A request that ended without a run (rejected, expired,
    withdrawn), or an approved request of another release, was never started
    and can never run. Each one gets the clean-up of
    :func:`~meho_backplane.operations.approval_queue.end_run_input`: each
    secret-named value of its params, and the param a connector hides (the
    manifest of a ``k8s.apply`` that holds a credential), is replaced with
    the marker, the parent of a step of a multi-step operation keeps only
    its ``op_id``, and its encrypted handoff row is deleted. This also covers
    rows stored by an older release. A row with nothing to hide is not
    written.

    Reads *batch_size* rows per transaction, in the order of their id, so a
    large table never holds one long transaction. Rows another transaction
    holds are skipped; the next start reads them again.

    An approved request of another release also ends as ``expired`` in the
    transaction that removes its inputs, with one decision audit row
    and an ``approval.expired`` step event after the commit. So a MEHO that
    still runs the older release during a rolling update cannot start it
    any more: the CHECK ``ck_approval_request_ended_not_started`` refuses
    a ``resumed_at`` on an ended request, also for a claim that read the
    row before this commit.
    """
    running_release = running_release_id()
    clause = _cannot_run_clause(running_release)
    count = CleanUpCount()
    last_id: uuid.UUID | None = None
    sessionmaker = get_sessionmaker()
    while True:
        async with sessionmaker() as session:
            stmt = select(ApprovalRequest).where(clause)
            if last_id is not None:
                stmt = stmt.where(ApprovalRequest.id > last_id)
            stmt = (
                stmt.order_by(ApprovalRequest.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                break
            ended = await _clean_batch(session, rows, count, running_release=running_release)
            await session.commit()
            last_id = rows[-1].id
        for request in ended:
            await record_approval_step_event(
                tenant_id=request.tenant_id,
                request=request,
                decision="expired",
                audit_id=request._audit_id,  # type: ignore[attr-defined]
            )
        if len(rows) < batch_size:
            break
    _log.info(
        "approval_stored_values_cleaned",
        requests_read=count.requests_read,
        params_hidden=count.params_hidden,
        handoffs_removed=count.handoffs_removed,
        approvals_ended=count.approvals_ended,
        batches=count.batches,
    )
    return count
