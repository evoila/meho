# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``own_approval`` part of ``meho policy explain``.

May the person who asked approve the request that their call makes? The
answer is the one of the approve step: :func:`own_approval_view` builds
that request in memory, as the park would store it, and asks
:func:`~meho_backplane.operations.approval_rights.approve_right_any_status`
about it. That is the check the approve sign and the approve itself run,
in the same order: a machine login never approves, a change of rights only
an admin in the web console, an own change by the own-approval rule
(:func:`~meho_backplane.operations.approval_queue._check_self_approval`),
the role floor, and the routing. Nothing is stored.

The rule of the part:

* Where the approve step reads a fact that only the call has, ``web`` and
  ``token`` are ``None`` and both reasons are :data:`DEPENDS_ON_VALUES`.
  Such facts are the values of the call, and the facts of the approval
  card that MEHO reads from the values or from the target (for example
  the login-realm fact). These are the operations registered with
  :func:`~meho_backplane.operations.grant_approval.register_rights_change_check`,
  :func:`~meho_backplane.operations.grant_approval.register_grant_op` or
  :func:`~meho_backplane.operations.role_floors.register_requester_check`;
  :func:`~meho_backplane.operations.grant_approval.approve_reads_call_facts`
  decides it.
* For every other operation, ``web`` and ``token`` are ``True`` or
  ``False``, as the approve step decides for that caller, and a refusal
  has the approve step's reason.

The part answers for the person and the request. The approve still checks
at the click what can change until then: the sign-in, the deadline and the
preview binding.

It asks twice: once as a click in the web console, once with a token (the
command line or REST). The request says it waits only because it came with
a token when the call's own check says so
(:func:`~meho_backplane.operations._validate.waits_only_for_token_call`),
the same check the park uses for ``waits_because``.

Only with ``CUSTOM_POLICY`` on, and only for a call that waits.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus, EndpointDescriptor
from meho_backplane.operations._validate import TOKEN_CALL_WAIT, waits_only_for_token_call
from meho_backplane.operations.approval_rights import approve_right_any_status
from meho_backplane.operations.grant_approval import (
    ASKED_IN_WEB_CONSOLE,
    approve_reads_call_facts,
    is_rights_change,
)
from meho_backplane.policy.explain_models import ExplainOwnApproval
from meho_backplane.policy.routing import RouteEntry, routing_enabled
from meho_backplane.policy.routing_floor import InstallFloor
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.policy.who_approves import Approvers
from meho_backplane.settings import get_settings

__all__ = ["DEPENDS_ON_VALUES", "own_approval_view", "request_of_call"]

#: Both reasons when the approve step reads a fact that only the call has.
DEPENDS_ON_VALUES = "This depends on the values of the call. Explain asks without them."


def request_of_call(
    asker: Operator,
    descriptor: EndpointDescriptor,
    connector_id: str,
    approvers: Approvers,
) -> ApprovalRequest:
    """The request that *asker*'s call of *descriptor* would store, built in memory.

    The fields the approve checks read, set as the park sets them
    (:func:`~meho_backplane.operations.approval_queue.create_pending_request`).
    The call has no parameters, as every explain question. *approvers* are
    the groups the routing gives the call; the park stores them only while
    routing is on, and never for a change of rights. Call it with the
    call's actor bound, as the call has it.
    """
    effect: dict[str, object] = {"safety_level": descriptor.safety_level}
    if waits_only_for_token_call(asker, descriptor):
        effect["waits_because"] = TOKEN_CALL_WAIT
    request = ApprovalRequest(
        tenant_id=asker.tenant_id,
        principal_sub=asker.sub,
        principal_kind=asker.principal_kind.value,
        op_id=descriptor.op_id,
        connector_id=connector_id,
        params={},
        proposed_effect=effect,
        status=ApprovalRequestStatus.PENDING.value,
        rights_change=False,
    )
    if is_rights_change(request):
        request.rights_change = True
        web = asker.web_session and is_human_principal(asker)
        request.proposed_effect = {**effect, ASKED_IN_WEB_CONSOLE: web}
    elif routing_enabled():
        request.approver_roles = [group.removeprefix("MEHO-") for group in approvers.groups]
        request.approver_rule = approvers.rule
        request.approver_category = descriptor.policy_category
        request.approver_install_category = descriptor.policy_install_category
    return request


def _install_floor(routes: Sequence[RouteEntry] | None) -> InstallFloor | None:
    """The install's routing the approve would check: the install entries of *routes*."""
    if routes is None:
        return None
    return InstallFloor(entries=tuple(entry for entry in routes if entry.install))


def own_approval_view(
    *,
    asker: Operator,
    me: bool,
    descriptor: EndpointDescriptor,
    connector_id: str,
    approvers: Approvers,
    routes: Sequence[RouteEntry] | None,
) -> ExplainOwnApproval | None:
    """Whether *asker* may approve the request of their own call; ``None`` with the switch off.

    Call it only for a call that waits. *routes* is the routing explain
    read (the stored one, or a file's), or ``None`` while routing is off.
    A question with groups unbinds the hosted agent of the caller's run,
    as for the rest of the answer (*me* is ``False``).
    """
    if not custom_policy_on(get_settings()):
        return None
    if approve_reads_call_facts(descriptor.op_id):
        reason = (DEPENDS_ON_VALUES, DEPENDS_ON_VALUES)
        return ExplainOwnApproval.of(web=None, web_reason=reason, token=None, token_reason=reason)
    actor = {} if me else {ACTOR_SUB_KEY: None}
    with structlog.contextvars.bound_contextvars(**actor):
        request = request_of_call(asker, descriptor, connector_id, approvers)
    floor = _install_floor(routes)
    answers: dict[str, tuple[bool, tuple[str, str]]] = {}
    for way, web in (("web", True), ("token", False)):
        login = asker.model_copy(update={"web_session": web})
        right = approve_right_any_status(login, request, floor=floor)
        hidden = approve_right_any_status(login, request, floor=floor, place=False)
        answers[way] = (right.may_approve, (right.message, hidden.message))
    return ExplainOwnApproval.of(
        web=answers["web"][0],
        web_reason=answers["web"][1],
        token=answers["token"][0],
        token_reason=answers["token"][1],
    )
