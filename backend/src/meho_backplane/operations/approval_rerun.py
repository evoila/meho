# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Run an approved request again after its run failed (#5417).

A person approved one exact call. When its run failed (for example because
a step it needed had not run yet), the requester can run the same call again
without a new approval, as long as nothing changed. The route is
``POST /api/v1/approvals/{id}/rerun``.

The rules, checked in this order:

1. Only the requester (``operator.sub == request.principal_sub``) may ask.
2. Only a request whose last run failed
   (:func:`~meho_backplane.operations.approval_queue.approval_result_state`
   is ``failed``).
3. Nothing changed. MEHO refuses and says a new approval is needed when the
   ``expires_at`` deadline has passed, MEHO was updated since the request was
   made, the work reference differs, the params do not match
   ``params_hash``, the saved target no longer resolves, or the preview hash
   of a ``destructive`` request differs. The operation, connector and target
   always come from the request, so the caller cannot change them.
4. One run at a time. The claim is one conditional ``UPDATE`` that clears
   ``resume_result`` and adds one to ``rerun_count``, only while the stored
   result is a failure. Two re-runs sent at the same time: one wins, the
   other is refused. ``resumed_at`` stays the one-way latch of the first run.

Every re-run writes an ``approval.rerun`` audit row that names the request
and the attempt, in the same transaction as the claim. A refused re-run
writes one too. The run itself writes its usual run row, which also carries
the attempt. The new result replaces ``resume_result``, so ``/result`` and
the approval views show the latest attempt.

Credential writes keep their params only in one-time encrypted custody, which
the first run deletes. A re-run of one must send the params again; they must
match ``params_hash``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

import structlog
from sqlalchemy import func, null, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditEventClass
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import params_hash_matches
from meho_backplane.operations.approval_queue import (
    ApprovalError,
    ApprovalResultState,
    _deadline_has_passed,
    _dispatch_resume_with_bound_context,
    _now,
    _resolve_default_ttl,
    _write_audit_row,
    approval_release_changed,
    approval_result_state,
    get_request,
    persist_resume_result,
    rehydrate_resume_target,
)

__all__ = [
    "RERUN_AUDIT_PATH",
    "RerunForbiddenError",
    "RerunNeedsNewApprovalError",
    "RerunNotAllowedError",
    "RerunOutcome",
    "RerunParamsRequiredError",
    "RerunRefusal",
    "rerun_failed_request",
]

#: Audit ``path`` of every re-run row, accepted or refused.
RERUN_AUDIT_PATH: Final = "approval.rerun"

_ACCEPTED_STATUS_CODE: Final = 202
_CONFLICT_STATUS_CODE: Final = 409
_PARAMS_REQUIRED_STATUS_CODE: Final = 422


def _log() -> Any:
    return structlog.get_logger(__name__)


class RerunRefusal(StrEnum):
    """Why a re-run needs a new approval (#5417)."""

    DEADLINE_PASSED = "deadline_passed"
    RELEASE_CHANGED = "release_changed"
    WORK_REF_CHANGED = "work_ref_changed"
    PARAMS_CHANGED = "params_changed"
    TARGET_GONE = "target_gone"
    PREVIEW_CHANGED = "preview_changed"
    INPUT_NOT_KEPT = "input_not_kept"


_REFUSAL_MESSAGES: Final[dict[RerunRefusal, str]] = {
    RerunRefusal.DEADLINE_PASSED: "the approval deadline (expires_at) has passed",
    RerunRefusal.RELEASE_CHANGED: "MEHO was updated after the request was made",
    RerunRefusal.WORK_REF_CHANGED: "the work reference differs from the approved one",
    RerunRefusal.PARAMS_CHANGED: "the params differ from the approved ones",
    RerunRefusal.TARGET_GONE: "the approved target no longer exists",
    RerunRefusal.PREVIEW_CHANGED: "the preview of the call differs from the approved one",
    RerunRefusal.INPUT_NOT_KEPT: (
        "MEHO did not keep the input of this multi-step credential write after its first run"
    ),
}


class RerunForbiddenError(ApprovalError):
    """The caller did not make the request; only the requester may re-run it."""

    def __init__(self, *, request_id: uuid.UUID, principal_sub: str) -> None:
        super().__init__(
            f"{principal_sub!r} did not make approval request {request_id}; "
            "only the requester can run it again"
        )
        self.request_id = request_id
        self.principal_sub = principal_sub


class RerunNotAllowedError(ApprovalError):
    """The request is not in a state that can run again (not a failed run)."""

    def __init__(self, request_id: uuid.UUID, state: str) -> None:
        super().__init__(
            f"approval request {request_id} cannot run again: its state is {state!r}. "
            "Only an approved request whose last run failed can run again"
        )
        self.request_id = request_id
        self.state = state


class RerunNeedsNewApprovalError(ApprovalError):
    """Something changed since the approval, so a new approval is needed."""

    def __init__(self, request_id: uuid.UUID, reason: RerunRefusal) -> None:
        super().__init__(
            f"approval request {request_id} cannot run again: {_REFUSAL_MESSAGES[reason]}. "
            "Make a new request and get it approved"
        )
        self.request_id = request_id
        self.reason = reason


class RerunParamsRequiredError(ApprovalError):
    """MEHO does not keep this request's params; the caller must send them."""

    def __init__(self, request_id: uuid.UUID) -> None:
        super().__init__(
            f"approval request {request_id} has no stored params (a credential write "
            "keeps them only until the first run). Send the original params again"
        )
        self.request_id = request_id


@dataclass(frozen=True)
class RerunOutcome:
    """A re-run that ran: its attempt number and the run result."""

    attempt: int
    result: OperationResult


def _not_failed_state(request: ApprovalRequest) -> str | None:
    """The state name when *request* is not a failed approved run, else ``None``."""
    state = approval_result_state(request)
    if state is ApprovalResultState.FAILED:
        return None
    return state.value if state is not None else request.status


def _static_refusal(request: ApprovalRequest, params: dict[str, Any]) -> RerunRefusal | None:
    """The first rule the re-run breaks that needs no I/O, or ``None``."""
    if _deadline_has_passed(request, _now(), _resolve_default_ttl()):
        return RerunRefusal.DEADLINE_PASSED
    if approval_release_changed(request):
        return RerunRefusal.RELEASE_CHANGED
    if work_ref_var.get() != request.work_ref:
        return RerunRefusal.WORK_REF_CHANGED
    if not params_hash_matches(params, request.params_hash):
        return RerunRefusal.PARAMS_CHANGED
    if request.resume_parent is not None and "params" not in request.resume_parent:
        return RerunRefusal.INPUT_NOT_KEPT
    return None


async def _preview_changed(
    operator: Operator, request: ApprovalRequest, target: Any, params: dict[str, Any]
) -> bool:
    """True when a ``destructive`` request's preview hash differs now (#3197)."""
    if request.preview_hash is None:
        return False
    from meho_backplane.operations._request_preview import (
        compute_preview_hash,
        preview_dispatch,
    )

    envelope = await preview_dispatch(
        operator=operator,
        connector_id=request.connector_id,
        op_id=request.op_id,
        target=target,
        params=params,
    )
    if envelope.get("status") != "ok":
        return True
    return compute_preview_hash(envelope) != request.preview_hash


async def _audit_rerun(
    session: AsyncSession,
    *,
    operator: Operator,
    request: ApprovalRequest,
    status_code: int,
    extra: dict[str, Any],
) -> uuid.UUID:
    audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=audit_id,
        event_class=AuditEventClass.POLICY_DECISION,
        operator=operator,
        request=request,
        path=RERUN_AUDIT_PATH,
        status_code=status_code,
        duration_ms=0.0,
        extra_payload=extra,
    )
    return audit_id


async def _refuse(
    operator: Operator, request: ApprovalRequest, error: ApprovalError, *, code: str, status: int
) -> ApprovalError:
    """Write the audit row of a refused re-run and return *error* to raise."""
    from meho_backplane.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        await _audit_rerun(
            session,
            operator=operator,
            request=request,
            status_code=status,
            extra={"result_status": "refused", "refusal": code},
        )
        await session.commit()
    _log().info(
        "approval_rerun_refused",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        refusal=code,
        operator_sub=operator.sub,
    )
    return error


async def _claim_rerun(operator: Operator, request: ApprovalRequest) -> int | None:
    """Take the re-run claim and write its audit row in one transaction.

    Returns the attempt number, or ``None`` when another re-run took the
    claim first (or the stored result is no longer a failure).
    """
    from meho_backplane.db.engine import get_sessionmaker

    result_status = ApprovalRequest.resume_result["status"].as_string()
    async with get_sessionmaker()() as session:
        claimed = await session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == request.id,
                ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
                ApprovalRequest.resume_result.is_not(None),
                func.coalesce(result_status, "") != "ok",
            )
            .values(resume_result=null(), rerun_count=ApprovalRequest.rerun_count + 1)
            .returning(ApprovalRequest.rerun_count)
            .execution_options(synchronize_session=False)
        )
        rerun_count = claimed.scalar_one_or_none()
        if rerun_count is None:
            return None
        attempt = int(rerun_count) + 1
        await _audit_rerun(
            session,
            operator=operator,
            request=request,
            status_code=_ACCEPTED_STATUS_CODE,
            extra={"result_status": "accepted", "attempt": attempt},
        )
        await session.commit()
    return attempt


async def _check_rerun(
    operator: Operator, request: ApprovalRequest, params: dict[str, Any] | None
) -> tuple[dict[str, Any], Any]:
    """Apply rules 2 and 3; return the params and target to run with."""
    state = _not_failed_state(request)
    if state is not None:
        raise await _refuse(
            operator,
            request,
            RerunNotAllowedError(request.id, state),
            code=f"state_{state}",
            status=_CONFLICT_STATUS_CODE,
        )
    effective = params if params is not None else request.params
    if effective is None:
        raise await _refuse(
            operator,
            request,
            RerunParamsRequiredError(request.id),
            code="params_required",
            status=_PARAMS_REQUIRED_STATUS_CODE,
        )
    effective = cast(dict[str, Any], effective)
    reason = _static_refusal(request, effective)
    target: Any = None
    if reason is None:
        target, denied = await rehydrate_resume_target(operator, request)
        if denied is not None:
            reason = RerunRefusal.TARGET_GONE
        elif await _preview_changed(operator, request, target, effective):
            reason = RerunRefusal.PREVIEW_CHANGED
    if reason is not None:
        raise await _refuse(
            operator,
            request,
            RerunNeedsNewApprovalError(request.id, reason),
            code=reason.value,
            status=_CONFLICT_STATUS_CODE,
        )
    return effective, target


async def rerun_failed_request(
    *,
    operator: Operator,
    request_id: uuid.UUID,
    params: dict[str, Any] | None = None,
) -> RerunOutcome:
    """Run an approved request again after its run failed (#5417).

    *params* are the original params. They are optional when MEHO stored
    them on the request, and required for a credential write. The run uses
    the caller's identity, like the agent's own resume after approval, with
    the approval as the authorization (``dispatch(..., _approved=True)``).

    Raises:
        ApprovalNotFoundError: No request with this id in the caller's tenant.
        RerunForbiddenError: The caller did not make the request.
        RerunNotAllowedError: The last run did not fail, or another re-run
            is running.
        RerunParamsRequiredError: No stored params and none were sent.
        RerunNeedsNewApprovalError: Something changed since the approval.
    """
    from meho_backplane.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        request = await get_request(session, tenant_id=operator.tenant_id, request_id=request_id)
    if operator.sub != request.principal_sub:
        raise RerunForbiddenError(request_id=request_id, principal_sub=operator.sub)

    effective_params, target = await _check_rerun(operator, request, params)

    attempt = await _claim_rerun(operator, request)
    if attempt is None:
        raise await _refuse(
            operator,
            request,
            RerunNotAllowedError(request.id, ApprovalResultState.STARTED.value),
            code="claim_lost",
            status=_CONFLICT_STATUS_CODE,
        )
    _log().info(
        "approval_rerun_started",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        attempt=attempt,
        operator_sub=operator.sub,
    )
    try:
        result = await _dispatch_resume_with_bound_context(
            operator=operator,
            request=request,
            resolved_target=target,
            effective_params=effective_params,
            rerun_attempt=attempt,
        )
    except Exception as exc:
        # Store a failed result, so the request can be run again later
        # instead of staying "started" for good.
        await persist_resume_result(
            request.id,
            OperationResult(
                status="error",
                op_id=request.op_id,
                error=f"rerun_failed: the run stopped with {type(exc).__name__}",
                duration_ms=0.0,
            ),
        )
        raise
    return RerunOutcome(attempt=attempt, result=cast(OperationResult, result))
