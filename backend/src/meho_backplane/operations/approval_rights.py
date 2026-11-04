# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""May this person approve this request? The answer the surfaces show.

The command line (``meho approvals list`` / ``show``, through the REST
fields ``may_approve`` and ``may_approve_reason``) and the web interface
(the sign and the Approve button in the request window) show, for each
request, whether the person may approve it and why not.

:func:`approve_right` runs the same checks as
:func:`~meho_backplane.operations.approval_queue.approve_request`, in the
same order, so the sign never says something else than the approve does:

1. A machine login never approves (:func:`_check_reviewer_role`).
2. A ``read_only`` person without the ``approver`` flag cannot decide.
3. Only a ``pending`` request can be approved.
4. The group rule (:func:`_check_approver_group`): an own change only for
   a MEHO-SA-Operator, or a ``safe`` / ``caution`` change under the
   break-glass switch; another person's ``dangerous`` or ``destructive``
   change only for a MEHO-SA-Operator or an admin.
5. The role floor of the operation (:func:`_check_approver_role_floor`).

The answer is about the person and the request. It does not check what
the approve checks at the moment of the click: the sign-in age, the
deadline, the params and the preview binding. Those can change between
the read and the click, and the approve still checks them.

The answer is a read. It writes nothing and logs no break-glass warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from meho_backplane.auth.operator import Operator
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    ApprovalRoleFloorError,
    ApprovalRule,
    NonHumanApprovalError,
    SelfApprovalForbiddenError,
    UnauthorizedApprovalError,
    _check_approver_group,
    _check_approver_role_floor,
    _check_reviewer_role,
)

__all__ = [
    "WEB_APPROVAL_HINT",
    "ApproveRight",
    "ApproveRightReason",
    "approve_right",
    "self_approval_confirmation",
]

#: Ends every approve refusal, on the command line and in the web interface.
WEB_APPROVAL_HINT = (
    "The web interface is the normal place to approve: "
    "it shows for each request whether you may approve it."
)


class ApproveRightReason(StrEnum):
    """Why a person may or may not approve a request.

    The value is sent as ``may_approve_reason`` on the REST views.

    * ``ok`` — another person's change; this person may approve it.
    * ``own_change`` — the person's own change. With ``may_approve`` true,
      a self-approval is allowed (a MEHO-SA-Operator, or the break-glass
      switch for a lower-risk change); with false, it is refused.
    * ``wrong_group`` — another person's ``dangerous`` or ``destructive``
      change, and the person is not in a group that may approve it.
    * ``role_too_low`` — the operation needs a higher role from the person
      who approves it.
    * ``no_approval_access`` — a ``read_only`` person without the
      ``approver`` flag.
    * ``not_a_person`` — an AI tool or another machine login.
    * ``not_pending`` — the request is already decided, expired or
      withdrawn.
    """

    OK = "ok"
    OWN_CHANGE = "own_change"
    WRONG_GROUP = "wrong_group"
    ROLE_TOO_LOW = "role_too_low"
    NO_APPROVAL_ACCESS = "no_approval_access"
    NOT_A_PERSON = "not_a_person"
    NOT_PENDING = "not_pending"


#: The rules that allow a person to approve their own change.
_SELF_RULES = frozenset(
    {ApprovalRule.SA_OPERATOR_SELF_APPROVAL, ApprovalRule.BREAK_GLASS_SELF_APPROVAL}
)


@dataclass(frozen=True)
class ApproveRight:
    """The answer of :func:`approve_right`.

    ``rule`` is the :class:`ApprovalRule` that would allow the approval,
    or ``None`` when the person may not approve. ``message`` is plain text
    for the web interface. For a refusal it is the text the approve shows
    when it refuses; for an own lower-risk change it also names the
    break-glass switch, as the REST refusal does.
    """

    may_approve: bool
    reason: ApproveRightReason
    rule: ApprovalRule | None
    message: str

    @property
    def self_approval(self) -> bool:
        """``True`` when the person may approve, and it is their own change."""
        return self.rule in _SELF_RULES


def _allowed(rule: ApprovalRule) -> ApproveRight:
    """Build the answer for a person the checks allow."""
    if rule == ApprovalRule.SA_OPERATOR_SELF_APPROVAL:
        message = (
            "This is your own change. You may approve it as a MEHO-SA-Operator. "
            "The audit log records it as a self-approval."
        )
    elif rule == ApprovalRule.BREAK_GLASS_SELF_APPROVAL:
        message = (
            "This is your own change. You may approve it because "
            "APPROVAL_ALLOW_SELF_APPROVAL is on. The audit log records it as a "
            "self-approval."
        )
    else:
        message = "You may approve this change."
    reason = ApproveRightReason.OWN_CHANGE if rule in _SELF_RULES else ApproveRightReason.OK
    return ApproveRight(may_approve=True, reason=reason, rule=rule, message=message)


def _refused(reason: ApproveRightReason, message: str) -> ApproveRight:
    """Build the answer for a person the checks refuse."""
    return ApproveRight(may_approve=False, reason=reason, rule=None, message=message)


def self_approval_confirmation(rule: ApprovalRule | None) -> str | None:
    """Return the confirmation line after a self-approval, else ``None``.

    *rule* is the :class:`ApprovalRule` that allowed the approval. The web
    interface shows this line after the click.
    """
    if rule == ApprovalRule.SA_OPERATOR_SELF_APPROVAL:
        return (
            "You approved your own change as a MEHO-SA-Operator. "
            "The audit log records it as a self-approval."
        )
    if rule == ApprovalRule.BREAK_GLASS_SELF_APPROVAL:
        return (
            "You approved your own change because APPROVAL_ALLOW_SELF_APPROVAL is on. "
            "The audit log records it as a self-approval."
        )
    return None


def approve_right(operator: Operator, request: ApprovalRequest) -> ApproveRight:
    """Return whether *operator* may approve *request*, and why not.

    Runs the checks of
    :func:`~meho_backplane.operations.approval_queue.approve_request` in
    the same order and turns each refusal into an answer. Raises nothing.
    """
    try:
        _check_reviewer_role(operator)
    except NonHumanApprovalError:
        return _refused(
            ApproveRightReason.NOT_A_PERSON,
            "Only a person can approve. AI tools and other machine logins never approve.",
        )
    except UnauthorizedApprovalError:
        return _refused(
            ApproveRightReason.NO_APPROVAL_ACCESS,
            "Your role cannot decide approval requests "
            "(operator role or the approver capability required).",
        )
    if request.status != ApprovalRequestStatus.PENDING.value:
        return _refused(
            ApproveRightReason.NOT_PENDING,
            f"This request is {request.status}. Only a pending request can be approved.",
        )
    try:
        rule = _check_approver_group(operator, request, log_break_glass=False)
        _check_approver_role_floor(operator, request)
    except SelfApprovalForbiddenError as exc:
        message = exc.reason_text
        if not exc.high_risk:
            # The break-glass switch can still allow a lower-risk own change.
            message += (
                " Or a deployment admin turns on APPROVAL_ALLOW_SELF_APPROVAL "
                "for an audited break-glass."
            )
        return _refused(ApproveRightReason.OWN_CHANGE, message)
    except ApprovalGroupForbiddenError as exc:
        return _refused(ApproveRightReason.WRONG_GROUP, exc.reason_text)
    except ApprovalRoleFloorError as exc:
        return _refused(ApproveRightReason.ROLE_TOO_LOW, exc.reason_text)
    return _allowed(rule)
