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
the role floor, and the routing. So the part cannot say something else
than the approve. Nothing is stored.

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
from meho_backplane.operations.grant_approval import ASKED_IN_WEB_CONSOLE, is_rights_change
from meho_backplane.policy.explain_models import ExplainOwnApproval
from meho_backplane.policy.routing import RouteEntry, routing_enabled
from meho_backplane.policy.routing_floor import InstallFloor
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.policy.who_approves import Approvers
from meho_backplane.settings import get_settings

__all__ = ["own_approval_view", "request_of_call"]


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
    actor = {} if me else {ACTOR_SUB_KEY: None}
    with structlog.contextvars.bound_contextvars(**actor):
        request = request_of_call(asker, descriptor, connector_id, approvers)
    floor = _install_floor(routes)
    web = approve_right_any_status(
        asker.model_copy(update={"web_session": True}), request, floor=floor
    )
    token = approve_right_any_status(
        asker.model_copy(update={"web_session": False}), request, floor=floor
    )
    return ExplainOwnApproval(
        web=web.may_approve,
        web_reason=web.message,
        token=token.may_approve,
        token_reason=token.message,
    )
