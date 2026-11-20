# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""An admin's change of rights in the web console runs at once.

A change of rights is an operation registered with
:func:`~meho_backplane.operations.grant_approval.register_rights_change_op`
or :func:`~meho_backplane.operations.grant_approval.register_grant_op`
(the Keycloak writes that change who has rights, ``policy.apply`` and
``policy.classify``). Such a call waits for approval, except in one case:
an admin (:meth:`~meho_backplane.auth.operator.Operator.may_approve_rights_change`)
makes it with a click in the web console
(:attr:`~meho_backplane.auth.operator.Operator.web_session`). Then it runs
at once (:func:`runs_at_once_in_console`), after the checks of who may make
it (:func:`console_refusal`).

The call still waits when:

* it is ``destructive``: such a call always waits for a person;
* it has the "always wait" mark (an override or a classification rule):
  such a call always waits for a person;
* it reads, writes or makes a credential
  (:func:`~meho_backplane.operations.service_grants.credential_call_waits`):
  such a call always waits for a person;
* its params carry a credential, such as a client secret or a password
  (:func:`params_write_credential`, with a check that the connector
  registers with :func:`register_credential_params_check`): such a call
  always waits for a person;
* it is a change of rights only because of its params (a ``secret.move``
  with a Keycloak side, registered with
  :func:`~meho_backplane.operations.grant_approval.register_rights_change_check`);
* it comes with a token (the command line, REST, MCP), also the admin's
  own token, or from an agent or a hosted agent run.

:func:`console_refusal` runs the same checks of who may make the change
as the approve (:func:`~meho_backplane.operations.grant_approval.check_grant_approval`),
on the approval card built against the live system: in MEHO's own login
realm the admin must be a tenant admin, and a platform admin when the
change can give platform admin rights. A refused call does not run and
gives the same text as the approve.

The audit row of such a run carries ``rights_change_run`` with the value
:data:`CONSOLE_RUN_AUDIT_VALUE`: a change of rights that an admin ran at
once in the web console.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.operations.grant_approval import check_grant_approval, is_rights_change_op

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "CONSOLE_RUN_AUDIT_VALUE",
    "CONSOLE_RUN_REASON",
    "console_refusal",
    "params_write_credential",
    "register_credential_params_check",
    "runs_at_once_in_console",
]

_log = structlog.get_logger(__name__)

#: The policy gate's reason for such a call. The dispatcher marks the audit row by it.
CONSOLE_RUN_REASON: Final[str] = (
    "a change of rights that an admin made in the web console runs at once"
)

#: The value of ``rights_change_run`` on the audit row of such a run.
CONSOLE_RUN_AUDIT_VALUE: Final[str] = "admin_in_web_console"


#: op_id -> a check of the call's params: True when they carry a credential.
#: The check reads the params only (no I/O).
_CREDENTIAL_PARAMS_CHECKS: dict[str, Callable[[dict[str, Any]], bool]] = {}


def register_credential_params_check(op_id: str, check: Callable[[dict[str, Any]], bool]) -> None:
    """Record that a call of *op_id* writes a credential when *check* passes on its params.

    Such a call always waits for a person, also for an admin in the web
    console (:func:`runs_at_once_in_console`).
    """
    _CREDENTIAL_PARAMS_CHECKS[op_id] = check


def params_write_credential(op_id: str, params: dict[str, Any] | None) -> bool:
    """True when the params of a call of *op_id* carry a credential. Reads no I/O."""
    check = _CREDENTIAL_PARAMS_CHECKS.get(op_id)
    return check is not None and isinstance(params, dict) and check(params)


def runs_at_once_in_console(
    operator: Operator, descriptor: EndpointDescriptor, *, params: dict[str, Any] | None
) -> bool:
    """True when *operator*'s call of *descriptor* is an admin's change of rights in the console.

    Reads no I/O. The caller still runs :func:`console_refusal` before the
    call runs.
    """
    from meho_backplane.operations.service_grants import credential_call_waits

    return (
        is_rights_change_op(descriptor.op_id)
        and descriptor.safety_level != "destructive"
        and not getattr(descriptor, "policy_always_wait", False)
        and not credential_call_waits(descriptor.op_id, getattr(descriptor, "op_class", None))
        and not params_write_credential(descriptor.op_id, params)
        and operator.web_session
        and is_human_principal(operator)
        and operator.may_approve_rights_change()
    )


async def console_refusal(
    operator: Operator,
    *,
    connector_id: str | None,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any] | None,
) -> str | None:
    """Why *operator* may not make this change of rights, or ``None`` when they may.

    Runs the op's own check before a wait for approval
    (:func:`~meho_backplane.operations._park_check.park_check_refusal`), as
    a call that waits would. Then builds the approval card against the live
    system and runs the approve's checks of who may make the change on it.
    A card that does not build counts as the strict case (MEHO's own login
    realm, platform admin rights), as on the approve.
    """
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations._park_check import park_check_refusal
    from meho_backplane.operations.approval_object_check import live_card
    from meho_backplane.operations.approval_queue import ApprovalRoleFloorError

    op_id = descriptor.op_id
    call_params = params or {}
    refused = await park_check_refusal(
        op_id=op_id,
        connector_id=connector_id or "",
        descriptor=descriptor,
        operator=operator,
        target=target,
        params=call_params,
        duration_ms=0.0,
    )
    if refused is not None:
        reason = (refused.extras or {}).get("reason")
        return reason if isinstance(reason, str) else (refused.error or "refused")
    card = None
    if connector_id is not None:
        card = await live_card(
            operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=call_params,
        )
    request = ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=operator.tenant_id,
        principal_sub=operator.sub,
        op_id=op_id,
        connector_id=connector_id or "",
        params=call_params,
        proposed_effect=card or {},
        rights_change=True,
    )
    try:
        check_grant_approval(operator, request)
    except ApprovalRoleFloorError as exc:
        _log.info("rights_change_console_refused", op_id=op_id, operator_sub=operator.sub)
        return exc.reason_text
    return None
