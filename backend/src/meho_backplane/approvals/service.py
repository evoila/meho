# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``ApprovalRequestService`` — tenant-scoped list/get/approve/reject.

Initiative #803 (G11.2 Agent identity + RBAC + approval), Task #818
(T5 — approval surfacing channel). The single code path the REST routes
(:mod:`meho_backplane.api.v1.approvals`), MCP tools
(:mod:`meho_backplane.mcp.tools.approvals`), and Go CLI verbs
(``cli/internal/cmd/approvals``) dispatch through, enforcing the tenant
boundary, RBAC invariants, and the two-audit-row commitment discipline
in one place.

Concurrency model
-----------------

Stateless and method-scoped: each public method opens its own
:class:`~sqlalchemy.ext.asyncio.AsyncSession`, commits, and closes.
Mirrors :class:`~meho_backplane.agents.service.AgentDefinitionService`.

Tenant scoping
--------------

Every public method takes ``tenant_id`` as first parameter; all queries
start with ``WHERE tenant_id = :tenant_id``. Cross-tenant rows are
structurally invisible.

RBAC
----

The service does **not** enforce roles — it assumes the caller has already
validated them (``operator`` for reads/list, ``operator`` for
approve/reject; ``read_only`` for list-only). REST routes / MCP tools /
CLI verbs own the :func:`~meho_backplane.auth.rbac.require_role` gate.

Resume-API integration (T4 / #817)
------------------------------------

On approve or reject, the service calls the T4 resume path via the
``agent_run_id`` soft-FK. In v0.2 (before T4 lands) the call is a
best-effort broadcast notification + status flip; the actual
dispatcher resume will be wired once T4's ``AgentRunResumeService``
is importable. The ``_resume_run`` stub is clearly marked for follow-up.

Broadcast notification
----------------------

On ``create``, the service publishes a ``pending_approval`` broadcast
event so an operator's ``broadcast_watch``-listening session learns of
new requests without polling. Uses the existing
:func:`~meho_backplane.broadcast.publisher.publish_event` fail-open path
(a Valkey blip never blocks the approval row from writing).

Error contract
--------------

* :class:`ApprovalNotFoundError` — requested row absent or cross-tenant.
  Callers map this to HTTP 404.
* :class:`ApprovalDecisionError` — attempted to decide on a non-pending
  request. Callers map this to HTTP 409.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.approvals.schemas import (
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalRequestDetail,
    ApprovalRequestSummary,
)
from meho_backplane.broadcast.events import BroadcastEvent, classify_op
from meho_backplane.broadcast.publisher import publish_event
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalStatus

__all__ = [
    "ApprovalDecisionError",
    "ApprovalNotFoundError",
    "ApprovalRequestService",
]

_log = structlog.get_logger(__name__)

#: Default page size for list queries.
_DEFAULT_LIMIT: Final[int] = 50

#: Maximum page size.
_MAX_LIMIT: Final[int] = 500


class ApprovalNotFoundError(Exception):
    """Raised when the requested approval request does not exist in the tenant."""


class ApprovalDecisionError(Exception):
    """Raised when a decision is attempted on a non-pending request.

    The ``current_status`` attribute carries the actual status so callers
    can surface it in error detail without re-querying.
    """

    def __init__(self, request_id: uuid.UUID, current_status: str) -> None:
        super().__init__(
            f"approval_request {request_id} is {current_status!r}; "
            "only pending requests can be approved or rejected"
        )
        self.request_id = request_id
        self.current_status = current_status


def _to_summary(row: ApprovalRequest) -> ApprovalRequestSummary:
    return ApprovalRequestSummary(
        id=row.id,
        tenant_id=row.tenant_id,
        status=ApprovalStatus(row.status),
        connector_id=row.connector_id,
        op_id=row.op_id,
        principal_sub=row.principal_sub,
        principal_act=row.principal_act,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _to_detail(
    row: ApprovalRequest,
    *,
    elicitation_url: str | None = None,
) -> ApprovalRequestDetail:
    return ApprovalRequestDetail(
        id=row.id,
        tenant_id=row.tenant_id,
        status=ApprovalStatus(row.status),
        agent_run_id=row.agent_run_id,
        connector_id=row.connector_id,
        op_id=row.op_id,
        target_id=row.target_id,
        params_hash=row.params_hash,
        proposed_effect=row.proposed_effect,
        principal_sub=row.principal_sub,
        principal_act=row.principal_act,
        reviewed_by=row.reviewed_by,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        request_audit_id=row.request_audit_id,
        decision_audit_id=row.decision_audit_id,
        elicitation_url=elicitation_url,
    )


async def _resume_run(
    *,
    agent_run_id: uuid.UUID,
    approved: bool,
    reason: str | None,
    reviewer_sub: str,
) -> None:
    """Best-effort stub for T4 resume integration.

    When T4 (#817, ``AgentRunResumeService``) lands, replace this stub
    with the real ``await resume_service.resume(agent_run_id, approved=approved)``.
    Until then the status flip on the ``approval_request`` row is the
    durable record and the broadcast event is the operator signal.
    """
    _log.info(
        "approval_resume_stub",
        agent_run_id=str(agent_run_id),
        approved=approved,
        reviewer_sub=reviewer_sub,
        note="T4 resume integration pending (#817)",
    )


class ApprovalRequestService:
    """Tenant-scoped list / get / approve / reject over ``approval_request``.

    Instantiate once per request (or per long-running CLI session) and
    call methods freely — the service is stateless and session-scoped.

    Parameters
    ----------
    base_url:
        The backplane's public base URL, used to construct the MCP
        elicitation URL-mode address on ``get``. Optional; when omitted,
        ``elicitation_url`` is ``None`` on detail responses.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None

    def _elicitation_url(self, request_id: uuid.UUID) -> str | None:
        if self._base_url is None:
            return None
        return f"{self._base_url}/api/v1/approvals/{request_id}/decide"

    async def list_(
        self,
        *,
        tenant_id: uuid.UUID,
        status: ApprovalStatus | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> ApprovalListResponse:
        """List approval requests for *tenant_id*, newest first.

        Parameters
        ----------
        tenant_id:
            The tenant to list for. Cross-tenant rows are invisible.
        status:
            When set, filter to only this status (``pending`` is the
            most common operator query). When ``None``, all statuses.
        limit:
            Page size cap (1 .. ``_MAX_LIMIT``).
        offset:
            Pagination offset.
        """
        limit = max(1, min(limit, _MAX_LIMIT))
        offset = max(0, offset)
        sm = get_sessionmaker()
        async with sm() as session:
            q = select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id)
            if status is not None:
                q = q.where(ApprovalRequest.status == status.value)
            total_q = select(func.count()).select_from(q.subquery())
            total: int = (await session.execute(total_q)).scalar_one()
            rows = (
                (
                    await session.execute(
                        q.order_by(ApprovalRequest.created_at.desc()).limit(limit).offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return ApprovalListResponse(
            items=[_to_summary(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get(
        self,
        *,
        tenant_id: uuid.UUID,
        request_id: uuid.UUID,
    ) -> ApprovalRequestDetail:
        """Fetch a single approval request by id.

        Raises :class:`ApprovalNotFoundError` when the row is absent or
        belongs to a different tenant.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            row = await _fetch(session, tenant_id=tenant_id, request_id=request_id)
        return _to_detail(row, elicitation_url=self._elicitation_url(row.id))

    async def approve(
        self,
        *,
        tenant_id: uuid.UUID,
        request_id: uuid.UUID,
        reviewer_sub: str,
        body: ApprovalDecision,
        decision_audit_id: uuid.UUID | None = None,
    ) -> ApprovalRequestDetail:
        """Approve a pending request.

        Flips status to ``approved``, stamps ``reviewed_by`` /
        ``decided_at``, calls the T4 resume stub, and publishes a
        ``approval_decided`` broadcast event.

        Raises :class:`ApprovalNotFoundError` for absent / cross-tenant
        rows. Raises :class:`ApprovalDecisionError` for non-pending rows.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            row = await _fetch(session, tenant_id=tenant_id, request_id=request_id)
            if row.status != ApprovalStatus.PENDING.value:
                raise ApprovalDecisionError(row.id, row.status)
            row.status = ApprovalStatus.APPROVED.value
            row.reviewed_by = reviewer_sub
            row.decided_at = datetime.now(UTC)
            if decision_audit_id is not None:
                row.decision_audit_id = decision_audit_id
            await session.flush()
            detail = _to_detail(row, elicitation_url=self._elicitation_url(row.id))
            await session.commit()
        _log.info(
            "approval_approved",
            request_id=str(request_id),
            tenant_id=str(tenant_id),
            reviewer_sub=reviewer_sub,
        )
        await _publish_decision_event(
            tenant_id=tenant_id,
            request_id=request_id,
            decision="approved",
            connector_id=detail.connector_id,
            op_id=detail.op_id,
            reviewer_sub=reviewer_sub,
        )
        if detail.agent_run_id is not None:
            await _resume_run(
                agent_run_id=detail.agent_run_id,
                approved=True,
                reason=body.reason,
                reviewer_sub=reviewer_sub,
            )
        return detail

    async def reject(
        self,
        *,
        tenant_id: uuid.UUID,
        request_id: uuid.UUID,
        reviewer_sub: str,
        body: ApprovalDecision,
        decision_audit_id: uuid.UUID | None = None,
    ) -> ApprovalRequestDetail:
        """Reject a pending request.

        Flips status to ``rejected``, stamps ``reviewed_by`` /
        ``decided_at``, calls the T4 resume stub, and publishes a
        ``approval_decided`` broadcast event.

        Raises :class:`ApprovalNotFoundError` for absent / cross-tenant
        rows. Raises :class:`ApprovalDecisionError` for non-pending rows.
        """
        sm = get_sessionmaker()
        async with sm() as session:
            row = await _fetch(session, tenant_id=tenant_id, request_id=request_id)
            if row.status != ApprovalStatus.PENDING.value:
                raise ApprovalDecisionError(row.id, row.status)
            row.status = ApprovalStatus.REJECTED.value
            row.reviewed_by = reviewer_sub
            row.decided_at = datetime.now(UTC)
            if decision_audit_id is not None:
                row.decision_audit_id = decision_audit_id
            await session.flush()
            detail = _to_detail(row, elicitation_url=self._elicitation_url(row.id))
            await session.commit()
        _log.info(
            "approval_rejected",
            request_id=str(request_id),
            tenant_id=str(tenant_id),
            reviewer_sub=reviewer_sub,
        )
        await _publish_decision_event(
            tenant_id=tenant_id,
            request_id=request_id,
            decision="rejected",
            connector_id=detail.connector_id,
            op_id=detail.op_id,
            reviewer_sub=reviewer_sub,
        )
        if detail.agent_run_id is not None:
            await _resume_run(
                agent_run_id=detail.agent_run_id,
                approved=False,
                reason=body.reason,
                reviewer_sub=reviewer_sub,
            )
        return detail


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
) -> ApprovalRequest:
    """Fetch by (tenant_id, id); raise :class:`ApprovalNotFoundError` if absent."""
    row = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.id == request_id,
                ApprovalRequest.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApprovalNotFoundError(request_id)
    return row


async def _publish_decision_event(
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    decision: str,
    connector_id: str,
    op_id: str,
    reviewer_sub: str,
) -> None:
    """Publish a fail-open broadcast event for an approval decision.

    The ``audit_id`` is a synthetic UUID because the approval-decision audit
    row is written by the calling REST route's audit middleware (not in this
    service). A future tightening can bind the pre-allocated audit id via
    :func:`~meho_backplane.audit.bind_preallocated_audit_id` and pass it
    here; the synthetic value is flagged in the payload so consumers know
    the ``audit_id`` FK is not yet trustworthy for join.
    """
    synthetic_audit_id = uuid.uuid4()
    broadcast_op_id = f"approval.{decision}"
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=tenant_id,
        principal_sub=reviewer_sub,
        op_id=broadcast_op_id,
        op_class=classify_op(broadcast_op_id),
        result_status="ok",
        audit_id=synthetic_audit_id,
        payload={
            "op_class": classify_op(broadcast_op_id),
            "result_status": "ok",
            "approval_request_id": str(request_id),
            "decision": decision,
            "connector_id": connector_id,
            "approval_op_id": op_id,
        },
    )
    await publish_event(event)
