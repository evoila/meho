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
at once (:func:`runs_at_once_in_console`), after :func:`console_check`.

The call still waits when:

* it is ``destructive``: such a call always waits for a person;
* it has the "always wait" mark (an override or a classification rule):
  such a call always waits for a person;
* it reads, writes or makes a credential
  (:func:`~meho_backplane.operations.service_grants.descriptor_credential_waits`):
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

:func:`console_check` makes the call wait when the console session lacks
the sign-in a console approve needs, or when the approval card, built
against the live system, does not build or does not say who the change
gives rights to. Then it runs the checks of who may make the change
(:func:`~meho_backplane.operations.grant_approval.who_may_make_refusal`,
the same rule as the approve): in MEHO's own login realm the admin must
be a tenant admin, and a platform admin when the change can give platform
admin rights. When the admin lacks that role, the call waits, and a
second person with that role approves it. A call that the op's own check
refuses does not run.

The audit row of such a run carries ``rights_change_run`` with the value
:data:`CONSOLE_RUN_AUDIT_VALUE`: a change of rights that an admin ran at
once in the web console. It also carries ``approval_auth``, the sign-in
proof of the run, in the form of the decision row of a console approve
(:meth:`~meho_backplane.auth.approval_sign_in.ApprovalAuth.audit_payload`):
among others the sign-in level (``acr``) and the sign-in time
(``auth_time``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.operations.grant_approval import (
    grant_facts,
    is_rights_change_op,
    who_may_make_refusal,
)

if TYPE_CHECKING:
    from meho_backplane.auth.approval_sign_in import ApprovalAuth
    from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor

__all__ = [
    "CONSOLE_RUN_AUDIT_VALUE",
    "CONSOLE_RUN_REASON",
    "ConsoleCheck",
    "console_check",
    "keep_console_run_sign_in",
    "params_write_credential",
    "register_credential_params_check",
    "register_no_login_realm_op",
    "runs_at_once_in_console",
    "take_console_run_sign_in",
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

    Reads no I/O. The caller still runs :func:`console_check` before the
    call runs.
    """
    from meho_backplane.operations.service_grants import descriptor_credential_waits

    return (
        is_rights_change_op(descriptor.op_id)
        and descriptor.safety_level != "destructive"
        and not getattr(descriptor, "policy_always_wait", False)
        and not descriptor_credential_waits(descriptor)
        and not params_write_credential(descriptor.op_id, params)
        and operator.web_session
        and is_human_principal(operator)
        and operator.may_approve_rights_change()
    )


@dataclass(frozen=True)
class ConsoleCheck:
    """The answer of :func:`console_check`: run at once, wait, or refuse.

    ``refusal`` is the text of a refused call. ``waits`` is ``True`` when
    the call waits for approval, as every other change of rights does. A
    call with neither runs at once; ``sign_in`` is then its sign-in proof.
    """

    waits: bool = False
    refusal: str | None = None
    sign_in: ApprovalAuth | None = None


#: The sign-in proof of the console run that the policy gate let run now.
_run_sign_in: ContextVar[ApprovalAuth | None] = ContextVar(
    "rights_change_console_run_sign_in", default=None
)


def keep_console_run_sign_in(sign_in: ApprovalAuth | None) -> None:
    """Keep *sign_in* as the proof of the console run the policy gate lets run now."""
    _run_sign_in.set(sign_in)


def take_console_run_sign_in() -> ApprovalAuth | None:
    """The proof :func:`keep_console_run_sign_in` kept, then drop it."""
    sign_in = _run_sign_in.get()
    _run_sign_in.set(None)
    return sign_in


#: Operations that are a change of rights but never write a login realm,
#: so a console call of them needs no card facts of who it gives rights to.
_NO_LOGIN_REALM_OPS: set[str] = set()


def register_no_login_realm_op(op_id: str) -> None:
    """Record that *op_id* never writes a login realm (:func:`console_check`)."""
    _NO_LOGIN_REALM_OPS.add(op_id)


async def _console_sign_in(operator: Operator) -> ApprovalAuth | None:
    # Imported late: the console's sign-in store is part of the web interface.
    from meho_backplane.ui.routes.approvals.sign_in import console_run_auth

    return await console_run_auth(operator)


def _card_built(card: dict[str, Any] | None) -> bool:
    return (
        isinstance(card, dict)
        and isinstance(card.get("preview"), dict)
        and not card.get("preview_unavailable")
    )


def _card_names_who(request: ApprovalRequest) -> bool:
    """True when the card holds the facts of who the change gives rights to."""
    if request.op_id in _NO_LOGIN_REALM_OPS:
        return True
    facts = grant_facts(request)
    if facts is None:
        return False
    preview = (request.proposed_effect or {}).get("preview")
    return (
        isinstance(preview, dict)
        and isinstance(preview.get("grant"), dict)
        and not (facts.for_login and facts.user_id is None)
        and not (facts.for_group and facts.group_id is None)
    )


async def console_check(
    operator: Operator,
    *,
    connector_id: str | None,
    descriptor: EndpointDescriptor,
    target: Any,
    params: dict[str, Any] | None,
) -> ConsoleCheck:
    """Whether *operator*'s change of rights in the console runs at once, waits, or is refused.

    In this order:

    * the op's own check before a wait for approval
      (:func:`~meho_backplane.operations._park_check.park_check_refusal`)
      refuses the call, as it would refuse a call that waits;
    * the call waits when the console session lacks the sign-in a console
      approve of a change of rights needs (a sign-in no older than the
      approval window ``FRESH_SIGN_IN_MAX_AGE_SECONDS``, and the approval
      sign-in level where ``APPROVAL_SIGN_IN_LEVEL`` asks for it);
    * the call waits when its approval card does not build against the
      live system, or the card does not say who the change gives rights
      to. Only an operation registered with
      :func:`register_no_login_realm_op` needs no such facts;
    * the call waits when the admin may not make the change alone
      (:func:`~meho_backplane.operations.grant_approval.who_may_make_refusal`):
      in MEHO's own login realm the admin must be a tenant admin, and a
      platform admin when the change can give platform admin rights, or
      when MEHO could not read whether it can. The approve then needs a
      second person with that role.
    """
    from meho_backplane.db.models import ApprovalRequest
    from meho_backplane.operations._park_check import park_check_refusal
    from meho_backplane.operations.approval_object_check import live_card

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
        text = reason if isinstance(reason, str) else (refused.error or "refused")
        return ConsoleCheck(refusal=text)
    sign_in = await _console_sign_in(operator)
    if sign_in is None:
        _log.info("rights_change_console_waits_for_sign_in", op_id=op_id, operator_sub=operator.sub)
        return ConsoleCheck(waits=True)
    card = None
    if connector_id is not None:
        card = await live_card(
            operator, connector_id=connector_id, op_id=op_id, target=target, params=call_params
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
    if not _card_built(card) or not _card_names_who(request):
        _log.info("rights_change_console_waits_for_card", op_id=op_id, operator_sub=operator.sub)
        return ConsoleCheck(waits=True)
    refusal = who_may_make_refusal(operator, request)
    if refusal is not None:
        _log.info("rights_change_console_waits_for_role", op_id=op_id, operator_sub=operator.sub)
        return ConsoleCheck(waits=True)
    return ConsoleCheck(sign_in=sign_in)
