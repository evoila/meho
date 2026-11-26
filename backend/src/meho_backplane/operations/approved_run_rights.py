# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved run checks the rights of who asked, as for a new call made now.

A person approved one exact call. Every run of that call checks the rights
of the login that asked again, at the moment it runs: the first run after
the approval (on an approvals surface, or in the agent's own wait), and
every re-run after a failed run. The check is the one a new call of the
same login would get now. Only the approval itself is not asked for again.

* **A new call would be refused** (a ``deny`` grant, no grant while
  ``AGENT_GRANT_REQUIRED`` is ``on``, a role below the operation's floor):
  the approved run is refused, and nothing runs.
* **A new call would wait for approval:** the approval covers it only when
  it would still be enough for a new request now. When the risk level of
  the operation went up to ``dangerous``, the person who approved must be in
  a group that may approve that level. When it went up to ``destructive``,
  a new approval is always needed: such an approval binds a preview.
* **A new call would run at once:** the run goes ahead.

Who is checked
==============

* **The caller, when it is the one that asked**: the agent's own resume, a
  hosted agent run, and the requester's re-run. Its token is the current
  one, so a lower role counts.
* **The agent that asked, when a person runs the call from an approvals
  surface** (console, CLI, REST ``/approve`` and ``/decide``). MEHO builds
  the agent again from the record on the approval card
  (:func:`~meho_backplane.operations.approved_step_check.requester_record`).
  A card without that record is not checked here.

A refusal does not use up the approval. On an approvals surface and in the
agent's wait, the run ends as a failed run with the reason. The re-run
checks the same rule before it takes the run
(:mod:`~meho_backplane.operations.approval_rerun`), so a refused re-run does
not change the request. When the rights come back, the requester may run it
again; a new call would then pass too.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import select

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator
from meho_backplane.operations.composite import COMPOSITE_DEPTH_TOP_LEVEL, composite_depth_var
from meho_backplane.operations.secondary_target_policy import approved_agent_requester

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor

__all__ = [
    "ApprovedRun",
    "RightsRefusal",
    "approved_run_refusal",
    "bind_approved_run",
]

_log = structlog.get_logger(__name__)

#: The risk levels from low to high.
_RISK_ORDER: Final[tuple[str, ...]] = ("safe", "caution", "dangerous", "destructive")

#: The approval rules that let a person approve a ``dangerous`` change.
_HIGH_RISK_RULES: Final[frozenset[str]] = frozenset(
    {"sa_operator_or_admin", "sa_operator_self_approval"}
)


@dataclass(frozen=True)
class ApprovedRun:
    """What the rights check reads about the approved request that runs."""

    request_id: uuid.UUID
    tenant_id: uuid.UUID
    request_audit_id: uuid.UUID | None
    op_id: str
    #: The risk level stamped on the request when it was made.
    approved_level: str | None


@dataclass(frozen=True)
class RightsRefusal:
    """Why an approved run may not run now.

    *new_approval* is ``True`` when a new call would wait for an approval
    that this one does not cover, and ``False`` when a new call would be
    refused.
    """

    reason: str
    new_approval: bool


_approved_run_var: ContextVar[ApprovedRun | None] = ContextVar("approved_run", default=None)


def _approved_run_of(request: ApprovalRequest) -> ApprovedRun:
    """The fields of *request* the rights check reads."""
    level = (request.proposed_effect or {}).get("safety_level")
    return ApprovedRun(
        request_id=request.id,
        tenant_id=request.tenant_id,
        request_audit_id=request.request_audit_id,
        op_id=request.op_id,
        approved_level=level if isinstance(level, str) else None,
    )


@contextmanager
def bind_approved_run(request: ApprovalRequest) -> Iterator[None]:
    """Bind *request* while its approved call runs, for :func:`approved_run_refusal`."""
    token = _approved_run_var.set(_approved_run_of(request))
    try:
        yield
    finally:
        _approved_run_var.reset(token)


def _rank(level: str | None) -> int | None:
    return _RISK_ORDER.index(level) if level in _RISK_ORDER else None


async def _approver_may_approve_high_risk(run: ApprovedRun) -> bool:
    """True when the person who approved *run* may approve a ``dangerous`` call.

    Read off the decision audit row of *run*. A row that does not say so
    counts as ``True`` only when its rule already was a high-risk rule.
    """
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import AuditLog

    stmt = select(AuditLog.payload).where(
        AuditLog.tenant_id == run.tenant_id,
        AuditLog.path == "approval.decision",
        AuditLog.payload["approval_request_id"].as_string() == str(run.request_id),
    )
    if run.request_audit_id is not None:
        stmt = stmt.where(AuditLog.parent_audit_id == run.request_audit_id)
    async with get_sessionmaker()() as session:
        for payload in (await session.execute(stmt)).scalars():
            if isinstance(payload, dict) and payload.get("decision") == "approved":
                if payload.get("approver_may_approve_high_risk") is True:
                    return True
                return payload.get("approval_rule") in _HIGH_RISK_RULES
    return False


async def _approval_still_covers(run: ApprovedRun, descriptor: EndpointDescriptor) -> str | None:
    """Why the approval of *run* does not cover a new wait now, or ``None``.

    Only the call the person approved is compared. A multi-step operation
    that runs again for one of its approved steps keeps the rules of its
    steps.
    """
    if descriptor.op_id != run.op_id:
        return None
    approved, now = _rank(run.approved_level), _rank(descriptor.safety_level)
    if approved is None or now is None or now <= approved:
        return None
    level = descriptor.safety_level
    if level == "destructive":
        return f"the risk level of {run.op_id!r} is now {level!r}, higher than the approved one"
    if level == "dangerous" and not await _approver_may_approve_high_risk(run):
        return (
            f"the risk level of {run.op_id!r} is now {level!r}, and the person who "
            "approved it may not approve that level"
        )
    return None


def _who_to_check(operator: Operator, *, requester_runs: bool) -> tuple[Operator, Any] | None:
    """The login whose rights count, and the context to bind; ``None`` = nobody to check."""
    if requester_runs:
        return operator, nullcontext()
    requester = approved_agent_requester()
    if requester is None:
        return None
    principal, act = requester
    if principal is None:
        return None
    return principal, structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: act})


async def approved_run_refusal(
    *,
    operator: Operator,
    requester_runs: bool,
    descriptor: EndpointDescriptor,
    connector_id: str,
    target: Any,
    params: dict[str, Any],
    preview_hash: str | None,
) -> RightsRefusal | None:
    """Why the approved call may not run now for who asked, or ``None``.

    *requester_runs* is ``True`` when *operator* is the login that asked.
    Only the top-level call is checked; a step of a multi-step operation
    keeps its own checks. The check only decides: it records no grant use.
    """
    from meho_backplane.db.models import PermissionVerdict
    from meho_backplane.operations._validate import policy_gate

    if composite_depth_var.get() != COMPOSITE_DEPTH_TOP_LEVEL:
        return None
    who = _who_to_check(operator, requester_runs=requester_runs)
    if who is None:
        return None
    principal, binding = who
    with binding:
        verdict, reason = await policy_gate(
            operator=principal,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            preview_hash=preview_hash,
            params=params,
            decide_only=True,
        )
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        return None
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        run = _approved_run_var.get()
        why = None if run is None else await _approval_still_covers(run, descriptor)
        if why is None:
            return None
        _log.info("approved_run_needs_new_approval", op_id=descriptor.op_id, reason=why)
        return RightsRefusal(f"a new approval is needed: {why}", new_approval=True)
    _log.info(
        "approved_run_rights_refused",
        op_id=descriptor.op_id,
        requester_sub=principal.sub,
        operator_sub=operator.sub,
    )
    return RightsRefusal(
        f"{reason or 'policy denied'} (an approved call checks the rights of who asked "
        "again when it runs, and a new call would be refused now)",
        new_approval=False,
    )
