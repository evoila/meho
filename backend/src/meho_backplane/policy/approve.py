# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval side of the policy evaluator: may this person approve this request?

:func:`may_approve` answers with today's rules, in the order of
:func:`~meho_backplane.operations.approval_rights.approve_right`:

1. **Floor:** only a person approves. Agents, services and runners never do.
2. A ``read_only`` person without the approver flag cannot decide.
3. Only a pending request can be approved.
4. **Floor:** only an admin approves a change of rights, and only with a
   click in the web interface. The admin may be the person who asked. A
   change of a login's or a group's rights also follows the rules on who
   the change is for (``grant_refusal``).
5. An own change: a MEHO-Admin may approve it; a
   person may approve their own ``caution`` change that waited only
   because it came with a token, with a click in the web interface; and
   the break-glass switch allows an own ``safe`` or ``caution`` change.
6. Another person's ``dangerous`` or ``destructive`` change: only a
   MEHO-Admin.
7. The role floor of the operation.

It does not check what the approve checks at the moment of the click: the
sign-in age, the deadline, the params and the preview binding.

The function is pure. While the ``POLICY_ENGINE_SHADOW`` switch is on it
runs next to today's checks and decides nothing
(:mod:`meho_backplane.policy.shadow`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meho_backplane.auth.operator import TenantRole
from meho_backplane.operations.role_floors import role_floor_denial
from meho_backplane.policy.evaluate import TOKEN_CALL_WAIT, Asker, RuleRef, RuleSource

__all__ = ["ApprovalAsk", "may_approve"]

_HIGH_RISK: Final[frozenset[str]] = frozenset({"dangerous", "destructive"})


@dataclass(frozen=True)
class ApprovalAsk:
    """The facts of one approval request that the answer depends on.

    ``risk`` and ``waits_because`` are what the dispatcher stamped on the
    request. ``rights_change`` is the rights-change mark. ``grant_refusal``
    is the refusal text of today's check of who a login or group change is
    for, or ``None``. ``break_glass`` is the ``APPROVAL_ALLOW_SELF_APPROVAL``
    setting.
    """

    requester_sub: str
    op_id: str
    risk: str | None
    pending: bool = True
    waits_because: str | None = None
    rights_change: bool = False
    grant_refusal: str | None = None
    break_glass: bool = False


def _no(rule_id: str, text: str, *, source: RuleSource = "default") -> tuple[bool, RuleRef]:
    return False, RuleRef(source=source, rule_id=rule_id, text=text)


def _yes(rule_id: str, text: str) -> tuple[bool, RuleRef]:
    return True, RuleRef(source="default", rule_id=rule_id, text=text)


def _may_self_approve(asker: Asker) -> bool:
    in_group = "sa_operator" in asker.roles or asker.tenant_role is TenantRole.TENANT_ADMIN
    return in_group and asker.tenant_chosen


def _rights_change_refusal(asker: Asker) -> tuple[bool, RuleRef] | None:
    is_admin = asker.tenant_role is TenantRole.TENANT_ADMIN or "platform_admin" in asker.roles
    if not (is_admin and asker.tenant_chosen):
        return _no("rights_change", "only an admin approves a change of rights", source="floor")
    if asker.via != "web":
        return _no(
            "rights_change",
            "approve this change of rights in the web console",
            source="floor",
        )
    return None


def _own_change(asker: Asker, ask: ApprovalAsk) -> tuple[bool, RuleRef]:
    if _may_self_approve(asker):
        return _yes("sa_operator_self_approval", "a MEHO-Admin")
    token_wait = ask.waits_because == TOKEN_CALL_WAIT and ask.risk == "caution"
    if token_wait and asker.via == "web":
        return _yes("token_call_self_approval", "own caution change that waited for its token")
    if ask.risk not in _HIGH_RISK and ask.break_glass:
        return _yes("break_glass_self_approval", "APPROVAL_ALLOW_SELF_APPROVAL is on")
    return _no("own_change", "a person may not approve their own change")


def _group_rule(asker: Asker, ask: ApprovalAsk) -> tuple[bool, RuleRef]:
    if ask.rights_change and (refusal := _rights_change_refusal(asker)) is not None:
        return refusal
    if ask.grant_refusal is not None:
        return _no("grant_change", ask.grant_refusal, source="floor")
    if ask.rights_change and asker.sub == ask.requester_sub:
        return _yes("sa_operator_self_approval", "an admin's own change of rights, in the web")
    if asker.sub == ask.requester_sub:
        return _own_change(asker, ask)
    if ask.risk not in _HIGH_RISK:
        return _yes("reviewer", "another person's lower-risk change")
    if _may_self_approve(asker) or asker.tenant_role is TenantRole.TENANT_ADMIN:
        return _yes("sa_operator_or_admin", "a MEHO-Admin")
    return _no("wrong_group", "only a MEHO-Admin approves this risk level")


def may_approve(asker: Asker, ask: ApprovalAsk) -> tuple[bool, RuleRef]:
    """Return whether *asker* may approve the request *ask*, and the rule that decided."""
    if asker.kind != "person":
        return _no(
            "only_a_person_approves", "agents, services and runners never approve", source="floor"
        )
    if asker.tenant_role is TenantRole.READ_ONLY and "approver" not in asker.roles:
        return _no("no_approval_access", "read_only without the approver flag")
    if not ask.pending:
        return _no("not_pending", "only a pending request can be approved")
    allowed, rule = _group_rule(asker, ask)
    if not allowed:
        return allowed, rule
    denial = role_floor_denial(ask.op_id, asker.tenant_role)
    if denial is not None:
        return _no("role_floor", denial, source="role_floor")
    return allowed, rule
