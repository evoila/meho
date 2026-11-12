# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shadow mode: the policy evaluator runs next to today's checks and decides nothing.

With the ``POLICY_ENGINE_SHADOW`` switch on:

* :func:`shadow_call` runs after today's policy gate
  (:func:`~meho_backplane.operations._validate.policy_gate`). It builds
  the :class:`~meho_backplane.policy.evaluate.Asker` and
  :class:`~meho_backplane.policy.evaluate.Asked` of the call, does the
  lookups the evaluator needs (read only: it uses no grant and writes
  nothing), and compares the two verdicts.
* :func:`shadow_approval` runs after today's "may this person approve
  this request?" check and compares the two answers.

On a difference it writes one ``policy_shadow_mismatch`` log line with
both answers and adds one to the ``policy_shadow_mismatch_total`` metric.
Today's answer is always the one that counts. A failure inside the shadow
is logged as ``policy_shadow_failed`` and never reaches the caller.

With the switch off, nothing here runs.
"""

from __future__ import annotations

from typing import Any

import structlog
from prometheus_client import Counter

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import Operator, PrincipalKind, is_human_principal
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, PermissionVerdict
from meho_backplane.policy.approve import ApprovalAsk, may_approve
from meho_backplane.policy.evaluate import (
    Asked,
    Asker,
    AskerKind,
    Decision,
    Lookups,
    Verdict,
    evaluate,
)
from meho_backplane.settings import get_settings

__all__ = [
    "POLICY_SHADOW_MISMATCH_TOTAL",
    "approval_ask_of",
    "asked_of",
    "asker_of",
    "evaluate_call",
    "shadow_approval",
    "shadow_call",
    "shadow_enabled",
]

_log = structlog.get_logger(__name__)

#: Differences between the evaluator and today's checks, by kind
#: (``call`` or ``approval``). Zero is the goal before any switch turns on.
POLICY_SHADOW_MISMATCH_TOTAL: Counter = Counter(
    "policy_shadow_mismatch_total",
    "Differences between the policy evaluator in shadow mode and today's checks.",
    ["kind"],
)

_KIND: dict[PrincipalKind, AskerKind] = {
    PrincipalKind.USER: "person",
    PrincipalKind.AGENT: "agent",
    PrincipalKind.SERVICE: "service",
    PrincipalKind.RUNNER: "runner",
}

_FROM_TODAY: dict[PermissionVerdict, Verdict] = {
    PermissionVerdict.AUTO_EXECUTE: Verdict.RUN,
    PermissionVerdict.NEEDS_APPROVAL: Verdict.WAIT,
    PermissionVerdict.DENY: Verdict.DENY,
}


def shadow_enabled() -> bool:
    """True when the ``POLICY_ENGINE_SHADOW`` switch is on."""
    return get_settings().policy_engine_shadow


def asker_of(operator: Operator, *, acting_agent: str | None = None) -> Asker:
    """The :class:`Asker` of *operator*; *acting_agent* is the agent of a hosted run."""
    roles = {operator.tenant_role.value}
    if operator.approver:
        roles.add("approver")
    if operator.self_approver:
        roles.add("sa_operator")
    if operator.platform_admin:
        roles.add("platform_admin")
    web = operator.web_session and is_human_principal(operator)
    return Asker(
        kind=_KIND.get(operator.principal_kind, "runner"),
        sub=operator.sub,
        tenant_id=operator.tenant_id,
        roles=frozenset(roles),
        via="web" if web else "token",
        acting_agent=acting_agent,
        tenant_chosen=operator.tenant_chosen,
    )


def _op_class(descriptor: EndpointDescriptor) -> frozenset[str]:
    from meho_backplane.op_classes import is_root_shell, needs_per_call_approval
    from meho_backplane.operations.service_grants import (
        credential_read_grant_reason,
        delete_grant_reason,
    )

    op_id = descriptor.op_id
    patterns = get_settings().service_grant_delete_shaped_patterns
    marks = {
        "root_shell": is_root_shell(op_id),
        "per_call_approval": needs_per_call_approval(op_id),
        "credential_read": credential_read_grant_reason(op_id) is not None,
        "delete_shaped": delete_grant_reason(op_id, patterns, descriptor) is not None,
        "classified_always_wait": descriptor.policy_always_wait,
    }
    return frozenset(name for name, on in marks.items() if on)


def asked_of(descriptor: EndpointDescriptor, *, connector_id: str | None, target: Any) -> Asked:
    """The :class:`Asked` of one call of *descriptor*.

    Category and labels stay empty: no customer rule reads them in part 1.
    """
    from meho_backplane.operations._validate import _is_mutating
    from meho_backplane.operations.caller_address import has_caller_address

    target_id = getattr(target, "id", None) if target is not None else None
    return Asked(
        connector_id=connector_id,
        op_id=descriptor.op_id,
        risk=descriptor.safety_level,
        always_wait=bool(descriptor.requires_approval),
        mutating=_is_mutating(descriptor),
        caller_address=has_caller_address(descriptor.parameter_schema),
        op_class=_op_class(descriptor),
        target=str(target_id) if target_id is not None else None,
    )


async def _agent_grant(operator: Operator, op_id: str, target: Any) -> tuple[Verdict, str] | None:
    from meho_backplane.auth.permissions import matched_grant

    target_id = getattr(target, "id", target) if target is not None else None
    async with get_sessionmaker()() as session:
        found = await matched_grant(
            session=session, operator=operator, op_id=op_id, target_id=target_id
        )
    if found is None:
        return None
    verdict, source = found
    return _FROM_TODAY[verdict], source


async def _service_lookups(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
    preview_hash: str | None,
    decide_only: bool,
) -> dict[str, Any]:
    """Read scope, standing grant and run approval of a service login. Writes nothing."""
    from meho_backplane.operations.run_approval_gate import consult_and_record_run_approval
    from meho_backplane.operations.service_grants import consult_and_record_grant
    from meho_backplane.operations.service_read_scopes import read_scope_denial

    found: dict[str, Any] = {
        "read_scope_denial": await read_scope_denial(operator=operator, target=target)
    }
    if connector_id is None:
        return found
    # A real destructive call uses a delete grant only with the hash of its
    # preview; without one, today's gate never uses the grant.
    if decide_only or descriptor.safety_level != "destructive" or preview_hash is not None:
        grant_id = await consult_and_record_grant(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            preview_hash=preview_hash,
            record=False,
        )
        found["standing_grant"] = str(grant_id) if grant_id is not None else None
    run_id = await consult_and_record_run_approval(
        operator=operator,
        descriptor=descriptor,
        target=target,
        connector_id=connector_id,
        record=False,
    )
    found["run_approval"] = str(run_id) if run_id is not None else None
    return found


async def evaluate_call(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
    preview_hash: str | None = None,
    decide_only: bool = False,
) -> Decision:
    """Do the lookups for one call and return the evaluator's decision. Writes nothing."""
    from meho_backplane.sandbox_mode import sandbox_mode_active

    actor_sub = resolve_actor_sub()
    asker = asker_of(operator, acting_agent=actor_sub)
    asked = asked_of(descriptor, connector_id=connector_id, target=target)
    found: dict[str, Any] = {
        "sandbox": sandbox_mode_active(),
        "agent_grant_required": get_settings().agent_grant_required,
    }
    if asker.kind == "agent":
        found["agent_grant"] = await _agent_grant(operator, descriptor.op_id, target)
    if actor_sub is not None:
        agent = operator.model_copy(
            update={"sub": actor_sub, "principal_kind": PrincipalKind.AGENT, "client_id": actor_sub}
        )
        found["acting_agent_grant"] = await _agent_grant(agent, descriptor.op_id, target)
    if asker.kind == "service":
        found.update(
            await _service_lookups(
                operator=operator,
                descriptor=descriptor,
                target=target,
                connector_id=connector_id,
                preview_hash=preview_hash,
                decide_only=decide_only,
            )
        )
    return evaluate(asker, asked, Lookups(**found))


async def shadow_call(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    target: Any,
    connector_id: str | None,
    today: tuple[PermissionVerdict, str | None],
    preview_hash: str | None = None,
    decide_only: bool = False,
) -> None:
    """Compare the evaluator with today's verdict *today* for one call. Never raises.

    Does nothing while the ``POLICY_ENGINE_SHADOW`` switch is off.
    """
    if not shadow_enabled():
        return
    try:
        decision = await evaluate_call(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=connector_id,
            preview_hash=preview_hash,
            decide_only=decide_only,
        )
        today_verdict, today_reason = today
        if decision.verdict is _FROM_TODAY[today_verdict]:
            return
        POLICY_SHADOW_MISMATCH_TOTAL.labels(kind="call").inc()
        _log.warning(
            "policy_shadow_mismatch",
            kind="call",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            principal_kind=operator.principal_kind.value,
            connector_id=connector_id,
            op_id=descriptor.op_id,
            safety_level=descriptor.safety_level,
            decide_only=decide_only,
            today_verdict=today_verdict.value,
            today_reason=today_reason,
            shadow_verdict=decision.verdict.value,
            shadow_rule_source=decision.rule.source,
            shadow_rule_id=decision.rule.rule_id,
            shadow_rule_text=decision.rule.text,
            shadow_trace=list(decision.trace),
        )
    except Exception:
        _log.exception("policy_shadow_failed", kind="call", op_id=descriptor.op_id)


def approval_ask_of(operator: Operator, request: ApprovalRequest) -> ApprovalAsk:
    """The :class:`ApprovalAsk` of *request*, as *operator* would see it."""
    from meho_backplane.db.models import ApprovalRequestStatus
    from meho_backplane.operations.approval_queue import ApprovalRoleFloorError
    from meho_backplane.operations.grant_approval import check_grant_approval, is_rights_change

    effect = request.proposed_effect or {}
    level = effect.get("safety_level")
    waits_because = effect.get("waits_because")
    grant_refusal: str | None = None
    try:
        check_grant_approval(operator, request)
    except ApprovalRoleFloorError as exc:
        grant_refusal = exc.reason_text
    return ApprovalAsk(
        requester_sub=request.principal_sub,
        op_id=request.op_id,
        risk=level if isinstance(level, str) else None,
        pending=request.status == ApprovalRequestStatus.PENDING.value,
        waits_because=waits_because if isinstance(waits_because, str) else None,
        rights_change=is_rights_change(request),
        grant_refusal=grant_refusal,
        break_glass=get_settings().approval_allow_self_approval,
    )


def shadow_approval(operator: Operator, request: ApprovalRequest, *, today: bool) -> None:
    """Compare the evaluator with today's answer *today* to "may approve?". Never raises.

    Does nothing while the ``POLICY_ENGINE_SHADOW`` switch is off.
    """
    if not shadow_enabled():
        return
    try:
        allowed, rule = may_approve(asker_of(operator), approval_ask_of(operator, request))
        if allowed == today:
            return
        POLICY_SHADOW_MISMATCH_TOTAL.labels(kind="approval").inc()
        _log.warning(
            "policy_shadow_mismatch",
            kind="approval",
            tenant_id=str(operator.tenant_id),
            operator_sub=operator.sub,
            principal_kind=operator.principal_kind.value,
            approval_request_id=str(request.id),
            op_id=request.op_id,
            today_may_approve=today,
            shadow_may_approve=allowed,
            shadow_rule_source=rule.source,
            shadow_rule_id=rule.rule_id,
            shadow_rule_text=rule.text,
        )
    except Exception:
        _log.exception("policy_shadow_failed", kind="approval", op_id=request.op_id)
