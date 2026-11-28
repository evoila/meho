# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An approved request is checked again when it runs.

A person approved one exact call. MEHO does not ask for the approval again
when the call runs, but it checks again what can change while the call
waits. The first run after the approval (on an approvals surface, or in the
agent's own wait) and every re-run check:

* **The risk level**, for every requester. MEHO compares the level stamped
  on the approval card with the level of the operation now. The run is
  refused when the level went up to ``destructive``, or went up to
  ``dangerous`` and the person who approved may not approve that level. A
  card with no stamped level is refused when the operation is
  ``destructive`` now.
* **The grants of an agent that asked**, and of the acting agent of a
  hosted agent run: a ``deny`` grant, or no grant while
  ``AGENT_GRANT_REQUIRED`` is ``on``, refuses the run.
* **A revoked agent login** (the caller, or the acting agent of a hosted
  agent run) and a blocked login refuse the run.

A re-run also checks the caller's role and grants from the caller's token
now, as for a new call. The approver is read as recorded at the decision.

Who is checked
==============

* **The caller, when it is the one that asked**: the agent's own wait, a
  hosted agent run, and the requester's re-run. A re-run of a request made
  inside a hosted agent run binds that acting agent again.
* **The agent that asked, when a person runs the call from an approvals
  surface** (console, CLI, REST ``/approve`` and ``/decide``). MEHO builds
  the agent again from the record on the approval card
  (:func:`~meho_backplane.operations.approved_step_check.requester_record`).
  An agent's request whose card has no readable record of the agent is
  refused when a person runs it from an approvals surface. The agent must
  ask again with a new request.

A refused request stays approved and can run again later. On an approvals
surface and in the agent's wait, the run ends as a failed run with the
reason. The approved step of a multi-step operation gets the risk level
and grant checks before it clears (:func:`approved_step_refusal_now`). A
step cannot run again on its own, so its answer says to start a new run
of the operation. The re-run checks the same rules before it takes the run
(:mod:`~meho_backplane.operations.approval_rerun`), so a refused re-run
does not change the request.
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
from meho_backplane.operations.composite import (
    COMPOSITE_DEPTH_TOP_LEVEL,
    composite_depth_var,
    composite_dispatch_var,
)
from meho_backplane.operations.deny_reasons import POLICY_DENIED
from meho_backplane.operations.secondary_target_policy import approved_agent_requester

if TYPE_CHECKING:
    from collections.abc import Sequence

    from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor

__all__ = [
    "ApprovedRun",
    "RightsRefusal",
    "approved_run_refusal",
    "approved_step_refusal_now",
    "bind_approved_run",
    "run_approver_may_approve",
]

_log = structlog.get_logger(__name__)

#: The risk levels from low to high.
_RISK_ORDER: Final[tuple[str, ...]] = ("safe", "caution", "dangerous", "destructive")

#: The approval rules that let a person approve a ``dangerous`` change.
_HIGH_RISK_RULES: Final[frozenset[str]] = frozenset(
    {"sa_operator_or_admin", "sa_operator_self_approval"}
)

#: The key of the decision audit row that says whether the approver may
#: approve a ``dangerous`` change.
_HIGH_RISK_KEY: Final[str] = "approver_may_approve_high_risk"


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

    *reason* is the short cause, and *text* the full answer of a refused
    first run. *new_approval* is ``True`` when the approval does not cover
    the risk level now, and ``False`` when a new call would be refused.
    """

    reason: str
    text: str
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

    Read off the decision audit row of *run*. When the row has the key
    ``approver_may_approve_high_risk``, its value decides. A row without the
    key counts as ``True`` only when its rule is a high-risk rule.
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
                if _HIGH_RISK_KEY in payload:
                    return payload[_HIGH_RISK_KEY] is True
                return payload.get("approval_rule") in _HIGH_RISK_RULES
    return False


async def run_approver_may_approve(level: str) -> bool:
    """True when the person who approved the run that goes on now may approve *level*.

    ``safe`` and ``caution`` need no more than an approver. A higher level
    needs a person who may approve another person's ``dangerous`` change,
    read off the decision of the bound approval
    (:func:`_approver_may_approve_high_risk`). With no bound approval the
    answer is ``False`` for such a level.
    """
    if level in _RISK_ORDER[:2]:
        return True
    run = _approved_run_var.get()
    return run is not None and await _approver_may_approve_high_risk(run)


async def _risk_refusal(run: ApprovedRun | None, descriptor: EndpointDescriptor) -> str | None:
    """Why the approval of *run* does not cover the risk level now, or ``None``.

    Only the call the person approved is compared, for every requester.
    """
    if run is None or descriptor.op_id != run.op_id:
        return None
    level = descriptor.safety_level
    approved, now = _rank(run.approved_level), _rank(level)
    if now is None:
        return None
    if approved is None:
        if level != "destructive":
            return None
        return f"the risk level of {run.op_id!r} is now {level!r}, and the request has none stamped"
    if now <= approved:
        return None
    if level == "destructive":
        return f"the risk level of {run.op_id!r} is now {level!r}, higher than the approved one"
    if level == "dangerous" and not await _approver_may_approve_high_risk(run):
        return (
            f"the risk level of {run.op_id!r} is now {level!r}, and the person who "
            "approved it may not approve that level"
        )
    return None


def _no_record_refusal(op_id: str) -> str:
    return (
        f"the grants of the agent that asked for {op_id!r} cannot be checked, because the "
        "approval request has no record of the agent"
    )


def _who_to_check(
    operator: Operator, op_id: str, *, requester_runs: bool
) -> tuple[Operator, Any] | str | None:
    """The login whose rights count and the context to bind.

    ``None`` when there is no agent to check; a refusal text when an agent
    asked but its card has no readable record.
    """
    if requester_runs:
        return operator, nullcontext()
    requester = approved_agent_requester()
    if requester is None:
        return None
    principal, act = requester
    if principal is None:
        return _no_record_refusal(op_id)
    return principal, structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: act})


def _refusal_text(
    run: ApprovedRun | None, why: str, *, new_approval: bool, no_record: bool = False
) -> str:
    """The answer to a refused approved run: the reason, and what to do next."""
    head = "the approved call" if run is None else f"approval request {run.request_id}"
    if no_record:
        return f"{head} did not run: {why}. The agent must ask again with a new request."
    if new_approval:
        return (
            f"{head} did not run: {why}, so this approval does not cover it. "
            "The request stays approved. Ask again with a new request."
        )
    return (
        f"{head} did not run: a new call of the login that asked would be refused now "
        f"({why}). The request stays approved. Run it again when the rights allow it, "
        "or ask again with a new request."
    )


def _refusal(
    run: ApprovedRun | None, why: str, *, new_approval: bool, no_record: bool = False
) -> RightsRefusal:
    text = _refusal_text(run, why, new_approval=new_approval, no_record=no_record)
    return RightsRefusal(why, text, new_approval)


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
    """Why the approved call may not run now, or ``None``.

    *requester_runs* is ``True`` when *operator* is the login that asked.
    Only the top-level call is checked; a step of a multi-step operation
    keeps its own checks (:func:`approved_step_refusal_now`). The check
    only decides: it records no grant use.
    """
    from meho_backplane.db.models import PermissionVerdict
    from meho_backplane.operations._validate import policy_gate

    if composite_depth_var.get() != COMPOSITE_DEPTH_TOP_LEVEL:
        return None
    run = _approved_run_var.get()
    who = _who_to_check(operator, descriptor.op_id, requester_runs=requester_runs)
    if isinstance(who, str):
        return _refusal(run, who, new_approval=True, no_record=True)
    if who is not None:
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
        if verdict is PermissionVerdict.DENY:
            _log.info(
                "approved_run_rights_refused",
                op_id=descriptor.op_id,
                requester_sub=principal.sub,
                operator_sub=operator.sub,
            )
            why = reason or "policy denied"
            return _refusal(run, why, new_approval=False)
    risk = await _risk_refusal(run, descriptor)
    if risk is None:
        return None
    _log.info("approved_run_needs_new_approval", op_id=descriptor.op_id, reason=risk)
    return _refusal(run, risk, new_approval=True)


def _step_refusal_text(reason: str) -> str:
    """The answer to a refused approved step: a step cannot run again on its own."""
    parent = composite_dispatch_var.get()
    operation = "the multi-step operation" if parent is None else repr(parent[0])
    return (
        f"{reason} (the approved step did not run. A step of a multi-step operation "
        f"cannot run again on its own: start a new run of {operation}.)"
    )


async def approved_step_refusal_now(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    connector_id: str,
    target: Any,
    level_rows: Sequence[EndpointDescriptor] = (),
) -> str | None:
    """Why the approved step of a multi-step operation may not run now, or ``None``.

    Read when MEHO runs the parent of an approved step again, before the
    step clears. *descriptor* is the step with its level now (after the
    override rows and the classification). Two checks, as for a single call:

    * **The risk level**, for every requester: the level stamped on the
      step's approval card is compared with the step's level now
      (:func:`_risk_refusal`). The level of each of *level_rows* counts
      too: with ``CUSTOM_POLICY`` on, the step gate passes the rows of
      :func:`~meho_backplane.operations.run_step_rules.step_rule_rows`,
      which carry the stored level of the step's operation.
    * **The grants of the agent that asked**, and of the acting agent of a
      hosted run: a ``deny`` grant, or no grant while
      ``AGENT_GRANT_REQUIRED`` is ``on``, refuses the step.
    """
    from meho_backplane.db.models import PermissionVerdict
    from meho_backplane.operations._preview_steps import agent_acts
    from meho_backplane.operations._validate import policy_gate

    run = _approved_run_var.get()
    for row in (descriptor, *level_rows):
        risk = await _risk_refusal(run, row)
        if risk is not None:
            _log.info("approved_step_needs_new_approval", op_id=descriptor.op_id, reason=risk)
            return _step_refusal_text(f"{POLICY_DENIED}: {risk}")
    who: tuple[Operator, Any] | str | None
    if agent_acts(operator):
        who = operator, nullcontext()
    else:
        who = _who_to_check(operator, descriptor.op_id, requester_runs=False)
    if who is None:
        return None
    if isinstance(who, str):
        return _step_refusal_text(f"{POLICY_DENIED}: {who}")
    principal, binding = who
    with binding:
        verdict, reason = await policy_gate(
            operator=principal,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            decide_only=True,
        )
    if verdict is not PermissionVerdict.DENY:
        return None
    _log.info("approved_step_grant_refused", op_id=descriptor.op_id, requester_sub=principal.sub)
    return _step_refusal_text(reason or POLICY_DENIED)
