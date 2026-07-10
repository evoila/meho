# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Durable approval queue for ``requires_approval`` dispatches.

Initiative #803 (G11.2 Agent permission model), Task #817 (T4). When the
G11.2-T3 policy gate resolves
:attr:`~meho_backplane.db.models.PermissionVerdict.NEEDS_APPROVAL` for a
dispatch (an agent principal on a ``requires_approval`` / caution /
dangerous op), the dispatcher creates an
:class:`~meho_backplane.db.models.ApprovalRequest` row (via
:func:`create_pending_request`) instead of executing the op. The row
parks the call durably so a process restart cannot lose the pending
request. Two REST endpoints (``/api/v1/approvals/{id}/approve`` and
``…/reject``) let authorized reviewers decide; approval re-dispatches the
original call with the original params (gate bypassed via
``dispatch(..., _approved=True)``).

Audit invariant
---------------

Every mutation writes a synchronous audit row in the **same transaction**:

* :func:`create_pending_request` writes a ``"request"`` audit row
  (``method='APPROVAL'``, ``path='approval.request'``) alongside the
  pending :class:`~meho_backplane.db.models.ApprovalRequest` insert.
* :func:`approve_request` / :func:`reject_request` /
  :func:`expire_stale_requests` write a ``"decision"`` audit row
  (``path='approval.decision'``) inside the same transaction as the
  status update. The decision row does **not** land until the status
  commit succeeds — mirroring the dispatcher's synchronous-audit
  invariant. An approval isn't "granted" until its decision row
  commits.

Session-replay lineage (#2086)
------------------------------

Every lifecycle audit row is linked into the G8.2 session-replay graph.
The parked row persists ``agent_session_id`` (resolved at creation on
the requester's task) and ``request_audit_id`` (the "request" audit
row's pre-generated id); :func:`_write_audit_row` stamps both onto the
rows it writes, and :func:`resume_dispatch_after_approval` re-binds them
(plus ``work_ref``) around the re-dispatch. The result is one replay
subtree — the ``approval.request`` row anchored on the originating
session, with the decision row and the executed dispatch's row as its
children — where previously every row in the chain was invisible to
``GET /api/v1/audit/sessions/{id}/replay``.

Transaction discipline
----------------------

Every mutating function takes an open
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, flushes its changes, and
returns — the **caller** owns the commit. This lets the dispatcher
compose the approval-row insert with the audit row in one transaction.
:func:`expire_stale_requests` follows the same contract; the CLI / task
scheduler calls it inside its own transaction.

Resume path
-----------

:func:`approve_request` re-dispatches the original call by calling
:func:`~meho_backplane.operations.dispatcher.dispatch` with the stored
``connector_id`` / ``op_id`` / ``target_id`` / ``params``. The caller
must pass the **original params** (not a re-hash); the service re-hashes
them and rejects if the hash does not match the stored ``params_hash``
(:class:`ParamsMismatchError`). This guards against a substitution attack
where a malicious approver swaps the params between the "request" and the
"approve" call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditLog,
)
from meho_backplane.operations._audit import (
    agent_session_id_var,
    parent_audit_id_var,
    policy_decision_var,
    resolve_agent_session_id,
    work_ref_var,
)
from meho_backplane.operations._errors import result_already_resumed, result_denied
from meho_backplane.operations._validate import compute_params_hash

__all__ = [
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalRequestAlreadyDecidedError",
    "ParamsMismatchError",
    "SelfApprovalForbiddenError",
    "UnauthorizedApprovalError",
    "approve_request",
    "claim_resume",
    "create_pending_request",
    "expire_stale_requests",
    "get_request",
    "list_pending",
    "publish_approval_event",
    "reject_request",
    "resume_dispatch_after_approval",
]

_log = structlog.get_logger(__name__)

#: Synthetic status code used for approval audit rows. Mirrors the
#: ``status_code_for_result`` convention: ``202`` means "accepted / pending".
_APPROVAL_STATUS_CODE: int = 202
_DECISION_STATUS_CODE_APPROVED: int = 200
_DECISION_STATUS_CODE_REJECTED: int = 403
_DECISION_STATUS_CODE_EXPIRED: int = 410


class ApprovalError(Exception):
    """Base class for approval queue failures."""


class ApprovalNotFoundError(ApprovalError):
    """No :class:`ApprovalRequest` row exists for the requested id.

    Raised by :func:`approve_request` / :func:`reject_request` when the id
    does not resolve or belongs to a different tenant. The two cases are
    indistinguishable to callers (tenant isolation); the route layer maps
    this to 404.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(f"no approval_request row for id {request_id}")


class ApprovalRequestAlreadyDecidedError(ApprovalError):
    """The approval request is already in a terminal state.

    Raised when :func:`approve_request` / :func:`reject_request` is
    called on a row that is not ``pending``. The route layer maps this to
    409 (conflict).
    """

    def __init__(self, request_id: uuid.UUID, status: str) -> None:
        self.request_id = request_id
        self.status = status
        super().__init__(f"approval_request {request_id} is already in terminal state {status!r}")


class ParamsMismatchError(ApprovalError):
    """The params hash does not match the stored hash on the request.

    Raised by :func:`approve_request` when the caller-supplied params
    hash against a value that differs from the stored
    :attr:`~meho_backplane.db.models.ApprovalRequest.params_hash`. This
    guards against param-substitution between queue and approve time.
    The route layer maps this to 422.
    """

    def __init__(self, request_id: uuid.UUID) -> None:
        self.request_id = request_id
        super().__init__(
            f"params hash mismatch on approval_request {request_id}; "
            "original params must be supplied unchanged"
        )


class SelfApprovalForbiddenError(ApprovalError):
    """The approver is the same principal that requested the approval.

    Raised by :func:`approve_request` when
    ``operator.sub == request.principal_sub`` and the deployment has not
    enabled the audited single-operator break-glass mode
    (``Settings.approval_allow_self_approval``). Enforces the
    requester != approver invariant (G11.7-T1 #1401): a single account
    must not be able to both request and grant a privileged connector
    write. The route layer maps this to 403.

    Reject is deliberately *not* guarded — an operator withdrawing their
    own pending request is not a privilege escalation.
    """

    def __init__(self, request_id: uuid.UUID, *, principal_sub: str) -> None:
        self.request_id = request_id
        self.principal_sub = principal_sub
        super().__init__(
            f"operator {principal_sub!r} may not approve approval_request "
            f"{request_id}: requester and approver must differ "
            "(set APPROVAL_ALLOW_SELF_APPROVAL=true for audited "
            "single-operator break-glass)"
        )


class UnauthorizedApprovalError(ApprovalError):
    """The operator lacks the role required to approve / reject a request.

    Raised when the operator's ``tenant_role`` is ``read_only``. The
    route layer maps this to 403.
    """

    def __init__(self, *, operator_sub: str, role: str) -> None:
        self.operator_sub = operator_sub
        self.role = role
        super().__init__(
            f"operator {operator_sub!r} with role {role!r} may not approve/reject "
            "an approval request (requires at least 'operator')"
        )


def _now() -> datetime:
    """Return the current UTC datetime. Isolated for testing."""
    return datetime.now(UTC)


async def _write_audit_row(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    operator: Operator,
    request: ApprovalRequest,
    path: str,
    status_code: int,
    duration_ms: float,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Insert one ``audit_log`` row for an approval queue event.

    Uses ``method='APPROVAL'`` and the provided *path* (``'approval.request'``
    or ``'approval.decision'``). The payload carries the
    ``approval_request_id``, ``op_id``, and ``connector_id`` so audit
    queries can surface the request context without joining the
    ``approval_request`` table.

    Session-replay lineage (#2086): ``agent_session_id`` and
    ``parent_audit_id`` are read off *request* (the durable copies
    captured at park time), not off this task's contextvars — the
    "request" row and the "decision" rows are written on different
    tasks (requester vs approver / expiry sweep). The "request" row
    itself is the one stored as ``request.request_audit_id``, so it
    parent-links to the contextvar value instead of self-parenting;
    decision rows parent-link to the stored id.
    """
    payload: dict[str, Any] = {
        "approval_request_id": str(request.id),
        "op_id": request.op_id,
        "connector_id": request.connector_id,
        "principal_sub": request.principal_sub,
        "result_status": path.split(".")[-1],  # "request" | "decision"
    }
    if extra_payload:
        payload.update(extra_payload)
    # policy-gate verdict (#130). The parked "request" row is written on the
    # requester's task, where the dispatcher bound ``policy_decision_var`` to
    # ``needs-approval`` before parking, so the var carries the verdict here.
    # A "decision" row is written on the *approver's* task (var unset) and on
    # a path where no gate ran, so it correctly stays NULL — the operator's
    # approve/reject is recorded via ``result_status``, not a gate verdict.
    # ``hasattr`` guard mirrors ``write_audit_row``: the column lands in ``0051``.
    policy_kwargs: dict[str, Any] = {}
    if hasattr(AuditLog, "policy_decision"):
        policy_kwargs["policy_decision"] = policy_decision_var.get()
    # Session-replay lineage (#2086), off the request row — the durable
    # source of truth, same discipline as ``work_ref`` below. The
    # "request" row and the "decision" rows are written on *different*
    # tasks (requester vs approver / expiry sweep), so reading the
    # contextvars here would drop the originating session on every
    # decision row; the row keeps them aligned with the values the
    # request was parked under.
    #
    # * ``agent_session_id`` — the parking session, so every lifecycle
    #   row anchors in the originating session's replay tree.
    # * ``parent_audit_id`` — the ``approval.request`` audit row's id
    #   (``request.request_audit_id``) for decision rows, which is what
    #   links park → decide into one subtree. The "request" row itself
    #   is the one being written under that id (``audit_id`` equals it),
    #   so it must not self-parent; it takes ``parent_audit_id_var``
    #   instead (NULL for a top-level dispatch; a composite parent's id
    #   when a composite child parked).
    stored_parent = request.request_audit_id
    parent_audit_id: uuid.UUID | None
    if stored_parent is not None and stored_parent != audit_id:
        parent_audit_id = stored_parent
    else:
        parent_audit_id = parent_audit_id_var.get()
    row = AuditLog(
        id=audit_id,
        occurred_at=_now(),
        operator_sub=operator.sub,
        tenant_id=operator.tenant_id,
        target_id=request.target_id,
        parent_audit_id=parent_audit_id,
        agent_session_id=request.agent_session_id,
        method="APPROVAL",
        path=path,
        status_code=status_code,
        request_id=None,
        duration_ms=Decimal(str(round(duration_ms, 2))),
        payload=payload,
        # work_ref I2-T1 (#1659): stamp the authorising change-ticket ref
        # straight off the request row rather than re-reading work_ref_var.
        # The approval-request and decision audit rows are written from the
        # request's own lifecycle (create, then a later approve/reject on a
        # different operator's task whose var is unset), so the row is the
        # durable source of truth -- it keeps both audit rows aligned with
        # the value the request was parked under.
        work_ref=request.work_ref,
        **policy_kwargs,
    )
    session.add(row)
    await session.flush()


# code-quality-allow: already over the 100-line limit on main before #1481
# (105 lines); the principal_act source fix adds a handful of lines, not the
# bulk — splitting the row-build + audit-write is out of scope for this fix.
async def create_pending_request(
    session: AsyncSession,
    *,
    operator: Operator,
    connector_id: str,
    op_id: str,
    target: Any,
    params: dict[str, Any],
    params_hash: str,
    run_id: uuid.UUID | None = None,
    proposed_effect: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> ApprovalRequest:
    """Insert a pending :class:`ApprovalRequest` row + one audit row.

    Both the request row and the audit row are inserted in the same
    session / transaction — the caller owns the commit. Until the commit
    lands, no external observer sees the pending request, preserving the
    synchronous-audit invariant.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        operator: The authenticated operator whose dispatch triggered
            the approval gate.
        connector_id: The full ``<impl_id>-<version>`` string passed to
            the dispatcher.
        op_id: The operation id.
        target: The dispatch target (or ``None`` for tenant-wide ops).
            ``target.id`` is extracted when present.
        params: The original dispatch params. Stored verbatim on the row
            (#1503) so any approval surface (REST ``/decide``, MCP by-id
            approve) can re-dispatch a parked direct operator op with the
            stored params, and also used to verify hash consistency.
        params_hash: Pre-computed
            :func:`~meho_backplane.operations._validate.compute_params_hash`
            of *params*. Stored for resume-path verification.
        run_id: The ``agent_run.id`` this request belongs to; ``None``
            for non-agent-run dispatches.
        proposed_effect: Human-readable summary for the reviewer.
            Defaults to ``{"op_id": op_id, "connector_id": connector_id}``.
        expires_at: Optional deadline. ``None`` means no expiry.

    Returns:
        The flushed :class:`ApprovalRequest` row.
    """
    target_id: uuid.UUID | None = None
    raw_tid = getattr(target, "id", None) if target is not None else None
    if isinstance(raw_tid, uuid.UUID):
        target_id = raw_tid

    if proposed_effect is None:
        proposed_effect = {
            "op_id": op_id,
            "connector_id": connector_id,
        }
        if target_id is not None:
            proposed_effect["target_id"] = str(target_id)

    # RFC 8693 ``act`` (#1481): the agent principal acting on the human
    # subject's behalf on a delegated run, read from the same
    # ``actor_delegation`` contextvar the synchronous audit log resolves
    # (``resolve_actor_sub``). Keeps the row's ``principal_sub`` +
    # ``principal_act`` lineage in lock-step with the audit log; a direct
    # human / autonomous-agent call (no delegation bound) resolves to
    # ``None``. The prior ``getattr(operator, "identity_act", None)`` was
    # dead code — ``Operator`` has no such field, so it was always ``None``.
    principal_act: str | None = resolve_actor_sub()

    # External change-ticket ref (work_ref I2-T1 #1659): read from the
    # same request-time ContextVar the audit writers read (bound at the
    # transport/agent boundary, #1657), so the parked request, its
    # decision audit row, and the re-dispatched op all carry one ref.
    # NULL when nothing bound it.
    #
    # Session-replay lineage (#2086): the "request" audit row's id is
    # generated *before* the row insert so it can be persisted as
    # ``request_audit_id`` — the anchor every later lifecycle audit row
    # (decision, resumed dispatch) parent-links to. ``agent_session_id``
    # is resolved here, on the requester's task, where the session
    # context (agent run var, or the MCP transport's session binding)
    # is still live; the approve / resume surfaces run on a different
    # operator's task and re-hydrate it from the row.
    request_audit_id = uuid.uuid4()
    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        run_id=run_id,
        principal_sub=operator.sub,
        principal_act=principal_act,
        op_id=op_id,
        connector_id=connector_id,
        target_id=target_id,
        params_hash=params_hash,
        params=params,
        proposed_effect=proposed_effect,
        status=ApprovalRequestStatus.PENDING.value,
        created_at=_now(),
        expires_at=expires_at,
        work_ref=work_ref_var.get(),
        agent_session_id=resolve_agent_session_id(),
        request_audit_id=request_audit_id,
    )
    session.add(request)
    await session.flush()

    # Synchronous "request" audit row -- same transaction.
    await _write_audit_row(
        session,
        audit_id=request_audit_id,
        operator=operator,
        request=request,
        path="approval.request",
        status_code=_APPROVAL_STATUS_CODE,
        duration_ms=0.0,
    )

    _log.info(
        "approval_request_created",
        approval_request_id=str(request.id),
        op_id=op_id,
        connector_id=connector_id,
        principal_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        run_id=str(run_id) if run_id else None,
    )
    # Expose the audit_id as a transient attr so callers can publish the
    # ``approval.pending`` broadcast event AFTER they commit the session.
    # A publish-before-commit would surface a phantom event if the commit
    # fails. The transient attr does not persist to the DB row.
    request._audit_id = request_audit_id  # type: ignore[attr-defined]
    return request


# code-quality-allow: 101 lines but ~70 are the Args/Raises docstring documenting
# the four preconditions + the params/reason contract; the executable body is ~15
# lines. Splitting it would scatter one linear approve flow for a line-count win.
async def approve_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    params: dict[str, Any] | None = None,
    reason: str = "",
) -> ApprovalRequest:
    """Approve a pending request.

    Loads the :class:`ApprovalRequest` row, verifies:

    1. The row exists and belongs to ``operator.tenant_id`` (else 404).
    2. The operator holds at least the ``operator`` role (else 403).
    3. The row is still ``pending`` (else 409).
    4. The approver is not the requester — ``operator.sub !=
       request.principal_sub`` — unless the deployment enabled the
       audited single-operator break-glass mode
       (``Settings.approval_allow_self_approval``). Else 403,
       :class:`SelfApprovalForbiddenError` (G11.7-T1 #1401). Checked
       before the hash check so a self-approver learns *why* they are
       refused rather than being told their params mismatch.
    5. If *params* is supplied, ``compute_params_hash(params)`` matches
       the stored ``params_hash`` (else 422, :class:`ParamsMismatchError`).
       The hash check is **skipped** when *params* is ``None`` — the
       operator-decision path (G11.2-T5 MCP/CLI surface) approves by
       request id alone and does not have the original params. The
       agent's REST path still supplies them so the swap defence applies
       on that branch.

    Then:

    * Flips the row to ``approved``, stamps ``reviewed_by`` + ``decided_at``.
    * Writes a synchronous "decision" audit row in the same transaction.

    The **re-dispatch** (executing the approved op) happens *after* the
    caller commits this transaction. The ``POST /api/v1/approvals/{id}/
    approve`` REST route calls :func:`~meho_backplane.operations.dispatcher.dispatch`
    with ``_approved=True`` (the gate-bypass: the approval is the
    authorization). The MCP/CLI operator-decision path commits the
    decision without re-dispatching — the agent picks up execution
    separately. Separating the decision commit from the re-dispatch
    means the approval is durable even if the re-dispatch fails.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        request_id: The pending row's id.
        operator: The authenticated reviewer.
        params: The **original** dispatch params (un-modified). Required
            on the REST path so the hash check applies; ``None`` on the
            MCP/CLI operator-decision path.
        reason: Optional human-readable approval reason (recorded in the
            audit payload). Mirrors :func:`reject_request`.

    Returns:
        The updated, flushed :class:`ApprovalRequest`.

    Raises:
        ApprovalNotFoundError: No row for *request_id* in this tenant.
        UnauthorizedApprovalError: Operator lacks ``operator`` role.
        ApprovalRequestAlreadyDecidedError: Row is not ``pending``.
        SelfApprovalForbiddenError: Approver is the requester and
            break-glass is disabled (G11.7-T1 #1401).
        ParamsMismatchError: Hash of *params* != stored ``params_hash``
            (only raised when *params* is supplied).
    """
    request = await _load_pending_for_approval(
        session, request_id, operator=operator, params=params
    )

    now = _now()
    request.status = ApprovalRequestStatus.APPROVED.value
    request.reviewed_by = operator.sub
    request.decided_at = now
    await session.flush()

    extra: dict[str, Any] = {"decision": "approved", "reviewed_by": operator.sub}
    if reason:
        extra["reason"] = reason

    approve_audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=approve_audit_id,
        operator=operator,
        request=request,
        path="approval.decision",
        status_code=_DECISION_STATUS_CODE_APPROVED,
        duration_ms=0.0,
        extra_payload=extra,
    )

    _log.info(
        "approval_request_approved",
        approval_request_id=str(request_id),
        op_id=request.op_id,
        reviewed_by=operator.sub,
        tenant_id=str(operator.tenant_id),
    )
    request._audit_id = approve_audit_id  # type: ignore[attr-defined]
    return request


async def reject_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    reason: str = "",
) -> ApprovalRequest:
    """Reject a pending request — the original dispatch is not executed.

    Same preconditions as :func:`approve_request` (tenant isolation,
    role check, ``pending`` guard) minus the hash check (no params
    needed to reject). Flips the row to ``rejected``, stamps
    ``reviewed_by`` + ``decided_at``, writes the decision audit row.

    Args:
        session: Open :class:`AsyncSession`; flushed, not committed.
        request_id: The pending row's id.
        operator: The authenticated reviewer.
        reason: Optional human-readable rejection reason (recorded in
            the audit payload).

    Returns:
        The updated, flushed :class:`ApprovalRequest`.

    Raises:
        ApprovalNotFoundError: No row for *request_id* in this tenant.
        UnauthorizedApprovalError: Operator lacks ``operator`` role.
        ApprovalRequestAlreadyDecidedError: Row is not ``pending``.
    """
    _check_reviewer_role(operator)

    request = await _load_for_tenant(session, request_id, operator.tenant_id)

    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalRequestAlreadyDecidedError(request_id, request.status)

    now = _now()
    request.status = ApprovalRequestStatus.REJECTED.value
    request.reviewed_by = operator.sub
    request.decided_at = now
    await session.flush()

    extra: dict[str, Any] = {"decision": "rejected", "reviewed_by": operator.sub}
    if reason:
        extra["reason"] = reason

    reject_audit_id = uuid.uuid4()
    await _write_audit_row(
        session,
        audit_id=reject_audit_id,
        operator=operator,
        request=request,
        path="approval.decision",
        status_code=_DECISION_STATUS_CODE_REJECTED,
        duration_ms=0.0,
        extra_payload=extra,
    )

    _log.info(
        "approval_request_rejected",
        approval_request_id=str(request_id),
        op_id=request.op_id,
        reviewed_by=operator.sub,
        tenant_id=str(operator.tenant_id),
        reason=reason or None,
    )
    request._audit_id = reject_audit_id  # type: ignore[attr-defined]
    return request


async def claim_resume(
    request_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> bool:
    """Win the exactly-one-resumer claim for *request_id* (#2293, G0.30).

    A single conditional ``UPDATE approval_request SET resumed_at = :now
    WHERE id = :request_id AND resumed_at IS NULL`` -- the atomic claim
    every resumer of an approved op must win before it re-dispatches
    ``dispatch(..., _approved=True)``: the in-process agent waiter
    (:mod:`meho_backplane.agent.approval_wait`), the shared
    :func:`resume_dispatch_after_approval` operator path (REST ``/approve``
    + ``/decide``, MCP by-id approve, UI approve), and any future resumer.
    Returns ``True`` when this caller set ``resumed_at`` (one row touched)
    and therefore owns the single execution; ``False`` when another
    resumer already claimed it (zero rows touched) and this caller must
    no-op cleanly.

    Why a conditional ``UPDATE`` rather than a Python ``if`` + edit (the
    same reasoning as :func:`~meho_backplane.operations.agent_run.extend_lease`'s
    lease claim): a read-then-write would race a concurrent resumer -- the
    agent waiter woken by the ``approval.approved`` broadcast vs. the
    operator surface that published it -- with both reading
    ``resumed_at IS NULL`` and both dispatching the write. The predicate
    and the write commit together in one statement, so at most one resumer
    wins even across processes / pods: a concurrent claim blocks on the
    row lock, then re-evaluates the predicate after commit and loses. No
    advisory locks.

    Runs in its **own committed transaction** so the latch is durable the
    instant it is won -- the decision commit and the re-dispatch happen in
    separate sessions, and the claim must be visible to a racing resumer
    independently of either. The column is a one-way latch (never cleared),
    so a dispatch that fails after a won claim is not silently retried into
    a possible double write; the residual expiry follow-up (out of scope
    here) makes such a void approval visibly "expired".
    """
    from meho_backplane.db.engine import get_sessionmaker

    stamp = now or _now()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # ``session.execute()`` on an ``UPDATE`` returns a ``CursorResult``
        # (carrying the DBAPI ``rowcount``) at runtime; the static stub is
        # the generic ``Result`` so mypy needs the cast to read ``rowcount``.
        # ``synchronize_session=False`` -- no ORM identity map to keep in
        # step, this is a bare claim probe. Read ``rowcount`` before the
        # commit closes the cursor.
        raw_result = await session.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .where(ApprovalRequest.resumed_at.is_(None))
            .values(resumed_at=stamp)
            .execution_options(synchronize_session=False)
        )
        won = cast(CursorResult[Any], raw_result).rowcount == 1
        await session.commit()
    return won


async def resume_dispatch_after_approval(
    *,
    operator: Operator,
    request: ApprovalRequest,
    params: dict[str, Any] | None = None,
) -> Any:
    """Re-hydrate the stored target and re-dispatch an approved op (#1503, G11.7-T1).

    The single execute-after-approve entry point shared by every operator
    approval surface — REST ``/approve`` and ``/decide``, and the MCP
    by-id approve tool. It re-runs the original dispatch under the
    committed approval decision (``dispatch(..., _approved=True)``), so a
    parked **direct** operator op is executed exactly once regardless of
    which surface granted it. The in-process agent-run resume path does
    not call this — it keeps the params in memory and re-dispatches from
    there (see :mod:`meho_backplane.agent.approval_wait`).

    *params* source:

    * REST ``/approve`` passes the caller-supplied params (already
      hash-verified against ``params_hash`` by :func:`approve_request`).
    * ``/decide`` and the MCP by-id approve hold only the request id, so
      they pass ``params=None`` and this helper falls back to the params
      stored on the row at park time (``request.params``, #1503). A row
      written before migration 0036 has ``params IS NULL`` and no
      caller-supplied params — there is nothing to re-dispatch, so the
      helper **fails closed** with a structured ``denied`` result naming
      the gap (the op must be resumed via REST ``/approve`` + params, the
      pre-0036 path).

    Target re-hydration (G11.7-T1 #1401): the row persists only
    ``target_id``, not the full target. A write op whose handler reads
    ``target.host`` / ``target.name`` would mis-resolve (or crash) if
    re-dispatched with ``target=None``, so a concrete ``target_id`` is
    re-loaded by id (tenant-scoped, ``deleted_at IS NULL``). A target
    soft-deleted (or revoked) between request and approval resolves to
    ``None``; the re-dispatch then **fails closed** (structured ``denied``,
    never calls :func:`dispatch`) rather than executing the approved
    privileged write outside the original target scope. Tenant-wide ops
    (no original target) keep ``target_id IS NULL`` → ``None`` and
    dispatch normally.

    The re-dispatch bypasses the policy gate (``_approved=True``): the
    committed approval decision is the authorization. Without the bypass
    the re-dispatch would re-queue (a human/service principal now routes
    ``requires_approval`` to ``needs-approval`` per G11.7-T1; an agent
    re-hits ``needs-approval``), so the approved op would never execute.

    Exactly-one-resumer claim (#2293): every operator surface routes
    through here, and for a run-bound request the in-process agent waiter
    (:mod:`meho_backplane.agent.approval_wait`) also resumes off the
    ``approval.approved`` broadcast. Both must win :func:`claim_resume`
    before dispatching, so the approved op executes exactly once: this
    path wins the claim right before :func:`dispatch` (after the
    fail-closed checks, so a refused resume never burns the claim) and, if
    a racing resumer already claimed it, returns a benign
    ``already_resumed`` result instead of re-dispatching the write. This
    is what lets ``/decide`` + MCP fall back to a server-side re-dispatch
    when the claim is free (covering waiter-gone -- timeout / restart /
    cancel) while the claim blocks the ``/approve`` / UI double-dispatch
    when the waiter is alive.
    """
    effective_params = params if params is not None else request.params
    if effective_params is None:
        return _resume_pre0036_denied(operator, request)

    resolved_target, denied = await _rehydrate_resume_target(operator, request)
    if denied is not None:
        return denied

    # Exactly-one-resumer claim (#2293): win the atomic conditional UPDATE
    # right before executing -- after the fail-closed checks above, so a
    # refused resume (pre-0036 params gap, unresolvable target) never burns
    # the claim and leaves the op un-executable. A lost claim means another
    # resumer already executed the approved op (the in-process agent waiter
    # woken by the same approval.approved broadcast, or a racing operator
    # surface), so no-op with a benign already_resumed result rather than
    # double-dispatching the approved write.
    if not await claim_resume(request.id):
        _log.info(
            "approval_resume_already_claimed",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            connector_id=request.connector_id,
            operator_sub=operator.sub,
        )
        return result_already_resumed(request.op_id, request.id, 0.0)

    return await _dispatch_resume_with_bound_context(
        operator=operator,
        request=request,
        resolved_target=resolved_target,
        effective_params=effective_params,
    )


def _resume_pre0036_denied(operator: Operator, request: ApprovalRequest) -> Any:
    """Fail-closed result for a pre-0036 row with no stored params to re-dispatch.

    A row parked before migration 0036 has ``params IS NULL`` and is
    approved via a surface that carries no params (/decide, MCP by-id), so
    there is nothing to re-dispatch — refuse rather than dispatch an empty
    call. Reached before the claim, so it never burns it. Extracted to keep
    :func:`resume_dispatch_after_approval` under the function-size budget.
    """
    _log.warning(
        "approval_resume_params_unavailable",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        connector_id=request.connector_id,
        operator_sub=operator.sub,
    )
    return result_denied(
        request.op_id,
        (
            f"approval request {request.id} has no stored params "
            "(parked before migration 0036); re-dispatch refused — "
            "approve via REST /approve with the original params instead"
        ),
        0.0,
    )


async def _dispatch_resume_with_bound_context(
    *,
    operator: Operator,
    request: ApprovalRequest,
    resolved_target: Any,
    effective_params: dict[str, Any],
) -> Any:
    """Re-bind the parked row's stored context, then dispatch ``_approved=True``.

    The decision surfaces run on a fresh task whose vars are unset, so the
    executed op's audit row would otherwise lose everything the parked
    request carried. Three row-sourced, token-reset bindings: ``work_ref``
    (#1659, the authorising ticket), ``agent_session_id`` (#2086, anchors
    the row in the requester's replay tree), and ``parent_audit_id`` (#2086,
    back-links it to the parking row). The in-process agent-run resume keeps
    its own bound vars and does not call this helper; pre-0053 NULLs bind
    ``None``, the vars' defaults. Only reached after the exactly-one-resumer
    claim is won (#2293), so it dispatches exactly once.
    """
    from meho_backplane.operations.dispatcher import dispatch

    work_ref_token = work_ref_var.set(request.work_ref)
    session_token = agent_session_id_var.set(request.agent_session_id)
    parent_token = parent_audit_id_var.set(request.request_audit_id)
    try:
        return await dispatch(
            operator=operator,
            connector_id=request.connector_id,
            op_id=request.op_id,
            target=resolved_target,
            params=effective_params,
            _approved=True,
        )
    finally:
        parent_audit_id_var.reset(parent_token)
        agent_session_id_var.reset(session_token)
        work_ref_var.reset(work_ref_token)


async def _rehydrate_resume_target(
    operator: Operator,
    request: ApprovalRequest,
) -> tuple[Any, Any]:
    """Re-load the stored target by id for a resume re-dispatch (G11.7-T1 #1401).

    Returns ``(resolved_target, None)`` on success — ``resolved_target``
    is ``None`` for a tenant-wide op (no ``target_id`` pinned) and the
    live :class:`Target` otherwise. Returns ``(None, denied_result)`` when
    a concrete ``target_id`` was pinned at request time but no live target
    resolves it now (soft-deleted / revoked between request and approval,
    or cross-tenant): the caller must **fail closed** and not dispatch,
    since dispatching with ``target=None`` would let a typed handler that
    derives its connection from ``connector_id`` / ``params`` execute the
    approved privileged write outside the original target scope.
    """
    if request.target_id is None:
        return None, None

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.targets.resolver import resolve_target_by_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved_target = await resolve_target_by_id(session, operator.tenant_id, request.target_id)
    if resolved_target is not None:
        return resolved_target, None

    _log.warning(
        "approval_resume_target_unresolvable",
        approval_request_id=str(request.id),
        op_id=request.op_id,
        connector_id=request.connector_id,
        target_id=str(request.target_id),
        operator_sub=operator.sub,
    )
    denied = result_denied(
        request.op_id,
        (
            f"approved target {request.target_id} no longer resolves "
            "(soft-deleted or revoked between request and approval); "
            "re-dispatch refused to avoid executing outside the "
            "original target scope"
        ),
        0.0,
    )
    return None, denied


async def expire_stale_requests(
    session: AsyncSession,
    *,
    operator: Operator,
    now: datetime | None = None,
) -> list[ApprovalRequest]:
    """Transition all ``pending`` rows past their ``expires_at`` to ``expired``.

    Writes one "decision" audit row per expired request inside the same
    session (caller commits). Intended to be called from a background
    task / CLI sweep on a periodic interval.

    Only rows with ``expires_at IS NOT NULL AND expires_at <= now`` and
    ``status = 'pending'`` are affected.

    Args:
        session: Open :class:`AsyncSession``; flushed, not committed.
        operator: The identity to record on the audit rows (typically a
            system service-account operator). Must hold at least the
            ``operator`` role.
        now: Override for "current time" (used in tests). Defaults to
            :func:`datetime.now(UTC)`.

    Returns:
        List of the expired :class:`ApprovalRequest` rows (may be empty).
        Each row carries the transient ``_audit_id`` attribute (the
        decision row's primary key) so the caller can publish a
        fail-open ``approval.expired`` broadcast event **after commit**
        via :func:`publish_approval_event`. The attribute is set on
        every returned row — see :func:`approve_request` /
        :func:`reject_request` for the same pattern.
    """
    # Enforce the operator-role floor the docstring promises, mirroring
    # approve_request / reject_request (CodeRabbit #1086).
    _check_reviewer_role(operator)
    cutoff = now or _now()

    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalRequestStatus.PENDING.value)
        .where(ApprovalRequest.expires_at.is_not(None))
        .where(ApprovalRequest.expires_at <= cutoff)
        .where(ApprovalRequest.tenant_id == operator.tenant_id)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    for request in rows:
        request.status = ApprovalRequestStatus.EXPIRED.value
        request.decided_at = cutoff
        await session.flush()

        expire_audit_id = uuid.uuid4()
        await _write_audit_row(
            session,
            audit_id=expire_audit_id,
            operator=operator,
            request=request,
            path="approval.decision",
            status_code=_DECISION_STATUS_CODE_EXPIRED,
            duration_ms=0.0,
            extra_payload={"decision": "expired", "expires_at": str(request.expires_at)},
        )

        _log.info(
            "approval_request_expired",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            tenant_id=str(operator.tenant_id),
        )
        # Expose the audit_id as a transient attr so the caller can
        # publish the ``approval.expired`` broadcast event AFTER commit.
        # See create_pending_request / approve_request / reject_request
        # for the same publish-after-commit invariant: a publish-before-
        # commit would surface a phantom event if the commit fails.
        request._audit_id = expire_audit_id  # type: ignore[attr-defined]

    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_reviewer_role(operator: Operator) -> None:
    """Raise :class:`UnauthorizedApprovalError` if the operator is read_only."""
    from meho_backplane.auth.operator import TenantRole

    if operator.tenant_role == TenantRole.READ_ONLY:
        raise UnauthorizedApprovalError(
            operator_sub=operator.sub,
            role=operator.tenant_role.value,
        )


def _check_self_approval(operator: Operator, request: ApprovalRequest) -> None:
    """Raise :class:`SelfApprovalForbiddenError` on a self-approval.

    Enforces requester != approver (G11.7-T1 #1401): the principal that
    parked the request (``request.principal_sub``) may not also approve
    it unless the deployment enabled the audited single-operator
    break-glass mode (``Settings.approval_allow_self_approval``). The
    comparison is on the stable ``sub`` claim, so a renamed display name
    cannot launder a self-approval.

    Imported lazily to keep the queue module decoupled from settings at
    import time (mirrors the local ``TenantRole`` import in
    :func:`_check_reviewer_role`).
    """
    if operator.sub != request.principal_sub:
        return

    from meho_backplane.settings import get_settings

    if get_settings().approval_allow_self_approval:
        _log.warning(
            "approval_self_approval_break_glass",
            approval_request_id=str(request.id),
            op_id=request.op_id,
            operator_sub=operator.sub,
            tenant_id=str(operator.tenant_id),
        )
        return

    raise SelfApprovalForbiddenError(request.id, principal_sub=operator.sub)


async def _load_pending_for_approval(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    operator: Operator,
    params: dict[str, Any] | None,
) -> ApprovalRequest:
    """Load + validate a row for approval, raising on any precondition failure.

    Runs the full approve precondition ladder in order so callers learn
    the most specific reason first: role gate → tenant-scoped load →
    pending guard → self-approval guard (G11.7-T1 #1401) → params-hash
    check (only when *params* is supplied). Returns the validated
    pending row; the caller flips status + writes the decision audit row.
    """
    _check_reviewer_role(operator)
    request = await _load_for_tenant(session, request_id, operator.tenant_id)
    if request.status != ApprovalRequestStatus.PENDING.value:
        raise ApprovalRequestAlreadyDecidedError(request_id, request.status)
    _check_self_approval(operator, request)
    if params is not None:
        incoming_hash = compute_params_hash(params)
        if incoming_hash != request.params_hash:
            raise ParamsMismatchError(request_id)
    return request


async def _load_for_tenant(
    session: AsyncSession,
    request_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> ApprovalRequest:
    """Load an :class:`ApprovalRequest` by id, enforcing tenant isolation.

    Returns the row if found and owned by *tenant_id*; raises
    :class:`ApprovalNotFoundError` for missing rows or cross-tenant
    access (the two cases are indistinguishable to callers).
    """
    row = await session.get(ApprovalRequest, request_id)
    if row is None or row.tenant_id != tenant_id:
        raise ApprovalNotFoundError(request_id)
    return row


# ---------------------------------------------------------------------------
# G11.2-T5 (#818) read helpers for the operator surfaces (REST GET /{id},
# MCP `meho.approvals.list` / `.get`, CLI `meho approvals list / show`).
# ---------------------------------------------------------------------------


async def list_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str | None = "pending",
    work_ref: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ApprovalRequest]:
    """Page through approval requests in *tenant_id*.

    G11.2-T5 (#818) read substrate. ``status=None`` returns every state;
    ``status="pending"`` (the default for the operator UX) returns only
    requests awaiting a decision. ``work_ref`` (work_ref I2-T1 #1659),
    when supplied, narrows to requests authorised by that exact external
    change ticket (``"gh:evoila/meho#1"``); ``None`` applies no work_ref
    filter. Tenant-isolated by the WHERE clause — cross-tenant ids are
    invisible.
    """
    from sqlalchemy import select

    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    if work_ref is not None:
        stmt = stmt.where(ApprovalRequest.work_ref == work_ref)
    stmt = stmt.order_by(ApprovalRequest.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> ApprovalRequest:
    """Fetch one approval request by id, tenant-isolated.

    G11.2-T5 (#818) — drives ``GET /api/v1/approvals/{id}``,
    ``meho.approvals.get``, and ``meho approvals show``. Raises
    :class:`ApprovalNotFoundError` for missing rows or cross-tenant
    access (indistinguishable to the caller).
    """
    return await _load_for_tenant(session, request_id, tenant_id)


# ---------------------------------------------------------------------------
# G11.2-T5 (#818) broadcast notifications. Fail-open: a broadcast outage
# never blocks the durable decision (the row + audit are the truth).
# ---------------------------------------------------------------------------


async def publish_approval_event(
    *,
    tenant_id: uuid.UUID,
    request: ApprovalRequest,
    decision: str,
    principal_sub: str,
    audit_id: uuid.UUID,
) -> None:
    """Publish a fail-open broadcast event for an approval lifecycle step.

    *decision* is one of ``"pending"`` (creation), ``"approved"``,
    ``"rejected"``, or ``"expired"`` (sweeper-driven). The broadcast
    ``op_id`` is ``approval.<decision>`` so operator watchers can match
    the family with a simple glob (``approval.*``).
    """
    try:
        from meho_backplane.broadcast.events import BroadcastEvent, classify_op
        from meho_backplane.broadcast.publisher import publish_event

        broadcast_op_id = f"approval.{decision}"
        event = BroadcastEvent(
            event_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            tenant_id=tenant_id,
            principal_sub=principal_sub,
            op_id=broadcast_op_id,
            op_class=classify_op(broadcast_op_id),
            result_status="ok",
            audit_id=audit_id,
            payload={
                "op_class": classify_op(broadcast_op_id),
                "result_status": "ok",
                "approval_request_id": str(request.id),
                "decision": decision,
                "connector_id": request.connector_id,
                "approval_op_id": request.op_id,
            },
        )
        await publish_event(event)
    except Exception:
        # Fail-open: a broadcast outage must not block the durable
        # decision. The row + audit row remain the source of truth.
        _log.exception(
            "approval_broadcast_failed",
            approval_request_id=str(request.id),
            decision=decision,
        )
