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
   ``params_hash``, the saved target no longer resolves, its connection
   settings changed since the request was made, or the preview hash of a
   ``destructive`` request differs. The operation, connector and target
   always come from the request, so the caller cannot change them.
   The caller's rights are checked as for a new call of the same caller
   made now (:mod:`~meho_backplane.operations.approved_run_rights`). A new
   call that would be refused refuses the re-run
   (:class:`RerunRightsRefusedError`). A new call that would wait for an
   approval this one does not cover needs a new approval.
4. One run at a time. The claim is one conditional ``UPDATE`` that clears
   ``resume_result`` and adds one to ``rerun_count``, only while the stored
   result is a failure that is not ``uncertain`` and ``rerun_count`` is
   still the value the caller read. Two re-runs sent at the same time: one
   wins, the other is refused, also when the first one already ran and
   failed again before the second claims. ``resumed_at`` stays the one-way
   latch of the first run. The
   same ``UPDATE`` sets ``rerun_started_at``, the start of the latest run,
   which the "outcome unknown" list counts its minutes from (#5026).

Every re-run writes an ``approval.rerun`` audit row that names the request
and the attempt, in the same transaction as the claim. A refused re-run
writes one too. The run itself writes its usual run row, which also carries
the attempt. The new result replaces ``resume_result``, so ``/result`` and
the approval views show the latest attempt.

A request of a step of a multi-step operation (``resume_parent`` set) never
runs again: a re-run would send the steps after the approved step again. The
requester starts a new run of the operation.

Credential writes keep their params only in one-time encrypted custody, which
the first run deletes. A re-run of one must send the params again; they must
match ``params_hash``.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, cast

import structlog
from sqlalchemy import func, null, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.delegation import ACTOR_SUB_KEY, resolve_actor_sub
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, AuditEventClass
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._validate import params_hash_matches
from meho_backplane.operations.approval_object_check import object_changed_since_request
from meho_backplane.operations.approval_queue import (
    ApprovalError,
    ApprovalNotFoundError,
    ApprovalResultState,
    _deadline_has_passed,
    _dispatch_resume_with_bound_context,
    _now,
    _resolve_default_ttl,
    _stored_call,
    _write_audit_row,
    approval_release_changed,
    approval_result_state,
    get_request,
    persist_resume_result,
    rehydrate_resume_target,
)
from meho_backplane.operations.approval_resume_checks import preview_changed_since_request
from meho_backplane.operations.approval_state_pin import state_changed_since_request
from meho_backplane.operations.approved_reveal import is_secret_reveal
from meho_backplane.operations.approved_run_rights import (
    approved_run_refusal,
    bind_approved_run,
)
from meho_backplane.operations.approved_step_check import approved_step_refusal
from meho_backplane.operations.grant_approval import grant_changed_since_request
from meho_backplane.operations.role_floors import reader_change_refusal
from meho_backplane.operations.uncertain_outcome import (
    LOGIN_ENDED_ADVICE,
    UNCERTAIN_OUTCOME,
    connection_lost_after_send,
    with_uncertain_outcome,
)
from meho_backplane.settings import get_settings

__all__ = [
    "RERUN_AUDIT_PATH",
    "RerunForbiddenError",
    "RerunNeedsNewApprovalError",
    "RerunNotAllowedError",
    "RerunOutcome",
    "RerunParamsRequiredError",
    "RerunRefusal",
    "RerunRightsRefusedError",
    "RerunRoleTooLowError",
    "rerun_failed_request",
]

#: Audit ``path`` of every re-run row, accepted or refused.
RERUN_AUDIT_PATH: Final = "approval.rerun"

_ACCEPTED_STATUS_CODE: Final = 202
_FORBIDDEN_STATUS_CODE: Final = 403
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
    TARGET_CHANGED = "target_changed"
    PREVIEW_CHANGED = "preview_changed"
    OBJECT_CHANGED = "object_changed"
    MULTI_STEP_RUN = "multi_step_run"
    RISK_RAISED = "risk_raised"


_REFUSAL_MESSAGES: Final[dict[RerunRefusal, str]] = {
    RerunRefusal.DEADLINE_PASSED: "the approval deadline (expires_at) has passed",
    RerunRefusal.RELEASE_CHANGED: "MEHO was updated after the request was made",
    RerunRefusal.WORK_REF_CHANGED: "the work reference differs from the approved one",
    RerunRefusal.PARAMS_CHANGED: "the params differ from the approved ones",
    RerunRefusal.TARGET_GONE: "the approved target no longer exists",
    RerunRefusal.TARGET_CHANGED: (
        "the connection settings of the approved target changed after the request was made"
    ),
    RerunRefusal.PREVIEW_CHANGED: "the preview of the call differs from the approved one",
    RerunRefusal.OBJECT_CHANGED: (
        "the object of the call is not the one on the approval card, or could not be read"
    ),
    RerunRefusal.MULTI_STEP_RUN: (
        "it is a step of a multi-step operation, and MEHO does not run such a request "
        "again, so that no step is sent twice; start a new run of the operation"
    ),
    RerunRefusal.RISK_RAISED: (
        "the risk level of the operation went up, and the approval does not cover it"
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
            + (f". {LOGIN_ENDED_ADVICE}" if state == "uncertain" else "")
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


class RerunRightsRefusedError(ApprovalError):
    """A new call of the caller, made now, would be refused, so the re-run is refused.

    The request stays approved: it can run again when the rights allow it.
    """

    def __init__(self, request_id: uuid.UUID, reason: str) -> None:
        super().__init__(
            f"approval request {request_id} did not run: a new call of yours would be "
            f"refused now ({reason}). The request stays approved. Run it again when your "
            "rights allow it, or ask again with a new request."
        )
        self.request_id = request_id
        self.reason = reason


class RerunRoleTooLowError(ApprovalError):
    """The caller's role is below ``operator``, the role of a new call."""

    def __init__(self, request_id: uuid.UUID) -> None:
        super().__init__(
            f"approval request {request_id} did not run: a re-run needs the operator role"
        )
        self.request_id = request_id


class RerunParamsRequiredError(ApprovalError):
    """MEHO does not keep this request's params; the caller must send them."""

    def __init__(self, request_id: uuid.UUID) -> None:
        super().__init__(
            f"approval request {request_id} has no stored params (a credential write, "
            "and a call whose params hold a password or a licence key, keeps them "
            "only until the first run). Send the original params again"
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
    return None


async def _object_changed(
    operator: Operator, request: ApprovalRequest, target: Any, params: dict[str, Any]
) -> bool:
    """The object of the call, or the login or group a change of rights is for, moved.

    Also true when the object is no longer as the approval card showed it.
    """
    if await object_changed_since_request(operator, request, target, params) is not None:
        return True
    if await grant_changed_since_request(operator, request, target, params) is not None:
        return True
    return await state_changed_since_request(operator, request, target, params) is not None


def _target_refusal(denied: OperationResult) -> RerunRefusal:
    """Map the target refusal of :func:`rehydrate_resume_target` to a reason."""
    extras = denied.extras or {}
    if extras.get("error_code") == "target_changed_since_approval":
        return RerunRefusal.TARGET_CHANGED
    return RerunRefusal.TARGET_GONE


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
    operator: Operator,
    request: ApprovalRequest,
    error: ApprovalError,
    *,
    code: str,
    status: int,
    reason: str | None = None,
) -> ApprovalError:
    """Write the audit row of a refused re-run and return *error* to raise."""
    from meho_backplane.db.engine import get_sessionmaker

    extra: dict[str, Any] = {"result_status": "refused", "refusal": code}
    if reason is not None:
        extra["reason"] = reason
    async with get_sessionmaker()() as session:
        await _audit_rerun(
            session,
            operator=operator,
            request=request,
            status_code=status,
            extra=extra,
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


async def _refuse_other_caller(
    operator: Operator, request_id: uuid.UUID, error: ApprovalError, *, code: str
) -> ApprovalError:
    """Write the row of a re-run refused to a caller who did not make the request.

    The row names the caller and the request id, and no other field of the
    request: the caller may not read the request. ``requester_sub`` is
    ``None``, so every audit view shows the caller only as the person who
    acted (:func:`~meho_backplane.audit_query.approval_payload.approval_payload_view`).
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import AuditLog

    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=_now(),
                operator_sub=operator.sub,
                tenant_id=operator.tenant_id,
                event_class=AuditEventClass.POLICY_DECISION,
                method="APPROVAL",
                path=RERUN_AUDIT_PATH,
                status_code=_FORBIDDEN_STATUS_CODE,
                request_id=None,
                duration_ms=Decimal("0"),
                payload={
                    "approval_request_id": str(request_id),
                    "principal_sub": operator.sub,
                    "requester_sub": None,
                    "result_status": "refused",
                    "refusal": code,
                },
            )
        )
        await session.commit()
    _log().info(
        "approval_rerun_refused",
        approval_request_id=str(request_id),
        refusal=code,
        operator_sub=operator.sub,
    )
    return error


async def _claim_rerun(operator: Operator, request: ApprovalRequest) -> int | None:
    """Take the re-run claim and write its audit row in one transaction.

    Returns the attempt number, or ``None`` when another re-run took the
    claim first (or the stored result is no longer a failure).

    The claim only matches the row as *request* read it: the same
    ``rerun_count``. So a caller that read the original failure cannot take
    the claim after another re-run already ran and failed in between. It
    is refused and has to read the request again.
    """
    from meho_backplane.db.engine import get_sessionmaker

    result_status = ApprovalRequest.resume_result["status"].as_string()
    execution_outcome = ApprovalRequest.resume_result[("extras", "execution_outcome")].as_string()
    async with get_sessionmaker()() as session:
        claimed = await session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == request.id,
                ApprovalRequest.status == ApprovalRequestStatus.APPROVED.value,
                ApprovalRequest.rerun_count == request.rerun_count,
                ApprovalRequest.resume_result.is_not(None),
                func.coalesce(result_status, "") != "ok",
                func.coalesce(execution_outcome, "") != UNCERTAIN_OUTCOME,
            )
            .values(
                resume_result=null(),
                rerun_count=ApprovalRequest.rerun_count + 1,
                rerun_started_at=_now(),
            )
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


def _acting_agent(request: ApprovalRequest) -> Any:
    """Bind the acting agent of a request made inside a hosted agent run.

    The re-run is then checked with the same caller and acting agent as the
    first run: the stricter of the requester's and the agent's rights.
    """
    if request.principal_act is None or resolve_actor_sub() is not None:
        return nullcontext()
    return structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: request.principal_act})


async def _rights_refusal(
    operator: Operator, request: ApprovalRequest, target: Any, params: dict[str, Any]
) -> ApprovalError | None:
    """Refuse the re-run when a new call of *operator* made now would not run.

    The steps of a multi-step operation are checked too, while
    ``AGENT_GRANT_REQUIRED`` is ``on``, so a refused step refuses the re-run
    before MEHO takes it. An operation MEHO does not know is left to the
    run, which refuses it.
    """
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

    product, version, impl_id = parse_connector_id(request.connector_id)
    descriptor = await lookup_descriptor(
        tenant_id=request.tenant_id,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=request.op_id,
    )
    if descriptor is None:
        return None
    with _acting_agent(request), bind_approved_run(request):
        refusal = await approved_run_refusal(
            operator=operator,
            requester_runs=True,
            descriptor=descriptor,
            connector_id=request.connector_id,
            target=target,
            params=params,
            preview_hash=request.preview_hash,
        )
        step_reason = None
        if refusal is None and get_settings().agent_grant_required == "on":
            step_reason = await approved_step_refusal(
                operator=operator,
                descriptor=descriptor,
                connector_id=request.connector_id,
                target=target,
            )
    if refusal is not None and refusal.new_approval:
        return await _refuse(
            operator,
            request,
            RerunNeedsNewApprovalError(request.id, RerunRefusal.RISK_RAISED),
            code=RerunRefusal.RISK_RAISED.value,
            status=_CONFLICT_STATUS_CODE,
            reason=refusal.reason,
        )
    reason = refusal.reason if refusal is not None else step_reason
    if reason is None:
        return None
    return await _refuse(
        operator,
        request,
        RerunRightsRefusedError(request.id, reason),
        code="rights_refused",
        status=_FORBIDDEN_STATUS_CODE,
        reason=reason,
    )


async def _check_rerun(
    operator: Operator, request: ApprovalRequest, params: dict[str, Any] | None
) -> tuple[dict[str, Any], Any, bool]:
    """Apply rules 2 and 3; return the params, target and reveal choice to run with.

    The reveal choice (``reveal_secret: true`` of the original call) is
    stored on the request, not in the params, the hash or the preview. A
    flag sent again with the params comes off before the hash check and
    does not change the stored choice, which goes on the params of the run.

    A secret reveal never runs again: its approval works once, for one fetch
    of the person who asked (:mod:`meho_backplane.operations.approved_reveal`).
    After a failed fetch, that person sends a new request.
    """
    state = "secret_reveal" if is_secret_reveal(request) else _not_failed_state(request)
    if state is not None:
        raise await _refuse(
            operator,
            request,
            RerunNotAllowedError(request.id, state),
            code=f"state_{state}",
            status=_CONFLICT_STATUS_CODE,
        )
    if request.resume_parent is not None:
        raise await _refuse(
            operator,
            request,
            RerunNeedsNewApprovalError(request.id, RerunRefusal.MULTI_STEP_RUN),
            code=RerunRefusal.MULTI_STEP_RUN.value,
            status=_CONFLICT_STATUS_CODE,
        )
    effective, reveal_secret = _stored_call(request, params)
    if effective is None:
        raise await _refuse(
            operator,
            request,
            RerunParamsRequiredError(request.id),
            code="params_required",
            status=_PARAMS_REQUIRED_STATUS_CODE,
        )
    reason = _static_refusal(request, effective)
    target: Any = None
    if reason is None:
        target, denied = await rehydrate_resume_target(operator, request)
        if denied is not None:
            reason = _target_refusal(denied)
        elif await preview_changed_since_request(operator, request, target, effective):
            reason = RerunRefusal.PREVIEW_CHANGED
        elif await _object_changed(operator, request, target, effective):
            reason = RerunRefusal.OBJECT_CHANGED
    if reason is not None:
        raise await _refuse(
            operator,
            request,
            RerunNeedsNewApprovalError(request.id, reason),
            code=reason.value,
            status=_CONFLICT_STATUS_CODE,
        )
    rights = await _rights_refusal(operator, request, target, effective)
    if rights is not None:
        raise rights
    return effective, target, reveal_secret


async def _check_caller(
    operator: Operator, request: ApprovalRequest, *, role_too_low: bool
) -> None:
    """Refuse a caller who did not make the request, or whose role is too low.

    Each refusal writes its ``approval.rerun`` row.
    """
    if operator.sub != request.principal_sub:
        if role_too_low:
            raise await _refuse_other_caller(
                operator, request.id, RerunRoleTooLowError(request.id), code="insufficient_role"
            )
        raise await _refuse_other_caller(
            operator,
            request.id,
            RerunForbiddenError(request_id=request.id, principal_sub=operator.sub),
            code="not_request_owner",
        )
    if role_too_low:
        raise await _refuse(
            operator,
            request,
            RerunRoleTooLowError(request.id),
            code="insufficient_role",
            status=_FORBIDDEN_STATUS_CODE,
            reason=reader_change_refusal(operator.tenant_role, is_change=True)
            or f"a re-run needs the operator role; your role is {operator.tenant_role.value}",
        )


async def rerun_failed_request(
    *,
    operator: Operator,
    request_id: uuid.UUID,
    params: dict[str, Any] | None = None,
) -> RerunOutcome:
    """Run an approved request again after its run failed (#5417).

    *params* are the original params. They are optional when MEHO stored
    them on the request, and required for a credential write. The run uses
    the caller's identity, like the agent's own resume after approval
    (``dispatch(..., _approved=True)``). The approval is not asked for
    again, but the caller's rights are checked as for a new call made now.

    Raises:
        ApprovalNotFoundError: No request with this id in the caller's tenant.
        RerunForbiddenError: The caller did not make the request.
        RerunNotAllowedError: The last run did not fail, or another re-run
            is running.
        RerunParamsRequiredError: No stored params and none were sent.
        RerunNeedsNewApprovalError: Something changed since the approval.
        RerunRightsRefusedError: A new call of the caller would be refused now.
        RerunRoleTooLowError: The caller's role is below ``operator``.
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.mcp.registry import role_at_least

    role_too_low = not role_at_least(operator.tenant_role, TenantRole.OPERATOR)
    try:
        async with get_sessionmaker()() as session:
            request = await get_request(
                session, tenant_id=operator.tenant_id, request_id=request_id
            )
    except ApprovalNotFoundError:
        if role_too_low:
            raise RerunRoleTooLowError(request_id) from None
        raise
    await _check_caller(operator, request, role_too_low=role_too_low)

    effective_params, target, reveal_secret = await _check_rerun(operator, request, params)

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
        with _acting_agent(request):
            result = await _dispatch_resume_with_bound_context(
                operator=operator,
                request=request,
                resolved_target=target,
                effective_params=effective_params,
                rerun_attempt=attempt,
                reveal_secret=reveal_secret,
            )
    except Exception as exc:
        # Store a result, so the request does not stay "started" for good. A
        # timeout or a lost connection may have reached the target: that is
        # uncertain and never runs again. Any other error is a failure that
        # can run again later. An approved request counts as a change.
        stopped = OperationResult(
            status="error",
            op_id=request.op_id,
            error=f"rerun_failed: the run stopped with {type(exc).__name__}",
            duration_ms=0.0,
        )
        if connection_lost_after_send(exc):
            stopped = with_uncertain_outcome(stopped)
        await persist_resume_result(request.id, stopped, parked_op_id=request.op_id)
        raise
    return RerunOutcome(attempt=attempt, result=cast(OperationResult, result))
